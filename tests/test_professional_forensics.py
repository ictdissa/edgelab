from __future__ import annotations

import csv
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

from scripts.analyze_observation_independence import build_independence_report
from scripts.audit_markout_computation import audit_markout_rows
from scripts.audit_relative_value_empirical_status import (
    STATUS_LIVE_EXECUTABLE_PACKAGE,
    STATUS_STATIC_RESEARCH_ONLY,
    classify_candidate,
)
from scripts.build_professional_quant_research_report import build_report
from scripts.run_markout_analysis import SignalRow, build_markout_observations


def test_buy_and_sell_markouts_use_conservative_future_bid_ask() -> None:
    now = datetime(2026, 5, 22, tzinfo=UTC)
    signals = [
        SignalRow(
            row={"signal_id": "buy", "market_id": "m1", "outcome": "Yes", "best_side": "BUY_YES", "ask": "0.40", "final_decision": "PAPER_CANDIDATE"},
            timestamp=now,
            market_id="m1",
            outcome="Yes",
        ),
        SignalRow(
            row={"signal_id": "sell", "market_id": "m2", "outcome": "Yes", "best_side": "SELL_YES", "bid": "0.60", "final_decision": "PAPER_CANDIDATE"},
            timestamp=now,
            market_id="m2",
            outcome="Yes",
        ),
        SignalRow(
            row={"signal_id": "buy_follow", "market_id": "m1", "outcome": "Yes", "bid": "0.43", "ask": "0.45", "mid": "0.44"},
            timestamp=now + timedelta(minutes=1),
            market_id="m1",
            outcome="Yes",
        ),
        SignalRow(
            row={"signal_id": "sell_follow", "market_id": "m2", "outcome": "Yes", "bid": "0.54", "ask": "0.56", "mid": "0.55"},
            timestamp=now + timedelta(minutes=1),
            market_id="m2",
            outcome="Yes",
        ),
    ]

    for signal in signals:
        signal.row.update(orderbook_timestamp=signal.timestamp.isoformat(), available_size="10",
                          book_bid_size="100", book_ask_size="100")
    rows = build_markout_observations(signals)
    buy = next(row for row in rows if row["signal_id"] == "buy" and row["horizon"] == "1m")
    sell = next(row for row in rows if row["signal_id"] == "sell" and row["horizon"] == "1m")

    assert buy["later_value_source"] == "FUTURE_BID"
    assert buy["markout"] == "0.03"
    assert buy["shadow_markout"] == "0.04"
    assert sell["later_value_source"] == "FUTURE_ASK"
    assert sell["markout"] == "0.04"
    assert sell["shadow_markout"] == "0.05"


def test_midpoint_followup_is_shadow_not_executable() -> None:
    now = datetime(2026, 5, 22, tzinfo=UTC)
    rows = build_markout_observations(
        [
            SignalRow(
                row={"signal_id": "buy", "market_id": "m1", "outcome": "Yes", "best_side": "BUY_YES", "ask": "0.40", "final_decision": "PAPER_CANDIDATE"},
                timestamp=now,
                market_id="m1",
                outcome="Yes",
            ),
            SignalRow(
                row={"signal_id": "follow", "market_id": "m1", "outcome": "Yes", "mid": "0.50", "orderbook_timestamp": (now+timedelta(minutes=1)).isoformat()},
                timestamp=now + timedelta(minutes=1),
                market_id="m1",
                outcome="Yes",
            ),
        ]
    )

    one_minute = next(row for row in rows if row["signal_id"] == "buy" and row["horizon"] == "1m")

    assert one_minute["status"] == "shadow_observed"
    assert one_minute["markout"] == ""
    assert one_minute["shadow_markout"] == "0.1"
    assert one_minute["markout_type"] == "midpoint_shadow"


def test_token_mismatch_invalidates_markout_audit() -> None:
    now = datetime(2026, 5, 22, tzinfo=UTC)
    markouts = [
        {
            "signal_id": "s1",
            "timestamp": now.isoformat(),
            "market_id": "m1",
            "outcome": "Yes",
            "side": "BUY_YES",
            "horizon": "1m",
            "status": "observed",
            "markout_type": "tradable_bid_ask",
            "entry_price": "0.40",
            "later_timestamp": (now + timedelta(minutes=1)).isoformat(),
            "later_value": "0.45",
            "later_value_source": "FUTURE_BID",
            "markout": "0.05",
        }
    ]
    lookup = {
        ("s1", "m1", "Yes"): {"signal_id": "s1", "timestamp": now.isoformat(), "market_id": "m1", "outcome": "Yes", "token_id": "t1", "ask": "0.40"},
        ((now + timedelta(minutes=1)).isoformat(), "m1", "Yes"): {
            "timestamp": (now + timedelta(minutes=1)).isoformat(),
            "market_id": "m1",
            "outcome": "Yes",
            "token_id": "t2",
            "bid": "0.45",
        },
    }

    audit = audit_markout_rows(markouts, lookup)

    assert any(row["violation_type"] == "TOKEN_MISMATCH" and row["severity"] == "VIOLATION" for row in audit)


def test_static_relative_value_source_cannot_be_executable() -> None:
    row = {
        "candidate_id": "rv1",
        "relative_value_source": "static_report",
        "empirical_evidence_eligible": "false",
        "status": "EXECUTABLE_PACKAGE",
        "legs_json": json.dumps(
            [
                {"market_id": "m1", "token_id": "t1", "bid": "0.4", "ask": "0.41", "data_age_seconds": "1", "size_available": "100"},
                {"market_id": "m2", "token_id": "t2", "bid": "0.5", "ask": "0.51", "data_age_seconds": "1", "size_available": "100"},
            ]
        ),
        "package_entry_cost": "0.91",
        "package_expected_payoff": "1",
        "risk_reasons": "fees included; legging risk flagged",
    }

    classified = classify_candidate(row, set())

    assert classified["empirical_status"] == STATUS_STATIC_RESEARCH_ONLY


def test_live_relative_value_requires_all_legs_priced_fresh_liquid_and_reconstructable() -> None:
    row = {
        "candidate_id": "rv1",
        "relative_value_source": "live_snapshot",
        "empirical_evidence_eligible": "true",
        "status": "EXECUTABLE_PACKAGE",
        "legs_json": json.dumps(
            [
                {"market_id": "m1", "token_id": "t1", "bid": "0.4", "ask": "0.41", "data_age_seconds": "1", "size_available": "100"},
                {"market_id": "m2", "token_id": "t2", "bid": "0.5", "ask": "0.51", "data_age_seconds": "1", "size_available": "100"},
            ]
        ),
        "package_entry_cost": "0.91",
        "package_expected_payoff": "1",
        "risk_reasons": "fees included; legging risk flagged",
    }

    classified = classify_candidate(row, set())

    assert classified["empirical_status"] == STATUS_LIVE_EXECUTABLE_PACKAGE


def test_repeated_multi_horizon_observations_not_counted_as_unique_fills(tmp_path: Path) -> None:
    experiment = tmp_path / "exp"
    reports = experiment / "reports"
    snapshots = experiment / "snapshots" / "iteration_0001"
    ledger_dir = experiment / "paper_trades"
    reports.mkdir(parents=True)
    snapshots.mkdir(parents=True)
    ledger_dir.mkdir(parents=True)
    with (reports / "markout_observations.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=["signal_id", "market_id", "token_id", "timestamp", "later_timestamp", "horizon", "status", "category", "fair_value_source"])
        writer.writeheader()
        writer.writerow({"signal_id": "s1", "market_id": "m1", "timestamp": "t0", "later_timestamp": "t1", "horizon": "1m", "status": "observed", "category": "crypto", "fair_value_source": "src"})
        writer.writerow({"signal_id": "s1", "market_id": "m1", "timestamp": "t0", "later_timestamp": "t2", "horizon": "5m", "status": "observed", "category": "crypto", "fair_value_source": "src"})
    with (ledger_dir / "ledger.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=["paper_trade_id"])
        writer.writeheader()
        writer.writerow({"paper_trade_id": "pt1"})
    (experiment / "snapshots" / "snapshot_index.csv").write_text("iteration\n1\n", encoding="utf-8")

    rows, _report = build_independence_report(experiment)

    metrics = {row["metric"]: row["value"] for row in rows if row["group"] == "overall"}
    assert metrics["raw_markout_rows"] == 2
    assert metrics["unique_paper_fills"] == 1
    assert metrics["effective_sample_unique_fill"] == 1


def test_professional_report_blocks_profitability_without_settlement_or_calibration(tmp_path: Path) -> None:
    experiment = tmp_path / "exp"
    reports = experiment / "reports"
    snapshots = experiment / "snapshots" / "iteration_0001"
    reports.mkdir(parents=True)
    snapshots.mkdir(parents=True)
    (reports / "evidence_scorecard.md").write_text("- readiness classification: `NEEDS_DATA_QUALITY_FIXES`\n", encoding="utf-8")

    report = build_report(experiment)

    assert "Fair-value correctness is not proven" in report
    assert "not a live trading system" in report
    assert "profitable Polymarket trading bot" in report
