"""No-vig probability conversion."""

from __future__ import annotations

from math import isclose


def _validate_decimal_odds(decimal_odds: float) -> float:
    try:
        value = float(decimal_odds)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Invalid decimal odds: {decimal_odds}") from exc
    if value <= 1.0:
        raise ValueError(f"Decimal odds must be > 1.0, got {value}")
    return value


def generic_no_vig(decimal_odds_by_outcome: dict[str, float]) -> dict[str, float]:
    """Convert decimal odds into no-vig probabilities for any exhaustive market."""

    if len(decimal_odds_by_outcome) < 2:
        raise ValueError("At least two outcomes are required")
    implied = {
        outcome: 1.0 / _validate_decimal_odds(odds)
        for outcome, odds in decimal_odds_by_outcome.items()
    }
    total = sum(implied.values())
    if total <= 0:
        raise ValueError("Implied probability total must be positive")
    probabilities = {outcome: prob / total for outcome, prob in implied.items()}
    if not isclose(sum(probabilities.values()), 1.0, abs_tol=1e-9):
        raise ValueError("No-vig probabilities failed to normalize")
    return probabilities


def no_vig_two_way(decimal_a: float, decimal_b: float) -> tuple[float, float]:
    probs = generic_no_vig({"a": decimal_a, "b": decimal_b})
    return probs["a"], probs["b"]


def no_vig_three_way(
    decimal_home: float, decimal_draw: float, decimal_away: float
) -> tuple[float, float, float]:
    probs = generic_no_vig(
        {"home": decimal_home, "draw": decimal_draw, "away": decimal_away}
    )
    return probs["home"], probs["draw"], probs["away"]
