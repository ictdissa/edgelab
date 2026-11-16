"""Base fair-value interfaces."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Sequence

from edgelab.data.schemas import ExternalAnchor, FairValue, OrderBook, PredictionMarket


class FairValueModel(ABC):
    @abstractmethod
    def compute(
        self,
        markets: Sequence[PredictionMarket],
        anchors: Sequence[ExternalAnchor],
        orderbooks: Sequence[OrderBook],
    ) -> list[FairValue]:
        """Return fair values for matched prediction-market outcomes."""
