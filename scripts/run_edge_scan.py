#!/usr/bin/env python
from __future__ import annotations

import argparse
import logging
from pathlib import Path

import _bootstrap  # noqa: F401

from edgelab.config.settings import Settings
from edgelab.research.pipeline import MockResearchRun, write_signals_csv


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run EdgeLab edge scan")
    parser.add_argument("--mock", action="store_true")
    parser.add_argument("--output", type=Path, default=Path("reports/mock_signals.csv"))
    return parser.parse_args()


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
    args = parse_args()
    if not args.mock:
        raise SystemExit("Only --mock mode is enabled in EdgeLab v0.1")
    signals = MockResearchRun(Settings()).signals()
    write_signals_csv(args.output, signals)
    print(f"Signals: {len(signals)}")
    print(f"Wrote {args.output}")


if __name__ == "__main__":
    main()
