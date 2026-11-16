from __future__ import annotations

import math

import pytest

from sharpedge.data.odds_collector import american_to_decimal, parse_odds_value
from sharpedge.research.no_vig_odds import generic_no_vig, no_vig_three_way, no_vig_two_way
from edgelab.fair_value.sports_odds_model import (
    american_to_decimal as edgelab_american_to_decimal,
    generic_no_vig as edgelab_generic_no_vig,
    no_vig_three_way as edgelab_no_vig_three_way,
)


def test_no_vig_two_way_sums_to_one() -> None:
    a, b = no_vig_two_way(1.91, 1.91)
    assert math.isclose(a + b, 1.0, abs_tol=1e-12)
    assert math.isclose(a, 0.5, abs_tol=1e-12)


def test_no_vig_three_way_sums_to_one() -> None:
    home, draw, away = no_vig_three_way(2.05, 3.5, 3.7)
    assert math.isclose(home + draw + away, 1.0, abs_tol=1e-12)
    assert home > draw > away


def test_generic_no_vig_rejects_invalid_odds() -> None:
    with pytest.raises(ValueError, match="Decimal odds must be > 1.0"):
        generic_no_vig({"A": 1.0, "B": 2.1})


def test_odds_format_conversion_and_validation() -> None:
    assert math.isclose(american_to_decimal(-200), 1.5)
    assert math.isclose(parse_odds_value("+150", "american"), 2.5)
    assert math.isclose(parse_odds_value("5/2", "fractional"), 3.5)
    with pytest.raises(ValueError):
        parse_odds_value(0.99, "decimal")


def test_edgelab_no_vig_and_odds_conversion() -> None:
    assert edgelab_american_to_decimal(-150) == pytest.approx(1.6666667)
    probs = edgelab_generic_no_vig({"A": 2.0, "B": 2.0})
    assert probs == {"A": pytest.approx(0.5), "B": pytest.approx(0.5)}
    assert sum(edgelab_no_vig_three_way(2.1, 3.5, 3.4)) == pytest.approx(1.0)
