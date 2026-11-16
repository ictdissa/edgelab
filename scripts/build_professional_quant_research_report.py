#!/usr/bin/env python
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from pathlib import Path
from statistics import median

try:
    import _bootstrap  # noqa: F401
except ModuleNotFoundError:
    from scripts import _bootstrap  # noqa: F401

from scripts.empirical_audit_utils import (
    average,
    default_experiment_dir,
    format_float,
    load_snapshot_signals,
    load_snapshot_warnings,
    markdown_table,
    parse_float,
    read_csv_rows,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build top-level professional quant research report")
    parser.add_argument("--experiment-dir", type=Path, default=default_experiment_dir())
    parser.add_argument("--output", type=Path, default=Path("professional_quant_research_report.md"))
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    report = build_report(args.experiment_dir)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(report, encoding="utf-8")
    print(f"professional_report={args.output}")


def build_report(experiment_dir: Path) -> str:
    reports = experiment_dir / "reports"
    snapshots = experiment_dir / "snapshots"
    markouts = read_csv_rows(reports / "markout_observations.csv")
    signals = load_snapshot_signals(snapshots)
    warnings = load_snapshot_warnings(snapshots)
    ledger = read_csv_rows(experiment_dir / "paper_trades" / "ledger.csv")
    rv_status = read_csv_rows(reports / "relative_value_empirical_status.csv")
    forensics = read_csv_rows(reports / "rejected_winner_forensics.csv")
    markout_audit = read_csv_rows(reports / "markout_computation_audit.csv")
    independence = read_csv_rows(reports / "observation_independence_table.csv")
    segment_stats = read_csv_rows(reports / "signal_segment_stats.csv")
    evidence_scorecard = read_text(reports / "evidence_scorecard.md")
    settlement = read_csv_rows(reports / "settlement_pnl.csv")
    calibration = read_csv_rows(reports / "fair_value_calibration.csv")
    rv_exec = sum(1 for row in rv_status if row.get("empirical_status") == "LIVE_EXECUTABLE_PACKAGE")
    rv_traded = sum(1 for row in rv_status if row.get("empirical_status") == "PAPER_TRADED_PACKAGE")
    observed = [row for row in markouts if row.get("status") == "observed" and parse_float(row.get("markout")) is not None]
    tradable = [row for row in observed if row.get("evidence_type") == "tradable_candidate"]
    accepted_by_horizon = horizon_table(tradable)
    edge_table = edge_bucket_table(tradable)
    warning_counts = Counter(row.get("warning_code", "") for row in warnings)
    forensic_counts = Counter(row.get("diagnostic_classification", "") for row in forensics)
    audit_violations = [row for row in markout_audit if row.get("severity") == "VIOLATION"]
    readiness = extract_scorecard_field(evidence_scorecard, "readiness classification") or "unknown"
    verdict = choose_professional_verdict(readiness, accepted_by_horizon, audit_violations, rv_exec, settlement, calibration)
    strongest, weakest = strongest_weakest_segments(segment_stats)
    lines = [
        "# EdgeLab Professional Quant Research Report",
        "",
        "## 1. Executive Summary",
        "",
        "EdgeLab is a Polymarket-style prediction-market quant research platform. It is not a live trading system. "
        "It computes executable bid/ask edge rather than midpoint fantasy edge, applies conservative paper fills, "
        "and produces CLV/markout, execution realism, fair-value calibration, settlement PnL, and relative-value audit artifacts. "
        "Current empirical evidence is weak or data-quality limited: accepted-candidate medians are not positive, settlement PnL is absent, calibration is absent, and relative value has no empirical paper-traded package.",
        "",
        "## 2. Research Question",
        "",
        "Primary question: Does adjusted executable edge predict future tradable markout or settlement PnL out of sample?",
        "",
        "Secondary questions: Are rejected positive-edge signals real missed alpha or data artifacts? Are fair values calibrated? Are relative-value packages actually executable? Which categories/sources, if any, contain signal?",
        "",
        "## 3. System Design",
        "",
        "market data -> fair value -> executable bid/ask edge -> cost adjustment -> freshness/liquidity/risk gates -> paper candidate / rejection / watchlist -> future markout -> evidence scorecard",
        "",
        "The design separates theoretical edge from executable edge, bid/ask fills from midpoint marks, accepted from rejected signals, single-leg from multi-leg relative-value packages, and research readiness from live readiness.",
        "",
        "## 4. Falsification Campaign Results",
        "",
        f"- snapshots: {metric_from_independence(independence, 'unique_snapshots')}",
        f"- signals: {len(signals)}",
        f"- positive adjusted-edge signals: {sum(1 for row in signals if (parse_float(row.get('best_adjusted_edge') or row.get('adjusted_edge')) or 0.0) > 0)}",
        f"- paper trades: {len(ledger)}",
        f"- execution-realistic fills: {execution_realistic_count(reports)}",
        f"- CLV/markout observations: {len(tradable)} accepted-candidate observed rows",
        f"- settlement PnL rows with resolved payout: {resolved_settlement_count(settlement)}",
        f"- calibration rows: {len(calibration)}",
        f"- RV candidates audited: {len(rv_status)}",
        f"- RV empirical executable packages: {rv_exec}",
        f"- RV paper trades: {rv_traded}",
        "",
        "Accepted-candidate markouts by horizon:",
        "",
    ]
    lines.extend(markdown_table(["horizon", "count", "mean", "median", "win_rate"], accepted_by_horizon))
    lines.extend(["", "Edge monotonicity table:", ""])
    lines.extend(markdown_table(["edge_bucket", "count", "mean", "median", "win_rate"], edge_table))
    lines.extend(
        [
            "",
            "Medians are the key sanity check. In the latest campaign, accepted-candidate median markouts are not robustly positive.",
            "",
            "## 5. Forensic Findings",
            "",
            f"- rejected/accepted rows inspected: {len(forensics)}",
            f"- top diagnostic classes: {', '.join(f'{k}={v}' for k, v in forensic_counts.most_common(5)) or 'none'}",
            f"- top warning sources: {', '.join(f'{k}={v}' for k, v in warning_counts.most_common(5)) or 'none'}",
            "",
            "Rejected winners should not be treated as missed alpha unless they are reconstructable, executable at signal time, use conservative future bid/ask marks, and are free of stale/suspicious warnings. Threshold loosening is not justified by the current forensic evidence.",
            "",
            "## 6. Markout Correctness",
            "",
            f"- markout audit violations: {len(audit_violations)}",
            "- BUY paper fills use ask entry and conservative future bid for tradable CLV.",
            "- SELL paper fills use bid entry and conservative future ask for tradable CLV.",
            "- midpoint/model marks are shadow evidence and are not executable CLV.",
            (
                "- Current accepted-candidate CLV is mechanically trustworthy under the audit."
                if not audit_violations
                else "- Markout violations exist; affected reports should be regenerated before interpretation."
            ),
            "",
            "## 7. Statistical Independence",
            "",
        ]
    )
    lines.extend(
        markdown_table(
            ["metric", "value"],
            [
                ["raw markout rows", metric_from_independence(independence, "raw_markout_rows")],
                ["independent paper fills", metric_from_independence(independence, "unique_paper_fills")],
                ["independent markets", metric_from_independence(independence, "unique_markets")],
                ["independent tokens", metric_from_independence(independence, "unique_tokens")],
                ["independent snapshot pairs", metric_from_independence(independence, "unique_snapshot_pairs")],
            ],
        )
    )
    lines.extend(
        [
            "",
            "Repeated multi-horizon observations are not independent evidence. The effective sample size is bounded by fills, markets, tokens, and snapshot pairs.",
            "",
            "## 8. Segment Analysis",
            "",
            f"- strongest category/source segment: {strongest}",
            f"- weakest category/source segment: {weakest}",
            "No segment should be promoted unless median markout, confidence intervals, warning fractions, and edge monotonicity all survive decomposition.",
            "",
            "## 9. Execution Realism",
            "",
            "Paper fills are conservative bid/ask fills and live orders remain disabled. The execution audit checks quote source, depth sufficiency, spread sanity, and reconstructability. Midpoint fills are excluded from tradable PnL.",
            "",
            "## 10. Fair-Value Reliability",
            "",
            (
                "Fair-value correctness is not proven: no settlement/calibration rows exist, so Brier score, log loss, realized frequency, and source-level bias are unavailable."
                if not calibration
                else "Calibration rows exist and must be reviewed by source before trusting edge magnitudes."
            ),
            "",
            "## 11. Relative-Value Status",
            "",
            f"RV candidates exist, but empirical executable RV packages are {rv_exec} and paper-traded RV packages are {rv_traded}. RV should be described as a logical-relationship discovery/audit engine, not a live or paper-proven trading strategy.",
            "",
            "## 12. Evidence For Edge",
            "",
            "- The infrastructure can falsify claims and produced conservative bid/ask paper fills.",
            "- Some higher adjusted-edge buckets have better mean markout than lower buckets.",
            "- The system now identifies data contamination instead of hiding it.",
            "",
            "## 13. Evidence Against Edge",
            "",
            "- Accepted-candidate medians are not robustly positive.",
            "- Win rates are weak.",
            "- There is no settlement PnL.",
            "- There is no fair-value calibration.",
            "- Data-quality warnings are concentrated.",
            "- RV has zero empirical paper-traded packages.",
            "",
            "## 14. Final Verdict",
            "",
            verdict,
            "",
            "## 15. Roadmap To Make This Competitive",
            "",
            "Phase 1: fix data-quality blockers, validate markout correctness, verify rejected winners, and de-duplicate observations.",
            "",
            "Phase 2: collect a larger fresh campaign with 100+ snapshots and 1,000+ conservative paper fills; segment by category/source and test edge monotonicity.",
            "",
            "Phase 3: add settlement PnL and calibration once outcomes exist; compare fair value to realized frequency and disable biased sources.",
            "",
            "Phase 4: only consider paper scaling if positive median CLV and positive settlement evidence appear.",
            "",
            "Phase 5: live trading remains disabled until evidence is statistically and operationally robust.",
            "",
            "## 16. Resume / Interview Framing",
            "",
            "Built EdgeLab, a prediction-market quant research platform that computes fair value, executable bid/ask edge, risk-gated paper fills, multi-horizon CLV, execution realism audits, and falsification reports. A 20-snapshot empirical campaign produced conservative paper fills and weak/negative median markouts, leading to data-quality forensics rather than premature profitability claims.",
            "",
            "Avoid claiming this is a profitable Polymarket trading bot.",
            "",
            "## 17. Appendix",
            "",
        ]
    )
    artifacts = [
        reports / "rejected_winner_forensics.md",
        reports / "markout_computation_audit.md",
        reports / "observation_independence_report.md",
        reports / "signal_segment_report.md",
        reports / "relative_value_empirical_status.md",
        reports / "evidence_scorecard.md",
        reports / "falsification_campaign_report.md",
        reports / "final_research_report.md",
    ]
    lines.extend(f"- `{path}`" for path in artifacts)
    return "\n".join(lines)


def horizon_table(rows: list[dict[str, str]]) -> list[list[str]]:
    groups: dict[str, list[float]] = defaultdict(list)
    for row in rows:
        value = parse_float(row.get("markout"))
        if value is not None:
            groups[row.get("horizon", "")].append(value)
    output = []
    for horizon in ["1m", "5m", "15m", "1h", "6h"]:
        values = groups.get(horizon, [])
        if values:
            output.append([horizon, str(len(values)), format_float(average(values)), format_float(median(values)), format_float(sum(1 for value in values if value > 0) / len(values))])
    return output or [["none", "0", "n/a", "n/a", "n/a"]]


def edge_bucket_table(rows: list[dict[str, str]]) -> list[list[str]]:
    groups: dict[str, list[float]] = defaultdict(list)
    for row in rows:
        value = parse_float(row.get("markout"))
        if value is not None:
            groups[row.get("edge_bucket", "missing")].append(value)
    output = []
    for bucket in ["<0", "0-1%", "1-3%", "3-10%", ">=10%", "missing"]:
        values = groups.get(bucket, [])
        if values:
            output.append([bucket, str(len(values)), format_float(average(values)), format_float(median(values)), format_float(sum(1 for value in values if value > 0) / len(values))])
    return output or [["none", "0", "n/a", "n/a", "n/a"]]


def execution_realistic_count(reports: Path) -> str:
    rows = read_csv_rows(reports / "execution_realism.csv")
    if not rows:
        return "0/0"
    realistic = sum(1 for row in rows if row.get("fill_realistic") == "true")
    return f"{realistic}/{len(rows)}"


def resolved_settlement_count(rows: list[dict[str, str]]) -> int:
    return sum(
        1
        for row in rows
        if row.get("final_payout") not in {"", None}
        and (row.get("settlement_status") or row.get("status")) not in {"missing_settlement_or_trade", "unresolved"}
    )


def metric_from_independence(rows: list[dict[str, str]], metric: str) -> str:
    return next((row.get("value", "") for row in rows if row.get("metric") == metric and row.get("group") == "overall"), "n/a")


def extract_scorecard_field(text: str, field: str) -> str:
    for line in text.splitlines():
        if field in line and ":" in line:
            return line.split(":", 1)[1].strip().strip("`")
    return ""


def strongest_weakest_segments(rows: list[dict[str, str]]) -> tuple[str, str]:
    candidates = [
        row
        for row in rows
        if row.get("evidence_type") == "tradable_candidate"
        and row.get("segment_type") in {"category", "fair_value_source"}
        and int(float(row.get("count") or 0)) >= 20
    ]
    if not candidates:
        return "none", "none"
    strongest = max(candidates, key=lambda row: parse_float(row.get("median_markout")) or -999)
    weakest = min(candidates, key=lambda row: parse_float(row.get("median_markout")) or 999)
    return (
        f"{strongest['segment_type']}={strongest['segment']} {strongest['horizon']} median={strongest['median_markout']}",
        f"{weakest['segment_type']}={weakest['segment']} {weakest['horizon']} median={weakest['median_markout']}",
    )


def choose_professional_verdict(
    readiness: str,
    accepted_by_horizon: list[list[str]],
    audit_violations: list[dict[str, str]],
    rv_exec: int,
    settlement: list[dict[str, str]],
    calibration: list[dict[str, str]],
) -> str:
    medians = [parse_float(row[3]) for row in accepted_by_horizon if row[3] != "n/a"]
    if audit_violations:
        return "NEEDS_DATA_QUALITY_FIXES"
    if readiness == "NEEDS_DATA_QUALITY_FIXES":
        return "NEEDS_DATA_QUALITY_FIXES"
    if medians and all(value is not None and value <= 0 for value in medians):
        return "WEAK_OR_NEGATIVE"
    if not settlement or not calibration or rv_exec == 0:
        return "PROMISING_BUT_UNPROVEN" if medians and any((value or 0) > 0 for value in medians) else "NEEDS_DATA_QUALITY_FIXES"
    return "READY_FOR_EXTENDED_PAPER_RESEARCH"


def read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8") if path.exists() else ""


if __name__ == "__main__":
    main()
