"""Offline order-book fixtures; live CLOB collection is in polymarket_collector."""

from __future__ import annotations

from edgelab.data.external_sources.mock_provider import MockProvider
from edgelab.data.schemas import OrderBook


def load_mock_orderbooks() -> list[OrderBook]:
    return MockProvider().orderbooks()
