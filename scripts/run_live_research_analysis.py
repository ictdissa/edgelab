#!/usr/bin/env python
from __future__ import annotations

import argparse
import logging
from pathlib import Path

import _bootstrap  # noqa: F401

from edgelab.config.settings import Settings
from edgelab.research.live_analysis import run_live_research, write_live_failure_report


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run research-only live EdgeLab analysis")
    parser.add_argument("--category", choices=["sports", "crypto", "weather"], required=True)
    parser.add_argument("--limit", type=int, default=50)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--refresh", action="store_true")
    parser.add_argument("--skip-provider-diagnosis", action="store_true")
    parser.add_argument(
        "--odds-provider", choices=["auto", "odds_api_io", "the_odds_api", "disabled"]
    )
    parser.add_argument("--sport", help="Provider sport key, e.g. football or soccer_epl")
    return parser.parse_args()


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
    args = parse_args()
    settings = Settings()
    try:
        run_live_research(
            category=args.category,
            output=args.output,
            settings=settings,
            limit=args.limit,
            refresh=args.refresh,
            skip_provider_diagnosis=args.skip_provider_diagnosis,
            odds_provider=args.odds_provider,
            sport=args.sport,
        )
    except Exception as exc:
        write_live_failure_report(args.category, args.output, settings, exc)
        raise SystemExit(
            "Research collection failed; AVOID. See the failure and freshness reports."
        ) from None
    print(f"Wrote {args.output}")


if __name__ == "__main__":
    main()
