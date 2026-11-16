#!/usr/bin/env python
from __future__ import annotations

import argparse
import csv
import json
import math
from pathlib import Path
from typing import Any

import _bootstrap  # noqa: F401

from sharpedge.config.settings import Settings
from sharpedge.data.odds_collector import OddsAPIIOProvider
from sharpedge.data.rate_limiter import RollingWindowRateLimiter
from sharpedge.data.schemas import ExternalOdds
from sharpedge.data.storage import Storage


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Collect rate-limited Odds-API.io events and odds snapshots"
    )
    parser.add_argument("--sport", default="football", help="Odds-API.io sport slug")
    parser.add_argument("--league", help="Optional league slug")
    parser.add_argument("--status", default="pending,live", help="Event statuses")
    parser.add_argument("--live", action="store_true", help="Use /events/live")
    parser.add_argument("--limit", type=int, default=20, help="Maximum events to fetch odds for")
    parser.add_argument(
        "--bookmakers",
        help="Comma-separated bookmaker names; defaults to ODDS_API_IO_BOOKMAKERS",
    )
    parser.add_argument(
        "--bookmaker-filter",
        help="Optional single bookmaker filter for /events to reduce event payloads",
    )
    parser.add_argument(
        "--max-requests-per-hour",
        type=int,
        help="Local cap, hard-limited to <=100; defaults to settings",
    )
    parser.add_argument(
        "--state-path",
        type=Path,
        help="Persistent local rate-limit state path",
    )
    parser.add_argument(
        "--events-output",
        type=Path,
        default=Path("data/raw/odds_api_io_events.json"),
    )
    parser.add_argument(
        "--odds-output",
        type=Path,
        default=Path("data/raw/odds_api_io_odds.json"),
    )
    parser.add_argument(
        "--normalized-output",
        type=Path,
        default=Path("data/raw/odds_api_io_normalized.csv"),
    )
    parser.add_argument("--persist", action="store_true", help="Insert normalized odds into DB")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show planned request budget without calling the API",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    if args.limit < 1:
        raise SystemExit("--limit must be >= 1")
    settings = Settings()
    max_requests = args.max_requests_per_hour or settings.odds_api_io_max_requests_per_hour
    if max_requests > 100:
        raise SystemExit("--max-requests-per-hour must be <= 100")

    limiter = RollingWindowRateLimiter(
        state_path=args.state_path or settings.odds_api_io_rate_limit_state,
        max_requests=max_requests,
    )
    planned_max_requests = 1 + math.ceil(args.limit / 10)
    status = limiter.status()
    if args.dry_run:
        print(
            "Odds-API.io dry run: "
            f"limit={args.limit}, planned_max_requests={planned_max_requests}, "
            f"remaining_this_hour={status.remaining}, local_cap={max_requests}"
        )
        return
    if not settings.odds_api_io_key:
        raise SystemExit(
            "ODDS_API_IO_KEY is required. Put it in your ignored .env or export it locally."
        )
    if status.remaining < planned_max_requests:
        minutes = status.retry_after_seconds / 60.0
        raise SystemExit(
            "Not enough local Odds-API.io request capacity for this run: "
            f"remaining={status.remaining}, planned_max={planned_max_requests}, "
            f"retry_after={minutes:.1f}m"
        )

    provider = OddsAPIIOProvider(
        api_key=settings.odds_api_io_key,
        base_url=settings.odds_api_io_base,
        bookmakers=args.bookmakers or settings.odds_api_io_bookmakers,
        rate_limiter=limiter,
    )
    events = provider.fetch_events(
        sport=args.sport,
        league=args.league,
        status=args.status,
        limit=args.limit,
        bookmaker=args.bookmaker_filter,
        live=args.live,
    )[: args.limit]
    event_ids = [event.get("id") for event in events if event.get("id") is not None]
    odds_payloads = provider.fetch_odds_payloads(
        event_ids,
        bookmakers=args.bookmakers or settings.odds_api_io_bookmakers,
    )
    normalized = provider.normalize_odds_payloads(odds_payloads)

    _write_json(args.events_output, events)
    _write_json(args.odds_output, odds_payloads)
    _write_normalized_csv(args.normalized_output, normalized)
    if args.persist:
        storage = Storage(settings.database_url)
        try:
            storage.insert_external_odds(normalized)
        finally:
            storage.close()
    remaining = limiter.status().remaining
    print(
        "Collected Odds-API.io snapshot: "
        f"events={len(events)}, odds_payloads={len(odds_payloads)}, "
        f"normalized_rows={len(normalized)}, remaining_this_hour={remaining}"
    )
    print(f"Wrote {args.events_output}")
    print(f"Wrote {args.odds_output}")
    print(f"Wrote {args.normalized_output}")


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")


def _write_normalized_csv(path: Path, rows: list[ExternalOdds]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    columns = [
        "event_id",
        "sport",
        "book",
        "market_type",
        "outcome",
        "decimal_odds",
        "timestamp",
    ]
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns)
        writer.writeheader()
        for row in rows:
            writer.writerow(
                {
                    "event_id": row.event_id,
                    "sport": row.sport,
                    "book": row.book,
                    "market_type": row.market_type,
                    "outcome": row.outcome,
                    "decimal_odds": row.decimal_odds,
                    "timestamp": row.timestamp.isoformat(),
                }
            )


if __name__ == "__main__":
    main()
