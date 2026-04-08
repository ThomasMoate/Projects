"""
Scrape football odds from Unibet France.

Sport code : FOOT
Marchés    : Résultat 1X2, Total buts, Handicap asiatique, Mi-Temps.

Usage:
    python football_odds.py
    python football_odds.py --prematch-only
    python football_odds.py --output foo.json
"""
import argparse
import json
import sys
import time
from dataclasses import dataclass, field, asdict
from typing import Optional

from unibet_client import UnibetClient

SPORT_CODE = 'FOOT'

# Market display group IDs for football (may need adjustment after testing)
MARKET_GROUPS: dict[int, str] = {
    1:   'Résultat',          # 1X2
    2:   'Handicap',          # Asian handicap
    5:   'Buts',              # Total goals
    60:  'Buts (O/U)',        # alternate total goals group
    700: 'Les deux équipes marquent',
    4:   'Score exact',
    8:   'Double chance',
    9:   'Mi-Temps',
    10:  'Mi-Temps/Match',
}


@dataclass
class Outcome:
    outcome_id: str
    label: str
    odd: float


@dataclass
class Market:
    market_id: str
    name: str
    category: str
    period: str
    outcomes: list[Outcome] = field(default_factory=list)


@dataclass
class MatchOdds:
    event_id: str
    title: str
    league: str
    status: str
    start_time: Optional[str]
    markets: list[Market] = field(default_factory=list)


def _parse_price(price_str) -> float:
    try:
        return round(float(str(price_str).replace(',', '.')), 3)
    except (ValueError, TypeError):
        return 0.0


def parse_event(event_id: str, raw: dict) -> MatchOdds:
    event    = raw['event']
    markets  = raw['markets']
    outcomes = raw['outcomes']

    result = MatchOdds(
        event_id=event_id,
        title=event.get('desc', ''),
        league='',
        status=event.get('status', ''),
        start_time=event.get('start'),
    )

    for mid, mkt in markets.items():
        group_ids = mkt.get('marketTypeDisplayGroupIds', [])
        category  = next(
            (MARKET_GROUPS[g] for g in group_ids if g in MARKET_GROUPS),
            'Autre'
        )
        market_outcomes = [
            Outcome(
                outcome_id=oc_id,
                label=oc.get('desc', ''),
                odd=_parse_price(oc.get('price', '0')),
            )
            for oc_id, oc in outcomes.items()
            if oc.get('parent') == mid and _parse_price(oc.get('price', '0')) > 1.0
        ]
        if market_outcomes:
            result.markets.append(Market(
                market_id=mid,
                name=mkt.get('desc', ''),
                category=category,
                period=mkt.get('period', ''),
                outcomes=market_outcomes,
            ))

    return result


def scrape_football_odds(
    client: UnibetClient,
    league_ids: Optional[list[int]] = None,
    prematch_only: bool = False,
    delay: float = 0.25,
) -> list[MatchOdds]:
    if league_ids is None:
        leagues = client.get_sport_leagues(SPORT_CODE)
        print(f'Found {len(leagues)} active football leagues.', file=sys.stderr)
    else:
        leagues = [{'id': lid, 'name': str(lid), 'category': ''} for lid in league_ids]

    results: list[MatchOdds] = []

    for league in leagues:
        lid   = league['id']
        lname = league['name']
        print(f'  League: {lname} (id={lid})', file=sys.stderr)

        try:
            event_ids = client.get_events_for_league(lid)
        except Exception as e:
            print(f'    ERROR fetching events: {e}', file=sys.stderr)
            continue

        for event_id in event_ids:
            try:
                raw   = client.get_event_offers(event_id)
                match = parse_event(event_id, raw)
                match.league = lname

                if prematch_only and match.status not in ('OPEN', ''):
                    continue

                results.append(match)
                print(
                    f'    {match.title} | {len(match.markets)} markets',
                    file=sys.stderr
                )
            except Exception as e:
                print(f'    ERROR {event_id}: {e}', file=sys.stderr)
            time.sleep(delay)

    return results


def print_summary(matches: list[MatchOdds]):
    for m in matches:
        print(f'\n{"="*70}')
        print(f'  {m.title}  (id={m.event_id}, status={m.status})')
        print(f'  League: {m.league} | Markets: {len(m.markets)}')
        print(f'{"="*70}')
        by_cat: dict[str, list[Market]] = {}
        for mkt in m.markets:
            by_cat.setdefault(mkt.category, []).append(mkt)
        for cat, mkts in sorted(by_cat.items()):
            print(f'\n  [{cat}]')
            for mkt in mkts:
                print(f'    • {mkt.name}')
                for oc in mkt.outcomes:
                    print(f'        {oc.label}: {oc.odd}')


def main():
    parser = argparse.ArgumentParser(description='Scrape Unibet France football odds')
    parser.add_argument('--output', '-o', help='Write JSON output to file')
    parser.add_argument('--league', '-l', type=int, action='append',
                        help='Filter to specific league ID (can repeat)')
    parser.add_argument('--prematch-only', action='store_true',
                        help='Skip live matches')
    parser.add_argument('--delay', type=float, default=0.25)
    args = parser.parse_args()

    with UnibetClient() as client:
        matches = scrape_football_odds(
            client,
            league_ids=args.league,
            prematch_only=args.prematch_only,
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
