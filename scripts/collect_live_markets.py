#!/usr/bin/env python
from __future__ import annotations

import argparse
import logging
from pathlib import Path

import _bootstrap  # noqa: F401

from edgelab.config.settings import Settings
from edgelab.research.live_analysis import collect_live_market_snapshot, write_live_failure_report


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Collect live EdgeLab prediction-market snapshots")
    parser.add_argument("--category", choices=["sports", "crypto", "weather"], required=True)
    parser.add_argument("--limit", type=int, default=50)
    parser.add_argument("--refresh", action="store_true")
    return parser.parse_args()


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
    args = parse_args()
    settings = Settings()
    try:
        markets, orderbooks, freshness_path = collect_live_market_snapshot(
            category=args.category,
            limit=args.limit,
            settings=settings,
            refresh=args.refresh,
        )
    except Exception as exc:
        write_live_failure_report(
            args.category, Path(f"reports/collection_failure_{args.category}.md"), settings, exc
        )
        raise SystemExit("Collection failed; see freshness report. No orders placed.") from None
    print(
        "Collected live markets: "
        f"category={args.category}, markets={len(markets)}, orderbooks={len(orderbooks)}"
    )
    print(f"Wrote freshness report {freshness_path}")


if __name__ == "__main__":
    main()
