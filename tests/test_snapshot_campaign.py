from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from scripts import run_snapshot_campaign


def _snapshot(timestamp: datetime, failures: dict[str, str] | None = None) -> dict[str, object]:
    return {
        "snapshot_version": 1,
        "timestamp": timestamp.isoformat(),
        "categories": ["crypto", "relative_value"],
        "category_summaries": {},
        "totals": {
            "markets_scanned": 2,
            "matched_markets": 1,
            "apparent_edges": 0,
            "risk_approved_paper_signals": 0,
            "average_adjusted_edge": None,
            "valid_books": 1,
        },
        "signals": [],
        "freshness": [],
        "match_audit": [],
        "relative_value_opportunities": [],
        "failures": failures or {},
    }


def test_campaign_runner_writes_resumable_state(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(run_snapshot_campaign, "build_offline_snapshot", lambda categories, started: _snapshot(started))

    result = run_snapshot_campaign.run_campaign(
        categories=["crypto", "relative_value"],
        output_dir=tmp_path / "snapshots",
        iterations=1,
        interval_seconds=0,
        summary_output=tmp_path / "summary.md",
    )

    state = json.loads((tmp_path / "snapshots" / "campaign_state.json").read_text(encoding="utf-8"))
    manifest = json.loads((tmp_path / "snapshots" / "campaign_manifest.json").read_text(encoding="utf-8"))
    assert state["completed_iterations"] == 1
    assert state["run_id"].startswith("run_")
    assert manifest["run_id"] == state["run_id"]
    assert manifest["live_trading_enabled"] is False
    assert result["snapshots_collected"] == 1
    assert (tmp_path / "snapshots" / "iteration_0001" / "relative_value_candidates.csv").exists()
    assert (tmp_path / "snapshots" / "iteration_0001" / "edge_sanity_warnings.csv").exists()


def test_campaign_resume_reuses_run_id(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(run_snapshot_campaign, "build_offline_snapshot", lambda categories, started: _snapshot(started))

    run_snapshot_campaign.run_campaign(
        categories=["crypto"],
        output_dir=tmp_path / "snapshots",
        iterations=1,
        interval_seconds=0,
        run_id="test-run",
        summary_output=tmp_path / "summary.md",
    )
    run_snapshot_campaign.run_campaign(
        categories=["crypto"],
        output_dir=tmp_path / "snapshots",
        iterations=2,
        interval_seconds=0,
        resume=True,
        summary_output=tmp_path / "summary.md",
    )

    state = json.loads((tmp_path / "snapshots" / "campaign_state.json").read_text(encoding="utf-8"))
    index = (tmp_path / "snapshots" / "snapshot_index.csv").read_text(encoding="utf-8")
    assert state["run_id"] == "test-run"
    assert "test-run" in index


def test_campaign_migrates_old_snapshot_index_schema(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(run_snapshot_campaign, "build_offline_snapshot", lambda categories, started: _snapshot(started))
    snapshot_dir = tmp_path / "snapshots"
    snapshot_dir.mkdir()
    (snapshot_dir / "snapshot_index.csv").write_text(
        "iteration,timestamp,categories\n1,2026-05-20T00:00:00+00:00,crypto\n",
        encoding="utf-8",
    )

    run_snapshot_campaign.run_campaign(
        categories=["crypto"],
        output_dir=snapshot_dir,
        iterations=1,
        interval_seconds=0,
        run_id="schema-test",
        summary_output=tmp_path / "summary.md",
    )

    header = (snapshot_dir / "snapshot_index.csv").read_text(encoding="utf-8").splitlines()[0]
    assert header.startswith("run_id,iteration,timestamp")
    assert "fully_priced_relative_value_packages" in header


def test_campaign_records_category_failure_without_stopping(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(
        run_snapshot_campaign,
        "build_offline_snapshot",
        lambda categories, started: _snapshot(started, {"crypto": "provider unavailable"}),
    )

    run_snapshot_campaign.run_campaign(
        categories=["crypto", "relative_value"],
        output_dir=tmp_path / "snapshots",
        iterations=1,
        interval_seconds=0,
        summary_output=tmp_path / "summary.md",
    )

    index = (tmp_path / "snapshots" / "snapshot_index.csv").read_text(encoding="utf-8")
    assert "provider unavailable" in index
    assert "snapshots collected: 1" in (tmp_path / "summary.md").read_text(encoding="utf-8")


def test_campaign_refresh_live_refreshes_existing_categories_only(tmp_path: Path, monkeypatch) -> None:
    calls: list[str] = []

    def fake_live_research(**kwargs) -> str:
        calls.append(kwargs["category"])
        Path(kwargs["output"]).write_text("live report", encoding="utf-8")
        return "live report"

    monkeypatch.setattr(run_snapshot_campaign, "run_live_research", fake_live_research)
    monkeypatch.setattr(run_snapshot_campaign, "build_offline_snapshot", lambda categories, started: _snapshot(started))

    run_snapshot_campaign.run_campaign(
        categories=["crypto", "relative_value"],
        output_dir=tmp_path / "snapshots",
        iterations=1,
        interval_seconds=0,
        refresh_live=True,
        summary_output=tmp_path / "summary.md",
    )

    assert calls == ["crypto"]
    assert (tmp_path / "snapshots" / "iteration_0001" / "live_crypto.md").exists()


def test_campaign_refresh_live_records_refresh_failure_without_stopping(tmp_path: Path, monkeypatch) -> None:
    def fake_live_research(**_kwargs) -> str:
        raise RuntimeError("network unavailable")

    monkeypatch.setattr(run_snapshot_campaign, "run_live_research", fake_live_research)
    monkeypatch.setattr(run_snapshot_campaign, "build_offline_snapshot", lambda categories, started: _snapshot(started))

    run_snapshot_campaign.run_campaign(
        categories=["crypto", "relative_value"],
        output_dir=tmp_path / "snapshots",
        iterations=1,
        interval_seconds=0,
        refresh_live=True,
        summary_output=tmp_path / "summary.md",
    )

    index = (tmp_path / "snapshots" / "snapshot_index.csv").read_text(encoding="utf-8")
    assert "live refresh failed: network unavailable" in index
    assert "snapshots collected: 1" in (tmp_path / "summary.md").read_text(encoding="utf-8")


def test_campaign_can_append_single_leg_paper_ledger(tmp_path: Path, monkeypatch) -> None:
    snapshot = _snapshot(datetime.fromisoformat("2026-05-20T00:00:00+00:00"))
    monkeypatch.setattr(run_snapshot_campaign, "build_offline_snapshot", lambda categories, started: snapshot)
    monkeypatch.setattr(
        run_snapshot_campaign,
        "build_candidates",
        lambda signals, settings, source_snapshot: (
            [
                {
                    "candidate_id": "c1",
                    "timestamp": "2026-05-20T00:00:00+00:00",
                    "category": "crypto",
                    "market_id": "m1",
                    "outcome": "Yes",
                    "side": "BUY_YES",
                    "execution_price": "0.40",
                    "execution_price_source": "ASK",
                    "size": "10",
                    "fair_probability_at_entry": "0.50",
                    "adjusted_edge_at_entry": "0.05",
                    "bid_at_entry": "0.39",
                    "ask_at_entry": "0.40",
                    "spread_at_entry": "0.01",
                    "liquidity_at_entry": "100",
                    "fair_value_source": "unit",
                    "risk_reason": "",
                }
            ],
            {},
            [],
        ),
    )

    ledger = tmp_path / "ledger.csv"
    result = run_snapshot_campaign.run_campaign(
        categories=["crypto"],
        output_dir=tmp_path / "snapshots",
        iterations=1,
        interval_seconds=0,
        paper_trade_ledger=ledger,
        summary_output=tmp_path / "summary.md",
    )

    assert result["paper_trades_entered"] == 0
    assert "BUY_YES" in ledger.read_text(encoding="utf-8")
    assert "paper trades entered: 0" in (tmp_path / "summary.md").read_text(encoding="utf-8")
    assert "missing contract identity" in ledger.read_text(encoding="utf-8")
