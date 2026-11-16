"""Inventory-adjusted quote-width heuristic; no fill or queue simulation."""

from __future__ import annotations


def quote_width(
    fair_probability: float,
    uncertainty: float,
    adverse_selection_buffer: float,
    inventory_penalty: float,
) -> tuple[float, float]:
    bid = fair_probability - uncertainty - adverse_selection_buffer - inventory_penalty
    ask = fair_probability + uncertainty + adverse_selection_buffer - inventory_penalty
    return max(0.01, bid), min(0.99, ask)
