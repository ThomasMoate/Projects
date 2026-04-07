"""
Scrape all tennis odds from Winamax, including specialized markets
(aces, breaks, sets, etc.).

Usage:
    python tennis_odds.py                  # print summary to stdout
    python tennis_odds.py --output odds.json   # also write to JSON file
    python tennis_odds.py --match 70456558     # single match only
"""

import argparse
import json
import sys
import time
from dataclasses import dataclass, field, asdict
from typing import Optional

from winamax_client import WinamaxClient

# ── Bet filter IDs that flag specialized / value markets ──────────────────────
SPECIALIZED_FILTER_IDS = {
    547: 'Breaks',
    548: 'Aces',
    664: 'Total aces (paliers)',
    665: 'Aces joueur 1 (paliers)',
    666: 'Aces joueur 2 (paliers)',
}

# ── Data structures ───────────────────────────────────────────────────────────

@dataclass
class Outcome:
    outcome_id: int
    label: str
    odd: float


@dataclass
class Bet:
    bet_id: int
    category: str
    name: str
    outcomes: list[Outcome] = field(default_factory=list)


@dataclass
class MatchOdds:
    match_id: int
    title: str
    tournament_id: int
    category_id: int
    status: str
    match_start: int           # epoch ms
    specialized_filters: list[str] = field(default_factory=list)
    bets: list[Bet] = field(default_factory=list)


# ── Parsing helpers ───────────────────────────────────────────────────────────

def _parse_match_data(data: dict) -> Optional[MatchOdds]:
    """
    Parse a full match-route response into a MatchOdds object.
    data keys: matches, bets, outcomes, odds
    """
    matches_map = data.get('matches', {})
    bets_map = data.get('bets', {})
    outcomes_map = data.get('outcomes', {})
    odds_map = data.get('odds', {})

    if not matches_map:
        return None

    # There's exactly one match in a match-route response
    match = next(iter(matches_map.values()))
    match_id = match['matchId']

    filters_present = match.get('filters', [])
    specialized = [
        SPECIALIZED_FILTER_IDS[f]
        for f in filters_present
        if f in SPECIALIZED_FILTER_IDS
    ]

    result = MatchOdds(
        match_id=match_id,
        title=match.get('title', ''),
        tournament_id=match.get('tournamentId', 0),
        category_id=match.get('categoryId', 0),
        status=match.get('status', ''),
        match_start=match.get('matchStart', 0),
        specialized_filters=specialized,
    )

    for bet_id_str, bet in bets_map.items():
        bet_id = int(bet_id_str)
        category = bet.get('betTypeCategory', '')
        name = bet.get('betTypeName', '') or bet.get('betTitle', '')

        parsed_outcomes = []
        for oc_id in bet.get('outcomes', []):
            oc = outcomes_map.get(str(oc_id), {})
            # odds map: {outcome_id_str: float}
            odd_val = odds_map.get(str(oc_id))
            if odd_val is None:
                continue
            parsed_outcomes.append(Outcome(
                outcome_id=oc_id,
                label=oc.get('label', ''),
                odd=round(float(odd_val), 3),
            ))

        if parsed_outcomes:
            result.bets.append(Bet(
                bet_id=bet_id,
                category=category,
                name=name,
                outcomes=parsed_outcomes,
            ))

    return result


# ── Core scraper ──────────────────────────────────────────────────────────────

def scrape_all_tennis_odds(
    client: WinamaxClient,
    only_specialized: bool = False,
    delay: float = 0.3,
) -> list[MatchOdds]:
    """
    1. Fetch all tennis matches via sport:5
    2. For each match, fetch full detail via match:{id}
    Returns a list of MatchOdds (one per match).
    """
    print('Fetching tennis match list…', file=sys.stderr)
    overview = client.get_route('sport:5')

    matches_raw = overview.get('matches', {})
    print(f'Found {len(matches_raw)} tennis matches.', file=sys.stderr)

    if not matches_raw:
        return []

    results: list[MatchOdds] = []

    for i, (mid_str, m) in enumerate(matches_raw.items(), 1):
        match_id = m['matchId']
        title = m.get('title', str(match_id))
        status = m.get('status', '')
        filters = m.get('filters', [])

        has_specialized = any(f in SPECIALIZED_FILTER_IDS for f in filters)

        if only_specialized and not has_specialized:
            continue

        print(
            f'[{i}/{len(matches_raw)}] {title} (id={match_id}, '
            f'status={status}, moreBets={m.get("moreBets", 0)})…',
            file=sys.stderr,
        )

        try:
            detail = client.get_route(f'match:{match_id}')
            parsed = _parse_match_data(detail)
            if parsed:
                results.append(parsed)
        except Exception as exc:
            print(f'  ERROR fetching match {match_id}: {exc}', file=sys.stderr)

        time.sleep(delay)

    return results


def scrape_single_match(client: WinamaxClient, match_id: int) -> Optional[MatchOdds]:
    detail = client.get_route(f'match:{match_id}')
    return _parse_match_data(detail)


# ── Output helpers ────────────────────────────────────────────────────────────

def print_summary(matches: list[MatchOdds]):
    for m in matches:
        spec = ', '.join(m.specialized_filters) if m.specialized_filters else '—'
        print(f'\n{"="*70}')
        print(f'  {m.title}  (id={m.match_id}, status={m.status})')
        print(f'  Specialized: {spec}  |  Total bet types: {len(m.bets)}')
        print(f'{"="*70}')

        # Group bets by category for readability
        by_cat: dict[str, list[Bet]] = {}
        for b in m.bets:
            by_cat.setdefault(b.category, []).append(b)

        for cat, bets in sorted(by_cat.items()):
            print(f'\n  [{cat}]')
            for b in bets:
                print(f'    • {b.name}')
                for oc in b.outcomes:
                    print(f'        {oc.label}: {oc.odd}')


# ── Entry point ───────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description='Scrape Winamax tennis odds')
    parser.add_argument('--output', '-o', help='Write results to JSON file')
    parser.add_argument('--match', '-m', type=int, help='Scrape a single match ID')
    parser.add_argument(
        '--specialized-only', action='store_true',
        help='Only fetch matches with aces/breaks/specialized markets'
    )
    parser.add_argument(
        '--delay', type=float, default=0.3,
        help='Delay between match requests in seconds (default: 0.3)'
    )
    args = parser.parse_args()

    with WinamaxClient() as client:
        if args.match:
            print(f'Fetching single match {args.match}…', file=sys.stderr)
            result = scrape_single_match(client, args.match)
            matches = [result] if result else []
        else:
            matches = scrape_all_tennis_odds(
                client,
                only_specialized=args.specialized_only,
                delay=args.delay,
            )

    print(f'\nScraped {len(matches)} matches total.', file=sys.stderr)

    if args.output:
        with open(args.output, 'w', encoding='utf-8') as f:
            json.dump(
                [asdict(m) for m in matches],
                f,
                ensure_ascii=False,
                indent=2,
            )
        print(f'Results written to {args.output}', file=sys.stderr)

    print_summary(matches)


if __name__ == '__main__':
    main()
