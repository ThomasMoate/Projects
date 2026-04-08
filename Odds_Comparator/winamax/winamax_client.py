"""
Winamax Socket.IO client using HTTP long-polling (EIO=4).
WebSocket upgrade returns 403 through proxy, polling works fine.

Auto-reconnects if the server drops the session mid-scrape.
"""

import json
import time
import requests
import urllib3
from requests.adapters import HTTPAdapter

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)


class NoVerifyAdapter(HTTPAdapter):
    def send(self, request, **kwargs):
        kwargs['verify'] = False
        return super().send(request, **kwargs)


class WinamaxClient:
    SOCKET_URL = 'https://sports-eu-west-3.winamax.fr/uof-sports-server/socket.io/'
    MAIN_URL = 'https://www.winamax.fr'

    def __init__(self, sport_id: int = 5):
        """
        sport_id : 1=Football, 2=Basketball, 3=Rugby, 5=Tennis (défaut)
        """
        self.sport_id = sport_id
        self.session = requests.Session()
        self.session.mount('https://', NoVerifyAdapter())
        self.session.headers.update({
            'User-Agent': (
                'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
                '(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
            ),
            'Accept-Language': 'fr-FR,fr;q=0.9',
        })
        self.socket_headers = {
            'Origin': self.MAIN_URL,
            'Referer': self.MAIN_URL + '/',
        }
        self.params = {
            'EIO': '4',
            'transport': 'polling',
            'language': 'fr',
            'version': '5.0',
            'store': 'false',
            'embed': 'false',
        }
        self.sid = None

    def connect(self, seed_url: str = None):
        """Establish Socket.IO session via EIO handshake + namespace connect.

        Retries up to 3 times with exponential backoff on failure.
        """
        seed = seed_url or f'{self.MAIN_URL}/paris-sportifs/sports/{self.sport_id}'
        last_exc: Exception | None = None
        for attempt in range(3):
            try:
                self.session.get(seed, timeout=10)

                # EIO handshake — returns  0{...json with sid...}
                params = {k: v for k, v in self.params.items() if k != 'sid'}
                resp = self.session.get(
                    self.SOCKET_URL, params=params,
                    headers=self.socket_headers, timeout=10
                )
                resp.raise_for_status()
                self.sid = json.loads(resp.text[1:])['sid']
                self.params['sid'] = self.sid

                # Socket.IO namespace connect
                self.session.post(
                    self.SOCKET_URL,
                    params=self.params,
                    headers=self.socket_headers,
                    data='40',
                    timeout=10,
                )
                # Consume the namespace-connect ACK (40{"sid":"..."})
                self._poll()
                return
            except Exception as exc:
                last_exc = exc
                if attempt < 2:
                    time.sleep(2 ** attempt)
        raise RuntimeError(f'Winamax: connexion impossible après 3 tentatives — {last_exc}')

    def _poll(self) -> str:
        """Single long-poll request; returns raw response text."""
        resp = self.session.get(
            self.SOCKET_URL, params=self.params, headers=self.socket_headers, timeout=15
        )
        resp.raise_for_status()
        return resp.text

    def _post(self, data: str):
        self.session.post(
            self.SOCKET_URL,
            params=self.params,
            headers=self.socket_headers,
            data=data,
            timeout=10,
        )

    def _is_disconnect(self, raw: str) -> bool:
        """Return True if the packet signals a server-side disconnect."""
        # EIO packet type 1 = close,  Socket.IO 41 = namespace disconnect
        return raw == '1' or raw.startswith('41')

    def _send_and_receive(self, payload: dict, max_retries: int = 5) -> dict:
        """Send a Socket.IO message and wait for the data response.

        Handles EIO PING/PONG heartbeat transparently.
        Automatically reconnects once if the server drops the session.
        """
        for reconnect_attempt in range(2):
            msg = f'42["m", {json.dumps(payload)}]'
            try:
                self._post(msg)
            except Exception:
                if reconnect_attempt == 0:
                    self.connect()
                    continue
                raise

            session_ok = True
            for _ in range(max_retries):
                try:
                    raw = self._poll()
                except Exception:
                    session_ok = False
                    break

                # Server PING → respond with PONG then re-poll
                if raw == '2':
                    try:
                        self._post('3')
                    except Exception:
                        pass
                    continue

                # Server disconnect — need to reconnect
                if self._is_disconnect(raw):
                    session_ok = False
                    break

                # Find the '42' prefix for the data message
                idx = raw.find('42')
                if idx == -1:
                    continue

                try:
                    arr = json.loads(raw[idx + 2:])
                    return arr[1]
                except (json.JSONDecodeError, IndexError, KeyError):
                    continue

            if not session_ok and reconnect_attempt == 0:
                self.connect()
            else:
                break

        raise RuntimeError(f'Winamax: aucune donnée reçue après {max_retries} tentatives')

    def get_route(self, route: str) -> dict:
        """Request data for a given Winamax route.

        Routes follow SPA URL conventions:
          sport:5          → all tennis matches (with main winner odds)
          match:70456558   → single match full detail (all markets)
        """
        payload = {
            'route': route,
            'data': True,
            'clientTime': int(time.time() * 1000),
        }
        return self._send_and_receive(payload)

    def close(self):
        self.session.close()

    def __enter__(self):
        self.connect()
        return self

    def __exit__(self, *_):
        self.close()
