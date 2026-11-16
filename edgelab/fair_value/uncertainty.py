"""Uncertainty estimation utilities."""

from __future__ import annotations

import statistics
from datetime import UTC, datetime


def bps(value: float) -> float:
    return value / 10_000.0


def estimate_probability_uncertainty(
    probabilities: list[float],
    latest_timestamp: datetime,
    base_bps: float,
    liquidity: float,
    as_of: datetime | None = None,
) -> float:
    as_of = as_of or datetime.now(UTC)
    disagreement = statistics.pstdev(probabilities) if len(probabilities) > 1 else 0.025
    book_penalty = 0.012 if len(probabilities) < 2 else 0.0
    age_seconds = max(0.0, (as_of - latest_timestamp).total_seconds())
    stale_penalty = min(0.04, age_seconds / 86_400.0 * 0.02)
    liquidity_penalty = 0.02 if liquidity < 25 else 0.01 if liquidity < 100 else 0.0
    return min(
        0.35, bps(base_bps) + disagreement + book_penalty + stale_penalty + liquidity_penalty
    )
