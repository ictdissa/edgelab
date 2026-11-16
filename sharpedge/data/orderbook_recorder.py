"""Convenience wrapper for recording order-book snapshots."""

from __future__ import annotations

from sharpedge.data.polymarket_collector import PolymarketCollector
from sharpedge.data.schemas import OrderBook
from sharpedge.data.storage import Storage


class OrderBookRecorder:
    def __init__(self, collector: PolymarketCollector, storage: Storage) -> None:
        self.collector = collector
        self.storage = storage

    def record_all_active(self) -> list[OrderBook]:
        _markets, orderbooks = self.collector.collect_snapshot()
        return orderbooks
