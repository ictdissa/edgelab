"""Market discovery facade."""

from __future__ import annotations

from edgelab.data.external_sources.mock_provider import MockProvider
from edgelab.data.schemas import PredictionMarket
from edgelab.matching.market_classifier import classify_market


def discover_mock_markets() -> list[PredictionMarket]:
    markets = MockProvider().markets()
    output: list[PredictionMarket] = []
    for market in markets:
        category = classify_market(market.title, market.description, market.outcomes)
        output.append(market.model_copy(update={"category": category}))
    return output
