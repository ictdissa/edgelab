from __future__ import annotations

import pytest

from edgelab.strategies.cross_market_constraints import implication_constraint


def test_implication_constraint_detects_violation() -> None:
    finding = implication_constraint("win_league", "top_4", 0.30, 0.20, liquidity_available=False)
    assert finding is not None
    assert not finding.executable
    assert finding.implied_edge == pytest.approx(0.10)
