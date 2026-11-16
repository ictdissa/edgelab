#!/usr/bin/env python
from __future__ import annotations

import argparse
import logging
from datetime import UTC, datetime

import _bootstrap  # noqa: F401

from edgelab.data.external_sources.mock_provider import MockProvider
from edgelab.research.backtest import replay
from edgelab.config.settings import Settings


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run EdgeLab point-in-time backtest")
    parser.add_argument("--mock", action="store_true")
    return parser.parse_args()


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
    args = parse_args()
    if not args.mock:
        raise SystemExit("Only --mock mode is enabled in EdgeLab v0.1")
    provider = MockProvider()
    result = replay(
        provider.markets(),
        provider.anchors(),
        provider.orderbooks(),
        Settings(),
        [datetime(2026, 5, 18, tzinfo=UTC)],
    )
    print(
        f"Synthetic execution smoke replay: signals={len(result.signals)}, fills={sum(d.approved for d in result.decisions)}"
    )
    print(
        "Not a strategy performance backtest: longitudinal held-out data and calibrated fills are still required."
    )


if __name__ == "__main__":
    main()
