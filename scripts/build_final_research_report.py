#!/usr/bin/env python
from __future__ import annotations

import argparse
import csv
from collections import Counter
from pathlib import Path
from statistics import median

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

MIN_PROFITABILITY_CLV_OBSERVATIONS = 100


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Assemble EdgeLab research and execution diagnostics"
    )
    parser.add_argument("--snapshot-dir", type=Path, default=Path("data/snapshots_campaign"))
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
    parser.add_argument("--output", type=Path, default=Path("reports/final_research_report.md"))
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    report = build_report(
        [args.snapshot_dir, *args.extra_snapshot_dir],
        args.single_leg_ledger,
        args.relative_value_ledger,
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(report, encoding="utf-8")
    print(f"report={args.output}")


def build_report(
    snapshot_dirs: list[Path], single_leg_ledger: Path, relative_value_ledger: Path
) -> str:
    records = load_snapshot_records_from_dirs(snapshot_dirs)
    primary_snapshot_dir = snapshot_dirs[0] if snapshot_dirs else Path("data/snapshots_campaign")
    scorecard = build_scorecard(
        records,
        single_leg_ledger,
        relative_value_ledger_path=relative_value_ledger,
        campaign_artifacts=load_campaign_artifacts(primary_snapshot_dir, Path("reports")),
    )
    all_signals = [row for record in records for row in record.signals]
    category_counts = Counter(row.get("category", "unknown") or "unknown" for row in all_signals)
    rejection_counts = Counter(
        reason
        for row in all_signals
        for reason in split_reasons(row.get("rejection_reason") or row.get("risk_reasons") or "")
    )
    liquidity_values = [
        value
        for row in all_signals
        if (value := parse_float_or_none(row.get("liquidity"))) is not None
    ]
    spread_values = [
        value
        for row in all_signals
        if (value := parse_float_or_none(row.get("spread"))) is not None
    ]
    tradable_clv_observations = (
        scorecard.clv_observations + scorecard.relative_value_clv_observations
    )
    robustness_rows = read_csv_rows(Path("reports/clv_robustness.csv"))
    execution_rows = read_csv_rows(Path("reports/execution_realism.csv"))
    no_profitability_claim = (
        tradable_clv_observations < MIN_PROFITABILITY_CLV_OBSERVATIONS
        or scorecard.average_clv is None
        or scorecard.average_clv <= 0
        or scorecard.median_clv is None
        or scorecard.median_clv <= 0
        or scorecard.clv_win_rate is None
        or scorecard.clv_win_rate <= 0.5
    )
    lines = [
        "# EdgeLab Final Research Report",
        "",
        "## 1. Abstract",
        "",
        "EdgeLab is a prediction-market research system for separating apparent edge from executable edge. "
        "It collects repeated market snapshots, estimates fair value, applies explicit risk gates, simulates paper fills at bid/ask, and measures CLV when follow-up snapshots exist. Live trading is disabled.",
        "",
        "## 2. Research Question",
        "",
        "Can Polymarket-style contracts produce repeatable, executable mispricings after spread, liquidity, freshness, and sanity checks?",
        "",
        "## 3. System Architecture",
        "",
        "The pipeline is snapshot collection -> signal lifecycle enrichment -> sanity warning triage -> candidate generation -> paper simulation -> CLV measurement -> evidence scorecard. Provider and live-adapter code is not modified by this report.",
        "",
        "## 4. Market Universe",
        "",
    ]
    if category_counts:
        lines.extend(
            markdown_table(
                ["category", "signals"],
                [[key, str(value)] for key, value in category_counts.items()],
            )
        )
    else:
        lines.append("No market signals were loaded from the selected snapshot directory.")
    lines.extend(
        [
            "",
            "## 5. Fair-Value Sources",
            "",
            "Crypto signals use the existing crypto threshold fair-value layer. Relative-value signals use internal logical constraints across related contracts. Mock or synthetic rows are treated as plumbing checks, not profitability evidence.",
            "",
            "## 6. Execution Assumptions",
            "",
            "Single-leg paper buys execute at ask and inventory sales at bid. Package fills remain disabled pending funded leg execution. Current follow-ups use exact-token future bid/ask with verified depth; midpoint and model-fair movement are shadow diagnostics only. Loaded legacy rows are not certified by this report.",
            "",
            "## 7. Signal Lifecycle",
            "",
            f"- total signals: {scorecard.total_signals}",
            f"- positive raw-edge signals: {scorecard.positive_raw_edge_count}",
            f"- positive adjusted-edge signals: {scorecard.positive_adjusted_edge_count}",
            f"- rejected directional edges: {scorecard.rejected_directional_edge_count}",
            f"- single-leg paper candidates: {scorecard.single_leg_paper_candidates}",
            f"- relative-value executable candidates: {scorecard.relative_value_executable_candidates}",
            f"- relative-value paper-eligible candidates: {scorecard.relative_value_paper_eligible_candidates}",
            "",
            "## 8. Risk Controls",
            "",
            "Risk gates require explicit side, executable quote, fresh data, sufficient liquidity, acceptable spread, and no blocking sanity warning. Risk rejection is preserved in reports rather than hidden behind HOLD or AVOID labels.",
            "",
            "## 9. Snapshot Campaign Results",
            "",
            f"- snapshots: {scorecard.total_snapshots}",
            f"- markets scanned: {scorecard.markets_scanned}",
            f"- matched markets: {scorecard.matched_markets}",
            f"- readiness classification: `{scorecard.readiness_classification}`",
            "",
            "## 10. Directional Edge Results",
            "",
            f"Directional signals produced {scorecard.positive_adjusted_edge_count} positive adjusted-edge rows, but only {scorecard.single_leg_paper_candidates} passed all single-leg paper-candidate filters.",
            "",
            "## 11. Relative-Value Results",
            "",
            f"Relative-value logic found {scorecard.relative_value_executable_candidates} empirically executable candidate rows and {scorecard.relative_value_paper_eligible_candidates} paper-eligible multi-leg packages under the current artifact set. Research-only or static-report packages are not treated as executable evidence and are not routed through the single-leg paper trader.",
            "",
            "## 12. Paper Trading Results",
            "",
            f"- single-leg paper trades: {scorecard.single_leg_paper_trades}",
            f"- relative-value paper trades: {scorecard.relative_value_paper_trades}",
            "",
            "## 13. CLV Analysis",
            "",
            f"- single-leg tradable CLV observations: {scorecard.clv_observations}",
            f"- relative-value package CLV observations: {scorecard.relative_value_clv_observations}",
            f"- average tradable CLV: {format_optional(scorecard.average_clv)}",
            f"- median tradable CLV: {format_optional(scorecard.median_clv)}",
            f"- tradable CLV win rate: {format_optional(scorecard.clv_win_rate)}",
            f"- CLV robustness verdict: {overall_value(robustness_rows, 'verdict') or 'not_available'}",
            "",
            "## 14. Sanity-Warning Analysis",
            "",
        ]
    )
    if scorecard.sanity_warning_counts:
        lines.extend(
            markdown_table(
                ["warning", "count"],
                [[key, str(value)] for key, value in scorecard.sanity_warning_counts.items()],
            )
        )
    else:
        lines.append("No sanity warnings were loaded.")
    lines.extend(["", "### Top Blockers", ""])
    if scorecard.top_blockers:
        lines.extend(
            markdown_table(
                ["blocker", "count"],
                [[key, str(value)] for key, value in scorecard.top_blockers.items()],
            )
        )
    else:
        lines.append("No blocking warnings were loaded.")
    lines.extend(
        [
            "",
            "## 15. Execution Realism",
            "",
            execution_realism_statement(execution_rows),
            "",
            "## 16. CLV Robustness",
            "",
            robustness_statement(robustness_rows),
            "",
            "## 17. Failure Modes",
            "",
        ]
    )
    if rejection_counts:
        lines.extend(
            markdown_table(
                ["rejection", "count"],
                [[key, str(value)] for key, value in rejection_counts.most_common(10)],
            )
        )
    else:
        lines.append(
            "Current failure modes are dominated by missing campaign data or strict risk gates, depending on the selected snapshot directory."
        )
    lines.extend(
        [
            "",
            "## 18. Evidence For Or Against Profitability",
            "",
            profitability_statement(
                no_profitability_claim,
                scorecard.average_clv,
                scorecard.median_clv,
                scorecard.clv_win_rate,
                tradable_clv_observations,
            ),
            "",
            "## 19. What Would Be Needed Before Real Money",
            "",
            "Live trading remains disabled. No row-count threshold authorizes real money. Settlement equivalence, calibrated execution costs, independent held-out evidence, risk scenarios and account reconciliation remain required.",
            "",
            "## 20. Future Work",
            "",
            "- Run the 72-iteration crypto and relative-value snapshot campaign.",
            "- Resolve blocking sanity warnings without weakening risk gates.",
            "- Convert executable relative-value rows into fully priced multi-leg paper packages.",
            "- Measure CLV at 5m, 30m, and 1h before expanding paper size.",
            "",
            "## Tables",
            "",
            "### Liquidity And Spread",
            "",
            f"- median liquidity: {format_optional(median(liquidity_values) if liquidity_values else None)}",
            f"- median spread: {format_optional(median(spread_values) if spread_values else None)}",
        ]
    )
    return label_legacy_report("\n".join(lines))


def profitability_statement(
    no_profitability_claim: bool,
    average_clv: float | None,
    median_clv: float | None,
    win_rate: float | None,
    observations: int,
) -> str:
    if no_profitability_claim:
        return (
            "No profitability claim is made. Existing results are either infrastructure validation, mock/synthetic plumbing, "
            "or insufficient tradable CLV evidence. "
            f"Current observations={observations}; average={format_optional(average_clv)}; "
            f"median={format_optional(median_clv)}; win_rate={format_optional(win_rate)}; "
            f"required_observations={MIN_PROFITABILITY_CLV_OBSERVATIONS}."
        )
    if average_clv is not None and average_clv > 0 and median_clv is not None and median_clv > 0:
        return (
            "Average and median tradable CLV are positive in the loaded evidence set, which supports continued paper testing. "
            "It is not live-profit evidence and does not approve real-money trading."
        )
    return "The loaded evidence does not support a profitability claim."


def execution_realism_statement(rows: list[dict[str, str]]) -> str:
    if not rows:
        return "Execution realism audit is not available for this report run."
    realistic = sum(1 for row in rows if row.get("fill_realistic") == "true")
    total = len(rows)
    return (
        f"Execution realism audit loaded {total} records; {realistic} were reconstructable as conservative bid/ask fills. "
        "The audit records `live_order_called=false` and excludes midpoint fills from tradable evidence."
    )


def robustness_statement(rows: list[dict[str, str]]) -> str:
    verdict = overall_value(rows, "verdict")
    observations = overall_value(rows, "observations")
    if not verdict:
        return "CLV robustness audit is not available for this report run."
    return (
        f"Overall CLV robustness verdict is `{verdict}` over {observations or 'n/a'} observations."
    )


def overall_value(rows: list[dict[str, str]], column: str) -> str:
    overall = next(
        (row for row in rows if row.get("group_type") == "overall" and row.get("group") == "all"),
        None,
    )
    return "" if overall is None else overall.get(column, "")


def split_reasons(value: str) -> list[str]:
    return [
        part.strip()
        for part in value.replace("[", "").replace("]", "").replace('"', "").split(";")
        if part.strip()
    ]


def read_csv_rows(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    with path.open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def parse_float_or_none(value: str | None) -> float | None:
    if value in {None, ""}:
        return None
    try:
        return float(value)
    except ValueError:
        return None


def format_optional(value: float | int | None) -> str:
    if value is None:
        return "n/a"
    if isinstance(value, int):
        return str(value)
    return f"{value:.4f}"


def markdown_table(headers: list[str], rows: list[list[str]]) -> list[str]:
    output = ["| " + " | ".join(headers) + " |", "| " + " | ".join("---" for _ in headers) + " |"]
    for row in rows:
        output.append("| " + " | ".join(str(cell).replace("|", "\\|") for cell in row) + " |")
    return output


if __name__ == "__main__":
    main()
