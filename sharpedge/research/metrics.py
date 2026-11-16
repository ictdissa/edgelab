"""Performance and research metrics."""

from __future__ import annotations

import statistics
from collections import defaultdict

from sharpedge.data.schemas import CLVObservation, Trade


def average(values: list[float]) -> float:
    return statistics.fmean(values) if values else 0.0


def median(values: list[float]) -> float:
    return statistics.median(values) if values else 0.0


def trade_pnl(trades: list[Trade]) -> float:
    return sum(trade.cash_flow for trade in trades)


def summarize_clv(observations: list[CLVObservation]) -> dict[str, float | dict[str, float]]:
    clv_values = [obs.clv for obs in observations]
    summary: dict[str, float | dict[str, float]] = {
        "average_clv": average(clv_values),
        "median_clv": median(clv_values),
    }
    for field in ("sport", "signal_type", "liquidity_bucket", "adjusted_edge_bucket"):
        grouped: dict[str, list[float]] = defaultdict(list)
        for obs in observations:
            key = getattr(obs, field) or "unknown"
            grouped[key].append(obs.clv)
        summary[f"average_clv_by_{field}"] = {
            key: average(values) for key, values in grouped.items()
        }
    return summary
