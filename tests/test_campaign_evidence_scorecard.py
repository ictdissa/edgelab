from __future__ import annotations

from pathlib import Path

from scripts.run_evidence_scorecard import (
    READINESS_LIVE_TRADING_NOT_APPROVED,
    build_scorecard,
    load_campaign_artifacts,
)


def test_campaign_evidence_scorecard_never_classifies_live_trading_ready(tmp_path: Path) -> None:
    scorecard = build_scorecard([], tmp_path / "ledger.csv", relative_value_ledger_path=tmp_path / "rv.csv")

    assert scorecard.live_trading_status == READINESS_LIVE_TRADING_NOT_APPROVED
    assert "LIVE_READY" not in scorecard.readiness_classification


def test_static_relative_value_candidates_do_not_count_as_empirical_evidence(tmp_path: Path) -> None:
    snapshot_dir = tmp_path / "snapshots"
    iteration_dir = snapshot_dir / "iteration_0001"
    iteration_dir.mkdir(parents=True)
    candidate_path = iteration_dir / "relative_value_candidates.csv"
    candidate_path.write_text(
        "\n".join(
            [
                "candidate_id,relative_value_source,status,paper_trade_eligible",
                "rv1,static_report,PAPER_ELIGIBLE,true",
            ]
        ),
        encoding="utf-8",
    )
    (snapshot_dir / "snapshot_index.csv").write_text(
        "\n".join(
            [
                "relative_value_candidates_path,executable_relative_value_candidates",
                f"{candidate_path},1",
            ]
        ),
        encoding="utf-8",
    )

    artifacts = load_campaign_artifacts(snapshot_dir, tmp_path / "reports")

    assert artifacts.relative_value_executable_candidates == 0
    assert artifacts.relative_value_paper_eligible_candidates == 0


def test_fully_priced_but_not_paper_eligible_relative_value_is_not_candidate_gate(tmp_path: Path) -> None:
    snapshot_dir = tmp_path / "snapshots"
    iteration_dir = snapshot_dir / "iteration_0001"
    iteration_dir.mkdir(parents=True)
    candidate_path = iteration_dir / "relative_value_candidates.csv"
    candidate_path.write_text(
        "\n".join(
            [
                "candidate_id,relative_value_source,empirical_evidence_eligible,status,paper_trade_eligible",
                "rv1,live_snapshot,true,EXECUTABLE_PACKAGE,false",
            ]
        ),
        encoding="utf-8",
    )
    (snapshot_dir / "snapshot_index.csv").write_text(
        "\n".join(
            [
                "relative_value_candidates_path,executable_relative_value_candidates,paper_eligible_relative_value_packages",
                f"{candidate_path},1,0",
            ]
        ),
        encoding="utf-8",
    )

    artifacts = load_campaign_artifacts(snapshot_dir, tmp_path / "reports")

    assert artifacts.relative_value_executable_candidates == 1
    assert artifacts.relative_value_paper_eligible_candidates == 0
