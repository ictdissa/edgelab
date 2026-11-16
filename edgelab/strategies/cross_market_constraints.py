"""Logical constraint engine."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ConstraintFinding:
    constraint_type: str
    left_market: str
    right_market: str
    implied_edge: float
    executable: bool
    reason: str


def implication_constraint(
    child_market: str,
    parent_market: str,
    child_probability: float,
    parent_probability: float,
    liquidity_available: bool,
) -> ConstraintFinding | None:
    if child_probability <= parent_probability:
        return None
    return ConstraintFinding(
        constraint_type="P(child)<=P(parent)",
        left_market=child_market,
        right_market=parent_market,
        implied_edge=child_probability - parent_probability,
        executable=liquidity_available,
        reason="theoretical only unless both legs have executable liquidity",
    )
