"""Reward-aware market-making quote simulator."""

from __future__ import annotations

from sharpedge.data.schemas import MarketMakerQuote


def simulate_market_maker_quote(
    market_id: str,
    outcome: str,
    fair_probability: float,
    uncertainty: float,
    orderbook_spread: float,
    volatility_proxy: float,
    inventory: float,
    event_time_remaining_seconds: float,
    reward_score: float = 0.0,
) -> MarketMakerQuote:
    base_width = max(0.005, orderbook_spread / 2)
    adverse_selection_buffer = uncertainty + volatility_proxy * 0.5
    time_buffer = 0.015 if event_time_remaining_seconds < 900 else 0.0
    inventory_penalty = max(-0.05, min(0.05, inventory * 0.002))
    width = base_width + adverse_selection_buffer + time_buffer
    bid = max(0.01, min(0.99, fair_probability - width - inventory_penalty))
    ask = max(0.01, min(0.99, fair_probability + width - inventory_penalty))
    expected_spread_capture = max(0.0, ask - bid) / 2
    adverse_selection_cost = adverse_selection_buffer + abs(inventory_penalty)
    estimated_reward_value = reward_score * 0.01
    expected_value = expected_spread_capture + estimated_reward_value - adverse_selection_cost
    if expected_value <= 0 or bid >= ask:
        return MarketMakerQuote(
            market_id=market_id,
            outcome=outcome,
            quote=False,
            reason="expected reward-adjusted spread capture does not cover adverse selection",
            expected_value=expected_value,
        )
    return MarketMakerQuote(
        market_id=market_id,
        outcome=outcome,
        bid=bid,
        ask=ask,
        quote=True,
        reason="simulated quote passes reward-adjusted economics",
        expected_value=expected_value,
    )
