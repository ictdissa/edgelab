from __future__ import annotations

from edgelab.fair_value.crypto_threshold_model import lognormal_above_probability


def test_lognormal_above_probability_bounds() -> None:
    probability = lognormal_above_probability(
        spot=95_000,
        threshold=100_000,
        years_to_expiry=30 / 365,
        volatility=0.55,
    )
    assert 0.0 < probability < 1.0
