"""Research metrics."""

from __future__ import annotations

import statistics


def mean(values: list[float]) -> float:
    if not values:
        raise ValueError("mean is undefined without observations")
    return statistics.fmean(values)


def median(values: list[float]) -> float:
    if not values:
        raise ValueError("median is undefined without observations")
    return statistics.median(values)
