"""Evidence validation and serialized ledger/CLV updates, without any network."""

import csv
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta

import pytest

from edgelab.research.clv import PaperTrade, SnapshotSignal
from scripts.analyze_clv_robustness import summarize_group
from scripts.run_paper_trader import LEDGER_COLUMNS, append_ledger_rows
from scripts.update_clv import (
    load_snapshot_signals_from_dirs,
    observe_horizon,
    read_ledger_rows,
    update_single_leg_ledger,
    write_ledger_rows,
)

NOW = datetime(2026, 10, 13, 12, tzinfo=UTC)


def ledger_row(key):
    row = dict.fromkeys(LEDGER_COLUMNS, "")
    row.update(
        paper_trade_id=key,
        timestamp=NOW.isoformat(),
        market_id="m",
        event_id="e",
        token_id="yes",
        outcome="Yes",
        side="BUY_YES",
        execution_price=".4",
        size="10",
        clv_status="pending",
    )
    return row


@pytest.mark.parametrize(
    "bid,ask", [(float("nan"), 0.6), (float("inf"), 0.6), (1.2, 0.6), (0.7, 0.6), (-0.1, 0.6)]
)
def test_invalid_followup_cannot_be_constructed(bid, ask):
    with pytest.raises(ValueError):
        SnapshotSignal(NOW, "sports_1x2", "m", "Yes", None, None, bid, ask)


def test_missing_token_cannot_be_reported_as_tradable():
    t = PaperTrade("t", NOW, "sports_1x2", "m", "Yes", "BUY_YES", 0.4)
    result = observe_horizon(t, [], "5m", timedelta(minutes=5))
    assert result["status"] == "unverified_contract_identity" and result["clv"] == ""


def test_snapshot_loader_quarantines_invalid_rows(tmp_path):
    folder = tmp_path / "iteration_0001"
    folder.mkdir()
    path = folder / "signals.csv"
    with path.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=["timestamp", "bid", "ask", "market_id", "outcome"])
        writer.writeheader()
        writer.writerow(
            dict(timestamp=NOW.isoformat(), bid="nan", ask=".5", market_id="m", outcome="Yes")
        )
    quarantine = tmp_path / "bad.csv"
    assert load_snapshot_signals_from_dirs([tmp_path], quarantine) == []
    assert "nonfinite" in quarantine.read_text()


def test_stale_clv_writer_preserves_new_append(tmp_path):
    path = tmp_path / "ledger.csv"
    append_ledger_rows(path, [ledger_row("first")])
    old = read_ledger_rows(path)
    append_ledger_rows(path, [ledger_row("second")])
    old[0]["clv_5m"] = ".1"
    write_ledger_rows(path, old)
    saved = read_ledger_rows(path)
    assert {r["paper_trade_id"] for r in saved} == {"first", "second"}
    assert saved[0]["clv_5m"] == ".1"


def test_concurrent_appends_and_clv_updates_do_not_lose_trades(tmp_path):
    path = tmp_path / "ledger.csv"

    def work(i):
        append_ledger_rows(path, [ledger_row(str(i))])
        update_single_leg_ledger(path, [])

    with ThreadPoolExecutor(max_workers=6) as pool:
        list(pool.map(work, range(24)))
    assert len(read_ledger_rows(path)) == 24
    with sqlite3.connect(path.with_suffix(".clv.sqlite")) as conn:
        assert conn.execute("SELECT COUNT(*) FROM observations").fetchone()[0] == 24 * 3


def test_block_estimate_and_interval_are_same_estimand():
    rows = [dict(paper_trade_id=f"busy-{i}", event_id="busy", clv=".1") for i in range(100)]
    rows += [dict(paper_trade_id=f"quiet-{i}", event_id=f"q-{i}", clv="-.02") for i in range(39)]
    result = summarize_group("overall", "all", rows)
    assert float(result["average_clv"]) == pytest.approx(-0.017)
    assert float(result["trade_weighted_clv"]) > 0
    assert float(result["bootstrap_ci_high"]) < 0
    assert result["verdict"] == "NON_POSITIVE_MEAN"
    repeated = summarize_group("overall", "all", rows + rows)
    assert repeated == result


def test_same_event_across_days_is_one_block():
    rows = [
        dict(
            paper_trade_id=str(i),
            event_id="same",
            clv=".1",
            entry_timestamp=f"2026-10-{i + 1:02}T00:00:00Z",
        )
        for i in range(20)
    ]
    assert summarize_group("overall", "all", rows)["independent_blocks"] == "1"


def test_net_markout_uses_frozen_entry_costs_not_current_environment(monkeypatch):
    t = PaperTrade(
        "t", NOW, "sports_1x2", "m", "Yes", "BUY_YES", 0.4, "yes", 10, 0.01, 0.02, 20, 25
    )
    later = NOW + timedelta(minutes=5)
    s = SnapshotSignal(
        later, "sports_1x2", "m", "Yes", 0.6, 0.6, 0.59, 0.61, "yes", later, 100, 100
    )
    a = observe_horizon(t, [s], "5m", timedelta(minutes=5))
    monkeypatch.setenv("FEE_BPS", "9999")
    monkeypatch.setenv("DEFAULT_SLIPPAGE_BPS", "9999")
    b = observe_horizon(t, [s], "5m", timedelta(minutes=5))
    assert a["net_markout_estimate"] == b["net_markout_estimate"]
    assert float(a["net_markout_estimate"]) == pytest.approx(0.19 - 0.003 - 0.59 * 0.002 - 0.0025)
