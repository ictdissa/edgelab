from __future__ import annotations

import csv
import json
from datetime import UTC, datetime
from pathlib import Path

from edgelab.config.settings import Settings
from scripts.run_universe_coverage import (
    recommendation_for,
    scan_universe_coverage,
    sports_failure_mode,
    write_outputs,
)


def test_universe_coverage_handles_missing_data(tmp_path: Path) -> None:
    rows = scan_universe_coverage(
        live_data_dir=tmp_path / "missing-live",
        signals_path=tmp_path / "missing-signals.csv",
        match_audit_path=tmp_path / "missing-matches.csv",
        relative_value_path=tmp_path / "missing-rv.csv",
        settings=_settings(),
    )

    sports = next(row for row in rows if row.category == "sports")
    rv = next(row for row in rows if row.category == "internal_relative_value")

    assert sports.polymarket_markets_found == 0
    assert sports.recommendation == "IGNORE"
    assert sports_failure_mode(sports) == "no-market"
    assert rv.missing_data_explanation == "No relative-value output was available."


def test_universe_coverage_scores_local_crypto_snapshot(tmp_path: Path) -> None:
    live = tmp_path / "live"
    live.mkdir()
    now = datetime(2026, 5, 19, tzinfo=UTC).isoformat()
    (live / "crypto_markets.json").write_text(
        json.dumps(
            [
                {
                    "market_id": "btc-100k",
                    "event_id": "btc",
                    "category": "crypto_threshold",
                    "title": "Will Bitcoin hit $100k?",
                    "outcomes": ["Yes", "No"],
                    "token_ids": ["yes", "no"],
                    "resolution_source": "index",
                }
            ]
        ),
        encoding="utf-8",
    )
    (live / "crypto_orderbooks.json").write_text(
        json.dumps(
            [
                {
                    "market_id": "btc-100k",
                    "token_id": "yes",
                    "timestamp": now,
                    "bids": [{"price": 0.48, "size": 50}],
                    "asks": [{"price": 0.50, "size": 50}],
                }
            ]
        ),
        encoding="utf-8",
    )
    (live / "crypto_anchors.json").write_text(
        json.dumps(
            [
                {
                    "source_id": "spot:btc",
                    "event_id": "btc",
                    "source": "spot",
                    "category": "crypto_threshold",
                    "outcome": "Yes",
                    "probability": 0.55,
                    "timestamp": now,
                    "metadata": {},
                }
            ]
        ),
        encoding="utf-8",
    )
    signals_path = tmp_path / "signals.csv"
    with signals_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=["market_id", "category", "action", "adjusted_edge", "risk_approved", "fair_value_source"],
        )
        writer.writeheader()
        writer.writerow(
            {
                "market_id": "btc-100k",
                "category": "crypto_threshold",
                "action": "BUY_YES",
                "adjusted_edge": "0.02",
                "risk_approved": "True",
                "fair_value_source": "crypto_lognormal_threshold",
            }
        )
    match_path = tmp_path / "matches.csv"
    with match_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=["market_id", "decision", "rejection_reason"])
        writer.writeheader()
        writer.writerow({"market_id": "btc-100k", "decision": "MATCH", "rejection_reason": ""})

    rows = scan_universe_coverage(
        live_data_dir=live,
        signals_path=signals_path,
        match_audit_path=match_path,
        relative_value_path=tmp_path / "missing-rv.csv",
        settings=_settings(),
    )
    crypto = next(row for row in rows if row.category == "crypto")

    assert crypto.markets_with_valid_clob_books == 1
    assert crypto.fair_value_source_available
    assert crypto.matched_markets == 1
    assert crypto.risk_approved_paper_signals == 1
    assert crypto.recommendation == "PAPER_TRADE_CANDIDATE"


def test_universe_coverage_writes_report_and_csv(tmp_path: Path) -> None:
    rows = scan_universe_coverage(
        live_data_dir=tmp_path / "missing-live",
        signals_path=tmp_path / "missing-signals.csv",
        match_audit_path=tmp_path / "missing-matches.csv",
        relative_value_path=tmp_path / "missing-rv.csv",
        settings=_settings(),
    )

    report = tmp_path / "universe_coverage.md"
    csv_path = tmp_path / "universe_coverage.csv"
    write_outputs(report, csv_path, rows)

    assert report.exists()
    assert csv_path.exists()
    assert "research_score =" in report.read_text(encoding="utf-8")


def test_recommendation_prefers_snapshot_collection_before_paper_trading() -> None:
    assert (
        recommendation_for(
            markets_found=10,
            fair_value_available=True,
            liquid_markets=8,
            possible_signals=2,
            risk_approved=0,
            research_score=60,
        )
        == "COLLECT_SNAPSHOTS"
    )


def _settings() -> Settings:
    return Settings(MIN_LIQUIDITY=10, MAX_SPREAD=0.08)
