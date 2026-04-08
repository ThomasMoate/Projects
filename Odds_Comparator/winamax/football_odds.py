"""
Scrape football (soccer) odds from Winamax.

Sport ID : 1 (football)
Marchés  : Résultat 1X2, Total buts (over/under), Handicap asiatique

Usage:
    python football_odds.py                   # tous les matchs
    python football_odds.py --prematch-only   # prématch uniquement
    python football_odds.py --output goals.json
"""
import argparse
import json
import sys
import time
from dataclasses import dataclass, field, asdict
from typing import Optional

from winamax_client import WinamaxClient

SPORT_ID = 1
SPORT_ROUTE = f'sport:{SPORT_ID}'


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
    match_start: int
    bets: list[Bet] = field(default_factory=list)


def _parse_match_data(data: dict) -> Optional[MatchOdds]:
    matches_map  = data.get('matches', {})
    bets_map     = data.get('bets', {})
    outcomes_map = data.get('outcomes', {})
    odds_map     = data.get('odds', {})
    if not matches_map:
        return None
    match    = next(iter(matches_map.values()))
    match_id = match['matchId']
    result   = MatchOdds(
        match_id=match_id,
        title=match.get('title', ''),
        tournament_id=match.get('tournamentId', 0),
        category_id=match.get('categoryId', 0),
        status=match.get('status', ''),
        match_start=match.get('matchStart', 0),
    )
    for bid_str, bet in bets_map.items():
        bet_id   = int(bid_str)
        category = bet.get('betTypeCategory', '')
        name     = bet.get('betTypeName', '') or bet.get('betTitle', '')
        parsed_outcomes = []
        for oc_id in bet.get('outcomes', []):
            oc      = outcomes_map.get(str(oc_id), {})
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


def scrape_all_football_odds(
    client: WinamaxClient,
    prematch_only: bool = False,
    delay: float = 0.3,
) -> list[MatchOdds]:
    print('Fetching football match list…', file=sys.stderr)
    overview    = client.get_route(SPORT_ROUTE)
    matches_raw = overview.get('matches', {})
    print(f'Found {len(matches_raw)} football matches.', file=sys.stderr)
    if not matches_raw:
        return []
    results: list[MatchOdds] = []
    for i, (mid_str, m) in enumerate(matches_raw.items(), 1):
        match_id = m['matchId']
        title    = m.get('title', str(match_id))
        status   = m.get('status', '')
        if prematch_only and status != 'PREMATCH':
            continue
        print(f'[{i}/{len(matches_raw)}] {title} (status={status})…', file=sys.stderr)
        try:
            detail = client.get_route(f'match:{match_id}')
            parsed = _parse_match_data(detail)
            if parsed:
                results.append(parsed)
        except Exception as exc:
            print(f'  ERROR: {exc}', file=sys.stderr)
        time.sleep(delay)
    return results


def print_summary(matches: list[MatchOdds]):
    for m in matches:
        print(f'\n{"="*70}')
        print(f'  {m.title}  (id={m.match_id}, status={m.status})')
        print(f'{"="*70}')
        for b in m.bets:
            print(f'    • {b.name}  [{b.category}]')
            for oc in b.outcomes:
                print(f'        {oc.label}: {oc.odd}')


def main():
    parser = argparse.ArgumentParser(description='Scrape Winamax football odds')
    parser.add_argument('--output', '-o', help='Write results to JSON file')
    parser.add_argument('--prematch-only', action='store_true',
                        help='Skip live matches (PREMATCH status only)')
    parser.add_argument('--delay', type=float, default=0.3,
                        help='Delay between match requests (default: 0.3s)')
    args = parser.parse_args()

    with WinamaxClient(sport_id=SPORT_ID) as client:
        matches = scrape_all_football_odds(
            client,
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
