from __future__ import annotations

from edgelab.config.settings import Settings
from scripts.analyze_near_misses import build_near_misses


def _settings() -> Settings:
    return Settings(MIN_ADJUSTED_EDGE=0.01, MIN_LIQUIDITY=1, MAX_SPREAD=0.08)


def test_near_miss_requires_valid_fair_value_and_bid_ask() -> None:
    rows = [
        {
            "signal_id": "missing_ask",
            "category": "crypto",
            "market_id": "m1",
            "outcome": "Yes",
            "fair_probability": "0.60",
            "bid": "0.50",
            "ask": "",
            "adjusted_edge": "0.03",
            "liquidity": "20",
            "data_age_seconds": "10",
            "action": "HOLD",
            "risk_decision": "REJECTED",
        },
        {
            "signal_id": "valid",
            "category": "crypto",
            "market_id": "m2",
            "outcome": "Yes",
            "fair_probability": "0.60",
            "bid": "0.40",
            "ask": "0.50",
            "spread": "0.01",
            "adjusted_edge": "0.03",
            "liquidity": "20",
            "data_age_seconds": "10",
            "action": "HOLD",
            "risk_decision": "REJECTED",
        },
    ]

    near_misses = build_near_misses(rows, _settings())

    assert len(near_misses) == 1
    assert near_misses[0]["market"] == "m2"


def test_negative_raw_edge_is_not_near_miss_candidate() -> None:
    rows = [
        {
            "signal_id": "negative",
            "category": "crypto",
            "market_id": "m1",
            "outcome": "Yes",
            "fair_probability": "0.40",
            "bid": "0.30",
            "ask": "0.50",
            "spread": "0.20",
            "adjusted_edge": "-0.02",
            "liquidity": "20",
            "data_age_seconds": "10",
            "action": "HOLD",
            "risk_decision": "REJECTED",
        },
    ]

    assert build_near_misses(rows, _settings()) == []
