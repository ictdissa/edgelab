#!/usr/bin/env python
from __future__ import annotations

import argparse
import csv
from collections import Counter, defaultdict
from pathlib import Path

try:
    import _bootstrap  # noqa: F401
except ModuleNotFoundError:
    from scripts import _bootstrap  # noqa: F401

TRIAGE_COLUMNS = [
    "warning_code",
    "category",
    "market_type",
    "fair_value_source",
    "market_id",
    "severity",
    "count",
    "blocking_count",
    "best_adjusted_edge_max",
    "sample_warning",
]

BLOCKER_CODES = {
    "MISSING_BID_ASK",
    "MISSING_TOKEN_ID",
    "MISSING_LEG",
    "CROSSED_BOOK",
    "BID_OUT_OF_RANGE",
    "ASK_OUT_OF_RANGE",
    "FAIR_PROBABILITY_INVALID",
    "STALE_FAIR_VALUE",
    "PARTIAL_EXHAUSTIVE_GROUP",
}

HIGH_CODES = {"WIDE_SPREAD"}
MEDIUM_CODES = {"MISSING_OUTCOME_MAPPING"}
DIAGNOSTIC_CODES = {"SUSPICIOUS_ADJUSTED_EDGE", "SUSPICIOUS_RAW_EDGE", "LOW_LIQUIDITY_DIAGNOSTIC"}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Triage EdgeLab edge sanity warnings")
    parser.add_argument("--input", type=Path, default=Path("reports/edge_sanity_warnings.csv"))
    parser.add_argument("--snapshot-dir", type=Path)
    parser.add_argument("--output", type=Path, default=Path("reports/sanity_warning_triage.md"))
    parser.add_argument("--csv-output", type=Path)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    rows = load_warning_rows(args.input, args.snapshot_dir)
    triage = build_triage(rows)
    csv_output = args.csv_output or args.output.with_suffix(".csv")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    csv_output.parent.mkdir(parents=True, exist_ok=True)
    write_csv(csv_output, triage, TRIAGE_COLUMNS)
    args.output.write_text(render_report(rows, triage, args.output, csv_output), encoding="utf-8")
    print(f"sanity_warnings={len(rows)}")
    print(f"triage_rows={len(triage)}")
    print(f"report={args.output}")
    print(f"csv={csv_output}")


def load_warning_rows(input_path: Path, snapshot_dir: Path | None) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    if snapshot_dir is not None:
        paths = warning_paths_from_index(snapshot_dir) or sorted(snapshot_dir.glob("iteration_*/edge_sanity_warnings.csv"))
        for path in paths:
            for row in read_csv_rows(path):
                row["_source_path"] = str(path)
                rows.append(row)
    if rows:
        return rows
    return read_csv_rows(input_path)


def warning_paths_from_index(snapshot_dir: Path) -> list[Path]:
    index_path = snapshot_dir / "snapshot_index.csv"
    if not index_path.exists():
        return []
    paths: list[Path] = []
    for row in read_csv_rows(index_path):
        value = row.get("edge_sanity_warnings_path", "")
        if not value:
            continue
        path = Path(value)
        if not path.is_absolute() and not path.exists():
            path = snapshot_dir / path
        if path.exists():
            paths.append(path)
    return paths


def build_triage(rows: list[dict[str, str]]) -> list[dict[str, str]]:
    groups: dict[tuple[str, str, str, str, str], list[dict[str, str]]] = defaultdict(list)
    for row in rows:
        category = row.get("category", "") or "unknown"
        key = (
            row.get("warning_code", "") or "UNKNOWN",
            category,
            market_type(category),
            row.get("fair_value_source", "") or source_from_category(category),
            row.get("market_id", "") or "unknown",
        )
        groups[key].append(row)
    output: list[dict[str, str]] = []
    for (code, category, market_type_value, fair_value_source, market_id), group_rows in sorted(groups.items()):
        best_edges = [value for row in group_rows if (value := parse_float_or_none(row.get("best_adjusted_edge"))) is not None]
        raw_blocking_count = sum(1 for row in group_rows if row.get("blocks_risk_approval") == "true")
        blocking_count = 0 if code in DIAGNOSTIC_CODES else raw_blocking_count
        output.append(
            {
                "warning_code": code,
                "category": category,
                "market_type": market_type_value,
                "fair_value_source": fair_value_source,
                "market_id": market_id,
                "severity": classify_severity(code, blocking_count),
                "count": str(len(group_rows)),
                "blocking_count": str(blocking_count),
                "best_adjusted_edge_max": format_float(max(best_edges) if best_edges else None),
                "sample_warning": group_rows[0].get("warning", ""),
            }
        )
    return output


def classify_severity(code: str, blocking_count: int) -> str:
    if code in DIAGNOSTIC_CODES:
        return "LOW"
    if code in BLOCKER_CODES or blocking_count > 0:
        return "BLOCKER"
    if code in HIGH_CODES:
        return "HIGH"
    if code in MEDIUM_CODES:
        return "MEDIUM"
    return "LOW"


def render_report(rows: list[dict[str, str]], triage: list[dict[str, str]], output: Path, csv_output: Path) -> str:
    severity_counts = Counter(row["severity"] for row in triage)
    code_counts = Counter(row.get("warning_code", "") for row in rows)
    blockers = [row for row in triage if row["severity"] == "BLOCKER"]
    suspicious_edges = [
        row
        for row in rows
        if row.get("warning_code") in {"SUSPICIOUS_ADJUSTED_EDGE", "SUSPICIOUS_RAW_EDGE"}
    ]
    sparse_expected = [
        row
        for row in rows
        if row.get("warning_code") in {"MISSING_BID_ASK", "FAIR_PROBABILITY_INVALID"}
        and row.get("category") == "relative_value"
    ]
    lines = [
        "# EdgeLab Sanity Warning Triage",
        "",
        "Warnings are triaged, not deleted. Blocking warnings continue to block paper approval unless a targeted test whitelist is used.",
        "",
        "## Summary",
        "",
        f"- warning rows: {len(rows)}",
        f"- triage rows: {len(triage)}",
        f"- report: `{output}`",
        f"- csv: `{csv_output}`",
        "",
        "## Severity Counts",
        "",
    ]
    if severity_counts:
        lines.extend(markdown_table(["severity", "groups"], [[key, str(value)] for key, value in severity_counts.items()]))
    else:
        lines.append("No warnings were found.")
    lines.extend(["", "## Top Warning Types", ""])
    if code_counts:
        lines.extend(markdown_table(["warning_code", "rows"], [[key, str(value)] for key, value in code_counts.most_common(12)]))
    else:
        lines.append("No warning types were found.")
    lines.extend(
        [
            "",
            "## Answers",
            "",
            f"- warnings that block paper trades: {', '.join(sorted({row['warning_code'] for row in blockers})) or 'none'}",
            f"- diagnostic-only warnings: {', '.join(sorted({row['warning_code'] for row in triage if row['severity'] == 'LOW'})) or 'none'}",
            f"- expected in mock/sparse data: {len(sparse_expected)} relative-value missing-book or missing-fair-value warnings",
            f"- best adjusted edges suspicious: {'yes' if suspicious_edges else 'no'}",
            f"- exclude from research analysis: {'yes for rows with BLOCKER severity; keep as diagnostics, not trade evidence' if blockers else 'no'}",
            f"- sanity logic too broad: {'possibly, if relative-value research-only rows dominate missing fair-value warnings' if sparse_expected else 'no obvious broadness detected'}",
            "",
            "## Blocker Examples",
            "",
        ]
    )
    if blockers:
        lines.extend(
            markdown_table(
                ["warning", "category", "market", "count", "best_edge"],
                [
                    [
                        row["warning_code"],
                        row["category"],
                        row["market_id"],
                        row["count"],
                        row["best_adjusted_edge_max"],
                    ]
                    for row in blockers[:20]
                ],
            )
        )
    else:
        lines.append("No blocker groups were found.")
    return "\n".join(lines)


def market_type(category: str) -> str:
    if category == "relative_value":
        return "internal_relative_value"
    return category or "unknown"


def source_from_category(category: str) -> str:
    if category == "relative_value":
        return "internal_relative_value"
    if category == "crypto":
        return "crypto_threshold"
    return category or "unknown"


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


def parse_float_or_none(value: str | None) -> float | None:
    if value in {None, ""}:
        return None
    try:
        return float(value)
    except ValueError:
        return None


def format_float(value: float | None) -> str:
    return "" if value is None else f"{value:.10g}"


def markdown_table(headers: list[str], rows: list[list[str]]) -> list[str]:
    output = ["| " + " | ".join(headers) + " |", "| " + " | ".join("---" for _ in headers) + " |"]
    for row in rows:
        output.append("| " + " | ".join(str(cell).replace("|", "\\|") for cell in row) + " |")
    return output


if __name__ == "__main__":
    main()
