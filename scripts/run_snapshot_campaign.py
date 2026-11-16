#!/usr/bin/env python
from __future__ import annotations

import argparse
import csv
import json
import time
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

try:
    import _bootstrap  # noqa: F401
except ModuleNotFoundError:
    from scripts import _bootstrap  # noqa: F401

from edgelab.config.settings import Settings
from scripts.collect_research_snapshots import (
    build_offline_snapshot,
    init_db,
    parse_categories,
    write_snapshot_files,
)
from edgelab.research.live_analysis import run_live_research
from scripts.generate_paper_signals import (
    RELATIVE_VALUE_CANDIDATE_COLUMNS,
    RV_STATUS_PAPER_ELIGIBLE,
    build_candidates,
    build_relative_value_candidates,
    is_empirical_fully_priced_relative_value_candidate,
)
from scripts.run_edge_sanity_checks import SANITY_COLUMNS, build_edge_sanity_warnings
from scripts.run_paper_trader import (
    append_ledger_rows as append_paper_ledger_rows,
    build_ledger_rows as build_paper_ledger_rows,
    ensure_ledger_schema as ensure_paper_ledger_schema,
    persistent_trader,
)

CAMPAIGN_INDEX_COLUMNS = [
    "run_id",
    "iteration",
    "timestamp",
    "categories",
    "snapshot_dir",
    "signals_path",
    "freshness_path",
    "relative_value_opportunities_path",
    "relative_value_candidates_path",
    "edge_sanity_warnings_path",
    "summary_path",
    "markets_scanned",
    "signals_count",
    "positive_raw_edges",
    "positive_adjusted_edges",
    "rejected_directional_edges",
    "relative_value_candidates",
    "executable_relative_value_candidates",
    "fully_priced_relative_value_packages",
    "paper_eligible_relative_value_packages",
    "sanity_warnings",
    "risk_approved_candidates",
    "paper_trades_entered",
    "failures",
]

LIVE_REFRESH_CATEGORIES = {"sports", "crypto", "weather"}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run a resumable EdgeLab snapshot campaign")
    parser.add_argument("--categories", required=True, help="Comma-separated categories")
    parser.add_argument("--interval-seconds", type=int, default=300)
    parser.add_argument("--iterations", type=int, default=72)
    parser.add_argument("--output-dir", type=Path, default=Path("data/snapshots_campaign"))
    parser.add_argument(
        "--run-id",
        help="Stable campaign identifier. If omitted, a timestamped run id is created and reused on --resume.",
    )
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--max-runtime-minutes", type=float)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--refresh-live",
        action="store_true",
        help="Refresh existing live providers before each snapshot iteration. This does not place orders.",
    )
    parser.add_argument("--live-limit", type=int, default=50)
    parser.add_argument("--skip-provider-diagnosis", action="store_true")
    parser.add_argument(
        "--odds-provider", choices=["auto", "odds_api_io", "the_odds_api", "disabled"]
    )
    parser.add_argument("--sport")
    parser.add_argument(
        "--paper-trade-ledger",
        type=Path,
        help="Append strict single-leg paper fills for each iteration to this ledger. Never places live orders.",
    )
    parser.add_argument(
        "--summary-output", type=Path, default=Path("reports/snapshot_campaign_summary.md")
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    categories = parse_categories(args.categories)
    result = run_campaign(
        categories=categories,
        output_dir=args.output_dir,
        run_id=args.run_id,
        iterations=args.iterations,
        interval_seconds=args.interval_seconds,
        resume=args.resume,
        max_runtime_minutes=args.max_runtime_minutes,
        dry_run=args.dry_run,
        refresh_live=args.refresh_live,
        live_limit=args.live_limit,
        skip_provider_diagnosis=args.skip_provider_diagnosis,
        odds_provider=args.odds_provider,
        sport=args.sport,
        paper_trade_ledger=args.paper_trade_ledger,
        summary_output=args.summary_output,
    )
    print(f"snapshots_collected={result['snapshots_collected']}")
    print(f"signals={result['signals_count']}")
    print(f"positive_adjusted_edges={result['positive_adjusted_edges']}")
    print(f"relative_value_candidates={result['relative_value_candidates']}")
    print(f"paper_trades_entered={result['paper_trades_entered']}")
    print(f"summary={args.summary_output}")


def run_campaign(
    *,
    categories: list[str],
    output_dir: Path,
    iterations: int,
    interval_seconds: int,
    run_id: str | None = None,
    resume: bool = False,
    max_runtime_minutes: float | None = None,
    dry_run: bool = False,
    refresh_live: bool = False,
    live_limit: int = 50,
    skip_provider_diagnosis: bool = False,
    odds_provider: str | None = None,
    sport: str | None = None,
    paper_trade_ledger: Path | None = None,
    summary_output: Path = Path("reports/snapshot_campaign_summary.md"),
) -> dict[str, Any]:
    output_dir.mkdir(parents=True, exist_ok=True)
    summary_output.parent.mkdir(parents=True, exist_ok=True)
    state_path = output_dir / "campaign_state.json"
    index_path = output_dir / "snapshot_index.csv"
    manifest_path = output_dir / "campaign_manifest.json"
    resolved_run_id = resolve_run_id(run_id, state_path, resume)
    settings = Settings()
    start_monotonic = time.monotonic()
    completed_before = completed_iterations(state_path, output_dir) if resume else 0
    start_iteration = completed_before + 1
    if dry_run:
        planned = {
            "dry_run": True,
            "run_id": resolved_run_id,
            "categories": categories,
            "start_iteration": start_iteration,
            "iterations": iterations,
            "output_dir": str(output_dir),
            "refresh_live": refresh_live,
        }
        write_campaign_state(state_path, planned)
        summary_output.write_text(
            render_campaign_summary(load_index_rows(index_path), planned), encoding="utf-8"
        )
        write_campaign_manifest(manifest_path, load_index_rows(index_path), planned)
        return {
            **planned,
            "snapshots_collected": 0,
            "signals_count": 0,
            "positive_adjusted_edges": 0,
            "relative_value_candidates": 0,
            "paper_trades_entered": 0,
        }

    init_db(output_dir / "snapshots.sqlite")
    failures: dict[str, str] = {}
    for iteration in range(start_iteration, iterations + 1):
        if max_runtime_minutes is not None:
            elapsed_minutes = (time.monotonic() - start_monotonic) / 60
            if elapsed_minutes >= max_runtime_minutes:
                break
        started = datetime.now(UTC)
        iteration_dir = output_dir / f"iteration_{iteration:04d}"
        iteration_dir.mkdir(parents=True, exist_ok=True)
        try:
            refresh_failures = {}
            if refresh_live:
                refresh_failures = refresh_live_sources(
                    categories,
                    iteration_dir,
                    settings=settings,
                    limit=live_limit,
                    skip_provider_diagnosis=skip_provider_diagnosis,
                    odds_provider=odds_provider,
                    sport=sport,
                )
                started = datetime.now(UTC)
            snapshot = build_offline_snapshot(categories, started)
            snapshot["run_id"] = resolved_run_id
            if refresh_failures:
                snapshot["failures"] = {
                    **{str(key): str(value) for key, value in snapshot.get("failures", {}).items()},
                    **refresh_failures,
                }
            write_snapshot_files(iteration_dir, snapshot)
            rv_candidates = build_relative_value_candidates(
                snapshot.get("relative_value_opportunities", []),
                snapshot_timestamp=started.isoformat(),
                settings=settings,
            )
            write_csv(
                iteration_dir / "relative_value_candidates.csv",
                rv_candidates,
                RELATIVE_VALUE_CANDIDATE_COLUMNS,
            )
            sanity_warnings = build_edge_sanity_warnings(snapshot.get("signals", []), settings)
            write_csv(iteration_dir / "edge_sanity_warnings.csv", sanity_warnings, SANITY_COLUMNS)
            candidates, _rejection_counts, rejected_directional = build_candidates(
                snapshot.get("signals", []),
                settings,
                iteration_dir / "signals.csv",
            )
            paper_trade_rows = []
            if paper_trade_ledger is not None:
                ensure_paper_ledger_schema(paper_trade_ledger)
                paper_trade_rows = build_paper_ledger_rows(
                    candidates, settings, trader=persistent_trader(paper_trade_ledger, settings)
                )
                append_paper_ledger_rows(paper_trade_ledger, paper_trade_rows)
            metrics = iteration_metrics(
                snapshot, candidates, rejected_directional, rv_candidates, sanity_warnings
            )
            metrics["paper_trades_entered"] = sum(
                1 for row in paper_trade_rows if row.get("clv_status") == "pending"
            )
            append_campaign_index(
                index_path,
                resolved_run_id,
                iteration,
                started,
                categories,
                iteration_dir,
                snapshot,
                metrics,
            )
            failures.update(
                {str(key): str(value) for key, value in snapshot.get("failures", {}).items()}
            )
        except Exception as exc:
            failures["campaign"] = str(exc)
            write_failure_iteration(iteration_dir, started, categories, exc, resolved_run_id)
            metrics = empty_iteration_metrics()
            append_failure_index(
                index_path, resolved_run_id, iteration, started, categories, iteration_dir, str(exc)
            )
        state = {
            "dry_run": False,
            "run_id": resolved_run_id,
            "categories": categories,
            "completed_iterations": iteration,
            "latest_iteration": iteration,
            "latest_iteration_dir": str(iteration_dir),
            "refresh_live": refresh_live,
            "updated_at": datetime.now(UTC).isoformat(),
            "failures": failures,
        }
        write_campaign_state(state_path, state)
        summary_output.write_text(
            render_campaign_summary(load_index_rows(index_path), state), encoding="utf-8"
        )
        write_campaign_manifest(manifest_path, load_index_rows(index_path), state)
        print(
            f"iteration={iteration} signals={metrics['signals_count']} "
            f"positive_adjusted_edges={metrics['positive_adjusted_edges']} "
            f"rv_candidates={metrics['relative_value_candidates']} "
            f"sanity_warnings={metrics['sanity_warnings']}"
        )
        if iteration < iterations:
            time.sleep(max(0, interval_seconds))
    rows = load_index_rows(index_path)
    final_state = json.loads(state_path.read_text(encoding="utf-8")) if state_path.exists() else {}
    summary_output.write_text(render_campaign_summary(rows, final_state), encoding="utf-8")
    write_campaign_manifest(manifest_path, rows, final_state)
    return campaign_totals(rows)


def resolve_run_id(run_id: str | None, state_path: Path, resume: bool) -> str:
    if run_id:
        return run_id
    if resume and state_path.exists():
        try:
            state = json.loads(state_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            state = {}
        existing = state.get("run_id")
        if isinstance(existing, str) and existing:
            return existing
    return f"run_{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}"


def refresh_live_sources(
    categories: list[str],
    iteration_dir: Path,
    *,
    settings: Settings,
    limit: int,
    skip_provider_diagnosis: bool,
    odds_provider: str | None,
    sport: str | None,
) -> dict[str, str]:
    failures: dict[str, str] = {}
    for category in categories:
        if category not in LIVE_REFRESH_CATEGORIES:
            continue
        try:
            run_live_research(
                category=category,
                output=iteration_dir / f"live_{category}.md",
                settings=settings,
                limit=limit,
                refresh=True,
                skip_provider_diagnosis=skip_provider_diagnosis,
                odds_provider=odds_provider,
                sport=sport,
            )
        except Exception as exc:
            failures[category] = f"live refresh failed: {exc}"
    return failures


def completed_iterations(state_path: Path, output_dir: Path) -> int:
    if state_path.exists():
        try:
            state = json.loads(state_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            state = {}
        value = state.get("completed_iterations")
        if isinstance(value, int):
            return value
    existing = sorted(output_dir.glob("iteration_*"))
    return len(existing)


def iteration_metrics(
    snapshot: dict[str, Any],
    candidates: list[dict[str, str]],
    rejected_directional: list[dict[str, str]],
    rv_candidates: list[dict[str, str]],
    sanity_warnings: list[dict[str, str]],
) -> dict[str, int]:
    signals = snapshot.get("signals", [])
    return {
        "signals_count": len(signals),
        "positive_raw_edges": sum(1 for row in signals if parse_float(row.get("raw_edge")) > 0),
        "positive_adjusted_edges": sum(
            1
            for row in signals
            if parse_float(row.get("best_adjusted_edge") or row.get("adjusted_edge")) > 0
        ),
        "rejected_directional_edges": len(rejected_directional),
        "relative_value_candidates": len(rv_candidates),
        "executable_relative_value_candidates": sum(
            1 for row in rv_candidates if is_empirical_fully_priced_relative_value_candidate(row)
        ),
        "fully_priced_relative_value_packages": sum(
            1 for row in rv_candidates if is_empirical_fully_priced_relative_value_candidate(row)
        ),
        "paper_eligible_relative_value_packages": sum(
            1
            for row in rv_candidates
            if row.get("paper_trade_eligible") == "true"
            and row.get("empirical_evidence_eligible") == "true"
            and row.get("status") == RV_STATUS_PAPER_ELIGIBLE
        ),
        "sanity_warnings": len(sanity_warnings),
        "risk_approved_candidates": len(candidates),
        "paper_eligible_relative_value_candidates": sum(
            1 for row in rv_candidates if row.get("status") == RV_STATUS_PAPER_ELIGIBLE
        ),
        "paper_trades_entered": 0,
    }


def empty_iteration_metrics() -> dict[str, int]:
    return {
        "signals_count": 0,
        "positive_raw_edges": 0,
        "positive_adjusted_edges": 0,
        "rejected_directional_edges": 0,
        "relative_value_candidates": 0,
        "executable_relative_value_candidates": 0,
        "fully_priced_relative_value_packages": 0,
        "paper_eligible_relative_value_packages": 0,
        "sanity_warnings": 0,
        "risk_approved_candidates": 0,
        "paper_trades_entered": 0,
    }


def append_campaign_index(
    path: Path,
    run_id: str,
    iteration: int,
    started: datetime,
    categories: list[str],
    iteration_dir: Path,
    snapshot: dict[str, Any],
    metrics: dict[str, int],
) -> None:
    totals = snapshot.get("totals", {})
    row = {
        "run_id": run_id,
        "iteration": str(iteration),
        "timestamp": started.isoformat(),
        "categories": ",".join(categories),
        "snapshot_dir": str(iteration_dir),
        "signals_path": str(iteration_dir / "signals.csv"),
        "freshness_path": str(iteration_dir / "freshness.csv"),
        "relative_value_opportunities_path": existing_path(
            iteration_dir / "relative_value_opportunities.csv"
        ),
        "relative_value_candidates_path": str(iteration_dir / "relative_value_candidates.csv"),
        "edge_sanity_warnings_path": str(iteration_dir / "edge_sanity_warnings.csv"),
        "summary_path": str(iteration_dir / "summary.json"),
        "markets_scanned": str(totals.get("markets_scanned", 0)),
        "signals_count": str(metrics["signals_count"]),
        "positive_raw_edges": str(metrics["positive_raw_edges"]),
        "positive_adjusted_edges": str(metrics["positive_adjusted_edges"]),
        "rejected_directional_edges": str(metrics["rejected_directional_edges"]),
        "relative_value_candidates": str(metrics["relative_value_candidates"]),
        "executable_relative_value_candidates": str(
            metrics["executable_relative_value_candidates"]
        ),
        "fully_priced_relative_value_packages": str(
            metrics["fully_priced_relative_value_packages"]
        ),
        "paper_eligible_relative_value_packages": str(
            metrics["paper_eligible_relative_value_packages"]
        ),
        "sanity_warnings": str(metrics["sanity_warnings"]),
        "risk_approved_candidates": str(metrics["risk_approved_candidates"]),
        "paper_trades_entered": str(metrics.get("paper_trades_entered", 0)),
        "failures": json.dumps(snapshot.get("failures", {}), sort_keys=True),
    }
    append_index_row(path, row)


def append_failure_index(
    path: Path,
    run_id: str,
    iteration: int,
    started: datetime,
    categories: list[str],
    iteration_dir: Path,
    reason: str,
) -> None:
    row = {column: "" for column in CAMPAIGN_INDEX_COLUMNS}
    row.update(
        {
            "iteration": str(iteration),
            "run_id": run_id,
            "timestamp": started.isoformat(),
            "categories": ",".join(categories),
            "snapshot_dir": str(iteration_dir),
            "summary_path": str(iteration_dir / "summary.json"),
            "failures": json.dumps({"campaign": reason}, sort_keys=True),
        }
    )
    append_index_row(path, row)


def append_index_row(path: Path, row: dict[str, str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    ensure_index_schema(path)
    write_header = not path.exists()
    with path.open("a", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=CAMPAIGN_INDEX_COLUMNS)
        if write_header:
            writer.writeheader()
        writer.writerow(row)


def ensure_index_schema(path: Path) -> None:
    if not path.exists():
        return
    with path.open(newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        rows = list(reader)
        fieldnames = reader.fieldnames or []
    if fieldnames == CAMPAIGN_INDEX_COLUMNS:
        return
    migrated = [{column: row.get(column, "") for column in CAMPAIGN_INDEX_COLUMNS} for row in rows]
    write_csv(path, migrated, CAMPAIGN_INDEX_COLUMNS)


def write_failure_iteration(
    iteration_dir: Path,
    started: datetime,
    categories: list[str],
    exc: Exception,
    run_id: str,
) -> None:
    summary = {
        "snapshot_version": 1,
        "run_id": run_id,
        "timestamp": started.isoformat(),
        "categories": categories,
        "category_summaries": {},
        "totals": {},
        "failures": {"campaign": str(exc)},
    }
    write_csv(iteration_dir / "signals.csv", [], [])
    write_csv(iteration_dir / "freshness.csv", [], [])
    (iteration_dir / "summary.json").write_text(
        json.dumps(summary, indent=2, sort_keys=True), encoding="utf-8"
    )


def render_campaign_summary(rows: list[dict[str, str]], state: dict[str, Any]) -> str:
    totals = campaign_totals(rows)
    failure_counts = Counter()
    for row in rows:
        try:
            failures = json.loads(row.get("failures") or "{}")
        except json.JSONDecodeError:
            failures = {}
        for category, reason in failures.items():
            failure_counts[f"{category}: {reason}"] += 1
    lines = [
        "# EdgeLab Snapshot Campaign Summary",
        "",
        "The campaign runner collects research snapshots only. It does not place live orders or change thresholds.",
        "",
        "## Summary",
        "",
        f"- snapshots collected: {totals['snapshots_collected']}",
        f"- run id: `{state.get('run_id', 'n/a')}`",
        f"- markets scanned: {totals['markets_scanned']}",
        f"- signals generated: {totals['signals_count']}",
        f"- positive raw edges: {totals['positive_raw_edges']}",
        f"- positive adjusted edges: {totals['positive_adjusted_edges']}",
        f"- rejected directional edges: {totals['rejected_directional_edges']}",
        f"- relative-value candidates: {totals['relative_value_candidates']}",
        f"- executable relative-value candidates: {totals['executable_relative_value_candidates']}",
        f"- fully priced relative-value packages: {totals['fully_priced_relative_value_packages']}",
        f"- paper-eligible relative-value packages: {totals['paper_eligible_relative_value_packages']}",
        f"- sanity warnings: {totals['sanity_warnings']}",
        f"- risk-approved candidates: {totals['risk_approved_candidates']}",
        f"- paper trades entered: {totals['paper_trades_entered']}",
        f"- latest state: `{state.get('updated_at', 'n/a')}`",
        "",
        "## Failures By Category",
        "",
    ]
    if failure_counts:
        lines.extend(
            markdown_table(
                ["failure", "count"],
                [[key, str(value)] for key, value in failure_counts.most_common()],
            )
        )
    else:
        lines.append("No category failures were recorded.")
    return "\n".join(lines)


def campaign_totals(rows: list[dict[str, str]]) -> dict[str, Any]:
    return {
        "snapshots_collected": len(rows),
        "markets_scanned": sum(parse_int(row.get("markets_scanned")) for row in rows),
        "signals_count": sum(parse_int(row.get("signals_count")) for row in rows),
        "positive_raw_edges": sum(parse_int(row.get("positive_raw_edges")) for row in rows),
        "positive_adjusted_edges": sum(
            parse_int(row.get("positive_adjusted_edges")) for row in rows
        ),
        "rejected_directional_edges": sum(
            parse_int(row.get("rejected_directional_edges")) for row in rows
        ),
        "relative_value_candidates": sum(
            parse_int(row.get("relative_value_candidates")) for row in rows
        ),
        "executable_relative_value_candidates": sum(
            parse_int(row.get("executable_relative_value_candidates")) for row in rows
        ),
        "fully_priced_relative_value_packages": sum(
            parse_int(
                row.get("fully_priced_relative_value_packages")
                or row.get("executable_relative_value_candidates")
            )
            for row in rows
        ),
        "paper_eligible_relative_value_packages": sum(
            parse_int(row.get("paper_eligible_relative_value_packages")) for row in rows
        ),
        "sanity_warnings": sum(parse_int(row.get("sanity_warnings")) for row in rows),
        "risk_approved_candidates": sum(
            parse_int(row.get("risk_approved_candidates")) for row in rows
        ),
        "paper_trades_entered": sum(parse_int(row.get("paper_trades_entered")) for row in rows),
    }


def load_index_rows(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    with path.open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def write_campaign_state(path: Path, state: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, indent=2, sort_keys=True), encoding="utf-8")


def write_campaign_manifest(path: Path, rows: list[dict[str, str]], state: dict[str, Any]) -> None:
    totals = campaign_totals(rows)
    manifest = {
        "run_id": state.get("run_id", ""),
        "categories": state.get("categories", []),
        "snapshot_index": str(path.parent / "snapshot_index.csv"),
        "campaign_state": str(path.parent / "campaign_state.json"),
        "live_trading_enabled": False,
        "updated_at": datetime.now(UTC).isoformat(),
        "totals": totals,
        "iterations": [
            {
                "iteration": row.get("iteration", ""),
                "timestamp": row.get("timestamp", ""),
                "snapshot_dir": row.get("snapshot_dir", ""),
                "signals_path": row.get("signals_path", ""),
                "freshness_path": row.get("freshness_path", ""),
                "relative_value_candidates_path": row.get("relative_value_candidates_path", ""),
                "edge_sanity_warnings_path": row.get("edge_sanity_warnings_path", ""),
                "summary_path": row.get("summary_path", ""),
                "failures": row.get("failures", ""),
            }
            for row in rows
        ],
    }
    path.write_text(json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8")


def write_csv(path: Path, rows: list[dict[str, str]], columns: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not columns:
        with path.open("w", newline="", encoding="utf-8") as handle:
            handle.write("")
        return
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns)
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


def existing_path(path: Path) -> str:
    return str(path) if path.exists() else ""


def parse_float(value: str | None) -> float:
    if value in {None, ""}:
        return 0.0
    try:
        return float(value)
    except ValueError:
        return 0.0


def parse_int(value: str | None) -> int:
    if value in {None, ""}:
        return 0
    try:
        return int(float(value))
    except ValueError:
        return 0


def markdown_table(headers: list[str], rows: list[list[str]]) -> list[str]:
    output = ["| " + " | ".join(headers) + " |", "| " + " | ".join("---" for _ in headers) + " |"]
    for row in rows:
        output.append("| " + " | ".join(str(cell).replace("|", "\\|") for cell in row) + " |")
    return output


if __name__ == "__main__":
    main()
