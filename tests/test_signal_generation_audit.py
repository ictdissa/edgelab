from __future__ import annotations

import pytest

from edgelab.config.settings import Settings
from scripts.audit_signal_generation import audit_signal, raw_edge, summarize_audit


def _settings() -> Settings:
    return Settings(MIN_ADJUSTED_EDGE=0.01, MIN_LIQUIDITY=1, MAX_SPREAD=0.08)


def test_positive_adjusted_edge_hold_is_flagged_as_possible_action_bug() -> None:
    row = {
        "signal_id": "s1",
        "category": "crypto",
        "market_id": "m1",
        "outcome": "Yes",
        "fair_probability": "0.60",
        "bid": "0.40",
        "ask": "0.50",
        "mid": "0.45",
        "spread": "0.01",
        "adjusted_edge": "0.03",
        "liquidity": "20",
        "data_age_seconds": "10",
        "action": "HOLD",
        "risk_decision": "NOT_EVALUATED",
    }

    audited = audit_signal(row, _settings())

    assert audited["inferred_side"] == "BUY_YES"
    assert audited["side_specific_action_expected"] == "true"
    assert audited["possible_action_bug"] == "true"


def test_audit_counts_edges_and_actions() -> None:
    settings = _settings()
    rows = [
        audit_signal(
            {
                "category": "crypto",
                "market_id": "m1",
                "outcome": "Yes",
                "fair_probability": "0.60",
                "bid": "0.40",
                "ask": "0.50",
                "spread": "0.01",
                "adjusted_edge": "0.03",
                "liquidity": "20",
                    "data_age_seconds": "10",
                    "action": "HOLD",
                    "risk_decision": "NOT_EVALUATED",
                },
            settings,
        )
    ]

    summary = summarize_audit(rows, settings)

    assert summary.total_signals == 1
    assert summary.action_counts["HOLD"] == 1
    assert summary.positive_raw_edge_count == 1
    assert summary.positive_adjusted_edge_count == 1
    assert summary.possible_action_bug_count == 1


def test_negative_raw_edge_is_not_positive_edge() -> None:
    row = {
        "fair_probability": "0.40",
        "bid": "0.30",
        "ask": "0.50",
    }

    assert raw_edge(row) == pytest.approx(-0.1)
