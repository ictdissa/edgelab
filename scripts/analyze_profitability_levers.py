#!/usr/bin/env python
from __future__ import annotations

import argparse
import csv
from collections import defaultdict
from pathlib import Path
from statistics import median

try:
    import _bootstrap  # noqa: F401
except ModuleNotFoundError:
    from scripts import _bootstrap  # noqa: F401

GROUP_COLUMNS = [
    "group_type",
    "group",
    "observations",
    "average_clv",
    "median_clv",
    "win_rate",
    "average_spread_at_entry",
    "average_adjusted_edge_at_entry",
    "recommendation",
]

MIN_EVIDENCE_OBSERVATIONS = 20


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Analyze empirical CLV levers without changing thresholds")
    parser.add_argument("--ledger", type=Path, default=Path("data/paper_trades/ledger.csv"))
    parser.add_argument("--clv-observations", type=Path, default=Path("reports/clv_observations.csv"))
    parser.add_argument("--output", type=Path, default=Path("reports/profitability_levers.md"))
    parser.add_argument("--csv-output", type=Path)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    ledger_rows = {row.get("paper_trade_id", ""): row for row in read_csv_rows(args.ledger)}
    observed_rows = [
        attach_ledger_fields(row, ledger_rows.get(row.get("paper_trade_id", ""), {}))
        for row in read_csv_rows(args.clv_observations)
        if row.get("status") == "observed"
    ]
    group_rows = build_group_rows(observed_rows)
    csv_output = args.csv_output or args.output.with_suffix(".csv")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    csv_output.parent.mkdir(parents=True, exist_ok=True)
    write_csv(csv_output, group_rows, GROUP_COLUMNS)
    args.output.write_text(render_report(observed_rows, group_rows, args.ledger, args.clv_observations), encoding="utf-8")
    print(f"observed_clv={len(observed_rows)}")
    print(f"groups={len(group_rows)}")
    print(f"report={args.output}")
    print(f"csv={csv_output}")


def attach_ledger_fields(row: dict[str, str], ledger: dict[str, str]) -> dict[str, str]:
    merged = dict(row)
    for key in (
        "spread_at_entry",
        "adjusted_edge_at_entry",
        "liquidity_at_entry",
        "fair_value_source",
        "execution_price_source",
    ):
        merged[key] = ledger.get(key, "")
    return merged


def build_group_rows(rows: list[dict[str, str]]) -> list[dict[str, str]]:
    groups: dict[tuple[str, str], list[dict[str, str]]] = defaultdict(list)
    for row in rows:
        groups[("overall", "all")].append(row)
        groups[("side", row.get("side", "unknown") or "unknown")].append(row)
        groups[("horizon", row.get("horizon", "unknown") or "unknown")].append(row)
        groups[("fair_value_source", row.get("fair_value_source", "unknown") or "unknown")].append(row)
        groups[("spread_bucket", spread_bucket(parse_optional_float(row.get("spread_at_entry"))))].append(row)
        groups[("liquidity_bucket", liquidity_bucket(parse_optional_float(row.get("liquidity_at_entry"))))].append(row)
    return [summarize_group(group_type, group, group_rows) for (group_type, group), group_rows in sorted(groups.items())]


def summarize_group(group_type: str, group: str, rows: list[dict[str, str]]) -> dict[str, str]:
    clv_values = [value for row in rows if (value := parse_optional_float(row.get("clv"))) is not None]
    spreads = [value for row in rows if (value := parse_optional_float(row.get("spread_at_entry"))) is not None]
    adjusted_edges = [
        value for row in rows if (value := parse_optional_float(row.get("adjusted_edge_at_entry"))) is not None
    ]
    average_clv = average(clv_values)
    return {
        "group_type": group_type,
        "group": group,
        "observations": str(len(clv_values)),
        "average_clv": format_optional(average_clv),
        "median_clv": format_optional(median(clv_values) if clv_values else None),
        "win_rate": format_optional(win_rate(clv_values)),
        "average_spread_at_entry": format_optional(average(spreads)),
        "average_adjusted_edge_at_entry": format_optional(average(adjusted_edges)),
        "recommendation": recommendation(len(clv_values), average_clv),
    }


def render_report(
    observed_rows: list[dict[str, str]],
    group_rows: list[dict[str, str]],
    ledger_path: Path,
    observations_path: Path,
) -> str:
    overall = next((row for row in group_rows if row["group_type"] == "overall"), None)
    average_clv = parse_optional_float(overall.get("average_clv") if overall else None)
    observations = len(observed_rows)
    lines = [
        "# EdgeLab Profitability Lever Analysis",
        "",
        "This report analyzes observed paper CLV. It does not change thresholds, fit a new model, place orders, or approve live trading.",
        "",
        "## Inputs",
        "",
        f"- ledger: `{ledger_path}`",
        f"- CLV observations: `{observations_path}`",
        f"- observed rows: {observations}",
        "",
        "## Profitability Status",
        "",
        profitability_status(observations, average_clv),
        "",
        "## Top Levers",
        "",
        "- If average CLV is near the negative half-spread, execution cost is dominating and the system needs either larger true edge or tighter markets.",
        "- If one side has persistently worse CLV, side-specific gating should be reviewed before changing global thresholds.",
        "- If high adjusted-edge buckets do not produce positive market CLV, the fair-value model is overconfident and should be calibrated before scaling.",
        "",
        "## Group Summary",
        "",
    ]
    if group_rows:
        lines.extend(
            markdown_table(
                GROUP_COLUMNS,
                [[row[column] for column in GROUP_COLUMNS] for row in group_rows],
            )
        )
    else:
        lines.append("No observed CLV rows were available.")
    lines.extend(
        [
            "",
            "## Next Action",
            "",
            next_action(observations, average_clv),
        ]
    )
    return "\n".join(lines)


def profitability_status(observations: int, average_clv: float | None) -> str:
    if observations == 0:
        return "No profitability claim is possible because there are no observed CLV rows."
    if observations < MIN_EVIDENCE_OBSERVATIONS:
        return (
            f"No profitability claim is possible: only {observations} CLV observations exist, below the "
            f"{MIN_EVIDENCE_OBSERVATIONS}-observation evidence gate."
        )
    if average_clv is None or average_clv <= 0:
        return "No profitability claim is possible: average market CLV is not positive."
    return "Paper evidence is promising, but live trading remains unapproved until robustness checks pass."


def next_action(observations: int, average_clv: float | None) -> str:
    if observations < MIN_EVIDENCE_OBSERVATIONS:
        return "Continue the fresh snapshot campaign until at least 20 bid/ask-filled CLV observations exist."
    if average_clv is None or average_clv <= 0:
        return "Do not scale. Use the grouped CLV table to tighten risk filters or recalibrate fair value, then rerun paper testing."
    return "Continue paper testing at small size and add robustness checks across horizons, sides, and market regimes."


def recommendation(observations: int, average_clv: float | None) -> str:
    if observations < MIN_EVIDENCE_OBSERVATIONS:
        return "COLLECT_MORE"
    if average_clv is None or average_clv <= 0:
        return "DO_NOT_SCALE"
    return "PAPER_PROMISING"


def spread_bucket(value: float | None) -> str:
    if value is None:
        return "missing"
    if value <= 0.0025:
        return "<=25bp"
    if value <= 0.01:
        return "<=100bp"
    return ">100bp"


def liquidity_bucket(value: float | None) -> str:
    if value is None:
        return "missing"
    if value < 25:
        return "<25"
    if value < 100:
        return "25-100"
    if value < 1000:
        return "100-1000"
    return ">=1000"


def win_rate(values: list[float]) -> float | None:
    return sum(1 for value in values if value > 0) / len(values) if values else None


def average(values: list[float]) -> float | None:
    return sum(values) / len(values) if values else None


def parse_optional_float(value: str | None) -> float | None:
    if value in {None, ""}:
        return None
    try:
        return float(value)
    except ValueError:
        return None


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
