#!/usr/bin/env python
from __future__ import annotations

import argparse
import csv
from datetime import UTC, datetime
from pathlib import Path

import _bootstrap  # noqa: F401


# Public World Cup outright odds from Covers, updated May 8, 2026 and listed as
# odds as of 5-6 on the page. These are not a true sharp feed. They are useful
# only as a temporary public benchmark for validating the research pipeline.
SOURCE = "CoversPublicWorldCupOdds_source_asof_2026-05-06"
ODDS_SOURCE_URL = "https://www.covers.com/world-cup/odds"

TEAM_DECIMAL_ODDS = {
    "Spain": 5.50,
    "France": 6.00,
    "England": 7.00,
    "Brazil": 9.00,
    "Argentina": 9.00,
    "Portugal": 12.00,
    "Germany": 13.00,
    "Netherlands": 21.00,
    "Norway": 26.00,
    "Belgium": 34.00,
    "Colombia": 34.00,
    "Japan": 51.00,
    "Morocco": 51.00,
    "USA": 67.00,
    "Uruguay": 67.00,
    "Switzerland": 81.00,
    "Mexico": 81.00,
    "Croatia": 81.00,
    "Turkiye": 81.00,
    "Canada": 151.00,
    "Ecuador": 101.00,
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Build a temporary normalized odds file for current World Cup Polymarket markets"
    )
    parser.add_argument(
        "--polymarket-csv",
        type=Path,
        default=Path("reports/current_polymarket_sports.csv"),
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("data/raw/current_external_odds.csv"),
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    timestamp = datetime.now(UTC).replace(microsecond=0).isoformat()
    with args.polymarket_csv.open(newline="", encoding="utf-8") as handle:
        markets = list(csv.DictReader(handle))

    event_by_country: dict[str, str] = {}
    for row in markets:
        if row["outcome"] != "Yes":
            continue
        title = row["title"]
        if not title.startswith("Will ") or not title.endswith(" win the 2026 FIFA World Cup?"):
            continue
        country = title.removeprefix("Will ").removesuffix(" win the 2026 FIFA World Cup?")
        event_by_country[country] = row["event_id"]

    rows: list[dict[str, str | float]] = []
    missing: list[str] = []
    for country, yes_decimal in TEAM_DECIMAL_ODDS.items():
        event_id = event_by_country.get(country)
        if not event_id:
            missing.append(country)
            continue
        yes_probability = 1.0 / yes_decimal
        no_decimal = 1.0 / (1.0 - yes_probability)
        rows.append(
            {
                "event_id": event_id,
                "sport": "Soccer",
                "book": SOURCE,
                "market_type": "moneyline",
                "outcome": "Yes",
                "decimal_odds": f"{yes_decimal:.6f}",
                "timestamp": timestamp,
            }
        )
        rows.append(
            {
                "event_id": event_id,
                "sport": "Soccer",
                "book": SOURCE,
                "market_type": "moneyline",
                "outcome": "No",
                "decimal_odds": f"{no_decimal:.6f}",
                "timestamp": timestamp,
            }
        )

    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=[
                "event_id",
                "sport",
                "book",
                "market_type",
                "outcome",
                "decimal_odds",
                "timestamp",
            ],
        )
        writer.writeheader()
        writer.writerows(rows)

    print(f"Wrote {len(rows)} rows to {args.output}")
    print(f"Source: {ODDS_SOURCE_URL}")
    if missing:
        print("Missing Polymarket events for: " + ", ".join(missing))


if __name__ == "__main__":
    main()
