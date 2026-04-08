#!/usr/bin/env python3
"""
compare.py — Détecte les cotes surévaluées sur Winamax, Betclic, Unibet.

Sports supportés : tennis (défaut), football, basketball

Usage :
    python compare.py --fetch                        # tennis, une fois
    python compare.py --fetch --sport football       # football
    python compare.py --fetch --sport basketball     # basket
    python compare.py --watch                        # tennis, temps réel (60s)
    python compare.py --watch 30 --sport football    # foot, refresh 30s
    python compare.py --fetch --min-edge 5.0         # seuil 5%
    python compare.py --fetch --no-reference         # sans Pinnacle
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


def _football_side(label: str, title_parts: list[str], sorted_keys: tuple[str, str]) -> str:
    """Side detection for football: handles short labels like 'Atalanta' vs 'Atalanta Bergame'."""
    lname = last_name(label)
    if lname == sorted_keys[0]:
        return 'p0'
    if lname == sorted_keys[1]:
        return 'p1'
    ln = norm(label)
    # Substring containment: label in team name or vice versa
    for part in title_parts:
        pn = norm(part)
        if ln in pn or pn in ln:
            key = last_name(part)
            return 'p0' if key == sorted_keys[0] else 'p1'
    # Word-level: any word (≥4 chars) of label found in team name
    for part in title_parts:
        pn = norm(part)
        for word in ln.split():
            if len(word) >= 4 and word in pn.split():
                key = last_name(part)
                return 'p0' if key == sorted_keys[0] else 'p1'
    return _wm_player_side(label, sorted_keys)

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

            # ── Total jeux ──
            elif norm_name == 'nombre total de jeux':
                for o in outcomes:
                    line = extract_line(o['name'])
                    over = is_over(o['name'])
                    if line and over is not None:
                        side = 'over' if over else 'under'
                        result[mk][('total_games', line)][side].append(
                            Offer('betclic', o['odds'], o['name']))

            # ── Aces par joueur : "Prénom Nom - Nombre total d'aces" ──
            elif "nombre total d'aces" in norm_name and ' - ' in name and 'vainqueur' not in norm_name:
                player_part = name.split(' - ')[0].strip()
                player_ln = last_name(player_part)
                for o in outcomes:
                    line = extract_line(o['name'])
                    over = is_over(o['name'])
                    if line and over is not None:
                        side = 'over' if over else 'under'
                        result[mk][('player_aces', line, player_ln)][side].append(
                            Offer('betclic', o['odds'], o['name']))

            # ── Total aces match : "Nombre total d'aces dans le match" ──
            elif "nombre total d'aces" in norm_name and 'vainqueur' not in norm_name:
                for o in outcomes:
                    line = extract_line(o['name'])
                    over = is_over(o['name'])
                    if line and over is not None:
                        side = 'over' if over else 'under'
                        result[mk][('total_aces', line)][side].append(
                            Offer('betclic', o['odds'], o['name']))

            # ── Total breaks ──
            elif 'nombre total de breaks' in norm_name or 'nombre de breaks' in norm_name:
                for o in outcomes:
                    line = extract_line(o['name'])
                    over = is_over(o['name'])
                    if line and over is not None:
                        side = 'over' if over else 'under'
                        result[mk][('total_breaks', line)][side].append(
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


# ── Parsing Football ─────────────────────────────────────────────────────────

def _is_draw_label(label: str) -> bool:
    n = norm(label)
    return 'nul' in n or 'draw' in n or n in ('x', 'n', 'egalite', 'tie', 'match nul')

def parse_winamax_football(data: list[dict]) -> dict:
    result = defaultdict(lambda: defaultdict(lambda: defaultdict(list)))
    for m in data:
        mk  = match_key(m['title'])
        _tp = re.split(r'\s+(?:[-–]|vs\.?)\s+', m['title'], flags=re.I)
        if len(_tp) != 2:
            _tp = ['', '']
        for bet in m.get('bets', []):
            name      = bet['name']
            norm_name = norm(name)
            outcomes  = bet.get('outcomes', [])

            # ── 1X2 : "Résultat" (cat=Match) ou "1X2" ──
            if norm_name in ('resultat', '1x2', 'resultat du match', '1 x 2') \
               or (norm_name == 'resultat' and bet.get('category') == 'Match'):
                for o in outcomes:
                    if _is_draw_label(o['label']):
                        side = 'draw'
                    else:
                        side = _football_side(o['label'], _tp, mk)
                    result[mk][('match_winner_1x2', None)][side].append(
                        Offer('winamax', o['odd'], o['label']))

            # ── Total buts ──
            elif re.search(r'nombre de buts|total buts|buts dans le match', norm_name):
                for o in outcomes:
                    line = extract_line(o['label'])
                    over = is_over(o['label'])
                    if line and over is not None:
                        result[mk][('total_goals', line)]['over' if over else 'under'].append(
                            Offer('winamax', o['odd'], o['label']))

    return result


def parse_betclic_football(data: list[dict]) -> dict:
    result = defaultdict(lambda: defaultdict(lambda: defaultdict(list)))
    for m in data:
        mk  = match_key(m['title'])
        _tp = re.split(r'\s+(?:[-–]|vs\.?)\s+', m['title'], flags=re.I)
        if len(_tp) != 2:
            _tp = ['', '']
        for mkt in m.get('markets', []):
            name      = mkt['name']
            norm_name = norm(name)
            outcomes  = mkt.get('outcomes', [])

            # ── 1X2 : "Résultat du match (tps rég.)" ou variantes ──
            if norm_name.startswith('resultat du match') or norm_name in ('resultat', '1x2'):
                for o in outcomes:
                    if _is_draw_label(o['name']):
                        side = 'draw'
                    else:
                        side = _football_side(o['name'], _tp, mk)
                    result[mk][('match_winner_1x2', None)][side].append(
                        Offer('betclic', o['odds'], o['name']))

            # ── Total buts : "Nombre total de buts" (match entier seulement) ──
            elif norm_name.startswith('nombre total de buts') or norm_name == 'total buts':
                for o in outcomes:
                    line = extract_line(o['name'])
                    over = is_over(o['name'])
                    if line and over is not None:
                        result[mk][('total_goals', line)]['over' if over else 'under'].append(
                            Offer('betclic', o['odds'], o['name']))

    return result


def parse_unibet_football(data: list[dict]) -> dict:
    result = defaultdict(lambda: defaultdict(lambda: defaultdict(list)))
    for m in data:
        mk  = match_key(m['title'])
        _tp = re.split(r'\s+(?:[-–]|vs\.?)\s+', m['title'], flags=re.I)
        if len(_tp) != 2:
            _tp = ['', '']
        for mkt in m.get('markets', []):
            name      = mkt['name']
            norm_name = norm(name)
            cat       = norm(mkt.get('category', ''))
            outcomes  = mkt.get('outcomes', [])

            # ── 1X2 : "1 N 2" ou présence d'un résultat nul parmi 3 issues ──
            is_1x2 = (
                cat in ('resultat', 'principal') or
                norm_name in ('resultat', 'resultat du match', '1x2', '1 n 2') or
                (any(_is_draw_label(o.get('label', '')) for o in outcomes) and len(outcomes) == 3)
            )
            if is_1x2:
                for o in outcomes:
                    if _is_draw_label(o.get('label', '')):
                        side = 'draw'
                    else:
                        side = _football_side(o.get('label', ''), _tp, mk)
                    result[mk][('match_winner_1x2', None)][side].append(
                        Offer('unibet', o['odd'], o.get('label', '')))

            # ── Total buts ──
            elif cat == 'buts' or re.search(r'but', norm_name):
                line = extract_line(name)
                if line:
                    for o in outcomes:
                        over = is_over(o.get('label', ''))
                        if over is not None:
                            result[mk][('total_goals', line)]['over' if over else 'under'].append(
                                Offer('unibet', o['odd'], o.get('label', '')))

    return result


# ── Parsing Basketball ────────────────────────────────────────────────────────

def parse_winamax_basketball(data: list[dict]) -> dict:
    result = defaultdict(lambda: defaultdict(lambda: defaultdict(list)))
    for m in data:
        mk = match_key(m['title'])
        sp = mk
        for bet in m.get('bets', []):
            name      = bet['name']
            norm_name = norm(name)
            outcomes  = bet.get('outcomes', [])

            # ── Vainqueur 2-way ──
            if norm_name == 'vainqueur' and len(outcomes) == 2:
                for o in outcomes:
                    side = _wm_player_side(o['label'], sp)
                    result[mk][('match_winner', None)][side].append(
                        Offer('winamax', o['odd'], o['label']))

            # ── Total points ──
            elif re.search(r'nombre de points|total points|points dans le match', norm_name):
                for o in outcomes:
                    line = extract_line(o['label'])
                    over = is_over(o['label'])
                    if line and over is not None:
                        result[mk][('total_points', line)]['over' if over else 'under'].append(
                            Offer('winamax', o['odd'], o['label']))

    return result


def parse_betclic_basketball(data: list[dict]) -> dict:
    result = defaultdict(lambda: defaultdict(lambda: defaultdict(list)))
    for m in data:
        mk = match_key(m['title'])
        sp = mk
        for mkt in m.get('markets', []):
            name      = mkt['name']
            norm_name = norm(name)
            outcomes  = mkt.get('outcomes', [])

            # ── Vainqueur 2-way ──
            if norm_name in ('vainqueur du match', 'vainqueur') and len(outcomes) == 2:
                for o in outcomes:
                    side = _wm_player_side(o['name'], sp)
                    result[mk][('match_winner', None)][side].append(
                        Offer('betclic', o['odds'], o['name']))

            # ── Total points ──
            elif re.search(r'total points|nombre total de points|points', norm_name):
                for o in outcomes:
                    line = extract_line(o['name'])
                    over = is_over(o['name'])
                    if line and over is not None:
                        result[mk][('total_points', line)]['over' if over else 'under'].append(
                            Offer('betclic', o['odds'], o['name']))

    return result


def parse_unibet_basketball(data: list[dict]) -> dict:
    result = defaultdict(lambda: defaultdict(lambda: defaultdict(list)))
    for m in data:
        mk = match_key(m['title'])
        sp = mk
        for mkt in m.get('markets', []):
            name      = mkt['name']
            norm_name = norm(name)
            cat       = norm(mkt.get('category', ''))
            outcomes  = mkt.get('outcomes', [])

            # ── Vainqueur 2-way : "Face à Face" ou "Résultat" ──
            if norm_name in ('face a face', 'vainqueur', 'resultat') and len(outcomes) == 2:
                for o in outcomes:
                    side = _wm_player_side(o.get('label', ''), sp)
                    result[mk][('match_winner', None)][side].append(
                        Offer('unibet', o['odd'], o.get('label', '')))

            # ── Total points ──
            elif cat == 'points' or re.search(r'point', norm_name):
                line = extract_line(name)
                if line:
                    for o in outcomes:
                        over = is_over(o.get('label', ''))
                        if over is not None:
                            result[mk][('total_points', line)]['over' if over else 'under'].append(
                                Offer('unibet', o['odd'], o.get('label', '')))

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
    ref_source: str = 'consensus'  # 'pinnacle' | 'oddsapi' | 'consensus'

def no_vig_prob(odds_a: float, odds_b: float) -> tuple[float, float]:
    """Retourne (prob_no_vig_A, prob_no_vig_B) sans marge (marché 2 issues)."""
    p_a, p_b = 1 / odds_a, 1 / odds_b
    total = p_a + p_b
    return p_a / total, p_b / total

def no_vig_three_way(h: float, d: float, a: float) -> tuple[float, float, float]:
    """Retourne (fair_h, fair_d, fair_a) sans marge (marché 1X2 à 3 issues)."""
    ph, pd, pa = 1 / h, 1 / d, 1 / a
    total = ph + pd + pa
    return ph / total, pd / total, pa / total  # probabilities (not odds)

def _market_label(key) -> str:
    market = key[0]
    labels = {
        'match_winner':      'Vainqueur',
        'match_winner_1x2':  '1X2',
        'total_games':       'Total jeux',
        'total_goals':       'Total buts',
        'total_points':      'Total points',
        'total_aces':        'Total aces',
        'total_breaks':      'Total breaks',
        'player_aces':       'Aces joueur',
        'player_breaks':     'Breaks joueur',
    }
    base = labels.get(market, market)
    if len(key) == 3 and key[2] not in ('over', 'under', 'p0', 'p1', 'home', 'away', 'draw'):
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
    ref_odds: dict | None = None,
    min_edge: float = 3.0,
    min_books: int = 2,       # au minimum 2 bookmakers pour calculer le consensus
) -> list[ValueBet]:
    """
    Fusionne les données des bookmakers et retourne les value bets.
    all_books = [winamax_dict, betclic_dict, unibet_dict]
    ref_odds  = {match_key: {market_key: {side: fair_odds, 'source': str}}}
                (depuis ReferenceClient — Pinnacle no-vig de préférence)
    """
    all_match_keys: set = set()
    for bk in all_books:
        all_match_keys.update(bk.keys())

    value_bets: list[ValueBet] = []

    for mk in all_match_keys:
        merged: dict = defaultdict(lambda: defaultdict(list))
        for bk_data in all_books:
            if mk not in bk_data:
                continue
            for group_key, sides in bk_data[mk].items():
                for side, offers in sides.items():
                    merged[group_key][side].extend(offers)

        for group_key, sides in merged.items():
            THREE_WAY = group_key[0] == 'match_winner_1x2'
            line = group_key[1] if len(group_key) > 1 else None

            def best_by_bk(offers: list[Offer]) -> dict[str, float]:
                d: dict[str, float] = {}
                for o in offers:
                    if o.bookmaker not in d or o.odds > d[o.bookmaker]:
                        d[o.bookmaker] = o.odds
                return d

            if THREE_WAY:
                # ── Marché 3-way (1X2 football) ──────────────────────────────
                if not all(s in sides for s in ('p0', 'p1', 'draw')):
                    continue
                bk_h  = best_by_bk(sides['p0'])
                bk_d  = best_by_bk(sides['draw'])
                bk_a2 = best_by_bk(sides['p1'])

                # Référence (Pinnacle)
                ref_h = ref_d = ref_a2 = None
                ref_src = ''
                if ref_odds:
                    rm = ref_odds.get(mk, {}).get(group_key)
                    if rm:
                        ref_h, ref_d, ref_a2 = rm.get('p0'), rm.get('draw'), rm.get('p1')
                        ref_src = rm.get('source', 'pinnacle')

                # Consensus
                common3 = set(bk_h) & set(bk_d) & set(bk_a2)
                fair_h_c = fair_d_c = fair_a2_c = None
                if len(common3) >= min_books:
                    ph_l, pd_l, pa_l = [], [], []
                    for bk in common3:
                        ph, pd, pa = no_vig_three_way(bk_h[bk], bk_d[bk], bk_a2[bk])
                        ph_l.append(ph); pd_l.append(pd); pa_l.append(pa)
                    def _avg_fair(pl): return 1 / (sum(pl) / len(pl))
                    fair_h_c  = _avg_fair(ph_l)
                    fair_d_c  = _avg_fair(pd_l)
                    fair_a2_c = _avg_fair(pa_l)

                if ref_h and ref_d and ref_a2:
                    fair_h, fair_d, fair_a2 = ref_h, ref_d, ref_a2
                    fair_source = ref_src
                elif fair_h_c:
                    fair_h, fair_d, fair_a2 = fair_h_c, fair_d_c, fair_a2_c
                    fair_source = 'consensus'
                else:
                    continue

                for side, all_bk, fair_odds in [
                    ('p0',  bk_h,  fair_h),
                    ('draw',bk_d,  fair_d),
                    ('p1',  bk_a2, fair_a2),
                ]:
                    for bk, odds in all_bk.items():
                        edge = (odds / fair_odds - 1) * 100
                        if edge >= min_edge:
                            side_lbl = 'Nul' if side == 'draw' else _side_label(side, mk)
                            value_bets.append(ValueBet(
                                match=' vs '.join(p.capitalize() for p in mk),
                                market=_market_label(group_key),
                                line=None,
                                side=side_lbl,
                                bookmaker=bk,
                                odds=odds,
                                fair_odds=round(fair_odds, 3),
                                edge_pct=round(edge, 1),
                                all_odds={**{f'{k}(H)': v for k, v in bk_h.items()},
                                          **{f'{k}(D)': v for k, v in bk_d.items()},
                                          **{f'{k}(A)': v for k, v in bk_a2.items()}},
                                ref_source=fair_source,
                            ))

            else:
                # ── Marché 2-way (tennis, basket, over/under) ─────────────────
                if group_key[0] == 'match_winner':
                    side_pair = ('p0', 'p1')
                else:
                    side_pair = ('over', 'under')

                s_a, s_b = side_pair
                if s_a not in sides or s_b not in sides:
                    continue

                bk_a = best_by_bk(sides[s_a])
                bk_b = best_by_bk(sides[s_b])

                # Référence Pinnacle
                ref_fair_a: float | None = None
                ref_fair_b: float | None = None
                ref_src = ''
                if ref_odds:
                    ref_match = ref_odds.get(mk)
                    if ref_match:
                        ref_mkt = ref_match.get(group_key)
                        if ref_mkt:
                            ref_fair_a = ref_mkt.get(s_a)
                            ref_fair_b = ref_mkt.get(s_b)
                            ref_src = ref_mkt.get('source', 'pinnacle')

                # Consensus soft books
                fair_a_cons: float | None = None
                fair_b_cons: float | None = None
                common = set(bk_a) & set(bk_b)
                if len(common) >= min_books:
                    probs_a, probs_b = [], []
                    for bk in common:
                        pa, pb = no_vig_prob(bk_a[bk], bk_b[bk])
                        probs_a.append(pa)
                        probs_b.append(pb)
                    ca = sum(probs_a) / len(probs_a)
                    cb = sum(probs_b) / len(probs_b)
                    fair_a_cons = 1 / ca if ca > 0 else 9999
                    fair_b_cons = 1 / cb if cb > 0 else 9999

                if ref_fair_a is not None and ref_fair_b is not None:
                    fair_a, fair_b = ref_fair_a, ref_fair_b
                    fair_source = ref_src
                elif fair_a_cons is not None:
                    fair_a, fair_b = fair_a_cons, fair_b_cons
                    fair_source = 'consensus'
                else:
                    continue

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
                                ref_source=fair_source,
                            ))

    return sorted(value_bets, key=lambda v: -v.edge_pct)



# ── Chargement dynamique des scrapers ─────────────────────────────────────────

_SCRIPT_DIR = Path(__file__).parent


def _load_scraper(subdir: str, filename: str = 'tennis_odds'):
    """Importe le module <filename> depuis le sous-dossier donné."""
    import importlib.util as _ilu
    subdir_path = _SCRIPT_DIR / subdir
    path_str = str(subdir_path)
    if path_str not in sys.path:
        sys.path.insert(0, path_str)
    spec = _ilu.spec_from_file_location(f'_{subdir}_{filename}', subdir_path / f'{filename}.py')
    mod = _ilu.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def fetch_all_odds(
    prematch_only: bool = True,
    with_reference: bool = True,
    sport: str = 'tennis',
) -> tuple[list, list, list, dict]:
    """Fetch odds from all 3 bookmakers + reference source in parallel threads.

    Returns (winamax_raw, betclic_raw, unibet_raw, ref_odds).
    sport : 'tennis' | 'football' | 'basketball'
    """
    import dataclasses
    import threading as _th

    results: dict = {'winamax': [], 'betclic': [], 'unibet': [], 'reference': {}}

    # ── Winamax ──────────────────────────────────────────────────────────────
    _WM_SPORT_ID  = {'tennis': 5, 'football': 1, 'basketball': 2}
    # (subdir, filename, fn_name, extra_kwargs)
    _WM_SCRAPE_FN = {
        'tennis':     ('winamax', 'tennis_odds',    'scrape_all_tennis_odds',    {}),
        'football':   ('winamax', 'football_odds',  'scrape_all_football_odds',  {}),
        'basketball': ('winamax', 'basketball_odds','scrape_all_basketball_odds',{}),
    }
    _BC_SCRAPE_FN = {
        'tennis':     ('betclic', 'tennis_odds',    'scrape_all_tennis',    {'all_categories': True}),
        'football':   ('betclic', 'football_odds',  'scrape_all_football',  {'all_categories': True}),
        'basketball': ('betclic', 'basketball_odds','scrape_all_basketball',{'all_categories': True}),
    }
    _UB_SCRAPE_FN = {
        'tennis':     ('unibet', 'tennis_odds',    'scrape_tennis_odds',    {}),
        'football':   ('unibet', 'football_odds',  'scrape_football_odds',  {}),
        'basketball': ('unibet', 'basketball_odds','scrape_basketball_odds',{}),
    }

    def _run_winamax():
        try:
            subdir, filename, fn_name, extra = _WM_SCRAPE_FN[sport]
            mod = _load_scraper(subdir, filename)
            sport_id = _WM_SPORT_ID.get(sport, 5)
            with mod.WinamaxClient(sport_id=sport_id) as c:
                fn = getattr(mod, fn_name)
                matches = fn(c, prematch_only=prematch_only, **extra)
            results['winamax'] = [dataclasses.asdict(m) for m in matches]
            print(f'  winamax : {len(matches)} matchs', flush=True)
        except Exception as e:
            print(f'  winamax : ERREUR — {e}', file=sys.stderr, flush=True)

    def _run_betclic():
        try:
            subdir, filename, fn_name, extra = _BC_SCRAPE_FN[sport]
            mod = _load_scraper(subdir, filename)
            with mod.BetclicClient() as c:
                fn = getattr(mod, fn_name)
                matches = fn(c, prematch_only=prematch_only, **extra)
            results['betclic'] = [dataclasses.asdict(m) for m in matches]
            print(f'  betclic : {len(matches)} matchs', flush=True)
        except Exception as e:
            print(f'  betclic : ERREUR — {e}', file=sys.stderr, flush=True)

    def _run_unibet():
        try:
            subdir, filename, fn_name, extra = _UB_SCRAPE_FN[sport]
            mod = _load_scraper(subdir, filename)
            with mod.UnibetClient() as c:
                fn = getattr(mod, fn_name)
                matches = fn(c, prematch_only=prematch_only, **extra)
            results['unibet'] = [dataclasses.asdict(m) for m in matches]
            print(f'  unibet  : {len(matches)} matchs', flush=True)
        except Exception as e:
            print(f'  unibet  : ERREUR — {e}', file=sys.stderr, flush=True)

    def _run_reference():
        if not with_reference:
            return
        try:
            ref_path = str(_SCRIPT_DIR / 'reference')
            if ref_path not in sys.path:
                sys.path.insert(0, ref_path)
            from reference_client import ReferenceClient
            with ReferenceClient(sport=sport) as ref:
                if ref.is_available():
                    results['reference'] = ref.get_reference_odds()
                    print(
                        f'  référence ({ref.source_name()}): {len(results["reference"])} matchs',
                        flush=True,
                    )
                else:
                    print(
                        '  référence: non configurée '
                        '(définissez PINNACLE_USER+PINNACLE_PASS ou ODDS_API_KEY)',
                        flush=True,
                    )
        except Exception as e:
            print(f'  référence: ERREUR — {e}', file=sys.stderr, flush=True)

    threads = [
        _th.Thread(target=_run_winamax,   daemon=True),
        _th.Thread(target=_run_betclic,   daemon=True),
        _th.Thread(target=_run_unibet,    daemon=True),
        _th.Thread(target=_run_reference, daemon=True),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    return results['winamax'], results['betclic'], results['unibet'], results['reference']


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
        src_tag = f'[{vb.ref_source}]' if vb.ref_source != 'consensus' else '[consensus]'
        print(f'    [{vb.market}{line_str}] {vb.side}')
        print(f'      {vb.bookmaker.upper():10s} cote {vb.odds:.2f}  '
              f'(juste: {vb.fair_odds:.2f} {src_tag})  edge: +{vb.edge_pct:.1f}%')


# ── Main ─────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description='Comparateur de cotes tennis')
    parser.add_argument('--winamax', help='JSON Winamax (lecture depuis fichier)')
    parser.add_argument('--betclic', help='JSON Betclic (lecture depuis fichier)')
    parser.add_argument('--unibet',  help='JSON Unibet  (lecture depuis fichier)')
    parser.add_argument('--sport',   default='tennis',
                        choices=['tennis', 'football', 'basketball'],
                        help='Sport à comparer (défaut: tennis)')
    parser.add_argument('--fetch',   action='store_true',
                        help='Récupère les cotes une fois (tous les bookmakers en parallèle)')
    parser.add_argument('--watch',   type=int, metavar='SECONDES', nargs='?', const=60,
                        help='Mode temps réel : rafraîchit toutes les N secondes (défaut: 60)')
    parser.add_argument('--min-edge', type=float, default=3.0,
                        help='Edge minimum en %% (défaut: 3.0)')
    parser.add_argument('--min-books', type=int, default=2,
                        help='Nbre min de bookmakers pour le consensus (défaut: 2)')
    parser.add_argument('--no-reference', action='store_true',
                        help='Désactiver la source de référence no-vig (Pinnacle/Odds API)')
    parser.add_argument('--output', help='Exporter les value bets en JSON')
    args = parser.parse_args()

    with_ref = not args.no_reference

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

                wm_raw, bc_raw, ub_raw, ref_odds = fetch_all_odds(
                    prematch_only=True, with_reference=with_ref, sport=args.sport
                )

                _parse_wm, _parse_bc, _parse_ub = {
                    'tennis':     (parse_winamax,            parse_betclic,            parse_unibet),
                    'football':   (parse_winamax_football,   parse_betclic_football,   parse_unibet_football),
                    'basketball': (parse_winamax_basketball, parse_betclic_basketball, parse_unibet_basketball),
                }[args.sport]

                wm = _parse_wm(wm_raw)
                bc = _parse_bc(bc_raw)
                ub = _parse_ub(ub_raw)

                value_bets = compute_value_bets(
                    [wm, bc, ub], ref_odds, args.min_edge, args.min_books
                )
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

        _parse_wm, _parse_bc, _parse_ub = {
            'tennis':     (parse_winamax,            parse_betclic,            parse_unibet),
            'football':   (parse_winamax_football,   parse_betclic_football,   parse_unibet_football),
            'basketball': (parse_winamax_basketball, parse_betclic_basketball, parse_unibet_basketball),
        }[args.sport]

        wm = load(args.winamax, _parse_wm, 'Winamax')
        bc = load(args.betclic, _parse_bc, 'Betclic')
        ub = load(args.unibet,  _parse_ub, 'Unibet')

        value_bets = compute_value_bets([wm, bc, ub], None, args.min_edge, args.min_books)
        print_results(value_bets, args.min_edge)

        if args.output:
            import dataclasses
            with open(args.output, 'w', encoding='utf-8') as f:
                json.dump([dataclasses.asdict(v) for v in value_bets],
                          f, ensure_ascii=False, indent=2)
            print(f'\nRésultats exportés → {args.output}')


if __name__ == '__main__':
    main()
