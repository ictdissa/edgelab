"""Offline sample-data loader for scripts, tests, and dashboard mock mode."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from sharpedge.data.odds_collector import MockOddsProvider
from sharpedge.data.polymarket_collector import PolymarketCollector
from sharpedge.data.schemas import ExternalOdds, Market, OrderBook, Resolution

SAMPLES_DIR = Path(__file__).resolve().parent / "samples"


def load_mock_markets() -> list[Market]:
    from sharpedge.config.settings import Settings

    return PolymarketCollector(Settings(), mock=True).fetch_active_sports_markets()


def load_mock_orderbooks() -> list[OrderBook]:
    with (SAMPLES_DIR / "orderbooks.json").open(encoding="utf-8") as handle:
        payload = json.load(handle)
    rows = payload.get("orderbooks", payload) if isinstance(payload, dict) else payload
    return [OrderBook(**row) for row in rows]


def load_mock_odds() -> list[ExternalOdds]:
    return MockOddsProvider().fetch_odds()


def load_mock_resolutions() -> list[Resolution]:
    with (SAMPLES_DIR / "resolutions.json").open(encoding="utf-8") as handle:
        payload = json.load(handle)
    rows = payload.get("resolutions", payload) if isinstance(payload, dict) else payload
    return [Resolution(**row) for row in rows]


def load_mock_timeseries() -> tuple[list[Market], list[OrderBook], list[ExternalOdds]]:
    with (SAMPLES_DIR / "time_series_snapshots.json").open(encoding="utf-8") as handle:
        payload = json.load(handle)
    markets = load_mock_markets()
    orderbooks = [OrderBook(**row) for row in payload.get("orderbooks", [])]
    odds = [ExternalOdds(**row) for row in payload.get("external_odds", [])]
    return markets, orderbooks, odds


def latest_mock_timestamp(
    orderbooks: list[OrderBook] | None = None, odds: list[ExternalOdds] | None = None
) -> datetime:
    orderbooks = orderbooks if orderbooks is not None else load_mock_orderbooks()
    odds = odds if odds is not None else load_mock_odds()
    timestamps = [book.timestamp for book in orderbooks] + [row.timestamp for row in odds]
    return max(timestamps).astimezone(UTC) if timestamps else datetime.now(UTC)
