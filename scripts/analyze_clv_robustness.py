#!/usr/bin/env python
from __future__ import annotations

import argparse
import csv
import random
import math
from collections import defaultdict
from pathlib import Path
from statistics import median

try:
    import _bootstrap  # noqa: F401
except ModuleNotFoundError:
    from scripts import _bootstrap  # noqa: F401

MIN_ROBUST_OBSERVATIONS = 20
BOOTSTRAP_SAMPLES = 1000

ROBUSTNESS_COLUMNS = [
    "group_type",
    "group",
    "metric",
    "observations",
    "independent_blocks",
    "average_clv",
    "trade_weighted_clv",
    "estimand",
    "median_clv",
    "win_rate",
    "bootstrap_ci_low",
    "bootstrap_ci_high",
    "average_without_best",
    "average_without_worst",
    "average_without_extremes",
    "verdict",
]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Analyze CLV robustness without changing thresholds"
    )
    parser.add_argument("--ledger", type=Path, default=Path("data/paper_trades/ledger.csv"))
    parser.add_argument(
        "--clv-observations", type=Path, default=Path("reports/clv_observations.csv")
    )
    parser.add_argument("--output", type=Path, default=Path("reports/clv_robustness.md"))
    parser.add_argument("--csv-output", type=Path)
    parser.add_argument("--metric", choices=["clv", "net_markout_estimate"], default="clv")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    ledger_rows = {row.get("paper_trade_id", ""): row for row in read_csv_rows(args.ledger)}
    observed_rows = [
        attach_ledger(row, ledger_rows.get(row.get("paper_trade_id", ""), {}))
        for row in read_csv_rows(args.clv_observations)
        if row.get("status") == "observed"
    ]
    group_rows = build_robustness_rows(observed_rows, args.metric)
    csv_output = args.csv_output or args.output.with_suffix(".csv")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    csv_output.parent.mkdir(parents=True, exist_ok=True)
    write_csv(csv_output, group_rows, ROBUSTNESS_COLUMNS)
    args.output.write_text(
        render_report(observed_rows, group_rows, args.ledger, args.clv_observations, csv_output),
        encoding="utf-8",
    )
    print(f"observed_clv={len(observed_rows)}")
    print(f"groups={len(group_rows)}")
    print(f"report={args.output}")
    print(f"csv={csv_output}")


def attach_ledger(row: dict[str, str], ledger: dict[str, str]) -> dict[str, str]:
    merged = dict(row)
    merged["fair_value_source"] = ledger.get("fair_value_source", "")
    merged["spread_at_entry"] = ledger.get("spread_at_entry", "")
    merged["liquidity_at_entry"] = ledger.get("liquidity_at_entry", "")
    merged["adjusted_edge_at_entry"] = ledger.get("adjusted_edge_at_entry", "")
    merged["event_id"] = ledger.get("event_id", "")
    merged["risk_event_id"] = ledger.get("risk_event_id", "")
    merged["category"] = ledger.get("category", row.get("category", ""))
    return merged


def build_robustness_rows(rows: list[dict[str, str]], metric: str = "clv") -> list[dict[str, str]]:
    groups: dict[tuple[str, str], list[dict[str, str]]] = defaultdict(list)
    for row in rows:
        # Declare one primary horizon for future experiments; historical analysis is exploratory.
        if row.get("horizon") == "5m":
            groups[("overall", "all")].append(row)
            groups[("side", row.get("side", "unknown") or "unknown")].append(row)
            groups[
                ("fair_value_source", row.get("fair_value_source", "unknown") or "unknown")
            ].append(row)
        groups[("horizon", row.get("horizon", "unknown") or "unknown")].append(row)
    return [
        summarize_group(group_type, group, group_rows, metric)
        for (group_type, group), group_rows in sorted(groups.items())
    ]


def summarize_group(
    group_type: str, group: str, rows: list[dict[str, str]], metric: str = "clv"
) -> dict[str, str]:
    if metric not in {"clv", "net_markout_estimate"}:
        raise ValueError("unsupported estimand metric")
    values: list[float] = []
    blocks: dict[str, list[float]] = defaultdict(list)
    seen: dict[str, float] = {}
    for row in rows:
        value = parse_optional_float(row.get(metric))
        if value is None or not -1 <= value <= 1:
            continue
        key = row.get("paper_trade_id", "")
        if key and key in seen:
            if seen[key] != value:
                raise ValueError("conflicting duplicate CLV observation")
            continue
        if key:
            seen[key] = value
        values.append(value)
        day = (row.get("entry_timestamp") or row.get("timestamp") or "unknown")[:10]
        subject = (
            f"crypto_all:{day}"
            if row.get("category", "").startswith("crypto")
            else row.get("risk_event_id")
            or row.get("event_id")
            or row.get("market_id")
            or "unknown"
        )
        blocks[subject].append(value)
    block_means = [sum(v) / len(v) for v in blocks.values()]
    ci_low, ci_high = (
        bootstrap_mean_ci(block_means)
        if len(block_means) >= MIN_ROBUST_OBSERVATIONS
        else (None, None)
    )
    return {
        "group_type": group_type,
        "group": group,
        "metric": metric,
        "observations": str(len(values)),
        "independent_blocks": str(len(block_means)),
        "average_clv": format_optional(average(block_means)),
        "trade_weighted_clv": format_optional(average(values)),
        "estimand": "equal_weight_event_or_crypto_day_mean",
        "median_clv": format_optional(median(block_means) if block_means else None),
        "win_rate": format_optional(win_rate(block_means)),
        "bootstrap_ci_low": format_optional(ci_low),
        "bootstrap_ci_high": format_optional(ci_high),
        "average_without_best": format_optional(drop_best_average(block_means)),
        "average_without_worst": format_optional(drop_worst_average(block_means)),
        "average_without_extremes": format_optional(drop_extremes_average(block_means)),
        "verdict": robustness_verdict(len(block_means), average(block_means), ci_low),
    }


def bootstrap_mean_ci(values: list[float]) -> tuple[float | None, float | None]:
    if len(values) < 2:
        return None, None
    rng = random.Random(17)
    means = []
    for _ in range(BOOTSTRAP_SAMPLES):
        sample = [values[rng.randrange(len(values))] for _ in values]
        means.append(sum(sample) / len(sample))
    means.sort()
    low_index = int(0.025 * (len(means) - 1))
    high_index = int(0.975 * (len(means) - 1))
    return means[low_index], means[high_index]


def robustness_verdict(observations: int, average_clv: float | None, ci_low: float | None) -> str:
    if observations < MIN_ROBUST_OBSERVATIONS:
        return "INSUFFICIENT_SAMPLE"
    if average_clv is None or average_clv <= 0:
        return "NON_POSITIVE_MEAN"
    if ci_low is None or ci_low <= 0:
        return "POSITIVE_BUT_NOT_ROBUST"
    return "POSITIVE_INTERVAL_EXPLORATORY"


def drop_best_average(values: list[float]) -> float | None:
    if len(values) < 2:
        return None
    return average(sorted(values)[:-1])


def drop_worst_average(values: list[float]) -> float | None:
    if len(values) < 2:
        return None
    return average(sorted(values)[1:])


def drop_extremes_average(values: list[float]) -> float | None:
    if len(values) < 3:
        return None
    return average(sorted(values)[1:-1])


def render_report(
    observed_rows: list[dict[str, str]],
    group_rows: list[dict[str, str]],
    ledger_path: Path,
    observations_path: Path,
    csv_output: Path,
) -> str:
    overall = next((row for row in group_rows if row["group_type"] == "overall"), None)
    verdict = overall["verdict"] if overall else "INSUFFICIENT_SAMPLE"
    lines = [
        "# EdgeLab CLV Robustness Report",
        "",
        "This report describes exploratory paper quote-markout evidence; it cannot authorize scaling. "
        "It does not change thresholds, fit a model, place orders, or approve live trading.",
        "",
        "## Inputs",
        "",
        f"- ledger: `{ledger_path}`",
        f"- CLV observations: `{observations_path}`",
        f"- csv: `{csv_output}`",
        f"- observed CLV rows: {len(observed_rows)}",
        "",
        "## Verdict",
        "",
        verdict_statement(verdict),
        "",
        "## Robustness Table",
        "",
    ]
    if group_rows:
        lines.extend(
            markdown_table(
                ROBUSTNESS_COLUMNS,
                [[row[column] for column in ROBUSTNESS_COLUMNS] for row in group_rows],
            )
        )
    else:
        lines.append("No observed CLV rows were available.")
    lines.extend(
        [
            "",
            "## Interpretation Rules",
            "",
            f"- primary horizon: 5m; estimates and intervals use equal-weight event blocks (all crypto grouped per UTC day). The same event across days remains one block. Fewer than {MIN_ROBUST_OBSERVATIONS} blocks: insufficient sample, not a power calculation",
            "- average_clv, median, win rate, intervals and leave-one-out summaries use block means; trade_weighted_clv is a separately labeled descriptive statistic",
            "- metric=clv is gross executable quote markout, not external fair-value CLV; metric=net_markout_estimate subtracts frozen placeholder costs, not realized PnL",
            "- block independence is an assumption, not a proof; correlated events, regime changes and multiple testing still require out-of-sample validation",
            "- positive mean with non-positive bootstrap lower bound: promising but not robust",
            "- a positive exploratory interval requires held-out replication, calibrated costs and a predeclared experiment before any scaling conclusion",
            "- real-money trading remains unapproved regardless of this report",
        ]
    )
    return "\n".join(lines)


def verdict_statement(verdict: str) -> str:
    if verdict == "POSITIVE_INTERVAL_EXPLORATORY":
        return "The exploratory block-mean interval is positive. Independence, net profitability and held-out replication remain unproven; no scaling or live-trading approval."
    if verdict == "POSITIVE_BUT_NOT_ROBUST":
        return "Average CLV is positive, but the confidence interval is not cleanly above zero. Do not scale yet."
    if verdict == "NON_POSITIVE_MEAN":
        return "Average CLV is not positive. The current evidence does not support profitability."
    return "No profitability claim is possible because the observed CLV sample is too small."


def win_rate(values: list[float]) -> float | None:
    return sum(1 for value in values if value > 0) / len(values) if values else None


def average(values: list[float]) -> float | None:
    return sum(values) / len(values) if values else None


def parse_optional_float(value: str | None) -> float | None:
    if value in {None, ""}:
        return None
    try:
        result = float(value)
        return result if math.isfinite(result) else None
    except ValueError:
        return None


def format_optional(value: float | None) -> str:
    return "n/a" if value is None else f"{value:.6f}"


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
