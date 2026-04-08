#!/usr/bin/env python3
"""
compare.py — Détecte les cotes surévaluées sur Winamax, Betclic, Unibet.

Principe :
  1. Charge les JSON de chaque bookmaker (ou les génère avec --fetch)
  2. Normalise les matchs et marchés pour les aligner entre bookmakers
  3. Calcule la probabilité "juste" (sans marge) = moyenne des proba no-vig
  4. Affiche les cotes supérieures à la cote juste (= value bets)

Usage :
    # Récupération unique (tous les bookmakers en parallèle, sans stockage fichier)
    python compare.py --fetch

    # Mode temps réel : rafraîchit toutes les 60s (Ctrl+C pour quitter)
    python compare.py --watch

    # Mode temps réel avec intervalle personnalisé (ex: 120s)
    python compare.py --watch 120

    # Seuil minimum d'edge (défaut 3%)
    python compare.py --fetch --min-edge 5.0

    # Consensus basé sur au moins 3 bookmakers
    python compare.py --watch --min-books 3

    # Depuis des fichiers JSON existants
    python compare.py --winamax w.json --betclic b.json --unibet u.json
"""
import re
import sys
import json
import argparse
import unicodedata
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path


# ── Normalisation ────────────────────────────────────────────────────────────

def _no_accents(s: str) -> str:
    return ''.join(c for c in unicodedata.normalize('NFKD', s)
                   if not unicodedata.combining(c))

def norm(s: str) -> str:
    return _no_accents(s).lower().strip()

def last_name(player: str) -> str:
    """'D.Medvedev' → 'medvedev', 'Gaël Monfils' → 'monfils', 'AugerAliassime' → 'augeraliassime'."""
    p = player.strip()
    p = re.sub(r'^[A-Z][A-Z]?\.\s*', '', p)   # supprime initiale "D." ou "FA."
    p = re.sub(r'^[A-Z][a-z]+-', '', p)        # supprime "Felix-" dans "Felix-Auger..."
    parts = p.split()
    return norm(parts[-1] if parts else p)

def match_key(title: str) -> tuple[str, str]:
    """Retourne (last1, last2) trié alphabétiquement."""
    parts = re.split(r'\s+(?:[-–]|vs\.?)\s+', title, flags=re.I)
    if len(parts) != 2:
        return (norm(title), '')
    l1, l2 = last_name(parts[0]), last_name(parts[1])
    return tuple(sorted([l1, l2]))

def extract_line(text: str) -> float | None:
    """'Plus de 18,5' → 18.5  |  '+ de 22.5' → 22.5."""
    m = re.search(r'(\d+)[,.](\d)', text)
    return float(f'{m.group(1)}.{m.group(2)}') if m else None

def is_over(text: str) -> bool | None:
    t = norm(text)
    if re.search(r'\bplus\b|^[+]|\bover\b', t):
        return True
    if re.search(r'\bmoins\b|^[-]|\bunder\b', t):
        return False
    return None


# ── Modèle unifié ────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class BetKey:
    """Identifiant canonique d'un pari."""
    market: str    # 'match_winner' | 'total_games' | 'total_aces' | 'total_breaks'
                   # | 'player_aces' | 'player_breaks'
    line: float | None = None      # ex: 18.5
    side: str = ''                 # 'over' | 'under' | 'p0' | 'p1' (index joueur trié)

@dataclass
class Offer:
    bookmaker: str
    odds: float
    raw_label: str

# {match_key: {BetKey: {side_complement: [Offer]}}}
# Pour calculer la proba no-vig on a besoin des 2 côtés du même marché.
# Structure: matched_bets[mkey][group_key] = {'over': [Offer,...], 'under': [Offer,...]}
#            où group_key = (market, line) ou (market, None) pour match_winner


# ── Parsing Winamax ──────────────────────────────────────────────────────────

def _wm_player_side(label: str, sorted_players: tuple[str, str]) -> str:
    """Détermine si le label correspond au joueur 0 ou 1 (selon ordre alphabétique)."""
    ln = last_name(label)
    if ln == sorted_players[0]:
        return 'p0'
    if ln == sorted_players[1]:
        return 'p1'
    # Fallback : similarité
    from difflib import SequenceMatcher
    s0 = SequenceMatcher(None, ln, sorted_players[0]).ratio()
    s1 = SequenceMatcher(None, ln, sorted_players[1]).ratio()
    return 'p0' if s0 >= s1 else 'p1'

def parse_winamax(data: list[dict]) -> dict:
    """→ {match_key: {(market, line): {'over'/'under'/'p0'/'p1': [Offer]}}}"""
    result = defaultdict(lambda: defaultdict(lambda: defaultdict(list)))
    for m in data:
        mk = match_key(m['title'])
        sp = mk  # sorted players (last names)
        for bet in m.get('bets', []):
            name = bet['name']
            outcomes = bet.get('outcomes', [])
            norm_name = norm(name)

            # ── Match winner ──
            if norm_name == 'vainqueur' and bet.get('category') == 'Match':
                for o in outcomes:
                    side = _wm_player_side(o['label'], sp)
                    result[mk][('match_winner', None)][side].append(
                        Offer('winamax', o['odd'], o['label']))

            # ── Total jeux ──
            elif norm_name == 'nombre de jeux':
                for o in outcomes:
                    line = extract_line(o['label'])
                    over = is_over(o['label'])
                    if line and over is not None:
                        side = 'over' if over else 'under'
                        result[mk][('total_games', line)][side].append(
                            Offer('winamax', o['odd'], o['label']))

            # ── Total aces ──
            elif norm_name in ("nombre d'aces", "nombre total d'aces"):
                for o in outcomes:
                    line = extract_line(o['label'])
                    over = is_over(o['label'])
                    if line and over is not None:
                        side = 'over' if over else 'under'
                        result[mk][('total_aces', line)][side].append(
                            Offer('winamax', o['odd'], o['label']))

            # ── Aces par joueur ──
            elif re.match(r"nombre d'aces de ", norm_name):
                player_part = re.sub(r"nombre d'aces de ", '', norm_name)
                for o in outcomes:
                    line = extract_line(o['label'])
                    over = is_over(o['label'])
                    if line and over is not None:
                        side = 'over' if over else 'under'
                        player_ln = last_name(player_part)
                        result[mk][('player_aces', line, player_ln)][side].append(
                            Offer('winamax', o['odd'], o['label']))

            # ── Total breaks ──
            elif 'nombre de breaks dans le match' in norm_name:
                for o in outcomes:
                    line = extract_line(o['label'])
                    over = is_over(o['label'])
                    if line and over is not None:
                        side = 'over' if over else 'under'
                        result[mk][('total_breaks', line)][side].append(
                            Offer('winamax', o['odd'], o['label']))

            # ── Breaks par joueur ──
            elif re.match(r'nombre de breaks de ', norm_name):
                player_part = re.sub(r'nombre de breaks de ', '', norm_name)
                for o in outcomes:
                    line = extract_line(o['label'])
                    over = is_over(o['label'])
                    if line and over is not None:
                        side = 'over' if over else 'under'
                        player_ln = last_name(player_part)
                        result[mk][('player_breaks', line, player_ln)][side].append(
                            Offer('winamax', o['odd'], o['label']))

    return result


# ── Parsing Betclic ──────────────────────────────────────────────────────────

def parse_betclic(data: list[dict]) -> dict:
    result = defaultdict(lambda: defaultdict(lambda: defaultdict(list)))
    for m in data:
        mk = match_key(m['title'])
        sp = mk
        for mkt in m.get('markets', []):
            name = mkt['name']
            norm_name = norm(name)
            outcomes = mkt.get('outcomes', [])

            # ── Match winner ──
            if norm_name == 'vainqueur du match':
                for o in outcomes:
                    side = _wm_player_side(o['name'], sp)
                    result[mk][('match_winner', None)][side].append(
                        Offer('betclic', o['odds'], o['name']))

            # ── Total jeux (toutes les lignes dans un seul marché) ──
            elif norm_name == 'nombre total de jeux':
                for o in outcomes:
                    line = extract_line(o['name'])
                    over = is_over(o['name'])
                    if line and over is not None:
                        side = 'over' if over else 'under'
                        result[mk][('total_games', line)][side].append(
                            Offer('betclic', o['odds'], o['name']))

    return result


# ── Parsing Unibet ───────────────────────────────────────────────────────────

def parse_unibet(data: list[dict]) -> dict:
    result = defaultdict(lambda: defaultdict(lambda: defaultdict(list)))
    for m in data:
        mk = match_key(m['title'])
        sp = mk
        for mkt in m.get('markets', []):
            name = mkt['name']
            norm_name = norm(name)
            cat = norm(mkt.get('category', ''))
            outcomes = mkt.get('outcomes', [])

            # ── Match winner ──
            if norm_name == 'face a face' and cat == 'resultat':
                for o in outcomes:
                    side = _wm_player_side(o['label'], sp)
                    result[mk][('match_winner', None)][side].append(
                        Offer('unibet', o['odd'], o['label']))

            # ── Total jeux : "Plus / Moins Jeu(x) 18,5" ──
            elif cat == 'jeu' and re.search(r'jeu', norm_name):
                line = extract_line(name)
                if line:
                    for o in outcomes:
                        over = is_over(o['label'])
                        if over is not None:
                            side = 'over' if over else 'under'
                            result[mk][('total_games', line)][side].append(
                                Offer('unibet', o['odd'], o['label']))

            # ── Total aces : "Plus / Moins (Aces) 9,5" (sans joueur) ──
            elif cat == 'aces' and re.match(r'plus / moins \(aces\) \d', norm_name):
                line = extract_line(name)
                if line:
                    for o in outcomes:
                        over = is_over(o['label'])
                        if over is not None:
                            side = 'over' if over else 'under'
                            result[mk][('total_aces', line)][side].append(
                                Offer('unibet', o['odd'], o['label']))

            # ── Aces par joueur : "Plus / Moins (Aces) - D.Medvedev 4,5" ──
            elif cat in ('aces', 'paris populaires') and re.search(r'aces.*-', norm_name):
                # Extraire nom joueur entre '-' et la ligne
                pm = re.search(r'-\s*([A-Za-z.]+)\s+(\d+[,.]\d)', name)
                if pm:
                    player_ln = last_name(pm.group(1))
                    line = extract_line(name)
                    if line:
                        for o in outcomes:
                            over = is_over(o['label'])
                            if over is not None:
                                side = 'over' if over else 'under'
                                result[mk][('player_aces', line, player_ln)][side].append(
                                    Offer('unibet', o['odd'], o['label']))

            # ── Total breaks : "Plus / Moins X,Y Break(s)" ──
            elif cat == 'breaks' and 'break' in norm_name and '-' not in name:
                line = extract_line(name)
                if line:
                    for o in outcomes:
                        over = is_over(o['label'])
                        if over is not None:
                            side = 'over' if over else 'under'
                            result[mk][('total_breaks', line)][side].append(
                                Offer('unibet', o['odd'], o['label']))

            # ── Breaks par joueur : "Plus / Moins X,Y Break(s) - Player" ──
            elif cat == 'breaks' and 'break' in norm_name and '-' in name:
                player_part = name.split('-', 1)[-1].strip()
                player_ln = last_name(player_part)
                line = extract_line(name)
                if line:
                    for o in outcomes:
                        over = is_over(o['label'])
                        if over is not None:
                            side = 'over' if over else 'under'
                            result[mk][('player_breaks', line, player_ln)][side].append(
                                Offer('unibet', o['odd'], o['label']))

    return result


# ── Fusion et calcul de value ────────────────────────────────────────────────

@dataclass
class ValueBet:
    match: str
    market: str
    line: float | None
    side: str
    bookmaker: str
    odds: float
    fair_odds: float
    edge_pct: float          # (odds/fair_odds - 1) * 100
    all_odds: dict           # {bk: odds}

def no_vig_prob(odds_a: float, odds_b: float) -> tuple[float, float]:
    """Retourne (prob_no_vig_A, prob_no_vig_B) sans marge."""
    p_a, p_b = 1 / odds_a, 1 / odds_b
    total = p_a + p_b
    return p_a / total, p_b / total

def _market_label(key) -> str:
    market = key[0]
    labels = {
        'match_winner': 'Vainqueur',
        'total_games':  'Total jeux',
        'total_aces':   'Total aces',
        'total_breaks': 'Total breaks',
        'player_aces':  'Aces joueur',
        'player_breaks':'Breaks joueur',
    }
    base = labels.get(market, market)
    if len(key) == 3 and key[2] not in ('over','under','p0','p1'):
        # player prop
        return f'{base} ({key[2]})'
    return base

def _side_label(side: str, match_players: tuple[str, str]) -> str:
    if side == 'p0':
        return match_players[0].capitalize()
    if side == 'p1':
        return match_players[1].capitalize()
    return side

def compute_value_bets(
    all_books: list[dict],
    min_edge: float = 3.0,
    min_books: int = 2,       # au minimum 2 bookmakers pour calculer le consensus
) -> list[ValueBet]:
    """
    Fusionne les données des 3 bookmakers et retourne les value bets.
    all_books = [winamax_dict, betclic_dict, unibet_dict]
    """
    # Rassembler toutes les clés de matchs
    all_match_keys: set = set()
    for bk in all_books:
        all_match_keys.update(bk.keys())

    value_bets: list[ValueBet] = []

    for mk in all_match_keys:
        # Fusionner les marchés de ce match depuis tous les bookmakers
        merged: dict = defaultdict(lambda: defaultdict(list))
        for bk_data in all_books:
            if mk not in bk_data:
                continue
            for group_key, sides in bk_data[mk].items():
                for side, offers in sides.items():
                    merged[group_key][side].extend(offers)

        # Pour chaque groupe (market, line) : calculer la fair value
        for group_key, sides in merged.items():
            # Identifier les 2 côtés attendus
            if group_key[0] == 'match_winner':
                side_pair = ('p0', 'p1')
            else:
                side_pair = ('over', 'under')

            s_a, s_b = side_pair
            if s_a not in sides or s_b not in sides:
                continue

            # Construire {bk: best_odds} pour chaque côté
            def best_by_bk(offers: list[Offer]) -> dict[str, float]:
                d: dict[str, float] = {}
                for o in offers:
                    if o.bookmaker not in d or o.odds > d[o.bookmaker]:
                        d[o.bookmaker] = o.odds
                return d

            bk_a = best_by_bk(sides[s_a])
            bk_b = best_by_bk(sides[s_b])

            # Bookmakers communs (ont les 2 côtés)
            common = set(bk_a) & set(bk_b)
            if len(common) < min_books:
                continue

            # Calculer la proba no-vig pour chaque bk commun
            probs_a, probs_b = [], []
            for bk in common:
                pa, pb = no_vig_prob(bk_a[bk], bk_b[bk])
                probs_a.append(pa)
                probs_b.append(pb)

            # Consensus = moyenne des probas no-vig
            consensus_a = sum(probs_a) / len(probs_a)
            consensus_b = sum(probs_b) / len(probs_b)

            # Fair odds
            fair_a = 1 / consensus_a if consensus_a > 0 else 9999
            fair_b = 1 / consensus_b if consensus_b > 0 else 9999

            line = group_key[1] if len(group_key) > 1 else None

            # Vérifier chaque bookmaker pour chaque côté
            for (side, all_bk, fair_odds) in [
                (s_a, bk_a, fair_a),
                (s_b, bk_b, fair_b),
            ]:
                for bk, odds in all_bk.items():
                    edge = (odds / fair_odds - 1) * 100
                    if edge >= min_edge:
                        value_bets.append(ValueBet(
                            match=' vs '.join(p.capitalize() for p in mk),
                            market=_market_label(group_key),
                            line=line,
                            side=_side_label(side, mk),
                            bookmaker=bk,
                            odds=odds,
                            fair_odds=round(fair_odds, 3),
                            edge_pct=round(edge, 1),
                            all_odds={**{f'{k}(A)': v for k, v in bk_a.items()},
                                      **{f'{k}(B)': v for k, v in bk_b.items()}},
                        ))

    return sorted(value_bets, key=lambda v: -v.edge_pct)



# ── Chargement dynamique des scrapers ─────────────────────────────────────────

_SCRIPT_DIR = Path(__file__).parent


def _load_scraper(subdir: str):
    """Importe tennis_odds depuis le sous-dossier sans passer par subprocess."""
    import importlib.util as _ilu
    subdir_path = _SCRIPT_DIR / subdir
    path_str = str(subdir_path)
    if path_str not in sys.path:
        sys.path.insert(0, path_str)
    spec = _ilu.spec_from_file_location(f'_{subdir}_odds', subdir_path / 'tennis_odds.py')
    mod = _ilu.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def fetch_all_odds(prematch_only: bool = True) -> tuple[list, list, list]:
    """Fetch odds from all 3 bookmakers in parallel threads.

    Returns (winamax_raw, betclic_raw, unibet_raw) as lists of dicts
    ready to be passed to parse_winamax / parse_betclic / parse_unibet.
    """
    import dataclasses
    import threading as _th

    results: dict[str, list] = {'winamax': [], 'betclic': [], 'unibet': []}

    def _run_winamax():
        try:
            mod = _load_scraper('winamax')
            with mod.WinamaxClient() as c:
                matches = mod.scrape_all_tennis_odds(c, prematch_only=prematch_only)
            results['winamax'] = [dataclasses.asdict(m) for m in matches]
            print(f'  winamax : {len(matches)} matchs', flush=True)
        except Exception as e:
            print(f'  winamax : ERREUR — {e}', file=sys.stderr, flush=True)

    def _run_betclic():
        try:
            mod = _load_scraper('betclic')
            with mod.BetclicClient() as c:
                matches = mod.scrape_all_tennis(c, prematch_only=prematch_only)
            results['betclic'] = [dataclasses.asdict(m) for m in matches]
            print(f'  betclic : {len(matches)} matchs', flush=True)
        except Exception as e:
            print(f'  betclic : ERREUR — {e}', file=sys.stderr, flush=True)

    def _run_unibet():
        try:
            mod = _load_scraper('unibet')
            with mod.UnibetClient() as c:
                matches = mod.scrape_tennis_odds(c, prematch_only=prematch_only)
            results['unibet'] = [dataclasses.asdict(m) for m in matches]
            print(f'  unibet  : {len(matches)} matchs', flush=True)
        except Exception as e:
            print(f'  unibet  : ERREUR — {e}', file=sys.stderr, flush=True)

    threads = [
        _th.Thread(target=_run_winamax, daemon=True),
        _th.Thread(target=_run_betclic, daemon=True),
        _th.Thread(target=_run_unibet,  daemon=True),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    return results['winamax'], results['betclic'], results['unibet']


# ── Affichage ────────────────────────────────────────────────────────────────

def print_results(value_bets: list[ValueBet], min_edge: float):
    if not value_bets:
        print(f'\nAucun value bet détecté (seuil: {min_edge}%).')
        return

    print(f'\n{"="*80}')
    print(f'  VALUE BETS — {len(value_bets)} opportunité(s) détectée(s)  (seuil: {min_edge}%)')
    print(f'{"="*80}')

    prev_match = None
    for vb in value_bets:
        if vb.match != prev_match:
            print(f'\n  ► {vb.match}')
            prev_match = vb.match

        line_str = f' {vb.line}' if vb.line is not None else ''
        print(f'    [{vb.market}{line_str}] {vb.side}')
        print(f'      {vb.bookmaker.upper():10s} cote {vb.odds:.2f}  '
              f'(juste: {vb.fair_odds:.2f})  edge: +{vb.edge_pct:.1f}%')


# ── Main ─────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description='Comparateur de cotes tennis')
    parser.add_argument('--winamax', help='JSON Winamax (lecture depuis fichier)')
    parser.add_argument('--betclic', help='JSON Betclic (lecture depuis fichier)')
    parser.add_argument('--unibet',  help='JSON Unibet  (lecture depuis fichier)')
    parser.add_argument('--fetch',   action='store_true',
                        help='Récupère les cotes une fois (tous les bookmakers en parallèle)')
    parser.add_argument('--watch',   type=int, metavar='SECONDES', nargs='?', const=60,
                        help='Mode temps réel : rafraîchit toutes les N secondes (défaut: 60)')
    parser.add_argument('--min-edge', type=float, default=3.0,
                        help='Edge minimum en %% (défaut: 3.0)')
    parser.add_argument('--min-books', type=int, default=2,
                        help='Nbre min de bookmakers pour le consensus (défaut: 2)')
    parser.add_argument('--output', help='Exporter les value bets en JSON')
    args = parser.parse_args()

    live_mode = args.watch is not None
    do_fetch  = args.fetch or live_mode
    interval  = args.watch or 0  # 0 = run once

    if do_fetch:
        import time as _time
        first_run = True
        try:
            while True:
                if live_mode and not first_run:
                    print('\033[2J\033[H', end='')  # clear screen
                first_run = False

                ts = _time.strftime('%H:%M:%S')
                print(f'[{ts}] Récupération des cotes en parallèle…', flush=True)

                wm_raw, bc_raw, ub_raw = fetch_all_odds(prematch_only=True)

                wm = parse_winamax(wm_raw)
                bc = parse_betclic(bc_raw)
                ub = parse_unibet(ub_raw)

                value_bets = compute_value_bets([wm, bc, ub], args.min_edge, args.min_books)
                print_results(value_bets, args.min_edge)

                if args.output:
                    import dataclasses
                    with open(args.output, 'w', encoding='utf-8') as f:
                        json.dump([dataclasses.asdict(v) for v in value_bets],
                                  f, ensure_ascii=False, indent=2)
                    print(f'\nRésultats exportés → {args.output}')

                if not interval:
                    break

                print(f'\nProchain refresh dans {interval}s…  [Ctrl+C pour quitter]', flush=True)
                _time.sleep(interval)

        except KeyboardInterrupt:
            print('\nArrêt.')

    else:
        # ── Chargement depuis fichiers JSON ──
        def load(path: str | None, parser_fn, name: str) -> dict:
            if not path:
                print(f'  {name}: non fourni, ignoré')
                return {}
            try:
                with open(path) as f:
                    d = json.load(f)
                print(f'  {name}: {len(d)} matchs chargés')
                return parser_fn(d)
            except Exception as e:
                print(f'  {name}: erreur — {e}', file=sys.stderr)
                return {}

        wm = load(args.winamax, parse_winamax, 'Winamax')
        bc = load(args.betclic, parse_betclic, 'Betclic')
        ub = load(args.unibet,  parse_unibet,  'Unibet')

        value_bets = compute_value_bets([wm, bc, ub], args.min_edge, args.min_books)
        print_results(value_bets, args.min_edge)

        if args.output:
            import dataclasses
            with open(args.output, 'w', encoding='utf-8') as f:
                json.dump([dataclasses.asdict(v) for v in value_bets],
                          f, ensure_ascii=False, indent=2)
            print(f'\nRésultats exportés → {args.output}')


if __name__ == '__main__':
    main()
