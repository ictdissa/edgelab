from __future__ import annotations

import csv
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

from scripts.analyze_signal_funnel import (
    load_snapshot_signals_from_dirs as load_funnel_signals_from_dirs,
)
from scripts.run_evidence_scorecard import load_snapshot_records_from_dirs
from scripts.update_clv import load_snapshot_signals_from_dirs, update_ledger_clv


def write_signal_snapshot(
    root: Path,
    iteration: int,
    timestamp: datetime,
    fair_probability: str,
    mid: str = "0.50",
    bid: str = "0.49",
    ask: str = "0.51",
) -> None:
    iteration_dir = root / f"iteration_{iteration:04d}"
    iteration_dir.mkdir(parents=True)
    (iteration_dir / "summary.json").write_text(
        json.dumps(
            {
                "timestamp": timestamp.isoformat(),
                "categories": ["crypto"],
                "totals": {
                    "markets_scanned": 1,
                    "matched_markets": 1,
                    "apparent_edges": 1,
                    "risk_approved_paper_signals": 1,
                },
            }
        ),
        encoding="utf-8",
    )
    with (iteration_dir / "signals.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=[
                "timestamp",
                "category",
                "market_id",
                "outcome",
                "token_id",
                "fair_probability",
                "mid",
                "bid",
                "ask",
                "orderbook_timestamp",
                "book_bid_size",
                "book_ask_size",
            ],
        )
        writer.writeheader()
        writer.writerow(
            {
                "timestamp": timestamp.isoformat(),
                "category": "crypto",
                "market_id": "m1",
                "outcome": "Yes",
                "token_id": "m1-yes",
                "fair_probability": fair_probability,
                "mid": mid,
                "bid": bid,
                "ask": ask,
                "orderbook_timestamp": timestamp.isoformat(),
                "book_bid_size": "100",
                "book_ask_size": "100",
            }
        )


def test_clv_can_use_follow_up_from_extra_snapshot_dir(tmp_path: Path) -> None:
    entry_time = datetime(2026, 5, 20, 10, 0, tzinfo=UTC)
    primary = tmp_path / "primary"
    follow_up = tmp_path / "follow_up"
    write_signal_snapshot(primary, 1, entry_time, "0.50")
    write_signal_snapshot(
        follow_up, 1, entry_time + timedelta(minutes=6), "0.56", mid="0.56", bid="0.55", ask="0.57"
    )

    ledger = [
        {
            "paper_trade_id": "pt1",
            "timestamp": entry_time.isoformat(),
            "category": "crypto",
            "market_id": "m1",
            "outcome": "Yes",
            "token_id": "m1-yes",
            "side": "BUY_YES",
            "execution_price": "0.51",
            "execution_price_source": "ASK",
            "size": "1",
            "fair_probability_at_entry": "0.50",
            "adjusted_edge_at_entry": "0.01",
            "bid_at_entry": "0.49",
            "ask_at_entry": "0.51",
            "spread_at_entry": "0.02",
            "liquidity_at_entry": "10",
            "fair_value_source": "unit_test",
            "risk_reason": "",
            "clv_5m": "",
            "clv_30m": "",
            "clv_1h": "",
            "clv_status": "pending",
        }
    ]

    signals = load_snapshot_signals_from_dirs([primary, follow_up])
    updated, observations = update_ledger_clv(ledger, signals)

    assert updated[0]["clv_5m"] == "0.04"
    assert updated[0]["clv_status"] == "partial"
    assert any(row["horizon"] == "5m" and row["status"] == "observed" for row in observations)


def test_evidence_scorecard_can_aggregate_extra_snapshot_dirs(tmp_path: Path) -> None:
    first = tmp_path / "first"
    second = tmp_path / "second"
    write_signal_snapshot(first, 1, datetime(2026, 5, 20, 10, 0, tzinfo=UTC), "0.50")
    write_signal_snapshot(second, 1, datetime(2026, 5, 20, 10, 6, tzinfo=UTC), "0.56")

    records = load_snapshot_records_from_dirs([first, second])

    assert len(records) == 2
    assert sum(len(record.signals) for record in records) == 2


def test_signal_funnel_can_aggregate_extra_snapshot_dirs(tmp_path: Path) -> None:
    first = tmp_path / "first"
    second = tmp_path / "second"
    write_signal_snapshot(first, 1, datetime(2026, 5, 20, 10, 0, tzinfo=UTC), "0.50")
    write_signal_snapshot(second, 1, datetime(2026, 5, 20, 10, 6, tzinfo=UTC), "0.56")

    rows = load_funnel_signals_from_dirs([first, second])

    assert len(rows) == 2
