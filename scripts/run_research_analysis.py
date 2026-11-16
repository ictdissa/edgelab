#!/usr/bin/env python
from __future__ import annotations

import argparse
import logging
from pathlib import Path

import _bootstrap  # noqa: F401

from edgelab.config.settings import Settings
from edgelab.research.pipeline import write_mock_report


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run EdgeLab research analysis")
    parser.add_argument("--mock", action="store_true", help="Use offline mock data")
    parser.add_argument("--output", type=Path, default=Path("reports/mock_research_analysis.md"))
    return parser.parse_args()


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
    args = parse_args()
    if not args.mock:
        raise SystemExit("Only --mock mode is enabled in EdgeLab v0.1")
    write_mock_report(args.output, Settings())
    print(f"Wrote {args.output}")


if __name__ == "__main__":
    main()
