#!/usr/bin/env python
from __future__ import annotations

import argparse
import csv
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

try:
    import _bootstrap  # noqa: F401
except ModuleNotFoundError:
    from scripts import _bootstrap  # noqa: F401

from edgelab.research.evidence_policy import label_legacy_report

from scripts.run_evidence_scorecard import (
    build_scorecard,
    load_campaign_artifacts,
    load_snapshot_records_from_dirs,
)

CHECKLIST_COLUMNS = [
    "dimension",
    "gate",
    "status",
    "evidence",
    "blocker",
    "next_action",
]


@dataclass(frozen=True)
class ProjectMetrics:
    snapshots: int
    total_signals: int
    positive_adjusted_edges: int
    single_leg_candidates: int
    relative_value_executable_packages: int
    relative_value_paper_trades: int
    clv_observations: int
    average_clv: float | None
    median_clv: float | None
    readiness: str
    live_status: str
    rv_candidates_analyzed: int
    rv_previously_executable: int
    rv_fully_priced: int
    rv_paper_eligible: int
    rv_skipped: int
    top_package_rejections: list[tuple[str, int]]
    top_sanity_blockers: list[tuple[str, int]]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Summarize legacy research coverage, execution gates and missing evidence"
    )
    parser.add_argument("--snapshot-dir", type=Path, default=Path("data/snapshots_campaign_test"))
    parser.add_argument(
        "--extra-snapshot-dir",
        type=Path,
        action="append",
        default=[],
        help="Additional snapshot directories to include in the report.",
    )
    parser.add_argument(
        "--single-leg-ledger", type=Path, default=Path("data/paper_trades/ledger.csv")
    )
    parser.add_argument(
        "--relative-value-ledger",
        type=Path,
        default=Path("data/paper_trades/relative_value_ledger.csv"),
    )
    parser.add_argument(
        "--output", type=Path, default=Path("reports/project_competitiveness_report.md")
    )
    parser.add_argument(
        "--checklist-output", type=Path, default=Path("reports/execution_readiness_checklist.csv")
    )
    parser.add_argument(
        "--summary-output",
        "--cv-output",
        dest="cv_output",
        type=Path,
        default=Path("reports/research_summary.md"),
        help="Research summary; --cv-output is retained as a legacy alias.",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    metrics = load_metrics(
        [args.snapshot_dir, *args.extra_snapshot_dir],
        args.single_leg_ledger,
        args.relative_value_ledger,
    )
    checklist = build_checklist(metrics)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.checklist_output.parent.mkdir(parents=True, exist_ok=True)
    args.cv_output.parent.mkdir(parents=True, exist_ok=True)
    write_csv(args.checklist_output, checklist, CHECKLIST_COLUMNS)
    args.output.write_text(
        render_report(metrics, checklist, args.checklist_output, args.cv_output), encoding="utf-8"
    )
    args.cv_output.write_text(render_cv_bullets(metrics), encoding="utf-8")
    print(f"research_report={args.output}")
    print(f"execution_checklist={args.checklist_output}")
    print(f"research_summary={args.cv_output}")
    print(f"readiness={metrics.readiness}")
    print(f"rv_paper_eligible={metrics.rv_paper_eligible}")
    print(f"clv_observations={metrics.clv_observations}")


def load_metrics(
    snapshot_dirs: list[Path], single_leg_ledger: Path, relative_value_ledger: Path
) -> ProjectMetrics:
    records = load_snapshot_records_from_dirs(snapshot_dirs)
    primary_snapshot_dir = (
        snapshot_dirs[0] if snapshot_dirs else Path("data/snapshots_campaign_test")
    )
    scorecard = build_scorecard(
        records,
        single_leg_ledger,
        relative_value_ledger_path=relative_value_ledger,
        campaign_artifacts=load_campaign_artifacts(primary_snapshot_dir, Path("reports")),
    )
    rv_audit = read_csv_rows(Path("reports/relative_value_candidate_audit.csv"))
    rv_ledger = read_csv_rows(relative_value_ledger)
    latest_rv_rows = (
        rv_ledger[-len(rv_audit) :] if rv_audit and len(rv_ledger) >= len(rv_audit) else rv_ledger
    )
    sanity_triage = read_csv_rows(Path("reports/sanity_warning_triage.csv"))
    top_package_rejections = Counter(
        row.get("risk_reasons", "unknown") or "unknown"
        for row in latest_rv_rows
        if row.get("execution_status") == "SKIPPED"
    ).most_common(6)
    sanity_blockers: Counter[str] = Counter()
    for row in sanity_triage:
        if row.get("severity") == "BLOCKER":
            sanity_blockers[row.get("warning_code", "unknown")] += int(
                row.get("blocking_count") or 0
            )
    return ProjectMetrics(
        snapshots=scorecard.total_snapshots,
        total_signals=scorecard.total_signals,
        positive_adjusted_edges=scorecard.positive_adjusted_edge_count,
        single_leg_candidates=scorecard.single_leg_paper_candidates,
        relative_value_executable_packages=scorecard.relative_value_executable_candidates,
        relative_value_paper_trades=scorecard.relative_value_paper_trades,
        clv_observations=scorecard.clv_observations + scorecard.relative_value_clv_observations,
        average_clv=scorecard.average_clv,
        median_clv=scorecard.median_clv,
        readiness=scorecard.readiness_classification,
        live_status=scorecard.live_trading_status,
        rv_candidates_analyzed=len(rv_audit),
        rv_previously_executable=sum(
            1 for row in rv_audit if row.get("source_status") == "EXECUTABLE"
        ),
        rv_fully_priced=sum(
            1 for row in rv_audit if row.get("status") in {"EXECUTABLE_PACKAGE", "PAPER_ELIGIBLE"}
        ),
        rv_paper_eligible=sum(1 for row in rv_audit if row.get("paper_trade_eligible") == "true"),
        rv_skipped=sum(1 for row in latest_rv_rows if row.get("execution_status") == "SKIPPED"),
        top_package_rejections=top_package_rejections,
        top_sanity_blockers=sanity_blockers.most_common(6),
    )


def build_checklist(metrics: ProjectMetrics) -> list[dict[str, str]]:
    rows = [
        checklist_row(
            "research_design",
            "Auditable research question and falsifiable evidence",
            "NOT_EVALUATED",
            f"{metrics.total_signals} loaded rows do not establish prospective registration",
            "protocol timing and held-out design are not verified by this report",
            "Verify the frozen hypothesis, selection rules and evaluation dates against the study manifest.",
        ),
        checklist_row(
            "software_validation",
            "Strong engineering hygiene",
            "NOT_EVALUATED",
            "This command does not run tests or verify the current source fingerprint",
            "source-bound validation is not performed by this report",
            "Consult the validation record and rerun tests, lint, type checks and numerical replay.",
        ),
        checklist_row(
            "profitability",
            "Independent evaluation coverage",
            "BLOCKED",
            f"{metrics.snapshots} snapshots; event independence is not assessed",
            "held-out event coverage and statistical power are unverified",
            "Assess independent fixture blocks and missingness against the registered evaluation plan.",
        ),
        checklist_row(
            "profitability",
            "Tradable CLV sample",
            clv_status(metrics),
            f"{metrics.clv_observations} CLV observations, average={format_optional(metrics.average_clv)}",
            clv_blocker(metrics),
            "Collect at least 20 bid/ask-filled CLV observations before claiming signal quality.",
        ),
        checklist_row(
            "profitability",
            "Relative-value package paper eligibility",
            "BLOCKED" if metrics.rv_paper_eligible == 0 else "PASS",
            f"{metrics.rv_fully_priced} fully priced packages, {metrics.rv_paper_eligible} paper eligible",
            "stale leg quotes or missing bid/ask" if metrics.rv_paper_eligible == 0 else "",
            "Run campaign iterations on constraint-bearing markets and require live_snapshot rows before treating packages as empirical evidence.",
        ),
        checklist_row(
            "executability",
            "Relative-value snapshot source integrity",
            "PASS",
            "Collector recomputes relative-value constraints from current local markets/books before using static-report fallback",
            "",
            "Monitor relative_value_source during campaigns; static_report rows are research-only until refreshed.",
        ),
        checklist_row(
            "executability",
            "Single-leg execution realism",
            "PASS",
            "BUY_YES uses ask; SELL_YES uses bid; midpoint excluded",
            "",
            "Keep midpoint only in shadow research reports.",
        ),
        checklist_row(
            "executability",
            "Relative-value package execution realism",
            "PARTIAL",
            f"{metrics.rv_fully_priced} packages reconstruct to bid/ask legs, {metrics.relative_value_paper_trades} filled",
            top_actionable_reason(metrics.top_package_rejections),
            "Make package data fresh at collection time, then re-run paper package simulation.",
        ),
        checklist_row(
            "executability",
            "Live trading disabled",
            "PASS",
            metrics.live_status,
            "",
            "Keep live disabled; row counts cannot replace independently validated net evidence and operational safety.",
        ),
    ]
    return rows


def render_report(
    metrics: ProjectMetrics,
    checklist: list[dict[str, str]],
    checklist_output: Path,
    cv_output: Path,
) -> str:
    lines = [
        "# EdgeLab Research Readiness Report",
        "",
        "Diagnostic summary of loaded historical artifacts. Counts and CLV values are not independent performance evidence.",
        "",
        "## Executive Read",
        "",
        executive_read(metrics),
        "",
        "## Current Metrics",
        "",
        f"- snapshots: {metrics.snapshots}",
        f"- signals: {metrics.total_signals}",
        f"- positive adjusted-edge signals: {metrics.positive_adjusted_edges}",
        f"- single-leg paper candidates: {metrics.single_leg_candidates}",
        f"- relative-value fully priced packages: {metrics.rv_fully_priced}",
        f"- relative-value paper-eligible packages: {metrics.rv_paper_eligible}",
        f"- relative-value paper trades: {metrics.relative_value_paper_trades}",
        f"- CLV observations: {metrics.clv_observations}",
        f"- average market CLV: {format_optional(metrics.average_clv)}",
        f"- median market CLV: {format_optional(metrics.median_clv)}",
        f"- readiness: `{metrics.readiness}`",
        f"- live trading status: `{metrics.live_status}`",
        "",
        "## Research Controls",
        "",
        "The analysis distinguishes contract constraints, quoted prices, paper eligibility and observed follow-ups. Missing data and risk rejections remain explicit; they are not simulated fills.",
        "",
        competitiveness_gap(metrics),
        "",
        "## Architecture Flow",
        "",
        "```mermaid",
        "flowchart LR",
        '    A["Local market/order-book snapshots"] --> B["Fair-value and relative-value analysis"]',
        '    B --> C["Signal lifecycle: side edge, risk, final decision"]',
        '    C --> D["Bid/ask-only paper candidate generation"]',
        '    D --> E["Paper ledgers and package rejections"]',
        '    E --> F["CLV and evidence scorecard"]',
        '    F --> G["Research report and missing-evidence summary"]',
        '    C --> H["Sanity warnings and blockers"]',
        "    H --> F",
        "```",
        "",
        "## Possible Profitability",
        "",
        profitability_read(metrics),
        "",
        "## Executability",
        "",
        "Executability is partial. Package reconstruction works, and snapshot collection now recomputes relative-value constraints from current local markets and orderbooks before falling back to static reports. Package eligibility remains blocked by stale leg quotes, missing bid/ask, or the absence of constraint-bearing live markets in the local snapshot.",
        "",
        "## Top Package Rejections",
        "",
    ]
    if metrics.top_package_rejections:
        lines.extend(
            markdown_table(
                ["reason", "count"],
                [[reason, str(count)] for reason, count in metrics.top_package_rejections],
            )
        )
    else:
        lines.append("No package rejection rows were available.")
    lines.extend(["", "## Top Sanity Blockers", ""])
    if metrics.top_sanity_blockers:
        lines.extend(
            markdown_table(
                ["warning", "blocking_count"],
                [[reason, str(count)] for reason, count in metrics.top_sanity_blockers],
            )
        )
    else:
        lines.append("No blocking sanity warnings were available.")
    lines.extend(
        [
            "",
            "## Execution Checklist",
            "",
            f"Machine-readable checklist: `{checklist_output}`",
            "",
        ]
    )
    lines.extend(
        markdown_table(
            ["dimension", "gate", "status", "blocker", "next_action"],
            [
                [
                    row["dimension"],
                    row["gate"],
                    row["status"],
                    row["blocker"],
                    row["next_action"],
                ]
                for row in checklist
            ],
        )
    )
    lines.extend(
        [
            "",
            "## Research Summary",
            "",
            f"A short diagnostic summary is written to `{cv_output}`.",
        ]
    )
    return "\n".join(lines)


def render_cv_bullets(metrics: ProjectMetrics) -> str:
    """Retain the historical function name while emitting a factual research summary."""
    return label_legacy_report(
        "\n".join(
            [
                "# EdgeLab Research Summary",
                "",
                "- Scope: prediction-market schemas, relative-value constraints, bid/ask paper decisions, risk rejections and timed follow-up diagnostics.",
                f"- Loaded {metrics.total_signals} signal rows across repeated snapshots; {metrics.positive_adjusted_edges} exceed the configured adjusted-edge threshold. These are not independent signals or validated trading opportunities.",
                f"- Package diagnostics: {metrics.rv_candidates_analyzed} candidates, {metrics.rv_fully_priced} fully priced packages and {metrics.rv_paper_eligible} paper-eligible packages. Current multi-leg execution remains disabled.",
                "- Live orders are disabled. Positive quoted-price movement alone cannot establish profitability, fill quality or capacity.",
                "",
                (
                    "Profitability is unproven; "
                    f"unvalidated loaded CLV observations={metrics.clv_observations}, "
                    f"average market CLV={format_optional(metrics.average_clv)}."
                ),
            ]
        )
    )


def checklist_row(
    dimension: str,
    gate: str,
    status: str,
    evidence: str,
    blocker: str,
    next_action: str,
) -> dict[str, str]:
    return {
        "dimension": dimension,
        "gate": gate,
        "status": status,
        "evidence": evidence,
        "blocker": blocker,
        "next_action": next_action,
    }


def top_reason(rows: list[tuple[str, int]]) -> str:
    return rows[0][0] if rows else ""


def top_actionable_reason(rows: list[tuple[str, int]]) -> str:
    for reason, _count in rows:
        if reason != "NO_VIOLATION":
            return reason
    return top_reason(rows)


def executive_read(metrics: ProjectMetrics) -> str:
    base = (
        "The loaded artifacts separate theoretical constraints, quoted bid/ask packages, "
        "paper eligibility, risk rejection and follow-up observations. "
        "They have not been independently validated as strategy performance."
    )
    if metrics.clv_observations == 0:
        return f"{base} No CLV follow-ups were loaded."
    if (
        metrics.average_clv is not None
        and metrics.average_clv > 0
        and metrics.clv_observations >= 20
    ):
        return f"{base} The loaded CLV mean is positive, but the row count does not establish independence or realizable returns."
    return (
        f"{base} The historical quoted-price CLV sample is "
        f"{format_optional(metrics.average_clv)} over {metrics.clv_observations} observations, below the evidence gate."
    )


def competitiveness_gap(metrics: ProjectMetrics) -> str:
    if metrics.clv_observations == 0:
        return "Missing evidence: timed follow-ups, validated execution and an independent-event held-out sample."
    return (
        "The loaded data does not establish calibrated execution or independent held-out performance. "
        "More repeated snapshots alone cannot resolve those gaps."
    )


def profitability_read(metrics: ProjectMetrics) -> str:
    if metrics.clv_observations == 0:
        return (
            "No profitability claim is justified. The shortest credible path is: make relative-value packages fresh and paper eligible, "
            "validate funded leg execution and collect a predeclared held-out sample with dependence-aware uncertainty. Positive row averages do not justify scaling."
        )
    if (
        metrics.average_clv is not None
        and metrics.average_clv > 0
        and metrics.clv_observations >= 20
    ):
        return (
            "The loaded CLV mean is positive under the legacy diagnostic threshold. This does not establish tradable CLV, independence, calibrated costs or net profitability. "
            "Live trading remains unapproved."
        )
    return (
        "No profitability claim is justified. The current market-mid CLV sample is insufficient and not positive enough to support scaling. "
        "The next research step is to validate fair-value calibration, execution and independent out-of-sample evidence, without a claim of a profitable path."
    )


def clv_status(metrics: ProjectMetrics) -> str:
    if metrics.clv_observations == 0:
        return "BLOCKED"
    if metrics.clv_observations < 20:
        return "PARTIAL"
    if metrics.average_clv is not None and metrics.average_clv > 0:
        return "PASS"
    return "BLOCKED"


def clv_blocker(metrics: ProjectMetrics) -> str:
    if metrics.clv_observations == 0:
        return "no paper-filled CLV observations"
    if metrics.clv_observations < 20:
        return "fewer than 20 CLV observations"
    if metrics.average_clv is None or metrics.average_clv <= 0:
        return "non-positive average CLV"
    return ""


def format_optional(value: float | None) -> str:
    return "n/a" if value is None else f"{value:.4f}"


def read_csv_rows(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    with path.open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def write_csv(path: Path, rows: list[dict[str, str]], columns: list[str]) -> None:
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns)
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


def markdown_table(headers: list[str], rows: list[list[str]]) -> list[str]:
    output = ["| " + " | ".join(headers) + " |", "| " + " | ".join("---" for _ in headers) + " |"]
    for row in rows:
        output.append("| " + " | ".join(str(cell).replace("|", "\\|") for cell in row) + " |")
    return output


if __name__ == "__main__":
    main()
