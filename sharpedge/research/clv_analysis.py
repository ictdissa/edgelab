"""Closing-line-value tracking."""

from __future__ import annotations

from collections import defaultdict
from datetime import timedelta

from sharpedge.data.schemas import CLVObservation, FairValue, Signal


HORIZONS = {
    "5m": timedelta(minutes=5),
    "30m": timedelta(minutes=30),
    "1h": timedelta(hours=1),
    "close": None,
}


class CLVTracker:
    def build_observations(
        self,
        signals: list[Signal],
        fair_values: list[FairValue],
        sport_by_market: dict[str, str] | None = None,
    ) -> list[CLVObservation]:
        values_by_key: dict[tuple[str, str], list[FairValue]] = defaultdict(list)
        for value in fair_values:
            values_by_key[(value.market_id, value.outcome)].append(value)
        for rows in values_by_key.values():
            rows.sort(key=lambda row: row.timestamp)

        observations: list[CLVObservation] = []
        for signal in signals:
            entry_price = (
                signal.polymarket_ask
                if signal.action == "BUY_YES"
                else signal.polymarket_bid
            )
            if entry_price is None:
                continue
            rows = values_by_key.get((signal.market_id, signal.outcome), [])
            if not rows:
                continue
            for horizon, delta in HORIZONS.items():
                target = rows[-1].timestamp if delta is None else signal.timestamp + delta
                future = _first_at_or_after(rows, target)
                if not future:
                    continue
                clv = (
                    future.fair_probability - entry_price
                    if signal.action == "BUY_YES"
                    else entry_price - future.fair_probability
                )
                observations.append(
                    CLVObservation(
                        market_id=signal.market_id,
                        outcome=signal.outcome,
                        signal_timestamp=signal.timestamp,
                        horizon=horizon,
                        entry_price=entry_price,
                        fair_probability_at_horizon=future.fair_probability,
                        clv=clv,
                        sport=(sport_by_market or {}).get(signal.market_id),
                        signal_type=signal.signal_type,
                        liquidity_bucket=_bucket(signal.available_size, [25, 100, 500]),
                        adjusted_edge_bucket=_bucket(signal.adjusted_edge, [0.02, 0.05, 0.10]),
                    )
                )
        return observations


def _first_at_or_after(rows: list[FairValue], target) -> FairValue | None:
    for row in rows:
        if row.timestamp >= target:
            return row
    return None


def _bucket(value: float, cuts: list[float]) -> str:
    if value < cuts[0]:
        return f"<{cuts[0]}"
    for low, high in zip(cuts, cuts[1:], strict=False):
        if low <= value < high:
            return f"{low}-{high}"
    return f">={cuts[-1]}"
