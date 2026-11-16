"""Quote-value arithmetic for assumed spread capture, rewards and adverse selection."""

from __future__ import annotations


def expected_quote_value(
    spread_capture: float, reward_value: float, adverse_selection_cost: float
) -> float:
    return spread_capture + reward_value - adverse_selection_cost
