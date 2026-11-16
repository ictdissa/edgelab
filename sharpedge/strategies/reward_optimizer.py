"""Helpers for market-making reward scoring."""

from __future__ import annotations


def reward_adjusted_value(
    spread_capture: float,
    estimated_reward_value: float,
    adverse_selection_cost: float,
    inventory_cost: float = 0.0,
) -> float:
    """Return net expected value for a simulated quote."""

    return spread_capture + estimated_reward_value - adverse_selection_cost - inventory_cost
