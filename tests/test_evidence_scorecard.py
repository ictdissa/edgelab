from __future__ import annotations

import csv
import json
from pathlib import Path

from scripts.collect_research_snapshots import parse_categories, write_snapshot_files
from scripts.run_evidence_scorecard import (
    READINESS_NOT_READY_NO_DATA,
    READINESS_NOT_READY_NO_CLV,
    READINESS_WEAK_OR_NEGATIVE,
    build_scorecard,
    load_snapshot_records,
    render_scorecard,
)


def test_parse_categories_accepts_relative_value_alias() -> None:
    assert parse_categories("sports,crypto,internal_relative_value") == [
        "sports",
        "crypto",
        "relative_value",
    ]


def test_snapshot_writer_emits_required_iteration_files(tmp_path: Path) -> None:
    snapshot = {
        "snapshot_version": 1,
        "timestamp": "2026-05-19T00:00:00+00:00",
        "categories": ["crypto"],
        "category_summaries": {},
        "totals": {
            "markets_scanned": 1,
            "matched_markets": 1,
            "apparent_edges": 1,
            "risk_approved_paper_signals": 0,
            "average_adjusted_edge": 0.02,
            "valid_books": 1,
        },
        "signals": [
            {
                "signal_id": "s1",
                "timestamp": "2026-05-19T00:00:00+00:00",
                "category": "crypto",
                "market_id": "m1",
                "event_id": "e1",
                "outcome": "Yes",
                "action": "HOLD",
                "adjusted_edge": "0.02",
                "available_size": "20",
                "risk_approved": "False",
            }
        ],
        "freshness": [
            {
                "category": "crypto",
                "source": "polymarket_markets",
                "rows": "1",
                "latest_timestamp": "2026-05-19T00:00:00+00:00",
                "age_seconds": "0",
                "status": "fresh",
                "reason": "ok",
            }
        ],
        "match_audit": [{"market_id": "m1", "decision": "MATCH", "score": "1.0"}],
    }

    write_snapshot_files(tmp_path, snapshot)

    assert (tmp_path / "signals.csv").exists()
    assert (tmp_path / "freshness.csv").exists()
    assert (tmp_path / "match_audit.csv").exists()
    assert (tmp_path / "summary.json").exists()


def test_scorecard_reports_no_data_without_snapshots(tmp_path: Path) -> None:
    scorecard = build_scorecard(load_snapshot_records(tmp_path / "snapshots"), tmp_path / "ledger.csv")

    assert scorecard.total_snapshots == 0
    assert scorecard.readiness_classification == READINESS_NOT_READY_NO_DATA


def test_scorecard_requires_clv_for_risk_approved_rows(tmp_path: Path) -> None:
    for index in range(1, 21):
        snapshot_dir = tmp_path / "snapshots" / f"iteration_{index:04d}"
        snapshot_dir.mkdir(parents=True)
        (snapshot_dir / "summary.json").write_text(
            json.dumps(
                {
                    "timestamp": "2026-05-19T00:00:00+00:00",
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
        with (snapshot_dir / "signals.csv").open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(
                handle,
                fieldnames=[
                    "signal_id",
                    "category",
                    "market_id",
                    "action",
                    "adjusted_edge",
                    "liquidity",
                    "bid",
                    "ask",
                    "risk_approved",
                    "risk_decision",
                    "rejection_reason",
                ],
            )
            writer.writeheader()
            writer.writerow(
                {
                    "signal_id": "s1",
                    "category": "crypto",
                    "market_id": "m1",
                    "action": "BUY_YES",
                    "adjusted_edge": "0.03",
                    "liquidity": "50",
                    "bid": "0.45",
                    "ask": "0.47",
                    "risk_approved": "True",
                    "risk_decision": "APPROVED",
                    "rejection_reason": "",
                }
            )
    ledger = tmp_path / "ledger.csv"
    with ledger.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=["market_id", "side", "execution_price", "clv_status"],
        )
        writer.writeheader()
        writer.writerow(
            {
                "market_id": "m1",
                "side": "BUY_YES",
                "execution_price": "0.47",
                "clv_status": "pending",
            }
        )

    records = load_snapshot_records(tmp_path / "snapshots")
    scorecard = build_scorecard(records, ledger)

    assert scorecard.readiness_classification == READINESS_NOT_READY_NO_CLV
    assert "real money remains blocked" in render_scorecard(scorecard, records, tmp_path / "snapshots").lower()


def test_scorecard_reads_clv_from_ledger(tmp_path: Path) -> None:
    snapshot_dir = tmp_path / "snapshots" / "iteration_0001"
    snapshot_dir.mkdir(parents=True)
    (snapshot_dir / "summary.json").write_text(
        json.dumps(
            {
                "timestamp": "2026-05-19T00:00:00+00:00",
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
    with (snapshot_dir / "signals.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=["market_id", "category", "adjusted_edge", "risk_approved"])
        writer.writeheader()
        writer.writerow({"market_id": "m1", "category": "crypto", "adjusted_edge": "0.03", "risk_approved": "True"})
    ledger = tmp_path / "ledger.csv"
    with ledger.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=["market_id", "side", "execution_price", "clv_status", "clv_5m", "clv_30m", "clv_1h"],
        )
        writer.writeheader()
        writer.writerow(
            {
                "market_id": "m1",
                "side": "BUY_YES",
                "execution_price": "0.42",
                "clv_status": "partial",
                "clv_5m": "0.01",
                "clv_30m": "0.02",
                "clv_1h": "",
            }
        )

    scorecard = build_scorecard(load_snapshot_records(tmp_path / "snapshots"), ledger)

    assert scorecard.average_clv == 0.01
    assert scorecard.clv_by_category["crypto"]["count"] == 1


def test_scorecard_does_not_promote_positive_average_with_negative_median(tmp_path: Path) -> None:
    for index in range(1, 21):
        snapshot_dir = tmp_path / "snapshots" / f"iteration_{index:04d}"
        snapshot_dir.mkdir(parents=True)
        (snapshot_dir / "summary.json").write_text(
            json.dumps(
                {
                    "timestamp": "2026-05-19T00:00:00+00:00",
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
        with (snapshot_dir / "signals.csv").open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=["market_id", "category", "adjusted_edge", "risk_approved"])
            writer.writeheader()
            writer.writerow({"market_id": "m1", "category": "crypto", "adjusted_edge": "0.03", "risk_approved": "True"})

    ledger = tmp_path / "ledger.csv"
    with ledger.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=["market_id", "side", "execution_price", "clv_status", "clv_5m"],
        )
        writer.writeheader()
        for index in range(100):
            writer.writerow(
                {
                    "market_id": "m1",
                    "side": "BUY_YES",
                    "execution_price": "0.42",
                    "clv_status": "partial",
                    "clv_5m": "0.1" if index < 10 else "-0.0005",
                }
            )

    scorecard = build_scorecard(load_snapshot_records(tmp_path / "snapshots"), ledger)

    assert scorecard.average_clv is not None and scorecard.average_clv > 0
    assert scorecard.median_clv is not None and scorecard.median_clv < 0
    assert scorecard.clv_win_rate == 0.1
    assert scorecard.readiness_classification == READINESS_WEAK_OR_NEGATIVE
