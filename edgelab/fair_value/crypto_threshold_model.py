"""Crypto threshold fair value using a simple lognormal model."""

from __future__ import annotations

import math
from datetime import UTC, datetime
from typing import Sequence

from scipy.stats import norm

from edgelab.config.settings import Settings
from edgelab.data.schemas import ExternalAnchor, FairValue, OrderBook, PredictionMarket
from edgelab.fair_value.base import FairValueModel
from edgelab.fair_value.inputs import contract_anchor, usable
from edgelab.matching.payoffs import crypto_payoff


def lognormal_above_probability(
    spot: float,
    threshold: float,
    years_to_expiry: float,
    volatility: float,
) -> float:
    if not all(math.isfinite(x) and x > 0 for x in (spot, threshold, years_to_expiry, volatility)):
        raise ValueError("spot, threshold, time, and volatility must be positive")
    sigma_t = volatility * math.sqrt(years_to_expiry)
    d2 = (math.log(spot / threshold) - 0.5 * volatility**2 * years_to_expiry) / sigma_t
    return float(norm.cdf(d2))


class CryptoThresholdModel(FairValueModel):
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
            if market.category != "crypto_threshold":
                continue
            payoff = crypto_payoff(market)
            if payoff is None or payoff.kind != "terminal" or payoff.expiry <= self.as_of:
                continue
            candidates = [
                a
                for a in anchors
                if a.category == market.category
                and a.event_id == market.event_id
                and usable(a, self.as_of, self.settings.max_data_age_seconds)
            ]
            anchor = contract_anchor(candidates, market.market_id)
            if anchor is None:
                continue
            meta = anchor.metadata
            try:
                if (
                    meta.get("threshold") is not None
                    and float(meta["threshold"]) != payoff.threshold
                ):
                    continue
                if (
                    meta.get("expiry")
                    and datetime.fromisoformat(str(meta["expiry"]).replace("Z", "+00:00"))
                    != payoff.expiry
                ):
                    continue
            except (TypeError, ValueError):
                continue
            if meta.get("direction", payoff.direction) != payoff.direction:
                continue
            asset = str(meta.get("asset", payoff.asset)).lower()
            if asset not in {
                payoff.asset.lower(),
                {"BTC": "bitcoin", "ETH": "ethereum"}.get(payoff.asset),
            }:
                continue
            years = (payoff.expiry - self.as_of).total_seconds() / (365 * 24 * 3600)
            try:
                probability = lognormal_above_probability(
                    float(meta["spot"]), payoff.threshold, years, float(meta["volatility"])
                )
            except (KeyError, TypeError, ValueError):
                continue
            if payoff.direction == "below":
                probability = 1 - probability
            values.append(
                FairValue(
                    event_id=market.event_id,
                    market_id=market.market_id,
                    outcome="Yes",
                    fair_probability=probability,
                    source="crypto_lognormal_threshold",
                    timestamp=anchor.timestamp,
                    available_at=anchor.available_at or anchor.timestamp,
                    computed_at=self.as_of,
                    uncertainty=min(0.30, self.settings.model_uncertainty_bps / 10_000 + 0.06),
                    metadata={
                        "years_to_expiry": years,
                        "payoff": payoff.model_dump(mode="json"),
                        "provider": anchor.source,
                        "match_decision": "MATCH",
                        "model_validated": anchor.source.startswith("mock_")
                        or meta.get("volatility_provenance") == "calibrated_out_of_sample",
                        "assumption": "zero arithmetic drift; volatility is not calibrated",
                    },
                )
            )
        return values
