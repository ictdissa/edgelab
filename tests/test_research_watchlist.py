from __future__ import annotations

from datetime import UTC, datetime, timedelta

from edgelab.config.settings import Settings
from scripts.generate_research_watchlist import build_watchlist
from scripts.update_shadow_clv import SnapshotSignal, build_shadow_observations


def test_watchlist_rows_are_research_only_not_tradeable() -> None:
    rows = [
        {
            "signal_id": "s1",
            "timestamp": "2026-05-19T00:00:00+00:00",
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
            "risk_decision": "REJECTED",
        }
    ]

    watchlist = build_watchlist(
        rows,
        Settings(MIN_ADJUSTED_EDGE=0.01, MIN_LIQUIDITY=1, MAX_SPREAD=0.08),
        limit=10,
    )

    assert watchlist
    assert all(row["RESEARCH_ONLY"] == "true" for row in watchlist)
    assert all(row["TRADEABLE"] == "false" for row in watchlist)


def test_shadow_clv_does_not_mix_with_tradable_clv() -> None:
    entry_time = datetime(2026, 5, 19, tzinfo=UTC)
    follow_up_time = entry_time + timedelta(minutes=5)
    watchlist_signal = SnapshotSignal(
        signal_id="watch:1",
        timestamp=entry_time,
        category="crypto",
        market_id="m1",
        outcome="Yes",
        action="HOLD",
        fair_probability=0.60,
        bid=0.49,
        ask=0.50,
        mid=0.495,
        adjusted_edge=0.03,
        source_snapshot="",
        side_hint="BUY_YES",
        entry_kind="RESEARCH_WATCHLIST",
        research_only=True,
    )
    paper_candidate = SnapshotSignal(
        signal_id="paper:1",
        timestamp=entry_time,
        category="crypto",
        market_id="m1",
        outcome="Yes",
        action="BUY_YES",
        fair_probability=0.60,
        bid=0.49,
        ask=0.50,
        mid=0.495,
        adjusted_edge=0.03,
        source_snapshot="",
        side_hint="BUY_YES",
        entry_price_override=0.50,
        entry_price_source_override="ASK",
        entry_kind="PAPER_CANDIDATE",
        research_only=False,
    )
    follow_up = SnapshotSignal(
        signal_id="followup",
        timestamp=follow_up_time,
        category="crypto",
        market_id="m1",
        outcome="Yes",
        action="HOLD",
        fair_probability=0.55,
        bid=0.54,
        ask=0.56,
        mid=0.55,
        adjusted_edge=0.0,
        source_snapshot="",
    )

    observations = build_shadow_observations(
        [watchlist_signal, paper_candidate],
        [follow_up],
    )
    observed_5m = [row for row in observations if row["horizon"] == "5m" and row["status"] == "observed"]

    watch_row = next(row for row in observed_5m if row["signal_id"] == "watch:1")
    paper_row = next(row for row in observed_5m if row["signal_id"] == "paper:1")
    assert watch_row["shadow_clv"]
    assert watch_row["tradable_clv"] == ""
    assert paper_row["tradable_clv"]
    assert paper_row["shadow_clv"] == ""
