#!/usr/bin/env python
from __future__ import annotations

import argparse
import csv
from collections import Counter
from pathlib import Path

try:
    import _bootstrap  # noqa: F401
except ModuleNotFoundError:
    from scripts import _bootstrap  # noqa: F401

from edgelab.config.settings import Settings
from edgelab.research.signal_lifecycle import compute_lifecycle

SANITY_COLUMNS = [
    "warning_id",
    "source_snapshot",
    "category",
    "market_id",
    "outcome",
    "warning_code",
    "warning",
    "blocks_risk_approval",
    "best_side",
    "best_adjusted_edge",
    "raw_edge",
    "bid",
    "ask",
    "spread",
    "fair_probability",
    "final_decision",
]


def main() -> None:
    parser = argparse.ArgumentParser(description="Run EdgeLab edge sanity checks")
    parser.add_argument("--snapshot-dir", type=Path, default=Path("data/snapshots"))
    parser.add_argument("--output", type=Path, default=Path("reports/edge_sanity_report.md"))
    parser.add_argument("--csv-output", type=Path)
    args = parser.parse_args()

    from scripts.audit_signal_generation import load_snapshot_signals

    settings = Settings()
    rows = load_snapshot_signals(args.snapshot_dir)
    warnings = build_edge_sanity_warnings(rows, settings)
    csv_output = args.csv_output or args.output.parent / "edge_sanity_warnings.csv"
    args.output.parent.mkdir(parents=True, exist_ok=True)
    csv_output.parent.mkdir(parents=True, exist_ok=True)
    write_csv(csv_output, warnings, SANITY_COLUMNS)
    args.output.write_text(render_report(warnings, args.snapshot_dir, csv_output), encoding="utf-8")

    print(f"signals_analyzed={len(rows)}")
    print(f"sanity_warnings={len(warnings)}")
    print(f"report={args.output}")
    print(f"csv={csv_output}")


def build_edge_sanity_warnings(
    rows: list[dict[str, str]],
    settings: Settings,
    *,
    allow_suspicious_edges: bool = False,
) -> list[dict[str, str]]:
    output: list[dict[str, str]] = []
    for row in rows:
        lifecycle = compute_lifecycle(
            row,
            settings,
            allow_suspicious_edges=allow_suspicious_edges,
        )
        raw = row.get("raw_edge")
        if not raw:
            raw = max_optional(lifecycle.buy_edge_raw, lifecycle.sell_edge_raw)
        for warning in lifecycle.sanity_warnings:
            output.append(
                {
                    "warning_id": f"sanity:{len(output) + 1}",
                    "source_snapshot": row.get("_source_snapshot", ""),
                    "category": row.get("category", ""),
                    "market_id": row.get("market_id", ""),
                    "outcome": row.get("outcome", ""),
                    "warning_code": warning.code,
                    "warning": warning.message,
                    "blocks_risk_approval": "true" if warning.blocks_risk_approval else "false",
                    "best_side": lifecycle.best_side,
                    "best_adjusted_edge": format_float(lifecycle.best_adjusted_edge),
                    "raw_edge": format_float(raw),
                    "bid": row.get("bid") or row.get("prediction_bid") or "",
                    "ask": row.get("ask") or row.get("prediction_ask") or "",
                    "spread": row.get("spread", ""),
                    "fair_probability": row.get("fair_probability", ""),
                    "final_decision": lifecycle.final_decision,
                }
            )
    return output


def render_report(warnings: list[dict[str, str]], snapshot_dir: Path, csv_output: Path) -> str:
    counts = Counter(row["warning_code"] for row in warnings)
    blocking = sum(1 for row in warnings if row["blocks_risk_approval"] == "true")
    lines = [
        "# EdgeLab Edge Sanity Report",
        "",
        "Sanity warnings do not delete rows. Blocking warnings prevent risk approval unless an "
        "explicit test whitelist is used.",
        "",
        "## Summary",
        "",
        f"- snapshot directory: `{snapshot_dir}`",
        f"- csv: `{csv_output}`",
        f"- sanity warnings: {len(warnings)}",
        f"- blocking warnings: {blocking}",
        "",
        "## Warning Counts",
        "",
    ]
    if counts:
        lines.extend(markdown_table(["warning", "rows"], [[key, str(value)] for key, value in counts.most_common()]))
    else:
        lines.append("No sanity warnings were found.")
    return "\n".join(lines)


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


def max_optional(*values: float | None) -> str:
    present = [value for value in values if value is not None]
    return "" if not present else format_float(max(present))


def format_float(value: float | str | int | None) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, int):
        return str(value)
    return f"{value:.10g}"


def markdown_table(headers: list[str], rows: list[list[str]]) -> list[str]:
    output = ["| " + " | ".join(headers) + " |", "| " + " | ".join("---" for _ in headers) + " |"]
    for row in rows:
        output.append("| " + " | ".join(str(cell).replace("|", "\\|") for cell in row) + " |")
    return output


if __name__ == "__main__":
    main()
