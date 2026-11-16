from __future__ import annotations

from pathlib import Path

from scripts.build_final_research_report import build_report


def test_final_research_report_no_profitability_claim_when_clv_missing(tmp_path: Path) -> None:
    report = build_report(
        [tmp_path / "snapshots"],
        tmp_path / "single_leg_ledger.csv",
        tmp_path / "relative_value_ledger.csv",
    )

    assert "No profitability claim is made" in report
    assert "Live trading is disabled" in report
    assert "Mock or synthetic rows are treated as plumbing checks" in report
