"""
Source de référence no-vig pour le comparateur de cotes.

Priorité :
  1. Pinnacle direct (PINNACLE_USER + PINNACLE_PASS) → tous marchés y compris aces/breaks
  2. The Odds API (ODDS_API_KEY)                     → h2h + total jeux uniquement

Configuration :
    # Option 1 — Pinnacle (recommandé, compte gratuit sur pinnacle.com)
    export PINNACLE_USER="votre_email"
    export PINNACLE_PASS="votre_mot_de_passe"

    # Option 2 — The Odds API (clé gratuite sur the-odds-api.com, 500 req/mois)
    export ODDS_API_KEY="votre_clé"

Usage :
    python reference_client.py              # teste la source disponible
    python reference_client.py --source pin # force Pinnacle
    python reference_client.py --source oda # force The Odds API
"""

import os
import sys
import re
import time
import unicodedata
import argparse
import requests
import urllib3

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)


# ── Helpers communs ───────────────────────────────────────────────────────────

def _no_accents(s: str) -> str:
    return ''.join(c for c in unicodedata.normalize('NFKD', s)
                   if not unicodedata.combining(c))

def _norm(s: str) -> str:
    return _no_accents(s).lower().strip()

def _last_name(player: str) -> str:
    p = player.strip()
    p = re.sub(r'^[A-Z][A-Z]?\.\s*', '', p)
    p = re.sub(r'^[A-Z][a-z]+-', '', p)
    parts = p.split()
    return _norm(parts[-1] if parts else p)

def _match_key(home: str, away: str) -> tuple[str, str]:
    return tuple(sorted([_last_name(home), _last_name(away)]))

def _no_vig(odds_a: float, odds_b: float) -> tuple[float, float]:
    """Fair odds sans marge pour un marché à deux issues."""
    pa, pb = 1 / odds_a, 1 / odds_b
    total = pa + pb
    return round(1 / (pa / total), 3), round(1 / (pb / total), 3)

def _no_vig_three_way(h: float, d: float, a: float) -> tuple[float, float, float]:
    """Fair odds sans marge pour un marché à trois issues (1X2 football)."""
    ph, pd, pa = 1 / h, 1 / d, 1 / a
    total = ph + pd + pa
    return round(1 / (ph / total), 3), round(1 / (pd / total), 3), round(1 / (pa / total), 3)


# ── Source Pinnacle (Basic Auth) ──────────────────────────────────────────────

class _PinnacleSource:
    def __init__(self, sport: str = 'tennis'):
        self.sport = sport
        from pinnacle_client import PinnacleClient
        self._client = PinnacleClient(sport=sport)

    def is_available(self) -> bool:
        return self._client.is_configured()

    def get_reference_odds(self) -> dict:
        """
        Retourne {match_key: {market_key: {side: fair_odds, 'source': 'pinnacle'}}}
        Gère tennis (2-way + specials), football (3-way 1X2 + total buts),
        basketball (2-way + total points).
        """
        client = self._client
        sport  = self.sport
        print(f'  [référence] Pinnacle ({sport}) — récupération…', file=sys.stderr)

        league_ids = client.get_active_league_ids()[:50]
        matches    = [m for m in client.get_fixtures(league_ids) if not m['live']]
        if not matches:
            return {}

        active_lgids = list({m['leagueId'] for m in matches})
        match_by_id  = {m['id']: m for m in matches}
        odds_map     = client.get_odds(active_lgids)

        result: dict = {}

        for eid, m in match_by_id.items():
            mk = _match_key(m['home'], m['away'])
            o  = odds_map.get(eid, {})
            ml = o.get('moneyline', {})
            result.setdefault(mk, {})

            # ── Moneyline ───────────────────────────────────────────────────────
            if ml.get('home') and ml.get('away'):
                ln_home = _last_name(m['home'])
                ln_away = _last_name(m['away'])
                p0, p1  = sorted([ln_home, ln_away])
                side_h  = 'p0' if ln_home == p0 else 'p1'
                side_a  = 'p1' if side_h == 'p0' else 'p0'

                if ml.get('draw') and sport == 'football':
                    # 3-way 1X2 (football)
                    fh, fd, fa = _no_vig_three_way(ml['home'], ml['draw'], ml['away'])
                    result[mk][('match_winner_1x2', None)] = {
                        side_h: fh, 'draw': fd, side_a: fa, 'source': 'pinnacle'
                    }
                else:
                    # 2-way (tennis, basketball)
                    fh, fa = _no_vig(ml['home'], ml['away'])
                    result[mk][('match_winner', None)] = {
                        side_h: fh, side_a: fa, 'source': 'pinnacle'
                    }

            # ── Totals (sport-specific market key) ───────────────────────────
            total_key = {
                'tennis':     'total_games',
                'football':   'total_goals',
                'basketball': 'total_points',
            }.get(sport, 'total_games')

            for t in o.get('totals', []):
                if t.get('over') and t.get('under'):
                    fo, fu = _no_vig(t['over'], t['under'])
                    result[mk][(total_key, t['line'])] = {
                        'over': fo, 'under': fu, 'source': 'pinnacle'
                    }

        # ── Marchés spéciaux (tennis uniquement : aces, breaks) ──────────────
        if sport == 'tennis':
            specials = client.get_all_specials(active_lgids)
            for sp in specials:
                parent_id = sp['parent_id']
                m = match_by_id.get(parent_id)
                if not m:
                    continue
                mk  = _match_key(m['home'], m['away'])
                nom = _norm(sp['name'])
                ocs = sp['outcomes']
                result.setdefault(mk, {})

                over_oc  = next((o for o in ocs if _is_over(o['name']) is True),  None)
                under_oc = next((o for o in ocs if _is_over(o['name']) is False), None)
                if not over_oc or not under_oc:
                    continue
                fo, fu = _no_vig(over_oc['odds'], under_oc['odds'])
                line   = _extract_line(over_oc['name']) or _extract_line(sp['name'])
                if not line:
                    continue

                if 'ace' in nom and re.search(r'total|match|both', nom):
                    result[mk][('total_aces', line)] = {'over': fo, 'under': fu, 'source': 'pinnacle'}
                elif 'ace' in nom:
                    player_ln = _extract_player_from_prop(sp['name'], m['home'], m['away'])
                    if player_ln:
                        result[mk][('player_aces', line, player_ln)] = {
                            'over': fo, 'under': fu, 'source': 'pinnacle'
                        }
                elif 'break' in nom:
                    if re.search(r'total|match|both', nom):
                        result[mk][('total_breaks', line)] = {'over': fo, 'under': fu, 'source': 'pinnacle'}
                    else:
                        player_ln = _extract_player_from_prop(sp['name'], m['home'], m['away'])
                        if player_ln:
                            result[mk][('player_breaks', line, player_ln)] = {
                                'over': fo, 'under': fu, 'source': 'pinnacle'
                            }

        return result

    def close(self):
        self._client.close()


# ── Source The Odds API ───────────────────────────────────────────────────────

class _OddsApiSource:
    BASE = 'https://api.the-odds-api.com/v4'

    # Sports API keys per sport
    SPORT_KEYS: dict[str, list[str]] = {
        'tennis':     ['tennis_atp', 'tennis_wta'],
        'football':   ['soccer_france_ligue1', 'soccer_spain_la_liga',
                       'soccer_england_league1', 'soccer_germany_bundesliga',
                       'soccer_italy_serie_a', 'soccer_uefa_champs_league'],
        'basketball': ['basketball_nba', 'basketball_euroleague'],
    }

    def __init__(self, sport: str = 'tennis', cache_ttl: int = 300):
        self.sport     = sport
        self.api_key   = os.environ.get('ODDS_API_KEY', '')
        self.cache_ttl = cache_ttl
        self._cache: dict = {}
        self.session  = requests.Session()

    def is_available(self) -> bool:
        return bool(self.api_key)

    def _fetch(self, sport: str) -> list[dict]:
        now = time.monotonic()
        cached_at, data = self._cache.get(sport, (0, []))
        if now - cached_at < self.cache_ttl:
            return data
        resp = self.session.get(
            f'{self.BASE}/sports/{sport}/odds/',
            params={
                'apiKey':      self.api_key,
                'regions':     'eu',
                'markets':     'h2h,totals',
                'oddsFormat':  'decimal',
                'bookmakers':  'pinnacle,betfair_ex_eu',
            },
            timeout=15, verify=False,
        )
        if resp.status_code == 401:
            raise RuntimeError('ODDS_API_KEY invalide.')
        resp.raise_for_status()
        remaining = resp.headers.get('x-requests-remaining', '?')
        print(f'  [référence] Odds API — quota restant : {remaining}', file=sys.stderr)
        data = resp.json()
        self._cache[sport] = (now, data)
        return data

    def get_reference_odds(self) -> dict:
        sport_list = self.SPORT_KEYS.get(self.sport, self.SPORT_KEYS['tennis'])
        # For football totals, the market key changes
        total_key  = {
            'tennis':     'total_games',
            'football':   'total_goals',
            'basketball': 'total_points',
        }.get(self.sport, 'total_games')

        result: dict = {}
        for sport in sport_list:
            try:
                events = self._fetch(sport)
            except Exception as e:
                print(f'  [référence] Odds API {sport}: {e}', file=sys.stderr)
                continue
            for event in events:
                home, away = event.get('home_team', ''), event.get('away_team', '')
                mk = _match_key(home, away)
                result.setdefault(mk, {})
                for bk in event.get('bookmakers', []):
                    for mkt in bk.get('markets', []):
                        key  = mkt.get('key')
                        ocs  = mkt.get('outcomes', [])
                        if key == 'h2h' and len(ocs) == 2:
                            odds_map = {o['name']: o['price'] for o in ocs}
                            names    = list(odds_map)
                            fa, fb   = _no_vig(odds_map[names[0]], odds_map[names[1]])
                            p0       = _last_name(names[0])
                            sorted_  = sorted([_last_name(n) for n in names])
                            sa       = 'p0' if p0 == sorted_[0] else 'p1'
                            sb       = 'p1' if sa == 'p0' else 'p0'
                            result[mk].setdefault(('match_winner', None), {
                                sa: fa, sb: fb, 'source': bk['key']
                            })
                        elif key == 'h2h' and len(ocs) == 3 and self.sport == 'football':
                            # 3-way (football 1X2 with draw)
                            odds_map = {o['name']: o['price'] for o in ocs}
                            draw_name = next((n for n in odds_map if 'draw' in n.lower()), None)
                            if draw_name:
                                team_names = [n for n in odds_map if n != draw_name]
                                if len(team_names) == 2:
                                    sorted_t = sorted([_last_name(n) for n in team_names])
                                    ln0 = _last_name(team_names[0])
                                    sa  = 'p0' if ln0 == sorted_t[0] else 'p1'
                                    sb  = 'p1' if sa == 'p0' else 'p0'
                                    fh, fd, fa2 = _no_vig_three_way(
                                        odds_map[team_names[0]],
                                        odds_map[draw_name],
                                        odds_map[team_names[1]],
                                    )
                                    result[mk].setdefault(('match_winner_1x2', None), {
                                        sa: fh, 'draw': fd, sb: fa2, 'source': bk['key']
                                    })
                        elif key == 'totals':
                            pairs: dict[float, dict] = {}
                            for o in ocs:
                                pt = o.get('point')
                                if pt is None:
                                    continue
                                side = 'over' if 'over' in o['name'].lower() else 'under'
                                pairs.setdefault(pt, {})[side] = o['price']
                            for pt, sides in pairs.items():
                                if 'over' in sides and 'under' in sides:
                                    fo, fu = _no_vig(sides['over'], sides['under'])
                                    result[mk].setdefault((total_key, pt), {
                                        'over': fo, 'under': fu, 'source': bk['key']
                                    })
        return result

    def close(self):
        self.session.close()


# ── Interface publique ────────────────────────────────────────────────────────

class ReferenceClient:
    """
    Client de référence unique — choisit automatiquement Pinnacle ou Odds API.

    Usage dans compare.py :
        with ReferenceClient() as ref:
            if ref.is_available():
                ref_odds = ref.get_reference_odds()
            else:
                ref_odds = {}
    """

    def __init__(self, source: str = 'auto', sport: str = 'tennis', cache_ttl: int = 300):
        """
        source    : 'auto' | 'pinnacle' | 'oddsapi'
        sport     : 'tennis' | 'football' | 'basketball'
        cache_ttl : secondes entre deux appels API (Odds API seulement)
        """
        self._source: _PinnacleSource | _OddsApiSource | None = None
        self._source_name = 'none'

        if source in ('auto', 'pinnacle'):
            try:
                ps = _PinnacleSource(sport=sport)
                if ps.is_available():
                    self._source = ps
                    self._source_name = 'pinnacle'
                    return
            except ImportError:
                pass

        if source in ('auto', 'oddsapi'):
            oa = _OddsApiSource(sport=sport, cache_ttl=cache_ttl)
            if oa.is_available():
                self._source = oa
                self._source_name = 'oddsapi'

    def is_available(self) -> bool:
        return self._source is not None

    def source_name(self) -> str:
        return self._source_name

    def get_reference_odds(self) -> dict:
        """
        Retourne les fair odds de référence.

        Structure : {match_key: {market_key: {'over'/'p0'/... : fair_odds, 'source': str}}}

        market_key exemples :
          ('match_winner', None)
          ('total_games',  22.5)
          ('total_aces',    9.5)
          ('player_aces',   4.5, 'medvedev')
          ('total_breaks',  5.5)
        """
        if not self._source:
            return {}
        return self._source.get_reference_odds()

    def close(self):
        if self._source:
            self._source.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()


# ── Helpers privés ────────────────────────────────────────────────────────────

def _is_over(text: str) -> bool | None:
    t = _norm(text)
    if re.search(r'\bover\b|^\+|plus|over', t):
        return True
    if re.search(r'\bunder\b|^-|moins|under', t):
        return False
    return None

def _extract_line(text: str) -> float | None:
    m = re.search(r'(\d+)[,.](\d)', text)
    return float(f'{m.group(1)}.{m.group(2)}') if m else None

def _extract_player_from_prop(prop_name: str, home: str, away: str) -> str | None:
    """Identifie de quel joueur parle le marché prop."""
    lh, la = _last_name(home), _last_name(away)
    pn = _norm(prop_name)
    if lh in pn:
        return lh
    if la in pn:
        return la
    # Tentative fuzzy
    from difflib import SequenceMatcher
    scores = {lh: SequenceMatcher(None, lh, pn).ratio(),
              la: SequenceMatcher(None, la, pn).ratio()}
    best = max(scores, key=scores.get)
    return best if scores[best] > 0.4 else None


# ── CLI ───────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description='Test source de référence odds')
    parser.add_argument('--source', choices=['auto', 'pinnacle', 'oddsapi'], default='auto')
    parser.add_argument('--sport',  choices=['tennis', 'football', 'basketball'], default='tennis')
    parser.add_argument('--cache',  type=int, default=300)
    args = parser.parse_args()

    with ReferenceClient(source=args.source, sport=args.sport, cache_ttl=args.cache) as ref:
        if not ref.is_available():
            print('Aucune source de référence configurée.')
            print()
            print('Option 1 — Pinnacle (tous marchés, compte gratuit) :')
            print('  export PINNACLE_USER="email"')
            print('  export PINNACLE_PASS="mot_de_passe"')
            print()
            print('Option 2 — The Odds API (h2h + total jeux, clé gratuite) :')
            print('  export ODDS_API_KEY="votre_clé"')
            print('  Créez une clé sur : https://the-odds-api.com/')
            return

        print(f'Source active : {ref.source_name()}')
        ref_odds = ref.get_reference_odds()
        print(f'{len(ref_odds)} matchs avec cotes de référence\n')

        for mk, markets in list(ref_odds.items())[:5]:
            print(f'  {" vs ".join(p.capitalize() for p in mk)}')
            for mkey, data in markets.items():
                src = data.get('source', '?')
                vals = {k: v for k, v in data.items() if k != 'source'}
                print(f'    {mkey}  [{src}]  {vals}')


if __name__ == '__main__':
    main()
