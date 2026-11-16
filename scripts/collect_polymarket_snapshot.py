#!/usr/bin/env python
from __future__ import annotations

import argparse

import _bootstrap  # noqa: F401

from sharpedge.config.settings import Settings
from sharpedge.data.polymarket_collector import PolymarketCollector
from sharpedge.data.storage import Storage


def main() -> None:
    parser = argparse.ArgumentParser(description="Collect Polymarket sports snapshot")
    parser.add_argument("--mock", action="store_true", help="Use bundled sample data")
    args = parser.parse_args()
    settings = Settings()
    storage = Storage(settings.database_url)
    try:
        collector = PolymarketCollector(settings=settings, storage=storage, mock=args.mock)
        markets, orderbooks = collector.collect_snapshot()
        print(f"Collected {len(markets)} markets and {len(orderbooks)} order books")
    finally:
        storage.close()


if __name__ == "__main__":
    main()
