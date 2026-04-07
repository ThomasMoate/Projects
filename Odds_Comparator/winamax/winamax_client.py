"""
Winamax Socket.IO client using HTTP long-polling (EIO=4).
WebSocket upgrade returns 403 through proxy, polling works fine.
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

    def __init__(self):
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
        """Establish Socket.IO session via EIO handshake + namespace connect."""
        # Seed cookies from main site so Winamax accepts our session
        seed = seed_url or f'{self.MAIN_URL}/paris-sportifs/sports/5'
        self.session.get(seed)

        # EIO handshake — returns  0{...json with sid...}
        resp = self.session.get(
            self.SOCKET_URL, params=self.params, headers=self.socket_headers
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
        )
        # Consume the namespace-connect ACK (40{"sid":"..."})
        self._poll()

    def _poll(self) -> str:
        """Single long-poll request; returns raw response text."""
        resp = self.session.get(
            self.SOCKET_URL, params=self.params, headers=self.socket_headers
        )
        resp.raise_for_status()
        return resp.text

    def _post(self, data: str):
        self.session.post(
            self.SOCKET_URL,
            params=self.params,
            headers=self.socket_headers,
            data=data,
        )

    def _send_and_receive(self, payload: dict, max_retries: int = 5) -> dict:
        """
        Send a Socket.IO message and wait for the data response.
        Handles EIO PING/PONG heartbeat transparently.
        Retries up to max_retries times if an empty or heartbeat-only
        response is received.
        """
        msg = f'42["m", {json.dumps(payload)}]'
        self._post(msg)

        for _ in range(max_retries):
            raw = self._poll()

            # Server PING → respond with PONG then re-poll
            if raw == '2':
                self._post('3')
                continue

            # Multiple packets may be concatenated (EIO packet length prefix)
            # Find the '42' prefix for the data message
            idx = raw.find('42')
            if idx == -1:
                # Could be another control frame; skip
                continue

            arr = json.loads(raw[idx + 2:])
            # arr = ["m", {data}]
            return arr[1]

        raise RuntimeError(f'No data received after {max_retries} retries')

    def get_route(self, route: str) -> dict:
        """
        Request data for a given Winamax route.
        Routes follow SPA URL conventions:
          sport:5          → all tennis matches
          match:70456558   → single match full detail
          tournament:175229 → tournament matches
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
