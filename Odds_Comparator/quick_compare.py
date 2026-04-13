#!/usr/bin/env python3
"""
quick_compare.py — Récupère et compare les cotes football en temps réel.

Compare Winamax, Betclic et Unibet sur : Total buts, 1X2, Handicap, BTTS,
Double chance, Mi-temps...

Usage :
    python quick_compare.py                       # Total buts (tous les matchs)
    python quick_compare.py --market total_goals  # Même chose explicitement
    python quick_compare.py --market 1x2          # Résultat 1X2
    python quick_compare.py --market btts         # Les 2 équipes marquent
    python quick_compare.py --market handicap     # Handicap buts
    python quick_compare.py --line 2.5            # Filtre sur la ligne 2.5
    python quick_compare.py --side under          # Filtre sur le côté (over/under)
    python quick_compare.py --limit 10            # Limiter à 10 matchs par book
    python quick_compare.py --no-winamax          # Exclure Winamax
    python quick_compare.py --output comp.json    # Sauvegarder en JSON
"""
import argparse
import json
import re
import sys
import time
import unicodedata
from collections import defaultdict
from dataclasses import dataclass, asdict
from pathlib import Path

# ── Imports bookmakers ─────────────────────────────────────────────────────────
sys.path.insert(0, str(Path(__file__).parent / 'winamax'))
sys.path.insert(0, str(Path(__file__).parent / 'betclic'))
sys.path.insert(0, str(Path(__file__).parent / 'unibet'))


# ── Normalisation ──────────────────────────────────────────────────────────────

def _no_accents(s: str) -> str:
    return ''.join(c for c in unicodedata.normalize('NFKD', s)
                   if not unicodedata.combining(c))

def norm(s: str) -> str:
    return _no_accents(s).lower().strip()

def last_name(player: str) -> str:
    p = player.strip()
    p = re.sub(r'^[A-Z][A-Z]?\.\s*', '', p)
    parts = p.split()
    return norm(parts[-1] if parts else p)

def match_key(title: str) -> tuple:
    parts = re.split(r'\s+(?:[-–]|vs\.?)\s+', title, flags=re.I)
    if len(parts) != 2:
        return (norm(title), '')
    return tuple(sorted([last_name(parts[0]), last_name(parts[1])]))

def extract_line(text: str) -> float | None:
    m = re.search(r'(\d+)[,.](\d)', text)
    return float(f'{m.group(1)}.{m.group(2)}') if m else None

def is_over(text: str) -> bool | None:
    t = norm(text)
    if re.search(r'\bplus\b|^[+]|\bover\b', t): return True
    if re.search(r'\bmoins\b|^[-]|\bunder\b', t): return False
    return None

def is_draw(label: str) -> bool:
    n = norm(label)
    return 'nul' in n or 'draw' in n or n in ('x', 'n', 'egalite', 'tie', 'match nul')


# ── Helpers de parsing ─────────────────────────────────────────────────────────

def _side_team(label: str, parts: list[str], mk: tuple) -> str:
    ln = last_name(label)
    if ln == mk[0]: return 'home'
    if ln == mk[1]: return 'away'
    nl = norm(label)
    for p in parts:
        np = norm(p)
        if nl in np or np in nl:
            key = last_name(p)
            return 'home' if key == mk[0] else 'away'
    return 'home' if mk[0] and mk[0][0] <= mk[1][0] else 'away'


# ── Fetch Winamax ─────────────────────────────────────────────────────────────

def fetch_winamax_football(limit: int = 50, delay: float = 0.2) -> list[dict]:
    from winamax_client import WinamaxClient
    print('[ Winamax ] Connexion…', file=sys.stderr)
    results = []
    with WinamaxClient(sport_id=1) as client:
        overview = client.get_route('sport:1')
        matches = overview.get('matches', {})
        prematch = [(k, v) for k, v in matches.items()
                    if v.get('status') == 'PREMATCH']
        print(f'[ Winamax ] {len(prematch)} matchs PREMATCH trouvés → traitement de {min(limit, len(prematch))}', file=sys.stderr)

        for i, (mid_str, m) in enumerate(prematch[:limit], 1):
            match_id = m['matchId']
            title = m.get('title', '?')
            print(f'[ Winamax ] [{i}/{min(limit,len(prematch))}] {title}', file=sys.stderr)
            try:
                detail = client.get_route(f'match:{match_id}')
                if not detail:
                    continue
                bets_map     = detail.get('bets', {})
                outcomes_map = detail.get('outcomes', {})
                odds_map     = detail.get('odds', {})

                bets = []
                for bid_str, bet in bets_map.items():
                    name     = bet.get('betTypeName', '') or bet.get('betTitle', '')
                    category = bet.get('betTypeCategory', '')
                    oc_ids   = bet.get('outcomes', [])
                    outcomes = []
                    for oc_id in oc_ids:
                        oc      = outcomes_map.get(str(oc_id), {})
                        odd_val = odds_map.get(str(oc_id))
                        if odd_val is None:
                            continue
                        label = oc.get('label', '')
                        outcomes.append({'label': label, 'odd': round(float(odd_val), 3)})
                    if outcomes:
                        bets.append({'name': name, 'category': category, 'outcomes': outcomes})

                results.append({'title': title, 'bets': bets})
            except Exception as e:
                print(f'[ Winamax ] ERREUR {title}: {e}', file=sys.stderr)
            time.sleep(delay)

    print(f'[ Winamax ] {len(results)} matchs récupérés.', file=sys.stderr)
    return results


# ── Fetch Betclic ─────────────────────────────────────────────────────────────

def fetch_betclic_football(limit: int = 50, delay: float = 0.0) -> list[dict]:
    from betclic_client import BetclicClient, FOOTBALL_CATEGORIES
    print('[ Betclic ] Connexion…', file=sys.stderr)
    results = []
    with BetclicClient() as client:
        all_matches = client.get_matches('football')
        prematch = [m for m in all_matches if not m['is_live']]
        print(f'[ Betclic ] {len(prematch)} matchs PREMATCH → traitement de {min(limit, len(prematch))}', file=sys.stderr)

        for i, m in enumerate(prematch[:limit], 1):
            match_id = m['match_id']
            title = m['title']
            print(f'[ Betclic ] [{i}/{min(limit,len(prematch))}] {title}', file=sys.stderr)
            try:
                raw_markets = client.get_all_match_markets_for_categories(
                    match_id, FOOTBALL_CATEGORIES, max_seconds=8
                )
                markets = [
                    {
                        'name': mkt['name'],
                        'outcomes': [
                            {'name': s['name'], 'odds': s['odds']}
                            for s in mkt['selections']
                        ],
                    }
                    for mkt in raw_markets
                ]
                results.append({'title': title, 'markets': markets})
            except Exception as e:
                print(f'[ Betclic ] ERREUR {title}: {e}', file=sys.stderr)
            if delay:
                time.sleep(delay)

    print(f'[ Betclic ] {len(results)} matchs récupérés.', file=sys.stderr)
    return results


# ── Fetch Unibet ──────────────────────────────────────────────────────────────

def fetch_unibet_football(limit: int = 50, delay: float = 0.25) -> list[dict]:
    from unibet_client import UnibetClient

    FOOTBALL_MARKET_GROUPS = {
        546: 'Principal', 7: 'Résultat', 9: 'Buts', 12: 'Double Chance',
        17: 'Score Exact', 208: 'Premier But', 283: 'Mi-Temps Résultat',
        284: 'Combiné', 285: 'Qualification',
    }

    def _parse_price(p) -> float:
        try:
            return round(float(str(p).replace(',', '.')), 3)
        except (ValueError, TypeError):
            return 0.0

    print('[ Unibet  ] Connexion…', file=sys.stderr)
    results = []
    total_done = 0

    with UnibetClient() as client:
        leagues = client.get_sport_leagues('FOOT')
        print(f'[ Unibet  ] {len(leagues)} ligues actives.', file=sys.stderr)

        for league in leagues:
            if total_done >= limit:
                break
            lid   = league['id']
            lname = league['name']
            try:
                event_ids = client.get_events_for_league(lid)
            except Exception:
                continue

            for event_id in event_ids:
                if total_done >= limit:
                    break
                try:
                    raw = client.get_event_offers(event_id)
                    if not raw:
                        continue
                    event    = raw.get('event', {})
                    title    = event.get('desc', '')
                    if not title:
                        continue
                    mkts_raw = raw.get('markets', {})
                    ocs_raw  = raw.get('outcomes', {})
                    markets  = []
                    for mid, mkt in mkts_raw.items():
                        group_ids = mkt.get('marketTypeDisplayGroupIds', [])
                        category  = next(
                            (FOOTBALL_MARKET_GROUPS[g] for g in group_ids
                             if g in FOOTBALL_MARKET_GROUPS), 'Autre'
                        )
                        ocs = [
                            {
                                'label': oc.get('desc', ''),
                                'odd':   _parse_price(oc.get('price', 0)),
                            }
                            for oc_id, oc in ocs_raw.items()
                            if oc.get('parent') == mid
                            and _parse_price(oc.get('price', 0)) > 1.0
                        ]
                        if ocs:
                            markets.append({
                                'name':     mkt.get('desc', ''),
                                'category': category,
                                'period':   mkt.get('period', ''),
                                'outcomes': ocs,
                            })
                    if markets:
                        results.append({'title': title, 'markets': markets})
                        total_done += 1
                        print(f'[ Unibet  ] [{total_done}/{limit}] {title}: {len(markets)} marchés', file=sys.stderr)
                except Exception as e:
                    print(f'[ Unibet  ] ERREUR {event_id}: {e}', file=sys.stderr)
                time.sleep(delay)

    print(f'[ Unibet  ] {len(results)} matchs récupérés.', file=sys.stderr)
    return results


# ── Parsers unifiés ───────────────────────────────────────────────────────────
# Chaque parser renvoie :
#   {match_key: {market_slug: {side_or_line_key: float}}}
# Exemple : {('torino','cremonese'): {'total_goals': {(2.5,'under'): 1.43, (2.5,'over'): 2.73}}}

def parse_winamax(data: list[dict]) -> dict:
    out = defaultdict(lambda: defaultdict(dict))
    for m in data:
        mk  = match_key(m['title'])
        _tp = re.split(r'\s+(?:[-–]|vs\.?)\s+', m['title'], flags=re.I)
        if len(_tp) != 2: _tp = ['', '']

        for bet in m.get('bets', []):
            name      = bet.get('name', '')
            nn        = norm(name)
            cat       = norm(bet.get('category', ''))
            outcomes  = bet.get('outcomes', [])

            # ── 1X2 ──
            if nn in ('resultat', '1x2', 'resultat du match') \
                    or (nn == 'resultat' and cat == 'match'):
                for o in outcomes:
                    lbl = o['label']
                    if is_draw(lbl):       side = 'draw'
                    else:                  side = _side_team(lbl, _tp, mk)
                    out[mk]['1x2'][(side,)] = o['odd']

            # ── Total buts (match entier) ──
            elif cat == 'total de buts' and nn == 'nombre de buts':
                for o in outcomes:
                    line = extract_line(o['label'])
                    ov   = is_over(o['label'])
                    if line and ov is not None:
                        out[mk]['total_goals'][(line, 'over' if ov else 'under')] = o['odd']

            # ── BTTS ──
            elif nn in ('les 2 equipes marquent', 'les deux equipes marquent'):
                for o in outcomes:
                    side = 'yes' if 'oui' in norm(o['label']) else 'no'
                    out[mk]['btts'][(side,)] = o['odd']

            # ── Double chance ──
            elif nn == 'double chance' and cat in ('resultat', ''):
                for o in outcomes:
                    lbl = o['label']
                    parts2 = re.split(r'\s*/\s*|\s+ou\s+', lbl, flags=re.I)
                    has_d  = any(is_draw(p.strip()) for p in parts2)
                    sides  = {_side_team(p.strip(), _tp, mk)
                              for p in parts2 if not is_draw(p.strip())}
                    if has_d and 'home' in sides:     slug = '1X'
                    elif has_d and 'away' in sides:   slug = 'X2'
                    elif {'home', 'away'} <= sides:   slug = '12'
                    else:                              slug = None
                    if slug:
                        out[mk]['double_chance'][(slug,)] = o['odd']

            # ── Handicap buts ──
            elif nn == 'ecart de buts (handicap)':
                for o in outcomes:
                    lbl = o['label']
                    sm  = re.search(r'([+-]?\d+[.,]\d+)\s*$', lbl)
                    if not sm: continue
                    line = abs(float(sm.group(1).replace(',', '.')))
                    team = lbl[:sm.start()].strip()
                    side = _side_team(team, _tp, mk)
                    out[mk]['handicap'][(line, side)] = o['odd']

            # ── MT Total buts ──
            elif cat == 'total de buts' and nn == 'mi-temps - nombre de buts':
                for o in outcomes:
                    line = extract_line(o['label'])
                    ov   = is_over(o['label'])
                    if line and ov is not None:
                        out[mk]['ht_total_goals'][(line, 'over' if ov else 'under')] = o['odd']

    return dict(out)


def parse_betclic(data: list[dict]) -> dict:
    out = defaultdict(lambda: defaultdict(dict))
    for m in data:
        mk  = match_key(m['title'])
        _tp = re.split(r'\s+(?:[-–]|vs\.?)\s+', m['title'], flags=re.I)
        if len(_tp) != 2: _tp = ['', '']

        for mkt in m.get('markets', []):
            name     = mkt.get('name', '')
            nn       = norm(name)
            outcomes = mkt.get('outcomes', [])
            lbl_key  = 'name'   # betclic uses 'name' for selection label
            odd_key  = 'odds'

            # ── 1X2 ──
            if nn.startswith('resultat du match') or nn in ('resultat', '1x2'):
                for o in outcomes:
                    lbl = o[lbl_key]
                    if is_draw(lbl):    side = 'draw'
                    else:               side = _side_team(lbl, _tp, mk)
                    out[mk]['1x2'][(side,)] = o[odd_key]

            # ── Total buts ──
            elif 'nombre total de buts' in nn or nn.startswith('total buts'):
                for o in outcomes:
                    line = extract_line(o[lbl_key])
                    ov   = is_over(o[lbl_key])
                    if line and ov is not None:
                        out[mk]['total_goals'][(line, 'over' if ov else 'under')] = o[odd_key]

            # ── BTTS ──
            elif nn == 'les 2 equipes marquent':
                for o in outcomes:
                    side = 'yes' if 'oui' in norm(o[lbl_key]) else 'no'
                    out[mk]['btts'][(side,)] = o[odd_key]

            # ── Double chance ──
            elif nn == 'double chance':
                for o in outcomes:
                    lbl    = o[lbl_key]
                    parts2 = re.split(r'\s*/\s*|\s+ou\s+', lbl, flags=re.I)
                    has_d  = any(is_draw(p.strip()) for p in parts2)
                    sides  = {_side_team(p.strip(), _tp, mk)
                              for p in parts2 if not is_draw(p.strip())}
                    if has_d and 'home' in sides:     slug = '1X'
                    elif has_d and 'away' in sides:   slug = 'X2'
                    elif {'home', 'away'} <= sides:   slug = '12'
                    else:                              slug = None
                    if slug:
                        out[mk]['double_chance'][(slug,)] = o[odd_key]

            # ── Handicap ──
            elif 'handicap asiatique' in nn or nn.startswith('ha '):
                for o in outcomes:
                    lbl = o[lbl_key]
                    sm  = re.search(r'([+-]?\d+[.,]\d+)\s*$', lbl)
                    if not sm: continue
                    line = abs(float(sm.group(1).replace(',', '.')))
                    team = lbl[:sm.start()].strip()
                    side = _side_team(team, _tp, mk)
                    out[mk]['handicap'][(line, side)] = o[odd_key]

            # ── MT Total buts ──
            elif 'mi-temps' in nn and 'but' in nn and 'resultat' not in nn \
                    and 'score' not in nn and 'equipe' not in nn:
                for o in outcomes:
                    line = extract_line(o[lbl_key])
                    ov   = is_over(o[lbl_key])
                    if line and ov is not None:
                        out[mk]['ht_total_goals'][(line, 'over' if ov else 'under')] = o[odd_key]

    return dict(out)


def parse_unibet(data: list[dict]) -> dict:
    out = defaultdict(lambda: defaultdict(dict))
    for m in data:
        mk  = match_key(m['title'])
        _tp = re.split(r'\s+(?:[-–]|vs\.?)\s+', m['title'], flags=re.I)
        if len(_tp) != 2: _tp = ['', '']

        for mkt in m.get('markets', []):
            name     = mkt.get('name', '')
            nn       = norm(name)
            cat      = norm(mkt.get('category', ''))
            period   = norm(mkt.get('period', ''))
            outcomes = mkt.get('outcomes', [])
            lbl_key  = 'label'
            odd_key  = 'odd'

            # ── 1X2 ──
            is_1x2 = (
                cat in ('resultat', 'principal') or
                nn in ('resultat', 'resultat du match', '1x2', '1 n 2') or
                (any(is_draw(o.get(lbl_key, '')) for o in outcomes) and len(outcomes) == 3)
            )
            if is_1x2:
                for o in outcomes:
                    lbl = o.get(lbl_key, '')
                    if is_draw(lbl):    side = 'draw'
                    else:               side = _side_team(lbl, _tp, mk)
                    out[mk]['1x2'][(side,)] = o[odd_key]

            # ── Détection période Unibet ──
            # period peut être : '90 Mins', 'Mi-temps', '2ème Mi-temps', 'Quarts', ''
            _p_norm = period  # déjà normalisé via norm()
            _is_fullmatch = '90' in _p_norm or _p_norm in ('', 'match', 'ft')
            _is_halftime  = ('mi' in _p_norm and 'mi-temps' in _p_norm
                             and '2' not in _p_norm and 'eme' not in _p_norm)

            # ── Total buts (match entier) ──
            # Deux formats Unibet : "Plus / Moins 2.5 But(s)" ou "Nombre total de buts 2.5"
            _is_match_total_goals = (
                re.match(r'plus / moins \d+[,.]\d+ but', nn) or
                re.match(r'nombre total de buts', nn)
            )
            if _is_match_total_goals and _is_fullmatch:
                line = extract_line(name)
                if line:
                    for o in outcomes:
                        ov = is_over(o.get(lbl_key, ''))
                        if ov is not None:
                            out[mk]['total_goals'][(line, 'over' if ov else 'under')] = o[odd_key]

            # ── BTTS ──
            elif nn.startswith('les 2 equipes marqueront') or nn.startswith('les deux equipes'):
                for o in outcomes:
                    side = 'yes' if 'oui' in norm(o.get(lbl_key, '')) else 'no'
                    out[mk]['btts'][(side,)] = o[odd_key]

            # ── Double chance ──
            elif nn == 'double chance' and _is_fullmatch:
                for o in outcomes:
                    lbl    = o.get(lbl_key, '')
                    parts2 = re.split(r'\s*/\s*|\s+ou\s+', lbl, flags=re.I)
                    has_d  = any(is_draw(p.strip()) for p in parts2)
                    sides  = {_side_team(p.strip(), _tp, mk)
                              for p in parts2 if not is_draw(p.strip())}
                    if has_d and 'home' in sides:     slug = '1X'
                    elif has_d and 'away' in sides:   slug = 'X2'
                    elif {'home', 'away'} <= sides:   slug = '12'
                    else:                              slug = None
                    if slug:
                        out[mk]['double_chance'][(slug,)] = o[odd_key]

            # ── Handicap ──
            elif re.search(r'handicap.*but|ecart.*but|but.*handicap', nn) and _is_fullmatch:
                line_m = re.search(r'\[([+-]?\d+[.,]\d+)\]', name)
                if line_m:
                    line = abs(float(line_m.group(1).replace(',', '.')))
                    for o in outcomes:
                        lbl  = o.get(lbl_key, '')
                        team = re.sub(r'\s*\[[^\]]*\]\s*$', '', lbl).strip()
                        side = _side_team(team, _tp, mk)
                        out[mk]['handicap'][(line, side)] = o[odd_key]

            # ── MT Total buts ──
            elif re.search(r'\bbut', nn) and _is_halftime \
                    and not re.search(r'equipe|buteur|exact', nn):
                line = extract_line(name)
                if line:
                    for o in outcomes:
                        ov = is_over(o.get(lbl_key, ''))
                        if ov is not None:
                            out[mk]['ht_total_goals'][(line, 'over' if ov else 'under')] = o[odd_key]

    return dict(out)


# ── Comparaison ───────────────────────────────────────────────────────────────

MARKET_LABELS = {
    'total_goals':   'Total buts',
    '1x2':           '1X2',
    'btts':          'Les 2 équipes marquent',
    'double_chance': 'Double chance',
    'handicap':      'Handicap buts',
    'ht_total_goals':'MT Total buts',
}

BOOKS = ['winamax', 'betclic', 'unibet']
BK_COL = 10


def _title_for_mk(mk: tuple, titles: dict) -> str:
    t = titles.get(mk, '')
    if not t:
        t = ' - '.join(p.capitalize() for p in mk)
    return t


def _key_label(k: tuple, market: str) -> str:
    if market in ('total_goals', 'ht_total_goals'):
        line, side = k
        sym = '+' if side == 'over' else '-'
        return f'{sym}{line}'
    if market in ('1x2', 'btts', 'double_chance'):
        return k[0]
    if market == 'handicap':
        line, side = k
        return f'{side} {line:+.1f}' if isinstance(line, float) else f'{side} ±{line}'
    return str(k)


def compare_markets(
    all_data: dict[str, dict],
    market: str | None = None,
    filter_line: float | None = None,
    filter_side: str | None = None,
) -> list[dict]:
    """
    all_data: {'winamax': parsed_dict, 'betclic': parsed_dict, 'unibet': parsed_dict}
    Retourne une liste de résultats de comparaison.
    """
    markets_to_show = ([market] if market else list(MARKET_LABELS.keys()))
    present_books   = [b for b in BOOKS if b in all_data]

    # Fusionner les match keys
    all_keys: set = set()
    for bk_data in all_data.values():
        all_keys.update(bk_data.keys())

    rows = []
    for mk in sorted(all_keys):
        for mkt in markets_to_show:
            # Collecter toutes les keys de paris disponibles pour ce marché
            all_bet_keys: set = set()
            for bk in present_books:
                bk_data = all_data.get(bk, {})
                all_bet_keys.update(bk_data.get(mk, {}).get(mkt, {}).keys())

            if not all_bet_keys:
                continue

            for bk_key in sorted(all_bet_keys, key=str):
                # Filtres
                if filter_line is not None and mkt in ('total_goals', 'ht_total_goals', 'handicap'):
                    key_line = bk_key[0] if len(bk_key) > 0 else None
                    if key_line != filter_line:
                        continue
                if filter_side is not None and mkt in ('total_goals', 'ht_total_goals'):
                    key_side = bk_key[1] if len(bk_key) > 1 else ''
                    if key_side != filter_side:
                        continue

                bk_odds: dict[str, float] = {}
                for bk in present_books:
                    odd = all_data.get(bk, {}).get(mk, {}).get(mkt, {}).get(bk_key)
                    if odd:
                        bk_odds[bk] = odd

                if not bk_odds:
                    continue

                best_bk  = max(bk_odds, key=bk_odds.get)
                best_odd = bk_odds[best_bk]

                rows.append({
                    'match_key':  mk,
                    'market':     mkt,
                    'bet_key':    bk_key,
                    'best_book':  best_bk,
                    'best_odds':  best_odd,
                    'all_odds':   bk_odds,
                })

    return rows


def print_comparison(rows: list[dict], titles: dict, present_books: list[str]):
    if not rows:
        print('Aucun marché comparé trouvé.')
        return

    # Grouper par match puis par marché
    by_match = defaultdict(lambda: defaultdict(list))
    for r in rows:
        by_match[r['match_key']][r['market']].append(r)

    COL = 12
    hdr_books = ''.join(f'{b[:COL-1]:>{COL}}' for b in present_books)

    for mk, by_mkt in by_match.items():
        title = _title_for_mk(mk, titles)
        print(f'\n{"="*72}')
        print(f'  {title}')
        print(f'{"="*72}')

        for mkt, mkt_rows in by_mkt.items():
            label = MARKET_LABELS.get(mkt, mkt)
            print(f'\n  [ {label} ]')
            print(f'    {"Pari":<18}' + hdr_books + f'{"Meilleur":>{COL}}')
            print(f'    {"-"*18}' + '-'*COL*len(present_books) + '-'*COL)

            for r in sorted(mkt_rows, key=lambda x: str(x['bet_key'])):
                key_lbl = _key_label(r['bet_key'], mkt)
                odds_str = ''
                for bk in present_books:
                    odd = r['all_odds'].get(bk)
                    odds_str += f'{odd:>{COL}.3f}' if odd else f'{"N/A":>{COL}}'
                best_str = f'{r["best_book"][:6]}({r["best_odds"]:.3f})'
                print(f'    {key_lbl:<18}' + odds_str + f'{best_str:>{COL}}')


def print_best_odds_summary(rows: list[dict], titles: dict):
    """Résumé: meilleure cote par bookmaker, par marché."""
    if not rows:
        return
    wins: dict[str, int] = defaultdict(int)
    for r in rows:
        wins[r['best_book']] += 1
    total = len(rows)
    print(f'\n{"="*72}')
    print('  RÉSUMÉ — Quel bookmaker propose la meilleure cote ?')
    print(f'{"="*72}')
    for bk in sorted(wins, key=wins.get, reverse=True):
        pct = wins[bk] / total * 100
        bar = '█' * int(pct / 5)
        print(f'  {bk:<10} {wins[bk]:>4} fois ({pct:5.1f}%)  {bar}')
    print(f'  Total comparaisons : {total}')


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description='Comparer les cotes football entre bookmakers')
    parser.add_argument('--market', '-m', choices=list(MARKET_LABELS.keys()),
                        default=None, help='Marché à comparer (défaut: tous)')
    parser.add_argument('--line', '-l', type=float, default=None,
                        help='Filtrer sur une ligne précise (ex: 2.5)')
    parser.add_argument('--side', '-s', choices=['over', 'under', 'home', 'away', 'draw'],
                        default=None, help='Filtrer sur un côté')
    parser.add_argument('--limit', '-n', type=int, default=20,
                        help='Nombre max de matchs par bookmaker (défaut: 20)')
    parser.add_argument('--no-winamax', action='store_true', help='Exclure Winamax')
    parser.add_argument('--no-betclic', action='store_true', help='Exclure Betclic')
    parser.add_argument('--no-unibet',  action='store_true', help='Exclure Unibet')
    parser.add_argument('--delay', type=float, default=0.2,
                        help='Délai entre requêtes (défaut: 0.2s)')
    parser.add_argument('--output', '-o', help='Sauvegarder les données brutes en JSON')
    parser.add_argument('--from-json', help='Charger données depuis JSON (au lieu de scraper)')
    args = parser.parse_args()

    # ── Chargement des données ──
    titles: dict = {}  # match_key → title string

    if args.from_json:
        print(f'Chargement depuis {args.from_json}…', file=sys.stderr)
        with open(args.from_json) as f:
            raw = json.load(f)
        wm_data = raw.get('winamax', [])
        bc_data = raw.get('betclic', [])
        ub_data = raw.get('unibet', [])
    else:
        wm_data = [] if args.no_winamax else fetch_winamax_football(args.limit, args.delay)
        bc_data = [] if args.no_betclic else fetch_betclic_football(args.limit)
        ub_data = [] if args.no_unibet  else fetch_unibet_football(args.limit, args.delay)

    # ── Sauvegarder si demandé ──
    if args.output:
        with open(args.output, 'w', encoding='utf-8') as f:
            json.dump({'winamax': wm_data, 'betclic': bc_data, 'unibet': ub_data},
                      f, ensure_ascii=False, indent=2)
        print(f'Données sauvegardées dans {args.output}', file=sys.stderr)

    # ── Collecter les titres ──
    for d in wm_data:
        mk = match_key(d['title'])
        titles[mk] = d['title']
    for d in bc_data:
        mk = match_key(d['title'])
        titles[mk] = d['title']
    for d in ub_data:
        mk = match_key(d['title'])
        titles[mk] = d['title']

    # ── Parser ──
    print('\nParsing…', file=sys.stderr)
    all_data = {}
    if wm_data: all_data['winamax'] = parse_winamax(wm_data)
    if bc_data: all_data['betclic'] = parse_betclic(bc_data)
    if ub_data: all_data['unibet']  = parse_unibet(ub_data)

    if not all_data:
        print('Aucune donnée disponible.', file=sys.stderr)
        sys.exit(1)

    present_books = [b for b in BOOKS if b in all_data]

    # ── Comparer ──
    rows = compare_markets(
        all_data,
        market=args.market,
        filter_line=args.line,
        filter_side=args.side,
    )

    print_comparison(rows, titles, present_books)
    print_best_odds_summary(rows, titles)


if __name__ == '__main__':
    main()
