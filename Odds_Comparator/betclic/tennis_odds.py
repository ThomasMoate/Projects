"""
Scrape tennis odds from Betclic France via gRPC-web.

Note: Betclic does NOT offer aces or breaks markets for tennis.
Markets available: match winner, set results, total games, handicap, points.

Usage:
    python tennis_odds.py                            # all matches, Le Top category (~7 markets)
    python tennis_odds.py --output odds.json         # write results to JSON
    python tennis_odds.py --prematch-only            # skip live matches
    python tennis_odds.py --all-categories           # all 5 categories (~34 unique markets)
    python tennis_odds.py --category ca_ten_gms      # specific category (Jeux)
    python tennis_odds.py --max-seconds 15           # higher safety cap per stream (default: 8)
    python tennis_odds.py --delay 0.5                # delay between matches (default: 0.0s)
"""
import argparse
import json
import sys
import time
from dataclasses import dataclass, field, asdict
from typing import Optional

from betclic_client import BetclicClient, TENNIS_CATEGORIES


@dataclass
class Outcome:
    name: str
    odds: float


@dataclass
class Market:
    name: str
    outcomes: list[Outcome] = field(default_factory=list)


@dataclass
class MatchOdds:
    match_id: int
    title: str
    start: str
    is_live: bool
    competition_id: int
    competition_name: str
    open_market_count: int
    markets: list[Market] = field(default_factory=list)


def scrape_all_tennis(
    client: BetclicClient,
    prematch_only: bool = False,
    max_seconds: float = 8.0,
    all_categories: bool = False,
    category_id: str | None = None,
    delay: float = 0.0,
) -> list[MatchOdds]:
    """
    1. Fetch all tennis matches.
    2. For each match fetch markets from the requested category/categories.
    """
    print('Fetching tennis match list…', file=sys.stderr)
    raw_matches = client.get_tennis_matches()
    print(f'Found {len(raw_matches)} tennis matches.', file=sys.stderr)

    results: list[MatchOdds] = []

    for i, m in enumerate(raw_matches, 1):
        if prematch_only and m['is_live']:
            continue

        match_id = m['match_id']
        title = m['title']
        status = 'LIVE' if m['is_live'] else 'PREMATCH'

        print(
            f'[{i}/{len(raw_matches)}] {title} ({status}, {m["open_market_count"]} markets)…',
            file=sys.stderr
        )

        try:
            if all_categories:
                markets_raw = client.get_all_match_markets(match_id, max_seconds=max_seconds)
            else:
                markets_raw = client.get_match_markets(
                    match_id, max_seconds=max_seconds, category_id=category_id
                )
        except Exception as e:
            print(f'  ERROR: {e}', file=sys.stderr)
            markets_raw = []

        markets = [
            Market(
                name=mkt['name'],
                outcomes=[Outcome(**oc) for oc in mkt['selections']],
            )
            for mkt in markets_raw
        ]

        results.append(MatchOdds(
            match_id=match_id,
            title=title,
            start=m['start'],
            is_live=m['is_live'],
            competition_id=m['competition_id'],
            competition_name=m['competition_name'],
            open_market_count=m['open_market_count'],
            markets=markets,
        ))

        print(f'  → {len(markets)} markets collected.', file=sys.stderr)
        if delay:
            time.sleep(delay)

    return results


def print_summary(matches: list[MatchOdds]):
    for m in matches:
        print(f'\n{"="*70}')
        print(f'  {m.title}  (id={m.match_id}, {"LIVE" if m.is_live else "PREMATCH"})')
        print(f'  {m.competition_name} | open={m.open_market_count} | fetched={len(m.markets)}')
        print(f'{"="*70}')
        for mkt in m.markets:
            print(f'    • {mkt.name}')
            for oc in mkt.outcomes:
                print(f'        {oc.name}: {oc.odds}')


def main():
    parser = argparse.ArgumentParser(description='Scrape Betclic France tennis odds')
    parser.add_argument('--output', '-o', help='Write results to JSON file')
    parser.add_argument('--prematch-only', action='store_true', help='Skip live matches')
    parser.add_argument('--max-seconds', type=float, default=8.0,
                        help='Safety cap (seconds) per stream request (default: 8)')
    parser.add_argument('--delay', type=float, default=0.0,
                        help='Delay between match requests (default: 0.0s)')
    parser.add_argument('--all-categories', action='store_true',
                        help='Fetch all 5 market categories in parallel (~34 markets)')
    parser.add_argument('--category', default=None,
                        choices=list(TENNIS_CATEGORIES.keys()),
                        help='Fetch a specific category (default: Le Top / ca_ten_top)')
    args = parser.parse_args()

    with BetclicClient() as client:
        matches = scrape_all_tennis(
            client,
            prematch_only=args.prematch_only,
            max_seconds=args.max_seconds,
            all_categories=args.all_categories,
            category_id=args.category,
            delay=args.delay,
        )

    print(f'\nScraped {len(matches)} matches total.', file=sys.stderr)

    if args.output:
        with open(args.output, 'w', encoding='utf-8') as f:
            json.dump([asdict(m) for m in matches], f, ensure_ascii=False, indent=2)
        print(f'Results written to {args.output}', file=sys.stderr)

    print_summary(matches)


if __name__ == '__main__':
    main()
