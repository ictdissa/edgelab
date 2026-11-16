"""Offline mock provider used by tests and default scripts."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from edgelab.data.schemas import ExternalAnchor, OrderBook, PredictionMarket

SAMPLES_DIR = Path(__file__).resolve().parents[1] / "samples"
LOGGER = logging.getLogger(__name__)


class MockProvider:
    def __init__(self, sample_dir: Path | None = None) -> None:
        self.sample_dir = sample_dir or SAMPLES_DIR

    def markets(self) -> list[PredictionMarket]:
        payload = self._load("mock_markets.json")
        markets = [PredictionMarket(**row) for row in payload["markets"]]
        for market in markets:
            market.raw_metadata["synthetic_data"] = True
        LOGGER.info("mock_markets_loaded count=%d", len(markets))
        return markets

    def orderbooks(self) -> list[OrderBook]:
        payload = self._load("mock_orderbooks.json")
        books = [
            OrderBook(**{**row, "available_at": row["timestamp"]}) for row in payload["orderbooks"]
        ]
        LOGGER.info("mock_orderbooks_loaded count=%d", len(books))
        return books

    def anchors(self) -> list[ExternalAnchor]:
        payload = self._load("mock_external_anchors.json")
        rows: list[dict[str, Any]] = []
        for key in ("sports_odds", "crypto", "weather"):
            rows.extend(payload.get(key, []))
        anchors = [ExternalAnchor(**{**row, "available_at": row["timestamp"]}) for row in rows]
        LOGGER.info("mock_anchors_loaded count=%d", len(anchors))
        return anchors

    def _load(self, filename: str) -> dict[str, Any]:
        path = self.sample_dir / filename
        if not path.exists():
            raise FileNotFoundError(f"Missing EdgeLab sample data file: {path}")
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise ValueError(f"Invalid JSON in EdgeLab sample data file: {path}") from exc
        if not isinstance(payload, dict):
            raise ValueError(f"Sample data file must contain a JSON object: {path}")
        return payload
