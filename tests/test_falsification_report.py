from __future__ import annotations

import csv
import json
from pathlib import Path

from scripts.build_falsification_report import build_report


def _write_csv(path: Path, rows: list[dict[str, str]], columns: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns)
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


def test_falsification_report_says_edge_not_reliable_when_monotonicity_fails(tmp_path: Path) -> None:
    exp = tmp_path / "experiment"
    (exp / "snapshots").mkdir(parents=True)
    (exp / "reports").mkdir()
    (exp / "paper_trades").mkdir()
    (exp / "experiment_manifest.json").write_text(
        json.dumps({"live_trading_enabled": False}),
        encoding="utf-8",
    )
    (exp / "snapshots" / "campaign_manifest.json").write_text(
        json.dumps({"totals": {"snapshots_collected": 2}}),
        encoding="utf-8",
    )
    _write_csv(
        exp / "reports" / "markout_observations.csv",
        [
            {
                "evidence_type": "tradable_candidate",
                "horizon": "1m",
                "status": "observed",
                "markout": "0.02",
                "edge_bucket": "1-3%",
            },
            {
                "evidence_type": "tradable_candidate",
                "horizon": "1m",
                "status": "observed",
                "markout": "-0.01",
                "edge_bucket": ">=10%",
            },
        ],
        ["evidence_type", "horizon", "status", "markout", "edge_bucket"],
    )
    _write_csv(exp / "reports" / "execution_realism.csv", [], ["fill_realistic"])
    _write_csv(exp / "reports" / "fair_value_calibration.csv", [], ["status"])
    _write_csv(exp / "reports" / "settlement_pnl.csv", [], ["status"])
    _write_csv(exp / "reports" / "relative_value_candidate_audit.csv", [], ["status"])
    _write_csv(exp / "paper_trades" / "ledger.csv", [{"paper_trade_id": "p1"}], ["paper_trade_id"])
    _write_csv(exp / "paper_trades" / "relative_value_ledger.csv", [], ["execution_status"])

    report, stats = build_report(exp, exp / "reports" / "report.md", exp / "reports" / "stats.csv")

    assert "Current edge score does not rank opportunities reliably" in report
    assert "NEEDS_MORE_DATA" in report
    assert stats
