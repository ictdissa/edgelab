from __future__ import annotations

from pathlib import Path

from scripts.build_project_competitiveness_report import (
    ProjectMetrics,
    build_checklist,
    render_cv_bullets,
    render_report,
)


def _metrics() -> ProjectMetrics:
    return ProjectMetrics(
        snapshots=3,
        total_signals=171,
        positive_adjusted_edges=51,
        single_leg_candidates=0,
        relative_value_executable_packages=6,
        relative_value_paper_trades=0,
        clv_observations=0,
        average_clv=None,
        median_clv=None,
        readiness="NOT_READY_NO_DATA",
        live_status="LIVE_TRADING_NOT_APPROVED",
        rv_candidates_analyzed=21,
        rv_previously_executable=6,
        rv_fully_priced=6,
        rv_paper_eligible=0,
        rv_skipped=21,
        top_package_rejections=[("stale leg quote", 6)],
        top_sanity_blockers=[("MISSING_BID_ASK", 111)],
    )


def test_competitiveness_report_is_honest_about_profitability_and_live_trading() -> None:
    metrics = _metrics()
    report = render_report(metrics, build_checklist(metrics), Path("checklist.csv"), Path("cv.md"))
    bullets = render_cv_bullets(metrics)

    assert "No profitability claim is justified" in report
    assert "Profitability is unproven" in bullets
    assert "not independent signals" in bullets
    assert "LIVE_TRADING_NOT_APPROVED" in report


def test_execution_checklist_blocks_clv_without_paper_fills() -> None:
    rows = build_checklist(_metrics())
    clv_gate = next(row for row in rows if row["gate"] == "Tradable CLV sample")

    assert clv_gate["status"] == "BLOCKED"
    assert "no paper-filled CLV observations" in clv_gate["blocker"]


def test_report_counts_cannot_certify_design_or_software_validation() -> None:
    rows = build_checklist(_metrics())
    for dimension in ("research_design", "software_validation"):
        row = next(row for row in rows if row["dimension"] == dimension)
        assert row["status"] == "NOT_EVALUATED"
        assert row["blocker"]
