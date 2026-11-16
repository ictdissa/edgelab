from __future__ import annotations

import json

from scripts.audit_relative_value_candidates import build_audit_rows


def test_relative_value_candidate_audit_reports_leg_blocker() -> None:
    candidates = [
        {
            "candidate_id": "rv1",
            "snapshot_timestamp": "2026-05-20T00:00:00+00:00",
            "relation_type": "implication",
            "status": "PRICING_INCOMPLETE",
            "source_status": "EXECUTABLE",
            "theoretical_violation": "0.05",
            "executable_violation": "0.03",
            "leg_count": "2",
            "legs_json": json.dumps(
                [
                    {
                        "market_id": "child",
                        "outcome": "Yes",
                        "side": "SELL_YES",
                        "bid": "",
                        "ask": "",
                        "entry_price": "",
                        "size_available": "",
                        "token_id": "",
                        "freshness_status": "unknown",
                        "leg_rejection_reason": "missing bid or ask",
                        "sanity_warnings": "[]",
                    }
                ]
            ),
            "min_leg_liquidity": "0",
            "paper_trade_eligible": "false",
            "paper_trade_blocker": "missing executable bid/ask",
        }
    ]

    audit = build_audit_rows(candidates)

    assert audit[0]["all_bid_ask_present"] == "false"
    assert audit[0]["paper_trade_eligible"] == "false"
    assert "missing bid or ask" in audit[0]["leg_level_reasons"]
