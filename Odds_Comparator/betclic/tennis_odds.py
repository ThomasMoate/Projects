"""
Scrape tennis odds from Betclic France via gRPC-web.

Note: Betclic does NOT offer aces or breaks markets for tennis.
Markets available: match winner, set results, total games, handicap, points.

Usage:
    python tennis_odds.py                        # all matches, Le Top category (~7 markets)
    python tennis_odds.py --output odds.json     # write to JSON
    python tennis_odds.py --prematch-only        # skip live matches
    python tennis_odds.py --all-categories       # all 5 categories (~34 unique markets)
    python tennis_odds.py --category ca_ten_gms  # specific category (Jeux)
"""
import argparse
import json
import sys
import time
import threading
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
    read_time: float = 5.0,
    all_categories: bool = False,
    category_id: str | None = None,
    workers: int = 5,
) -> list[MatchOdds]:
    """
    1. Fetch all tennis matches.
    2. Fetch markets in parallel (workers threads).
    """
    print('Fetching tennis match list…', file=sys.stderr)
    raw_matches = client.get_tennis_matches()
    print(f'Found {len(raw_matches)} tennis matches.', file=sys.stderr)

    candidates = [m for m in raw_matches if not (prematch_only and m['is_live'])]
    results: list[MatchOdds | None] = [None] * len(candidates)
    lock = threading.Lock()

    def _fetch(idx: int, m: dict):
        match_id = m['match_id']
        try:
            if all_categories:
                markets_raw = client.get_all_match_markets(match_id, read_seconds=read_time)
            else:
                markets_raw = client.get_match_markets(
                    match_id, read_seconds=read_time, category_id=category_id
                )
        except Exception as e:
            with lock:
                print(f'  ERROR {m["title"]}: {e}', file=sys.stderr)
            markets_raw = []

        markets = [
            Market(name=mkt['name'],
                   outcomes=[Outcome(**oc) for oc in mkt['selections']])
            for mkt in markets_raw
        ]
        results[idx] = MatchOdds(
            match_id=match_id,
            title=m['title'],
            start=m['start'],
            is_live=m['is_live'],
            competition_id=m['competition_id'],
            competition_name=m['competition_name'],
            open_market_count=m['open_market_count'],
            markets=markets,
        )
        with lock:
            status = 'LIVE' if m['is_live'] else 'PREMATCH'
            print(f'  [{idx+1}/{len(candidates)}] {m["title"]} '
                  f'({status}) → {len(markets)} markets', file=sys.stderr)

    # Parallélisation par batch de `workers`
    for batch_start in range(0, len(candidates), workers):
        batch = candidates[batch_start:batch_start + workers]
        threads = [
            threading.Thread(target=_fetch, args=(batch_start + i, m), daemon=True)
            for i, m in enumerate(batch)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=read_time + 10)

    return [r for r in results if r is not None]


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
    parser.add_argument('--read-time', type=float, default=5.0,
                        help='Seconds to read per match stream (default: 5)')
    parser.add_argument('--all-categories', action='store_true',
                        help='Fetch all 5 market categories in parallel (~34 markets)')
    parser.add_argument('--category', default=None,
                        choices=list(TENNIS_CATEGORIES.keys()),
                        help='Fetch a specific category (default: Le Top / ca_ten_top)')
    parser.add_argument('--workers', type=int, default=5,
                        help='Threads parallèles pour les requêtes marché (défaut: 5)')
    args = parser.parse_args()

    with BetclicClient() as client:
        matches = scrape_all_tennis(
            client,
            prematch_only=args.prematch_only,
            read_time=args.read_time,
            all_categories=args.all_categories,
            category_id=args.category,
            workers=args.workers,
        )

    print(f'\nScraped {len(matches)} matches total.', file=sys.stderr)

    if args.output:
        with open(args.output, 'w', encoding='utf-8') as f:
            json.dump([asdict(m) for m in matches], f, ensure_ascii=False, indent=2)
        print(f'Results written to {args.output}', file=sys.stderr)

    print_summary(matches)


if __name__ == '__main__':
    main()
