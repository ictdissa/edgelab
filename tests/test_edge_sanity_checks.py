from __future__ import annotations

from edgelab.config.settings import Settings
from edgelab.research.signal_lifecycle import compute_lifecycle
from scripts.run_edge_sanity_checks import build_edge_sanity_warnings


def test_suspicious_adjusted_edge_warns_but_is_diagnostic_only() -> None:
    row = {
        "category": "crypto",
        "market_id": "m1",
        "outcome": "Yes",
        "fair_probability": "0.99",
        "bid": "0.10",
        "ask": "0.20",
        "spread": "0.10",
        "liquidity": "50",
        "data_age_seconds": "1",
        "risk_decision": "APPROVED",
    }
    settings = Settings(MIN_ADJUSTED_EDGE=0.01, MIN_LIQUIDITY=10, MAX_SPREAD=0.20)

    lifecycle = compute_lifecycle(row, settings)
    warnings = build_edge_sanity_warnings([row], settings)

    assert any(warning["warning_code"] == "SUSPICIOUS_ADJUSTED_EDGE" for warning in warnings)
    assert all(
        warning["blocks_risk_approval"] == "false"
        for warning in warnings
        if warning["warning_code"] == "SUSPICIOUS_ADJUSTED_EDGE"
    )
    assert lifecycle.risk_decision == "APPROVED"


def test_suspicious_adjusted_edge_can_be_whitelisted_for_mock_tests() -> None:
    row = {
        "category": "crypto",
        "market_id": "m1",
        "outcome": "Yes",
        "fair_probability": "0.99",
        "bid": "0.10",
        "ask": "0.20",
        "spread": "0.10",
        "liquidity": "50",
        "data_age_seconds": "1",
        "risk_decision": "APPROVED",
    }
    settings = Settings(MIN_ADJUSTED_EDGE=0.01, MIN_LIQUIDITY=10, MAX_SPREAD=0.20)

    lifecycle = compute_lifecycle(row, settings, allow_suspicious_edges=True)

    assert lifecycle.risk_decision == "APPROVED"


def test_no_live_trading_is_enabled_by_default() -> None:
    assert Settings().enable_live_trading is False
