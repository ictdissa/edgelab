from __future__ import annotations

import json
from datetime import UTC, datetime

from scripts.update_clv import SnapshotSignal, update_relative_value_ledger_clv


def test_relative_value_clv_does_not_fabricate_missing_follow_up() -> None:
    now = datetime(2026, 5, 20, tzinfo=UTC)
    legs = [
        {
            "market_id": "parent",
            "outcome": "Yes",
            "side": "BUY_YES",
            "price_source": "ASK",
            "price": "0.40",
            "size_available": "20",
        },
        {
            "market_id": "child",
            "outcome": "Yes",
            "side": "SELL_YES",
            "price_source": "BID",
            "price": "0.60",
            "size_available": "20",
        },
    ]
    ledger = [
        {
            "paper_trade_id": "rvtrade1",
            "timestamp": now.isoformat(),
            "candidate_id": "rv1",
            "snapshot_timestamp": now.isoformat(),
            "relation_type": "implication",
            "leg_count": "2",
            "legs_json": json.dumps(legs),
            "package_entry_cost": "-2",
            "package_expected_payoff": "-1.7",
            "package_expected_edge": "0.3",
            "max_trade_size": "10",
            "executable_violation": "0.03",
            "theoretical_violation": "0.05",
            "risk_decision": "APPROVED",
            "risk_reasons": "",
            "execution_status": "FILLED_PAPER",
            "clv_5m": "",
            "clv_30m": "",
            "clv_1h": "",
            "clv_status": "pending",
        }
    ]
    snapshots = [
        SnapshotSignal(
            timestamp=now,
            category="relative_value",
            market_id="parent",
            outcome="Yes",
            fair_probability=None,
            mid=None,
            bid=0.39,
            ask=0.41,
        )
    ]

    updated, observations = update_relative_value_ledger_clv(ledger, snapshots)

    assert updated[0]["clv_status"] == "partial_or_missing_followup"
    assert all(row["status"] == "partial_or_missing_followup" for row in observations)
    assert all(row["package_clv"] == "" for row in observations)


def test_relative_value_clv_zero_values_still_count_as_complete() -> None:
    now = datetime(2026, 5, 20, tzinfo=UTC)
    legs = [
        {
            "market_id": "parent",
            "token_id": "parent-yes",
            "outcome": "Yes",
            "side": "BUY_YES",
            "price_source": "ASK",
            "price": "0.40",
            "size_available": "10",
        },
        {
            "market_id": "child",
            "token_id": "child-yes",
            "outcome": "Yes",
            "side": "SELL_YES",
            "price_source": "BID",
            "price": "0.60",
            "size_available": "10",
        },
    ]
    ledger = [
        {
            "paper_trade_id": "rvtrade1",
            "timestamp": now.isoformat(),
            "candidate_id": "rv1",
            "snapshot_timestamp": now.isoformat(),
            "relation_type": "implication",
            "leg_count": "2",
            "legs_json": json.dumps(legs),
            "package_entry_cost": "-2",
            "package_expected_payoff": "-2",
            "package_expected_edge": "0",
            "max_trade_size": "10",
            "executable_violation": "0",
            "theoretical_violation": "0",
            "risk_decision": "APPROVED",
            "risk_reasons": "",
            "execution_status": "FILLED_PAPER",
            "clv_5m": "",
            "clv_30m": "",
            "clv_1h": "",
            "clv_status": "pending",
        }
    ]
    snapshots = []
    for minutes in (5, 30, 60):
        observed_at = now.replace(
            minute=minutes if minutes < 60 else 0, hour=now.hour if minutes < 60 else now.hour + 1
        )
        snapshots.extend(
            [
                SnapshotSignal(
                    timestamp=observed_at,
                    category="relative_value",
                    market_id="parent",
                    token_id="parent-yes",
                    outcome="Yes",
                    bid_size=100,
                    ask_size=100,
                    fair_probability=None,
                    mid=None,
                    bid=0.40,
                    ask=0.42,
                ),
                SnapshotSignal(
                    timestamp=observed_at,
                    category="relative_value",
                    market_id="child",
                    token_id="child-yes",
                    outcome="Yes",
                    bid_size=100,
                    ask_size=100,
                    fair_probability=None,
                    mid=None,
                    bid=0.58,
                    ask=0.60,
                ),
            ]
        )

    updated, observations = update_relative_value_ledger_clv(ledger, snapshots)

    assert updated[0]["clv_status"] == "complete"
    assert all(row["status"] == "observed" for row in observations)
    assert all(row["package_clv"] == "0" for row in observations)
