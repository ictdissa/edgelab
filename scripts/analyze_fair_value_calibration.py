#!/usr/bin/env python
from __future__ import annotations

import argparse
import csv
import math
from collections import defaultdict
from pathlib import Path

try:
    import _bootstrap  # noqa: F401
except ModuleNotFoundError:
    from scripts import _bootstrap  # noqa: F401

CALIBRATION_COLUMNS = [
    "group_type",
    "group",
    "probability_bucket",
    "observations",
    "average_fair_probability",
    "realized_frequency",
    "average_market_mid",
    "fair_brier_score",
    "market_brier_score",
    "fair_log_loss",
    "market_log_loss",
    "bias",
    "status",
]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Analyze fair-value calibration against settlement outcomes")
    parser.add_argument("--signals", type=Path, default=Path("data/processed/market_research_table.csv"))
    parser.add_argument("--settlements", type=Path, default=Path("data/settlements.csv"))
    parser.add_argument("--output", type=Path, default=Path("reports/fair_value_calibration.md"))
    parser.add_argument("--csv-output", type=Path)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    settlements = load_settlements(args.settlements)
    joined = join_settlements(read_csv_rows(args.signals), settlements)
    rows = build_calibration_rows(joined)
    csv_output = args.csv_output or args.output.with_suffix(".csv")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    csv_output.parent.mkdir(parents=True, exist_ok=True)
    write_csv(csv_output, rows, CALIBRATION_COLUMNS)
    args.output.write_text(render_report(joined, rows, args.signals, args.settlements, csv_output), encoding="utf-8")
    print(f"settled_signal_rows={len(joined)}")
    print(f"calibration_rows={len(rows)}")
    print(f"report={args.output}")


def load_settlements(path: Path) -> dict[tuple[str, str], float]:
    settlements: dict[tuple[str, str], float] = {}
    for row in read_csv_rows(path):
        market_id = row.get("market_id", "")
        outcome = row.get("outcome", "")
        payout = parse_optional_float(row.get("final_payout") or row.get("realized"))
        if market_id and payout is not None:
            settlements[(market_id, outcome)] = payout
            settlements[(market_id, "")] = payout
    return settlements


def join_settlements(signals: list[dict[str, str]], settlements: dict[tuple[str, str], float]) -> list[dict[str, str]]:
    joined = []
    for row in signals:
        market_id = row.get("market_id", "")
        outcome = row.get("outcome", "")
        realized = settlements.get((market_id, outcome))
        if realized is None:
            realized = settlements.get((market_id, ""))
        fair = parse_optional_float(row.get("fair_value") or row.get("fair_probability"))
        if realized is None or fair is None:
            continue
        joined.append({**row, "realized": str(realized)})
    return joined


def build_calibration_rows(rows: list[dict[str, str]]) -> list[dict[str, str]]:
    groups: dict[tuple[str, str, str], list[dict[str, str]]] = defaultdict(list)
    for row in rows:
        fair = parse_optional_float(row.get("fair_value") or row.get("fair_probability"))
        bucket = probability_bucket(fair)
        dimensions = {
            "overall": "all",
            "category": row.get("category", "unknown") or "unknown",
            "fair_value_source": row.get("fair_value_source", "unknown") or "unknown",
        }
        for group_type, group in dimensions.items():
            groups[(group_type, group, bucket)].append(row)
    return [
        summarize_group(group_type, group, bucket, group_rows)
        for (group_type, group, bucket), group_rows in sorted(groups.items())
    ]


def summarize_group(group_type: str, group: str, bucket: str, rows: list[dict[str, str]]) -> dict[str, str]:
    fair_values = [parse_optional_float(row.get("fair_value") or row.get("fair_probability")) for row in rows]
    realized_values = [parse_optional_float(row.get("realized")) for row in rows]
    mids = [parse_optional_float(row.get("mid")) for row in rows]
    pairs = [(fair, realized) for fair, realized in zip(fair_values, realized_values, strict=False) if fair is not None and realized is not None]
    market_pairs = [(mid, realized) for mid, realized in zip(mids, realized_values, strict=False) if mid is not None and realized is not None]
    avg_fair = average([fair for fair, _realized in pairs])
    realized_frequency = average([realized for _fair, realized in pairs])
    return {
        "group_type": group_type,
        "group": group,
        "probability_bucket": bucket,
        "observations": str(len(pairs)),
        "average_fair_probability": format_optional(avg_fair),
        "realized_frequency": format_optional(realized_frequency),
        "average_market_mid": format_optional(average([mid for mid, _realized in market_pairs])),
        "fair_brier_score": format_optional(average([(fair - realized) ** 2 for fair, realized in pairs])),
        "market_brier_score": format_optional(average([(mid - realized) ** 2 for mid, realized in market_pairs])),
        "fair_log_loss": format_optional(average([log_loss(fair, realized) for fair, realized in pairs])),
        "market_log_loss": format_optional(average([log_loss(mid, realized) for mid, realized in market_pairs])),
        "bias": format_optional(None if avg_fair is None or realized_frequency is None else avg_fair - realized_frequency),
        "status": "OK" if len(pairs) >= 20 else "INSUFFICIENT_SETTLED_SAMPLE",
    }


def render_report(
    joined: list[dict[str, str]],
    rows: list[dict[str, str]],
    signals_path: Path,
    settlements_path: Path,
    csv_output: Path,
) -> str:
    lines = [
        "# EdgeLab Fair-Value Calibration Report",
        "",
        "Calibration compares fair probabilities against resolved settlement outcomes. Without settlements, edge estimates remain unproven research signals.",
        "",
        f"- signals: `{signals_path}`",
        f"- settlements: `{settlements_path}`",
        f"- csv: `{csv_output}`",
        f"- settled signal rows: {len(joined)}",
        "",
        "## Summary",
        "",
    ]
    overall = [row for row in rows if row["group_type"] == "overall"]
    if overall:
        lines.extend(markdown_table(CALIBRATION_COLUMNS, [[row[column] for column in CALIBRATION_COLUMNS] for row in overall]))
    else:
        lines.append("No settled rows are available. Do not treat fair-value edge as calibrated.")
    return "\n".join(lines)


def probability_bucket(value: float | None) -> str:
    if value is None:
        return "missing"
    low = int(min(9, max(0, math.floor(value * 10)))) * 10
    return f"{low}-{low + 10}%"


def log_loss(probability: float, realized: float) -> float:
    clipped = min(1 - 1e-9, max(1e-9, probability))
    return -(realized * math.log(clipped) + (1 - realized) * math.log(1 - clipped))


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
    return "" if value is None else f"{value:.10g}"


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
