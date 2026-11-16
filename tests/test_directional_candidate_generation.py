from __future__ import annotations

from edgelab.config.settings import Settings
from scripts.generate_paper_signals import build_candidates


def test_directional_candidate_generation_uses_best_side() -> None:
    rows = [
        {
            "signal_id": "s1",
            "timestamp": "2026-05-19T00:00:00+00:00",
            "category": "crypto",
            "market_id": "m1",
            "outcome": "Yes",
            "fair_probability": "0.60",
            "bid": "0.50",
            "ask": "0.55",
            "spread": "0.05",
            "liquidity": "50",
            "data_age_seconds": "1",
            "action": "HOLD",
            "risk_decision": "APPROVED",
        }
    ]

    candidates, _rejections, rejected = build_candidates(
        rows,
        Settings(MIN_ADJUSTED_EDGE=0.01, MIN_LIQUIDITY=10, MAX_SPREAD=0.08),
        None,
    )

    assert len(candidates) == 1
    assert candidates[0]["side"] == "BUY_YES"
    assert candidates[0]["execution_price_source"] == "ASK"
    assert rejected == []


def test_rejected_directional_edges_are_not_paper_candidates() -> None:
    rows = [
        {
            "signal_id": "s1",
            "timestamp": "2026-05-19T00:00:00+00:00",
            "category": "crypto",
            "market_id": "m1",
            "outcome": "Yes",
            "fair_probability": "0.40",
            "bid": "0.45",
            "ask": "0.50",
            "spread": "0.05",
            "liquidity": "50",
            "data_age_seconds": "1",
            "action": "HOLD",
            "risk_decision": "REJECTED",
            "risk_reasons": "unclear resolution rules",
        }
    ]

    candidates, _rejections, rejected = build_candidates(
        rows,
        Settings(MIN_ADJUSTED_EDGE=0.01, MIN_LIQUIDITY=10, MAX_SPREAD=0.08),
        None,
    )

    assert candidates == []
    assert len(rejected) == 1
    assert rejected[0]["best_side"] == "SELL_YES"


def test_missing_bid_ask_is_not_paper_candidate() -> None:
    rows = [
        {
            "signal_id": "s1",
            "category": "crypto",
            "market_id": "m1",
            "outcome": "Yes",
            "fair_probability": "0.60",
            "bid": "",
            "ask": "",
            "liquidity": "50",
            "data_age_seconds": "1",
            "risk_decision": "APPROVED",
        }
    ]

    candidates, _rejections, rejected = build_candidates(
        rows,
        Settings(MIN_ADJUSTED_EDGE=0.01, MIN_LIQUIDITY=10, MAX_SPREAD=0.08),
        None,
    )

    assert candidates == []
    assert rejected == []
