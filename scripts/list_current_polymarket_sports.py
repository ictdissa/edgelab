#!/usr/bin/env python
from __future__ import annotations

import argparse
import csv
import sys
from pathlib import Path

import _bootstrap  # noqa: F401

from sharpedge.config.settings import Settings
from sharpedge.data.polymarket_collector import PolymarketCollector


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="List active Polymarket sports outcomes for manual odds mapping"
    )
    parser.add_argument("--mock", action="store_true", help="Use bundled sample markets")
    parser.add_argument("--limit", type=int, default=250, help="Gamma markets limit")
    parser.add_argument("--output", type=Path, help="Optional CSV output path")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    collector = PolymarketCollector(Settings(), mock=args.mock)
    markets = collector.fetch_active_sports_markets(limit=args.limit)
    rows = []
    for market in markets:
        for outcome, token_id in zip(market.outcomes, market.token_ids, strict=False):
            rows.append(
                {
                    "event_id": market.event_id,
                    "market_id": market.market_id,
                    "sport": market.sport,
                    "league": market.league or "",
                    "title": market.title,
                    "outcome": outcome,
                    "token_id": token_id,
                    "start_time": market.start_time.isoformat() if market.start_time else "",
                    "status": market.status,
                }
            )

    fieldnames = [
        "event_id",
        "market_id",
        "sport",
        "league",
        "title",
        "outcome",
        "token_id",
        "start_time",
        "status",
    ]
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        with args.output.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(rows)
        print(f"Wrote {len(rows)} outcomes to {args.output}")
    else:
        writer = csv.DictWriter(sys.stdout, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


if __name__ == "__main__":
    main()
