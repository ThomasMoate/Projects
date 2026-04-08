"""
Client Pinnacle direct via leur API officielle (Basic Auth).
Couvre TOUS les marchés : vainqueur, total jeux, aces, breaks, sets, etc.

Configuration :
    export PINNACLE_USER="votre_email"
    export PINNACLE_PASS="votre_mot_de_passe"

Compte Pinnacle gratuit sur pinnacle.com (accessible depuis la France).
Aucun dépôt requis pour lire les cotes.

Usage :
    python pinnacle_client.py                  # liste matchs tennis + cotes
    python pinnacle_client.py --props          # inclut aces/breaks/props
    python pinnacle_client.py --league 2627    # tournoi spécifique
"""

import os
import sys
import time
import json
import re
import unicodedata
import argparse
import requests
import urllib3

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

BASE = 'https://api.pinnacle.com'

# Sport IDs Pinnacle
TENNIS_SPORT_ID     = 33
FOOTBALL_SPORT_ID   = 29   # soccer
BASKETBALL_SPORT_ID = 4

SPORT_IDS: dict[str, int] = {
    'tennis':     TENNIS_SPORT_ID,
    'football':   FOOTBALL_SPORT_ID,
    'basketball': BASKETBALL_SPORT_ID,
}

KNOWN_LEAGUE_IDS: list[int] = []


class PinnacleClient:
    """
    Client Pinnacle via API officielle.

    Marchés disponibles :
      - Moneyline (vainqueur du match)
      - Spread (handicap sets)
      - Total (total jeux)
      - Specials/Props : aces par joueur, total aces, total breaks, etc.
    """

    def __init__(
        self,
        sport: str = 'tennis',
        username: str | None = None,
        password: str | None = None,
    ):
        """
        sport : 'tennis' | 'football' | 'basketball'
        """
        self.sport_id = SPORT_IDS.get(sport, TENNIS_SPORT_ID)
        self.sport    = sport
        self.username = username or os.environ.get('PINNACLE_USER', '')
        self.password = password or os.environ.get('PINNACLE_PASS', '')
        self.session = requests.Session()
        if self.username and self.password:
            self.session.auth = (self.username, self.password)
        self.session.headers.update({
            'Content-Type': 'application/json',
            'Accept':        'application/json',
            'User-Agent':    'Mozilla/5.0',
        })
        self._league_cache: list[dict] | None = None

    def is_configured(self) -> bool:
        return bool(self.username and self.password)

    def _get(self, path: str, **params) -> dict:
        resp = self.session.get(
            f'{BASE}{path}', params=params, timeout=15, verify=False
        )
        if resp.status_code == 401:
            raise RuntimeError(
                'Identifiants Pinnacle incorrects. '
                'Définissez PINNACLE_USER et PINNACLE_PASS.'
            )
        if resp.status_code == 403:
            raise RuntimeError('Accès refusé. Vérifiez votre compte Pinnacle.')
        resp.raise_for_status()
        return resp.json()

    # ── Leagues ──────────────────────────────────────────────────────────────

    def get_leagues(self) -> list[dict]:
        """Retourne tous les tournois/ligues actifs pour le sport configuré."""
        if self._league_cache is not None:
            return self._league_cache
        data = self._get('/v2/leagues', sportId=self.sport_id)
        leagues = [
            {'id': lg['id'], 'name': lg.get('name', ''), 'home_team_type': lg.get('homeTeamType', '')}
            for lg in data.get('leagues', [])
        ]
        self._league_cache = leagues
        return leagues

    def get_tennis_leagues(self) -> list[dict]:
        """Alias backward-compatible."""
        return self.get_leagues()

    def get_active_league_ids(self) -> list[int]:
        """IDs des ligues/tournois actifs pour le sport configuré."""
        return [lg['id'] for lg in self.get_leagues()]

    # ── Fixtures (liste des matchs) ───────────────────────────────────────────

    def get_fixtures(self, league_ids: list[int]) -> list[dict]:
        """
        Retourne les matchs à venir pour les tournois donnés.
        Chaque item : {id, home, away, starts, leagueId, live}
        """
        data = self._get(
            '/v1/fixtures',
            sportId=self.sport_id,
            leagueIds=','.join(map(str, league_ids)),
        )
        matches = []
        for league in data.get('league', []):
            for evt in league.get('events', []):
                matches.append({
                    'id':        evt['id'],
                    'home':      evt.get('home', ''),
                    'away':      evt.get('away', ''),
                    'starts':    evt.get('starts', ''),
                    'leagueId':  league['id'],
                    'live':      evt.get('liveStatus', 0) > 0,
                })
        return matches

    # ── Cotes principales (moneyline / spread / total) ────────────────────────

    def get_odds(self, league_ids: list[int]) -> dict:
        """
        Cotes principales par eventId.
        Retourne {event_id: {'moneyline': {...}, 'total': [...], 'spread': [...]}}
        """
        data = self._get(
            '/v2/odds',
            sportId=self.sport_id,
            leagueIds=','.join(map(str, league_ids)),
            oddsFormat='Decimal',
        )
        result: dict[int, dict] = {}
        for league in data.get('leagues', []):
            for evt in league.get('events', []):
                eid = evt['id']
                result[eid] = {}

                # Moneyline (vainqueur)
                ml = evt.get('periods', {}).get('num_0', {}).get('moneyline', {})
                if ml:
                    result[eid]['moneyline'] = {
                        'home': ml.get('home'),
                        'away': ml.get('away'),
                        'draw': ml.get('draw'),
                    }

                # Total jeux (over/under)
                totals = evt.get('periods', {}).get('num_0', {}).get('totals', [])
                result[eid]['totals'] = [
                    {'line': t.get('points'), 'over': t.get('over'), 'under': t.get('under')}
                    for t in totals
                    if t.get('points') and t.get('over') and t.get('under')
                ]

                # Spread (handicap)
                spreads = evt.get('periods', {}).get('num_0', {}).get('spreads', [])
                result[eid]['spreads'] = [
                    {'hdp': s.get('hdp'), 'home': s.get('home'), 'away': s.get('away')}
                    for s in spreads
                ]

        return result

    # ── Marchés spéciaux / props (aces, breaks, etc.) ─────────────────────────

    def get_special_fixtures(self, league_ids: list[int]) -> dict:
        """
        Retourne la liste des marchés spéciaux (props) pour les tournois donnés.
        Retourne {special_id: {name, parent_event_id, category, participants}}
        """
        data = self._get(
            '/v2/fixtures/special',
            sportId=self.sport_id,
            leagueIds=','.join(map(str, league_ids)),
        )
        result: dict[int, dict] = {}
        for league in data.get('leagues', []):
            for evt in league.get('events', []):
                parent_id = evt['id']
                for sp in evt.get('specials', []):
                    result[sp['id']] = {
                        'name':          sp.get('name', ''),
                        'category':      sp.get('category', ''),
                        'parent_id':     parent_id,
                        'participants':  sp.get('contestants', sp.get('participants', [])),
                    }
        return result

    def get_special_odds(self, league_ids: list[int]) -> dict:
        """
        Cotes des marchés spéciaux.
        Retourne {special_id: {participant_id: price}}
        """
        data = self._get(
            '/v2/odds/special',
            sportId=self.sport_id,
            leagueIds=','.join(map(str, league_ids)),
        )
        result: dict[int, dict] = {}
        for league in data.get('leagues', []):
            for evt in league.get('events', []):
                for sp in evt.get('specials', []):
                    sid = sp['id']
                    result[sid] = {
                        p['id']: p.get('price')
                        for p in sp.get('participants', [])
                        if p.get('price')
                    }
        return result

    def get_all_specials(self, league_ids: list[int]) -> list[dict]:
        """
        Combine fixtures + odds pour les marchés spéciaux.
        Retourne une liste de marchés avec leurs cotes :
        [{name, category, parent_id, outcomes: [{name, designation, odds}]}]
        """
        fixtures = self.get_special_fixtures(league_ids)
        odds_map  = self.get_special_odds(league_ids)

        markets = []
        for sp_id, sp in fixtures.items():
            sp_odds = odds_map.get(sp_id, {})
            outcomes = []
            for p in sp['participants']:
                pid  = p.get('id') or p.get('contestantId')
                name = p.get('name', '')
                desig = p.get('designation', p.get('type', ''))
                price = sp_odds.get(pid)
                if price:
                    outcomes.append({'name': name, 'designation': desig, 'odds': price})
            if outcomes:
                markets.append({
                    'special_id': sp_id,
                    'name':       sp['name'],
                    'category':   sp['category'],
                    'parent_id':  sp['parent_id'],
                    'outcomes':   outcomes,
                })
        return markets

    def close(self):
        self.session.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()


# ── CLI de test ───────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description='Test client Pinnacle')
    parser.add_argument('--props', action='store_true',
                        help='Inclure les marchés spéciaux (aces, breaks…)')
    parser.add_argument('--league', type=int, action='append',
                        help='Filtrer à un tournoi spécifique (ID Pinnacle)')
    args = parser.parse_args()

    with PinnacleClient() as client:
        if not client.is_configured():
            print('PINNACLE_USER / PINNACLE_PASS non configurés.')
            print('Créez un compte gratuit sur pinnacle.com')
            print('puis : export PINNACLE_USER="email" PINNACLE_PASS="password"')
            return

        print('Récupération des tournois tennis…')
        league_ids = args.league or client.get_active_league_ids()[:20]
        print(f'  {len(league_ids)} tournois')

        print('Récupération des matchs…')
        matches = [m for m in client.get_fixtures(league_ids) if not m['live']]
        print(f'  {len(matches)} matchs prématch')

        active_leagues = list({m['leagueId'] for m in matches})
        odds = client.get_odds(active_leagues)

        for m in matches[:5]:
            eid = m['id']
            o = odds.get(eid, {})
            ml = o.get('moneyline', {})
            totals = o.get('totals', [])
            print(f'\n  {m["home"]} vs {m["away"]}')
            if ml.get('home') and ml.get('away'):
                print(f'    Moneyline : {m["home"]} {ml["home"]}  /  {m["away"]} {ml["away"]}')
            for t in totals[:2]:
                print(f'    Total {t["line"]} jeux : over {t["over"]}  under {t["under"]}')

        if args.props and matches:
            print('\nRécupération des marchés spéciaux (aces/breaks)…')
            specials = client.get_all_specials(active_leagues)
            for sp in specials[:20]:
                ocs = ', '.join(f'{o["name"]} {o["odds"]}' for o in sp['outcomes'])
                print(f'  [{sp["category"]}] {sp["name"]}: {ocs}')


if __name__ == '__main__':
    main()
