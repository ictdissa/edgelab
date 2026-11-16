from __future__ import annotations

from pathlib import Path

from scripts.build_market_research_table import build_research_rows


def test_market_research_table_exports_required_signal_fields(tmp_path: Path) -> None:
    snapshot_dir = tmp_path / "snapshots"
    iteration_dir = snapshot_dir / "iteration_0001"
    iteration_dir.mkdir(parents=True)
    (iteration_dir / "signals.csv").write_text(
        "timestamp,market_id,outcome,category,bid,ask,mid,spread,fair_probability,fair_value_source,buy_edge_adjusted,sell_edge_adjusted,final_decision\n"
        "2026-05-21T00:00:00+00:00,m1,Yes,crypto_threshold,0.40,0.42,0.41,0.02,0.50,crypto,0.08,-0.1,PAPER_CANDIDATE\n",
        encoding="utf-8",
    )

    rows = build_research_rows(snapshot_dir, tmp_path / "live", "unit")

    assert rows[0]["market_id"] == "m1"
    assert rows[0]["best_bid"] == "0.40"
    assert rows[0]["fair_value"] == "0.50"
    assert rows[0]["config_version"] == "unit"
