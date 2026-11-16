from __future__ import annotations

from datetime import UTC, datetime

from edgelab.strategies.relative_value_graph import mock_relative_value_inputs
from scripts.collect_research_snapshots import (
    build_current_relative_value_rows,
    mark_relative_value_source,
    relative_value_freshness_rows,
    relative_value_signal_rows,
)


def test_current_relative_value_rows_are_marked_live_snapshot() -> None:
    markets, orderbooks = mock_relative_value_inputs()

    rows = build_current_relative_value_rows(markets, orderbooks)

    assert rows
    assert {row["relative_value_source"] for row in rows} == {"live_snapshot"}
    assert all(row.get("relation_type") for row in rows)
    assert all(row.get("status") for row in rows)


def test_relative_value_snapshot_source_flows_to_signals_and_freshness() -> None:
    timestamp = datetime(2026, 5, 20, tzinfo=UTC)
    rows = [
        {
            "relation_type": "implication",
            "parent_market": "parent",
            "child_market": "child",
            "involved_outcomes": "child:Yes | parent:Yes",
            "theoretical_violation": "0.03",
            "executable_violation": "0.02",
            "available_liquidity": "10",
            "status": "EXECUTABLE_PACKAGE",
            "relative_value_source": "live_snapshot",
        }
    ]

    signals = relative_value_signal_rows(rows, timestamp)
    freshness = relative_value_freshness_rows(rows, timestamp)

    assert signals[0]["source"] == "live_snapshot"
    assert freshness[0]["source"] == "live_snapshot:1"
    assert freshness[0]["status"] == "fresh"


def test_static_relative_value_rows_are_explicitly_marked_as_fallback() -> None:
    rows = mark_relative_value_source([{"status": "NO_VIOLATION"}], "static_report")

    assert rows == [{"status": "NO_VIOLATION", "relative_value_source": "static_report"}]
