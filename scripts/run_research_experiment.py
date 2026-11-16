#!/usr/bin/env python
from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
from datetime import UTC, datetime
from pathlib import Path

try:
    import _bootstrap  # noqa: F401
except ModuleNotFoundError:
    from scripts import _bootstrap  # noqa: F401

from edgelab.config.settings import Settings
from edgelab.research.experiment import source_hashes, settings_snapshot


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run a reproducible EdgeLab research experiment")
    parser.add_argument(
        "--experiment-id", default=f"experiment_{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}"
    )
    parser.add_argument("--categories", default="crypto,relative_value")
    parser.add_argument("--iterations", type=int, default=72)
    parser.add_argument("--interval-seconds", type=int, default=300)
    parser.add_argument("--root", type=Path, default=Path("experiments"))
    parser.add_argument("--refresh-live", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    experiment_dir = args.root / args.experiment_id
    plan = experiment_plan(args, experiment_dir)
    experiment_dir.mkdir(parents=True, exist_ok=True)
    with (experiment_dir / "experiment_manifest.json").open("x", encoding="utf-8") as handle:
        json.dump(plan, handle, indent=2, sort_keys=True)
    if args.dry_run:
        print(f"experiment_manifest={experiment_dir / 'experiment_manifest.json'}")
        print("dry_run=true")
        return
    for command in plan["commands"]:
        subprocess.run(command, check=True)
    print(f"experiment_manifest={experiment_dir / 'experiment_manifest.json'}")


def experiment_plan(args: argparse.Namespace, experiment_dir: Path) -> dict[str, object]:
    snapshot_dir = experiment_dir / "snapshots"
    reports_dir = experiment_dir / "reports"
    paper_dir = experiment_dir / "paper_trades"
    dataset_path = experiment_dir / "research_dataset.sqlite"
    python = ".venv/bin/python"
    campaign_command = [
        python,
        "scripts/run_snapshot_campaign.py",
        "--categories",
        args.categories,
        "--interval-seconds",
        str(args.interval_seconds),
        "--iterations",
        str(args.iterations),
        "--output-dir",
        str(snapshot_dir),
        "--run-id",
        args.experiment_id,
        "--paper-trade-ledger",
        str(paper_dir / "ledger.csv"),
        "--summary-output",
        str(reports_dir / "snapshot_campaign_summary.md"),
    ]
    if args.refresh_live:
        campaign_command.append("--refresh-live")
    commands = [
        campaign_command,
        [
            python,
            "scripts/audit_relative_value_candidates.py",
            "--snapshot-dir",
            str(snapshot_dir),
            "--output",
            str(reports_dir / "relative_value_candidate_audit.md"),
            "--csv-output",
            str(reports_dir / "relative_value_candidate_audit.csv"),
            "--candidates-output",
            str(reports_dir / "relative_value_candidates.csv"),
        ],
        [
            python,
            "scripts/run_relative_value_paper_trader.py",
            "--snapshot-dir",
            str(snapshot_dir),
            "--ledger",
            str(paper_dir / "relative_value_ledger.csv"),
            "--output",
            str(reports_dir / "relative_value_paper_trades.md"),
            "--rejections-output",
            str(reports_dir / "relative_value_paper_rejections.csv"),
        ],
        [
            python,
            "scripts/run_markout_analysis.py",
            "--snapshot-dir",
            str(snapshot_dir),
            "--output",
            str(reports_dir / "markout_report.md"),
            "--observations-output",
            str(reports_dir / "markout_observations.csv"),
            "--summary-output",
            str(reports_dir / "markout_summary.csv"),
        ],
        [
            python,
            "scripts/update_clv.py",
            "--snapshot-dir",
            str(snapshot_dir),
            "--single-leg-ledger",
            str(paper_dir / "ledger.csv"),
            "--relative-value-ledger",
            str(paper_dir / "relative_value_ledger.csv"),
            "--output",
            str(reports_dir / "clv_report.md"),
            "--observations-output",
            str(reports_dir / "clv_observations.csv"),
            "--relative-value-observations-output",
            str(reports_dir / "relative_value_clv_observations.csv"),
        ],
        [
            python,
            "scripts/analyze_clv_robustness.py",
            "--ledger",
            str(paper_dir / "ledger.csv"),
            "--clv-observations",
            str(reports_dir / "clv_observations.csv"),
            "--output",
            str(reports_dir / "clv_robustness.md"),
            "--csv-output",
            str(reports_dir / "clv_robustness.csv"),
        ],
        [
            python,
            "scripts/audit_execution_realism.py",
            "--ledger",
            str(paper_dir / "ledger.csv"),
            "--output",
            str(reports_dir / "execution_realism.md"),
            "--csv-output",
            str(reports_dir / "execution_realism.csv"),
        ],
        [
            python,
            "scripts/build_market_research_table.py",
            "--snapshot-dir",
            str(snapshot_dir),
            "--output",
            str(experiment_dir / "market_research_table.csv"),
            "--config-version",
            args.experiment_id,
        ],
        [
            python,
            "scripts/analyze_settlement_pnl.py",
            "--ledger",
            str(paper_dir / "ledger.csv"),
            "--settlements",
            "data/settlements.csv",
            "--output",
            str(reports_dir / "settlement_pnl.md"),
            "--csv-output",
            str(reports_dir / "settlement_pnl.csv"),
        ],
        [
            python,
            "scripts/analyze_fair_value_calibration.py",
            "--signals",
            str(experiment_dir / "market_research_table.csv"),
            "--settlements",
            "data/settlements.csv",
            "--output",
            str(reports_dir / "fair_value_calibration.md"),
            "--csv-output",
            str(reports_dir / "fair_value_calibration.csv"),
        ],
        [
            python,
            "scripts/run_evidence_scorecard.py",
            "--snapshot-dir",
            str(snapshot_dir),
            "--single-leg-ledger",
            str(paper_dir / "ledger.csv"),
            "--relative-value-ledger",
            str(paper_dir / "relative_value_ledger.csv"),
            "--output",
            str(reports_dir / "evidence_scorecard.md"),
        ],
        [
            python,
            "scripts/build_final_research_report.py",
            "--snapshot-dir",
            str(snapshot_dir),
            "--single-leg-ledger",
            str(paper_dir / "ledger.csv"),
            "--relative-value-ledger",
            str(paper_dir / "relative_value_ledger.csv"),
            "--output",
            str(reports_dir / "final_research_report.md"),
        ],
        [
            python,
            "scripts/build_research_dataset.py",
            "--snapshot-dir",
            str(snapshot_dir),
            "--ledger",
            str(paper_dir / "ledger.csv"),
            "--relative-value-ledger",
            str(paper_dir / "relative_value_ledger.csv"),
            "--clv-observations",
            str(reports_dir / "clv_observations.csv"),
            "--relative-value-clv-observations",
            str(reports_dir / "relative_value_clv_observations.csv"),
            "--output",
            str(dataset_path),
            "--report",
            str(reports_dir / "research_dataset.md"),
        ],
        [
            python,
            "scripts/build_falsification_report.py",
            "--experiment-dir",
            str(experiment_dir),
            "--output",
            str(reports_dir / "falsification_campaign_report.md"),
            "--stats-output",
            str(reports_dir / "falsification_markout_stats.csv"),
        ],
    ]
    return {
        "experiment_id": args.experiment_id,
        "created_at": datetime.now(UTC).isoformat(),
        "categories": args.categories,
        "iterations": args.iterations,
        "interval_seconds": args.interval_seconds,
        "refresh_live": args.refresh_live,
        "live_trading_enabled": False,
        "evidence_status": "exploratory_campaign_not_preregistered",
        "profitability_claim": False,
        "settings": settings_snapshot(Settings()),
        "source_hashes": source_hashes(),
        "git_commit": git_commit(),
        "script_hashes": script_hashes(commands),
        "experiment_dir": str(experiment_dir),
        "snapshot_dir": str(snapshot_dir),
        "reports_dir": str(reports_dir),
        "paper_trades_dir": str(paper_dir),
        "dataset": str(dataset_path),
        "commands": commands,
    }


def git_commit() -> str:
    try:
        completed = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
        )
    except (OSError, subprocess.CalledProcessError):
        return "unavailable"
    return completed.stdout.strip()


def script_hashes(commands: list[list[str]]) -> dict[str, str]:
    scripts = sorted(
        {part for command in commands for part in command if part.startswith("scripts/")}
    )
    output: dict[str, str] = {}
    for script in scripts:
        path = Path(script)
        if path.exists():
            output[script] = hashlib.sha256(path.read_bytes()).hexdigest()
    return output


if __name__ == "__main__":
    main()
