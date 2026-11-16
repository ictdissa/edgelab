from __future__ import annotations

from datetime import UTC, datetime, timedelta

from scripts.run_markout_analysis import SignalRow, build_markout_observations


def test_markout_analysis_separates_tradable_and_shadow_markouts() -> None:
    now = datetime(2026, 5, 21, tzinfo=UTC)
    signals = [
        SignalRow(
            row={
                "signal_id": "s1",
                "timestamp": now.isoformat(),
                "market_id": "m1",
                "outcome": "Yes",
                "category": "crypto",
                "best_side": "BUY_YES",
                "ask": "0.40",
                "best_adjusted_edge": "0.03",
                "final_decision": "PAPER_CANDIDATE",
                "available_size": "10",
            },
            timestamp=now,
            market_id="m1",
            outcome="Yes",
        ),
        SignalRow(
            row={
                "signal_id": "s2",
                "timestamp": (now + timedelta(minutes=1)).isoformat(),
                "market_id": "m1",
                "outcome": "Yes",
                "mid": "0.45",
                "bid": "0.44",
                "ask": "0.46",
                "orderbook_timestamp": (now+timedelta(minutes=1)).isoformat(),
                "book_bid_size": "100",
            },
            timestamp=now + timedelta(minutes=1),
            market_id="m1",
            outcome="Yes",
        ),
    ]

    observations = build_markout_observations(signals)
    one_minute = next(row for row in observations if row["signal_id"] == "s1" and row["horizon"] == "1m")

    assert one_minute["status"] == "observed"
    assert one_minute["markout"] == "0.04"
    assert one_minute["markout_type"] == "tradable_bid_ask"
    assert one_minute["shadow_markout"] == "0.05"
    assert one_minute["evidence_type"] == "tradable_candidate"
