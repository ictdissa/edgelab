"""Weather threshold fair value from a forecast distribution."""

from __future__ import annotations

import math

from datetime import UTC, datetime
from typing import Sequence

from scipy.stats import norm

from edgelab.config.settings import Settings
from edgelab.data.schemas import ExternalAnchor, FairValue, OrderBook, PredictionMarket
from edgelab.fair_value.base import FairValueModel
from edgelab.fair_value.inputs import contract_anchor, usable


def normal_exceedance_probability(mean: float, std: float, threshold: float) -> float:
    if std <= 0 or not all(math.isfinite(x) for x in (mean, std, threshold)):
        raise ValueError("forecast std must be positive")
    return float(1.0 - norm.cdf(threshold, loc=mean, scale=std))


class WeatherThresholdModel(FairValueModel):
    def __init__(self, settings: Settings | None = None, as_of: datetime | None = None) -> None:
        self.settings = settings or Settings()
        self.as_of = as_of or datetime.now(UTC)

    def compute(
        self,
        markets: Sequence[PredictionMarket],
        anchors: Sequence[ExternalAnchor],
        orderbooks: Sequence[OrderBook],
    ) -> list[FairValue]:
        _ = orderbooks
        values: list[FairValue] = []
        for market in markets:
            if market.category != "weather_threshold":
                continue
            anchor = contract_anchor(
                [
                    a
                    for a in anchors
                    if a.event_id == market.event_id
                    and a.category == market.category
                    and usable(a, self.as_of, self.settings.max_data_age_seconds)
                ],
                market.market_id,
            )
            if not anchor:
                continue
            meta = anchor.metadata
            # Live forecast error distributions have not been station-calibrated. Fail closed.
            if not anchor.source.startswith("mock_") or (
                market.end_time and market.end_time <= self.as_of
            ):
                continue
            if float(meta.get("threshold", -1)) != float(market.raw_metadata.get("threshold", -2)):
                continue
            probability = normal_exceedance_probability(
                float(meta["forecast_mean"]),
                float(meta["forecast_std"]),
                float(meta["threshold"]),
            )
            values.append(
                FairValue(
                    event_id=market.event_id,
                    market_id=market.market_id,
                    outcome="Yes",
                    fair_probability=probability,
                    source="synthetic_weather_normal_example",
                    timestamp=anchor.timestamp,
                    computed_at=self.as_of,
                    available_at=anchor.available_at or anchor.timestamp,
                    uncertainty=min(0.35, self.settings.model_uncertainty_bps / 10_000 + 0.08),
                    metadata={
                        "match_decision": "MATCH",
                        "synthetic_distribution": True,
                        "warning": "Synthetic normal weather example; not a rainfall pricing model.",
                    },
                )
            )
        return values
