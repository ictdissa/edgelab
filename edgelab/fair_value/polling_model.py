"""Disabled election valuation: polling observations are not tradable anchors."""

from __future__ import annotations

from typing import Sequence

from edgelab.data.schemas import ExternalAnchor, FairValue, OrderBook, PredictionMarket
from edgelab.fair_value.base import FairValueModel


class PollingModel(FairValueModel):
    def compute(
        self,
        markets: Sequence[PredictionMarket],
        anchors: Sequence[ExternalAnchor],
        orderbooks: Sequence[OrderBook],
    ) -> list[FairValue]:
        _ = markets, anchors, orderbooks
        return []
