"""
Unibet France odds scraper.
Uses the LVS (Live Betting System) REST API with X-LVS-HSToken auth.
"""
import re
import json
import time
import urllib3
import requests
from requests.adapters import HTTPAdapter

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)


class NoVerifyAdapter(HTTPAdapter):
    def send(self, request, **kwargs):
        kwargs['verify'] = False
        return super().send(request, **kwargs)


# Market display group IDs → category names
MARKET_GROUPS = {
    1:   'Résultat',
    6:   'Aces',
    4:   'Score Exact',
    122: 'Plus / Moins',
    121: 'Performance Joueur',
    342: 'Jeu',
    343: 'Tie-Break',
    581: 'Breaks',
    641: 'Paris Populaires',
}

SPECIALIZED_GROUPS = {6, 581}  # Aces, Breaks


class UnibetClient:
    BASE = 'https://www.unibet.fr'
    UA = (
        'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
    )

    def __init__(self):
        self.session = requests.Session()
        self.session.mount('https://', NoVerifyAdapter())
        self.session.headers.update({
            'User-Agent': self.UA,
            'Accept': 'application/json',
            'Accept-Language': 'fr-FR,fr;q=0.9',
            'Referer': self.BASE,
        })
        self.token = None

    def connect(self):
        """Fetch the non-expiring HS token from the main tennis page."""
        resp = self.session.get(f'{self.BASE}/paris-tennis')
        resp.raise_for_status()
        for script in re.findall(r'<script[^>]*>(.*?)</script>', resp.text, re.DOTALL):
            script = script.strip()
            if not script.startswith('{') or 'HsToken' not in script:
                continue
            try:
                obj = json.loads(script)
                self.token = obj['app.config']['pselNonExpiringHsToken']
                break
            except Exception:
                continue
        if not self.token:
            raise RuntimeError('Could not extract Unibet HS token')
        self.session.headers['X-LVS-HSToken'] = self.token

    def _get(self, path, **params):
        default_params = {'lineId': '1', 'originId': '3'}
        default_params.update(params)
        resp = self.session.get(f'{self.BASE}/{path.lstrip("/")}', params=default_params)
        resp.raise_for_status()
        return resp.json()

    def get_tennis_leagues(self):
        """Return list of {id, name} for all active tennis leagues."""
        data = self._get(
            'lvs-api/ept',
            up=1, hidden=0, liveCount='e', preCount='e', status='OPEN,SUSPENDED'
        )
        tennis = next((s for s in data.get('ept', []) if s.get('code') == 'TENN'), None)
        if not tennis:
            return []
        leagues = []
        for cat in tennis.get('path', []):
            for league in cat.get('path', []):
                if league.get('count', 0) > 0:
                    leagues.append({
                        'id': league['id'],
                        'name': league['desc'],
                        'category': cat['desc'],
                    })
        return leagues

    def get_events_for_league(self, league_id, limit=50):
        """Return list of event IDs (strings like 'e3336406') for a league."""
        data = self._get(
            f'lvs-api/next/{limit}/p{league_id}',
            breakdownEventsIntoDays='true', showPromotions='true'
        )
        items = data.get('items', {})
        return [k for k in items if k.startswith('e')]

    def get_event_offers(self, event_id):
        """
        Return full bet offers for one event (e.g. 'e3336406').
        Returns {'event': {...}, 'markets': {...}, 'outcomes': {...}}.
        """
        data = self._get(
            f'lvs-api/ff/{event_id}',
            ext=1, showPromotions='true', showMarketTypeGroups='true'
        )
        items = data.get('items', {})
        event = items.get(event_id, {})
        markets = {k: v for k, v in items.items() if k.startswith('m')}
        outcomes = {k: v for k, v in items.items() if k.startswith('o')}
        return {'event': event, 'markets': markets, 'outcomes': outcomes, 'to_basket': data.get('toBasket')}

    def close(self):
        self.session.close()

    def __enter__(self):
        self.connect()
        return self

    def __exit__(self, *_):
        self.close()
