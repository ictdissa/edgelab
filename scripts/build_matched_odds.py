#!/usr/bin/env python
from __future__ import annotations

import argparse
from pathlib import Path

import _bootstrap  # noqa: F401

from sharpedge.data.event_resolver import (
    load_external_odds_csv,
    load_polymarket_markets_csv,
    load_provider_events_json,
    resolve_market_event_matches,
    rewrite_odds_for_polymarket,
    write_external_odds_csv,
    write_matches_csv,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Match Odds-API.io events to Polymarket markets and rewrite odds IDs"
    )
    parser.add_argument(
        "--polymarket-csv",
        type=Path,
        default=Path("reports/current_polymarket_sports.csv"),
        help="CSV from scripts/list_current_polymarket_sports.py",
    )
    parser.add_argument(
        "--odds-events",
        type=Path,
        default=Path("data/raw/odds_api_io_events.json"),
        help="Raw Odds-API.io events JSON",
    )
    parser.add_argument(
        "--odds-payloads",
        type=Path,
        default=Path("data/raw/odds_api_io_odds.json"),
        help="Raw Odds-API.io odds JSON; used as fallback event metadata",
    )
    parser.add_argument(
        "--odds-normalized",
        type=Path,
        default=Path("data/raw/odds_api_io_normalized.csv"),
        help="Normalized provider odds CSV from collect_odds_api_io_snapshot.py",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("data/raw/odds_api_io_matched_polymarket.csv"),
        help="Output odds CSV consumable by run_edge_scan.py --odds-file",
    )
    parser.add_argument(
        "--report",
        type=Path,
        default=Path("reports/odds_api_io_event_matches.csv"),
        help="Auditable match report",
    )
    parser.add_argument("--min-confidence", type=float, default=0.72)
    parser.add_argument("--min-gap", type=float, default=0.04)
    parser.add_argument(
        "--include-review",
        action="store_true",
        help="Include review-grade matches in output odds; default writes matched only",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    markets = load_polymarket_markets_csv(args.polymarket_csv)
    external_odds = load_external_odds_csv(args.odds_normalized)
    provider_events = load_provider_events_json(
        args.odds_events,
        args.odds_payloads,
        external_odds,
    )
    matches = resolve_market_event_matches(
        markets=markets,
        provider_events=provider_events,
        external_odds=external_odds,
        min_confidence=args.min_confidence,
        min_gap=args.min_gap,
    )
    matched_odds = rewrite_odds_for_polymarket(
        markets=markets,
        external_odds=external_odds,
        matches=matches,
        include_review=args.include_review,
    )
    write_matches_csv(args.report, matches)
    write_external_odds_csv(args.output, matched_odds)

    matched_count = sum(1 for match in matches if match.decision == "matched")
    review_count = sum(1 for match in matches if match.decision == "review")
    rejected_count = sum(1 for match in matches if match.decision == "rejected")
    print(
        "Matched external odds to Polymarket: "
        f"markets={len(markets)}, provider_events={len(provider_events)}, "
        f"matched={matched_count}, review={review_count}, rejected={rejected_count}, "
        f"odds_rows={len(matched_odds)}"
    )
    print(f"Wrote {args.output}")
    print(f"Wrote {args.report}")
    if review_count:
        print("Review-grade matches were excluded from odds output unless --include-review is set.")


if __name__ == "__main__":
    main()
