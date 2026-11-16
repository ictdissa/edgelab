#!/usr/bin/env python
from __future__ import annotations

import argparse

import _bootstrap  # noqa: F401

from sharpedge.config.settings import Settings
from sharpedge.data.odds_collector import MockOddsProvider, OddsAPIProvider
from sharpedge.data.storage import Storage


def main() -> None:
    parser = argparse.ArgumentParser(description="Collect external odds snapshot")
    parser.add_argument("--mock", action="store_true", help="Use bundled sample odds")
    args = parser.parse_args()
    settings = Settings()
    provider = (
        MockOddsProvider()
        if args.mock
        else OddsAPIProvider(settings.odds_api_key, settings.odds_api_base)
    )
    odds = provider.fetch_odds()
    storage = Storage(settings.database_url)
    try:
        storage.insert_external_odds(odds)
    finally:
        storage.close()
    print(f"Collected {len(odds)} external odds rows")


if __name__ == "__main__":
    main()
