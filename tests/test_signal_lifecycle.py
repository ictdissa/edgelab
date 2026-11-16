from __future__ import annotations

from edgelab.config.settings import Settings
from edgelab.research.signal_lifecycle import (
    FINAL_PAPER_CANDIDATE,
    FINAL_REJECTED_BY_RISK,
    compute_lifecycle,
)


def _settings() -> Settings:
    return Settings(MIN_ADJUSTED_EDGE=0.01, MIN_LIQUIDITY=10, MAX_SPREAD=0.08)


def test_buy_edge_above_threshold_approved_is_paper_candidate() -> None:
    lifecycle = compute_lifecycle(
        {
            "fair_probability": "0.60",
            "bid": "0.50",
            "ask": "0.55",
            "spread": "0.05",
            "liquidity": "50",
            "data_age_seconds": "1",
            "outcome": "Yes",
            "risk_decision": "APPROVED",
        },
        _settings(),
        allow_suspicious_edges=True,
    )

    assert lifecycle.best_side == "BUY_YES"
    assert lifecycle.final_decision == FINAL_PAPER_CANDIDATE


def test_sell_edge_above_threshold_approved_is_paper_candidate() -> None:
    lifecycle = compute_lifecycle(
        {
            "fair_probability": "0.40",
            "bid": "0.45",
            "ask": "0.50",
            "spread": "0.05",
            "liquidity": "50",
            "data_age_seconds": "1",
            "outcome": "Yes",
            "risk_decision": "APPROVED",
        },
        _settings(),
        allow_suspicious_edges=True,
    )

    assert lifecycle.best_side == "SELL_YES"
    assert lifecycle.final_decision == FINAL_PAPER_CANDIDATE


def test_liquidity_failure_preserves_best_side_but_rejects_by_risk() -> None:
    lifecycle = compute_lifecycle(
        {
            "fair_probability": "0.60",
            "bid": "0.50",
            "ask": "0.55",
            "spread": "0.05",
            "liquidity": "1",
            "data_age_seconds": "1",
            "outcome": "Yes",
            "risk_decision": "APPROVED",
        },
        _settings(),
        allow_suspicious_edges=True,
    )

    assert lifecycle.best_side == "BUY_YES"
    assert lifecycle.final_decision == FINAL_REJECTED_BY_RISK


def test_positive_edge_hold_avoid_requires_risk_reasons() -> None:
    lifecycle = compute_lifecycle(
        {
            "fair_probability": "0.60",
            "bid": "0.50",
            "ask": "0.55",
            "spread": "0.05",
            "liquidity": "50",
            "data_age_seconds": "1",
            "outcome": "Yes",
            "action": "HOLD",
            "risk_decision": "REJECTED",
            "risk_reasons": "model uncertainty",
        },
        _settings(),
        allow_suspicious_edges=True,
    )

    assert lifecycle.best_side == "BUY_YES"
    assert lifecycle.risk_reasons
    assert lifecycle.final_decision == FINAL_REJECTED_BY_RISK
