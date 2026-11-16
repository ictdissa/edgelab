from __future__ import annotations

import json

from edgelab.config.settings import Settings
from scripts.run_relative_value_paper_trader import build_relative_value_ledger_rows


def _candidate(price: str = "0.40") -> dict[str, str]:
    legs = [
        {
            "market_id": "parent",
            "outcome": "Yes",
            "side": "BUY_YES",
            "price_source": "ASK",
            "price_source_for_entry": "ASK",
            "price": price,
            "entry_price": price,
            "bid": "0.39" if price else "",
            "ask": price,
            "size_available": "20",
            "leg_is_executable": "true" if price else "false",
            "leg_rejection_reason": "" if price else "missing or invalid executable leg price",
            "sanity_warnings": "[]",
        },
        {
            "market_id": "child",
            "outcome": "Yes",
            "side": "SELL_YES",
            "price_source": "BID",
            "price_source_for_entry": "BID",
            "price": "0.60",
            "entry_price": "0.60",
            "bid": "0.60",
            "ask": "0.62",
            "size_available": "20",
            "leg_is_executable": "true",
            "leg_rejection_reason": "",
            "sanity_warnings": "[]",
        },
    ]
    return {
        "candidate_id": "rv1",
        "relative_value_source": "live_snapshot",
        "empirical_evidence_eligible": "true",
        "relation_type": "implication",
        "status": "PAPER_ELIGIBLE" if price else "PRICING_INCOMPLETE",
        "leg_count": "2",
        "legs_json": json.dumps(legs),
        "executable_violation": "0.03",
        "theoretical_violation": "0.05",
        "min_leg_liquidity": "20",
        "max_trade_size": "10",
        "package_expected_edge": "0.03",
        "package_expected_payoff": "-0.17",
        "paper_trade_eligible": "true" if price else "false",
        "paper_trade_blocker": "" if price else "missing executable bid/ask",
        "final_decision": "RELATIVE_VALUE_MULTI_LEG",
    }


def test_relative_value_package_cannot_claim_fill_without_verified_funding() -> None:
    rows, rejections = build_relative_value_ledger_rows(
        [_candidate()],
        Settings(MIN_LIQUIDITY=1, MAX_TRADE_SIZE=10),
    )

    assert rows[0]["risk_decision"] == "REJECTED"
    assert rows[0]["execution_status"] == "SKIPPED"
    assert rows[0]["clv_status"] == "skipped"
    assert "funding" in rows[0]["risk_reasons"]
    legs = json.loads(rows[0]["legs_json"])
    assert legs[0]["price_source_for_entry"] == "ASK"
    assert legs[1]["price_source_for_entry"] == "BID"
    assert rejections


def test_relative_value_candidate_with_missing_leg_price_is_skipped() -> None:
    rows, rejections = build_relative_value_ledger_rows(
        [_candidate(price="")],
        Settings(MIN_LIQUIDITY=1, MAX_TRADE_SIZE=10),
    )

    assert rows[0]["risk_decision"] == "REJECTED"
    assert rows[0]["execution_status"] == "SKIPPED"
    assert rows[0]["clv_status"] == "skipped"
    assert "missing executable bid/ask" in rows[0]["risk_reasons"]
    assert rejections


def test_static_relative_value_candidate_is_research_only_not_paper_filled() -> None:
    candidate = _candidate()
    candidate["relative_value_source"] = "static_report"
    candidate["empirical_evidence_eligible"] = "false"

    rows, rejections = build_relative_value_ledger_rows(
        [candidate],
        Settings(MIN_LIQUIDITY=1, MAX_TRADE_SIZE=10),
    )

    assert rows[0]["risk_decision"] == "REJECTED"
    assert rows[0]["execution_status"] == "SKIPPED"
    assert "requires live_snapshot" in rows[0]["risk_reasons"]
    assert rejections


def test_relative_value_candidate_with_entry_price_not_matching_quote_is_skipped() -> None:
    candidate = _candidate()
    legs = json.loads(candidate["legs_json"])
    legs[0]["entry_price"] = "0.41"
    legs[0]["price"] = "0.41"
    candidate["legs_json"] = json.dumps(legs)

    rows, rejections = build_relative_value_ledger_rows(
        [candidate],
        Settings(MIN_LIQUIDITY=1, MAX_TRADE_SIZE=10),
    )

    assert rows[0]["risk_decision"] == "REJECTED"
    assert rows[0]["execution_status"] == "SKIPPED"
    assert "entry price does not match executable quote" in rows[0]["risk_reasons"]
    assert rejections
