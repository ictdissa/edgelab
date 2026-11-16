from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from scripts.build_research_dataset import build_dataset


def test_research_dataset_exports_queryable_snapshot_tables(tmp_path: Path) -> None:
    snapshot_dir = tmp_path / "snapshots"
    iteration_dir = snapshot_dir / "iteration_0001"
    iteration_dir.mkdir(parents=True)
    (iteration_dir / "summary.json").write_text(
        json.dumps(
            {
                "run_id": "unit",
                "timestamp": "2026-05-21T00:00:00+00:00",
                "categories": ["crypto"],
                "totals": {},
            }
        ),
        encoding="utf-8",
    )
    (iteration_dir / "signals.csv").write_text(
        "signal_id,timestamp,category,market_id,outcome,best_side,best_adjusted_edge,final_decision\n"
        "s1,2026-05-21T00:00:00+00:00,crypto,m1,Yes,BUY_YES,0.02,PAPER_CANDIDATE\n",
        encoding="utf-8",
    )

    output = tmp_path / "research.sqlite"
    counts = build_dataset(
        snapshot_dirs=[snapshot_dir],
        ledger=tmp_path / "ledger.csv",
        relative_value_ledger=tmp_path / "rv_ledger.csv",
        clv_observations=tmp_path / "clv.csv",
        relative_value_clv_observations=tmp_path / "rv_clv.csv",
        output=output,
    )

    with sqlite3.connect(output) as conn:
        signal_count = conn.execute("SELECT COUNT(*) FROM signals").fetchone()[0]
        live_enabled = conn.execute("SELECT live_trading_enabled FROM dataset_manifest").fetchone()[0]

    assert counts["snapshots"] == 1
    assert counts["signals"] == 1
    assert signal_count == 1
    assert live_enabled == "false"
