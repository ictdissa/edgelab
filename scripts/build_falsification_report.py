#!/usr/bin/env python
from __future__ import annotations

import argparse
import csv
import json
import random
from collections import Counter, defaultdict
from pathlib import Path
from statistics import median

try:
    import _bootstrap  # noqa: F401
except ModuleNotFoundError:
    from scripts import _bootstrap  # noqa: F401

VERDICT_PROMISING = "PROMISING_BUT_UNPROVEN"
VERDICT_WEAK = "WEAK_OR_NEGATIVE"
VERDICT_NEEDS_MORE_DATA = "NEEDS_MORE_DATA"
VERDICT_KILL = "KILL_OR_PIVOT"
VERDICT_READY = "READY_FOR_PAPER_SCALING"

HORIZON_ORDER = ["1m", "5m", "15m", "1h", "6h"]
EDGE_BUCKET_ORDER = ["<0", "0-1%", "1-3%", "3-10%", ">=10%", "missing"]

MARKOUT_STAT_COLUMNS = [
    "scope",
    "group_type",
    "group",
    "horizon",
    "count",
    "mean",
    "median",
    "trimmed_mean",
    "win_rate",
    "bootstrap_ci_low",
    "bootstrap_ci_high",
]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build EdgeLab falsification campaign report")
    parser.add_argument("--experiment-dir", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--stats-output", type=Path)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    output = args.output or args.experiment_dir / "reports" / "falsification_campaign_report.md"
    stats_output = args.stats_output or args.experiment_dir / "reports" / "falsification_markout_stats.csv"
    report, stats_rows = build_report(args.experiment_dir, output, stats_output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(report, encoding="utf-8")
    write_csv(stats_output, stats_rows, MARKOUT_STAT_COLUMNS)
    summary = summarize_for_console(args.experiment_dir, stats_rows)
    print(f"falsification_report={output}")
    print(f"falsification_stats={stats_output}")
    for key, value in summary.items():
        print(f"{key}={value}")


def build_report(experiment_dir: Path, output: Path, stats_output: Path) -> tuple[str, list[dict[str, str]]]:
    data = load_experiment_data(experiment_dir)
    stats_rows = build_markout_stats(data["markouts"])
    verdict = choose_verdict(data, stats_rows)
    edge_table = edge_monotonicity_rows(data["markouts"], scope="tradable_candidate")
    monotonic = edge_monotonicity_verdict(edge_table)
    accepted_vs_rejected = accepted_rejected_rows(data["markouts"])
    suspicious_rejected = suspicious_rejected_winners(data)
    lines = [
        "# EdgeLab Falsification Campaign Report",
        "",
        "## Executive Verdict",
        "",
        verdict,
        "",
        "This report is deliberately conservative. It tests whether adjusted executable edge predicts future markout or settlement PnL. It does not approve live trading.",
        "",
        "## What Worked",
        "",
        worked_section(data),
        "",
        "## What Failed",
        "",
        failed_section(data, stats_rows),
        "",
        "## Evidence For Edge",
        "",
        evidence_for_edge(stats_rows, edge_table, monotonic),
        "",
        "## Evidence Against Edge",
        "",
        evidence_against_edge(stats_rows, edge_table, monotonic),
        "",
        "## Execution Realism",
        "",
        execution_realism_section(data),
        "",
        "## Fair-Value Reliability",
        "",
        fair_value_section(data),
        "",
        "## Relative-Value Status",
        "",
        relative_value_section(data),
        "",
        "## Data-Quality Issues",
        "",
        data_quality_section(data),
        "",
        "## Statistical Strength",
        "",
        statistical_strength_section(data, stats_rows),
        "",
        "## Edge Monotonicity",
        "",
    ]
    if edge_table:
        lines.extend(markdown_table(["edge_bucket", "count", "avg_markout", "median_markout", "win_rate"], edge_table))
    else:
        lines.append("No tradable-candidate markout rows are available for monotonicity testing.")
    lines.extend(
        [
            "",
            monotonic,
            "",
            "## Accepted Vs Rejected",
            "",
        ]
    )
    if accepted_vs_rejected:
        lines.extend(markdown_table(["evidence_type", "horizon", "count", "mean", "median", "win_rate"], accepted_vs_rejected))
    else:
        lines.append("No accepted/rejected markout comparison is available.")
    if suspicious_rejected:
        lines.extend(["", "Rejected positive markouts with data-quality warnings:"])
        lines.extend(markdown_table(["warning_code", "count"], suspicious_rejected))
    lines.extend(
        [
            "",
            "## Recommended Next Action",
            "",
            recommended_next_action(verdict, data, stats_rows, monotonic),
            "",
            "## Kill Criteria",
            "",
            "- Kill or pivot if 100+ execution-realistic paper trades still show non-positive median markout across 1m/5m/15m.",
            "- Kill or pivot if adjusted-edge buckets are non-monotonic after sufficient sample.",
            "- Kill or pivot if fair-value calibration is materially biased after settlement data arrives.",
            "- Kill relative-value execution claims until every package is live-snapshot sourced, fully priced, liquid, and payoff-reconstructable.",
            "",
            "## Continue Criteria",
            "",
            "- Continue if accepted bid/ask paper candidates show positive median markout and positive bootstrap lower bound over 100+ observations.",
            "- Continue if higher adjusted-edge buckets produce higher future markouts.",
            "- Continue if execution realism stays at 100% with conservative depth checks.",
            "- Continue if settlement PnL agrees with positive CLV once outcomes are available.",
            "",
            "## Final Judgment",
            "",
            final_judgment(verdict, data, stats_rows, monotonic),
            "",
            "## Artifact Paths",
            "",
            f"- experiment directory: `{experiment_dir}`",
            f"- report: `{output}`",
            f"- markout stats csv: `{stats_output}`",
            f"- manifest: `{experiment_dir / 'experiment_manifest.json'}`",
            f"- dataset: `{experiment_dir / 'research_dataset.sqlite'}`",
        ]
    )
    return "\n".join(lines), stats_rows


def load_experiment_data(experiment_dir: Path) -> dict[str, object]:
    reports = experiment_dir / "reports"
    snapshots = experiment_dir / "snapshots"
    data = {
        "manifest": read_json(experiment_dir / "experiment_manifest.json"),
        "campaign_manifest": read_json(snapshots / "campaign_manifest.json"),
        "campaign_state": read_json(snapshots / "campaign_state.json"),
        "snapshot_index": read_csv_rows(snapshots / "snapshot_index.csv"),
        "signals": read_snapshot_csvs(snapshots, "signals.csv"),
        "markouts": read_csv_rows(reports / "markout_observations.csv"),
        "execution": read_csv_rows(reports / "execution_realism.csv"),
        "calibration": read_csv_rows(reports / "fair_value_calibration.csv"),
        "settlement": read_csv_rows(reports / "settlement_pnl.csv"),
        "rv_audit": read_csv_rows(reports / "relative_value_candidate_audit.csv"),
        "rv_ledger": read_csv_rows(experiment_dir / "paper_trades" / "relative_value_ledger.csv"),
        "paper_ledger": read_csv_rows(experiment_dir / "paper_trades" / "ledger.csv"),
        "sanity": read_snapshot_csvs(snapshots, "edge_sanity_warnings.csv"),
    }
    return data


def build_markout_stats(markouts: list[dict[str, str]]) -> list[dict[str, str]]:
    groups: dict[tuple[str, str, str, str], list[float]] = defaultdict(list)
    for row in markouts:
        if row.get("status") != "observed":
            continue
        value = parse_optional_float(row.get("markout"))
        if value is None:
            continue
        horizon = row.get("horizon", "")
        dimensions = {
            "overall": "all",
            "evidence_type": row.get("evidence_type", "unknown"),
            "side": row.get("side", "unknown"),
            "category": row.get("category", "unknown"),
            "fair_value_source": row.get("fair_value_source", "unknown"),
            "edge_bucket": row.get("edge_bucket", "unknown"),
            "liquidity_bucket": row.get("liquidity_bucket", "unknown"),
            "spread_bucket": row.get("spread_bucket", "unknown"),
            "quote_age_bucket": row.get("quote_age_bucket", "unknown"),
            "final_decision": row.get("final_decision", "unknown"),
        }
        scope = row.get("evidence_type", "unknown")
        for group_type, group in dimensions.items():
            groups[(scope, group_type, group or "unknown", horizon)].append(value)
    return [
        stat_row(scope, group_type, group, horizon, values)
        for (scope, group_type, group, horizon), values in sorted(groups.items())
    ]


def stat_row(scope: str, group_type: str, group: str, horizon: str, values: list[float]) -> dict[str, str]:
    low, high = bootstrap_ci(values)
    return {
        "scope": scope,
        "group_type": group_type,
        "group": group,
        "horizon": horizon,
        "count": str(len(values)),
        "mean": format_optional(average(values)),
        "median": format_optional(median(values) if values else None),
        "trimmed_mean": format_optional(trimmed_mean(values)),
        "win_rate": format_optional(sum(1 for value in values if value > 0) / len(values) if values else None),
        "bootstrap_ci_low": format_optional(low),
        "bootstrap_ci_high": format_optional(high),
    }


def edge_monotonicity_rows(markouts: list[dict[str, str]], scope: str) -> list[list[str]]:
    by_bucket: dict[str, list[float]] = defaultdict(list)
    for row in markouts:
        if row.get("status") != "observed" or row.get("evidence_type") != scope:
            continue
        value = parse_optional_float(row.get("markout"))
        if value is None:
            continue
        by_bucket[row.get("edge_bucket", "missing")].append(value)
    rows = []
    for bucket in EDGE_BUCKET_ORDER:
        values = by_bucket.get(bucket, [])
        if not values:
            continue
        rows.append(
            [
                bucket,
                str(len(values)),
                format_optional(average(values)),
                format_optional(median(values)),
                format_optional(sum(1 for value in values if value > 0) / len(values)),
            ]
        )
    return rows


def edge_monotonicity_verdict(rows: list[list[str]]) -> str:
    usable = [(row[0], parse_optional_float(row[2])) for row in rows if row[0] != "missing" and parse_optional_float(row[2]) is not None]
    if len(usable) < 2:
        return "Edge monotonicity is unavailable: fewer than two adjusted-edge buckets have observed tradable markouts."
    values = [value for _bucket, value in usable if value is not None]
    monotonic = all(next_value >= value for value, next_value in zip(values, values[1:], strict=False))
    if monotonic:
        return "Adjusted-edge buckets are monotonic in this sample, but sample-size gates still apply."
    return "Current edge score does not rank opportunities reliably."


def accepted_rejected_rows(markouts: list[dict[str, str]]) -> list[list[str]]:
    groups: dict[tuple[str, str], list[float]] = defaultdict(list)
    for row in markouts:
        if row.get("status") != "observed":
            continue
        if row.get("evidence_type") not in {"tradable_candidate", "rejected_directional"}:
            continue
        value = parse_optional_float(row.get("markout"))
        if value is not None:
            groups[(row.get("evidence_type", ""), row.get("horizon", ""))].append(value)
    output = []
    for (evidence_type, horizon), values in sorted(groups.items()):
        output.append(
            [
                evidence_type,
                horizon,
                str(len(values)),
                format_optional(average(values)),
                format_optional(median(values)),
                format_optional(sum(1 for value in values if value > 0) / len(values)),
            ]
        )
    return output


def suspicious_rejected_winners(data: dict[str, object]) -> list[list[str]]:
    markouts = data["markouts"]
    sanity = data["sanity"]
    warning_by_market: dict[str, set[str]] = defaultdict(set)
    for row in sanity:
        if row.get("market_id"):
            warning_by_market[row["market_id"]].add(row.get("warning_code", "unknown"))
    counts: Counter[str] = Counter()
    for row in markouts:
        if row.get("evidence_type") != "rejected_directional":
            continue
        value = parse_optional_float(row.get("markout"))
        if value is None or value <= 0:
            continue
        for warning in warning_by_market.get(row.get("market_id", ""), {"no_warning_linked"}):
            counts[warning] += 1
    return [[key, str(value)] for key, value in counts.most_common()]


def choose_verdict(data: dict[str, object], stats_rows: list[dict[str, str]]) -> str:
    snapshots = snapshot_count(data)
    trades = len(data["paper_ledger"])
    if snapshots < 20:
        return VERDICT_NEEDS_MORE_DATA
    tradable_overall = [
        row
        for row in stats_rows
        if row["scope"] == "tradable_candidate" and row["group_type"] == "overall"
    ]
    total_obs = sum(parse_int(row["count"]) for row in tradable_overall)
    if trades == 0 or total_obs < 100:
        return VERDICT_NEEDS_MORE_DATA
    medians = [parse_optional_float(row["median"]) for row in tradable_overall]
    means = [parse_optional_float(row["mean"]) for row in tradable_overall]
    if medians and all(value is not None and value <= 0 for value in medians):
        return VERDICT_WEAK
    if means and all(value is not None and value > 0 for value in means):
        return VERDICT_PROMISING
    return VERDICT_NEEDS_MORE_DATA


def worked_section(data: dict[str, object]) -> str:
    execution = data["execution"]
    realistic = sum(1 for row in execution if row.get("fill_realistic") == "true")
    return "\n".join(
        [
            f"- Snapshot collection completed with {snapshot_count(data)} snapshots.",
            f"- Paper candidate ledger contains {len(data['paper_ledger'])} single-leg rows.",
            f"- Execution realism audit passed {realistic}/{len(execution)} rows.",
            "- Live trading stayed disabled.",
            "- Research artifacts were written into a reproducible experiment directory.",
        ]
    )


def failed_section(data: dict[str, object], stats_rows: list[dict[str, str]]) -> str:
    tradable = tradable_overall_rows(stats_rows)
    if not tradable:
        return "No tradable-candidate markout sample exists yet."
    rows = [f"- {row['horizon']}: count={row['count']}, mean={row['mean']}, median={row['median']}, win_rate={row['win_rate']}" for row in tradable]
    if resolved_settlement_count(data["settlement"]) == 0:
        rows.append("- No resolved settlement PnL rows exist.")
    if not data["calibration"]:
        rows.append("- No fair-value calibration rows exist because no settlement outcomes were available.")
    return "\n".join(rows)


def evidence_for_edge(stats_rows: list[dict[str, str]], edge_table: list[list[str]], monotonic: str) -> str:
    tradable = tradable_overall_rows(stats_rows)
    positive = [row for row in tradable if parse_optional_float(row["mean"]) is not None and parse_optional_float(row["mean"]) > 0]
    if not positive:
        return "No robust evidence for executable edge was found in accepted paper candidates."
    return "\n".join(
        [
            f"- Positive mean tradable markout exists for {len(positive)} horizon group(s), but sample-size and monotonicity gates still apply.",
            f"- Edge monotonicity finding: {monotonic}",
        ]
    )


def evidence_against_edge(stats_rows: list[dict[str, str]], edge_table: list[list[str]], monotonic: str) -> str:
    tradable = tradable_overall_rows(stats_rows)
    non_positive_medians = [row for row in tradable if parse_optional_float(row["median"]) is not None and parse_optional_float(row["median"]) <= 0]
    rows = []
    if non_positive_medians:
        rows.append(f"- {len(non_positive_medians)} tradable horizon group(s) have non-positive median markout.")
    rows.append(f"- {monotonic}")
    if not edge_table:
        rows.append("- Edge monotonicity cannot be tested yet.")
    return "\n".join(rows)


def execution_realism_section(data: dict[str, object]) -> str:
    execution = data["execution"]
    if not execution:
        return "No execution realism rows exist; no paper fills were available."
    blockers = Counter(row.get("blocker") or "none" for row in execution if row.get("fill_realistic") != "true")
    realistic = sum(1 for row in execution if row.get("fill_realistic") == "true")
    lines = [
        f"- execution audit rows: {len(execution)}",
        f"- reconstructable bid/ask fills: {realistic}",
        "- midpoint executable fills: 0 by construction",
        "- live orders called: false",
    ]
    if blockers:
        lines.append("- execution blockers:")
        lines.extend(f"  - {key}: {value}" for key, value in blockers.most_common())
    return "\n".join(lines)


def fair_value_section(data: dict[str, object]) -> str:
    calibration = data["calibration"]
    if not calibration:
        return "Calibration is unavailable: no resolved settlement/outcome rows were found. Fair-value correctness is not proven."
    return f"Calibration rows available: {len(calibration)}. Review Brier score, log loss, and bias by source before trusting edge magnitudes."


def relative_value_section(data: dict[str, object]) -> str:
    rv_audit = data["rv_audit"]
    rv_ledger = data["rv_ledger"]
    if not rv_audit and not rv_ledger:
        return "No relative-value candidates were present."
    statuses = Counter(row.get("status", "unknown") for row in rv_audit)
    filled = sum(1 for row in rv_ledger if row.get("execution_status") == "FILLED_PAPER")
    eligible = sum(1 for row in rv_audit if row.get("paper_trade_eligible") == "true")
    return "\n".join(
        [
            f"- relative-value candidates audited: {len(rv_audit)}",
            f"- paper-eligible packages: {eligible}",
            f"- actual relative-value paper trades: {filled}",
            f"- status counts: {', '.join(f'{key}={value}' for key, value in statuses.most_common()) or 'none'}",
            "- Relative value remains research-only unless every leg is live, fresh, priced, liquid, and payoff-reconstructable.",
        ]
    )


def data_quality_section(data: dict[str, object]) -> str:
    sanity = data["sanity"]
    if not sanity:
        return "No sanity warnings were recorded."
    counts = Counter(row.get("warning_code", "unknown") for row in sanity)
    return "\n".join(f"- {key}: {value}" for key, value in counts.most_common())


def statistical_strength_section(data: dict[str, object], stats_rows: list[dict[str, str]]) -> str:
    observed = sum(parse_int(row["count"]) for row in stats_rows if row["scope"] == "tradable_candidate" and row["group_type"] == "overall")
    return "\n".join(
        [
            f"- snapshots: {snapshot_count(data)}",
            f"- tradable-candidate markout observations across horizons: {observed}",
            "- Sample is considered weak below 100 accepted-candidate observations and 20 snapshots.",
            "- Bootstrap intervals are reported in the stats CSV where sample size allows.",
        ]
    )


def recommended_next_action(verdict: str, data: dict[str, object], stats_rows: list[dict[str, str]], monotonic: str) -> str:
    if verdict == VERDICT_NEEDS_MORE_DATA:
        return "Run a longer fresh campaign: at least 20 snapshots, then preferably 100+ snapshots, with 60s spacing or longer to populate 1m/5m/15m markouts. Do not change thresholds yet."
    if verdict == VERDICT_WEAK:
        return "Do not scale. Investigate fair-value calibration and data-quality blockers before collecting more paper fills."
    if verdict == VERDICT_PROMISING:
        return "Continue paper testing only. Require positive median markout, monotonic edge buckets, and settlement confirmation before any live-trading discussion."
    return "Stop trading research on this signal until a new falsifiable hypothesis is defined."


def final_judgment(verdict: str, data: dict[str, object], stats_rows: list[dict[str, str]], monotonic: str) -> str:
    return (
        f"Final judgment: `{verdict}`. Infrastructure is functioning, but executable alpha is not established. "
        f"{monotonic} Live trading remains disabled."
    )


def tradable_overall_rows(stats_rows: list[dict[str, str]]) -> list[dict[str, str]]:
    return [
        row
        for row in stats_rows
        if row["scope"] == "tradable_candidate" and row["group_type"] == "overall"
    ]


def summarize_for_console(experiment_dir: Path, stats_rows: list[dict[str, str]]) -> dict[str, str]:
    data = load_experiment_data(experiment_dir)
    edge_table = edge_monotonicity_rows(data["markouts"], scope="tradable_candidate")
    tradable_1m = next(
        (
            row
            for row in stats_rows
            if row["scope"] == "tradable_candidate"
            and row["group_type"] == "overall"
            and row["horizon"] == "1m"
        ),
        {},
    )
    return {
        "snapshots": str(snapshot_count(data)),
        "signals": str(len(data["signals"])),
        "paper_trades": str(len(data["paper_ledger"])),
        "tradable_1m_count": tradable_1m.get("count", "0"),
        "tradable_1m_mean": tradable_1m.get("mean", ""),
        "tradable_1m_median": tradable_1m.get("median", ""),
        "edge_monotonicity": edge_monotonicity_verdict(edge_table),
        "settlement_rows": str(resolved_settlement_count(data["settlement"])),
        "calibration_rows": str(len(data["calibration"])),
        "rv_paper_trades": str(sum(1 for row in data["rv_ledger"] if row.get("execution_status") == "FILLED_PAPER")),
        "verdict": choose_verdict(data, stats_rows),
    }


def snapshot_count(data: dict[str, object]) -> int:
    campaign = data.get("campaign_manifest", {})
    if isinstance(campaign, dict):
        totals = campaign.get("totals", {})
        if isinstance(totals, dict):
            parsed = parse_int(str(totals.get("snapshots_collected", "")))
            if parsed:
                return parsed
    return len(data["snapshot_index"])


def resolved_settlement_count(rows: list[dict[str, str]]) -> int:
    return sum(
        1
        for row in rows
        if row.get("final_payout") not in {"", None}
        and (row.get("settlement_status") or row.get("status")) not in {"missing_settlement_or_trade", "unresolved"}
    )


def read_snapshot_csvs(snapshot_dir: Path, filename: str) -> list[dict[str, str]]:
    rows = []
    for path in sorted(snapshot_dir.glob(f"iteration_*/{filename}")):
        rows.extend(read_csv_rows(path))
    return rows


def read_json(path: Path) -> dict[str, object]:
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}
    return payload if isinstance(payload, dict) else {}


def bootstrap_ci(values: list[float]) -> tuple[float | None, float | None]:
    if len(values) < 2:
        return None, None
    rng = random.Random(31)
    means = []
    for _ in range(1000):
        sample = [values[rng.randrange(len(values))] for _ in values]
        means.append(sum(sample) / len(sample))
    means.sort()
    return means[int(0.025 * (len(means) - 1))], means[int(0.975 * (len(means) - 1))]


def trimmed_mean(values: list[float]) -> float | None:
    if not values:
        return None
    if len(values) < 5:
        return average(values)
    ordered = sorted(values)
    trim = max(1, int(len(ordered) * 0.1))
    trimmed = ordered[trim:-trim]
    return average(trimmed or ordered)


def average(values: list[float]) -> float | None:
    return sum(values) / len(values) if values else None


def parse_optional_float(value: str | None) -> float | None:
    if value in {None, ""}:
        return None
    try:
        return float(value)
    except ValueError:
        return None


def parse_int(value: str | None) -> int:
    if value in {None, ""}:
        return 0
    try:
        return int(float(value))
    except ValueError:
        return 0


def format_optional(value: float | None) -> str:
    return "" if value is None else f"{value:.10g}"


def read_csv_rows(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    with path.open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def write_csv(path: Path, rows: list[dict[str, str]], columns: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
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
