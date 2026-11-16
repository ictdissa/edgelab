#!/usr/bin/env python
from __future__ import annotations

import argparse
import csv
import json
from collections import Counter
from pathlib import Path
from typing import Any

try:
    import _bootstrap  # noqa: F401
except ModuleNotFoundError:
    from scripts import _bootstrap  # noqa: F401

from edgelab.config.settings import Settings
from scripts.generate_paper_signals import (
    FULLY_PRICED_RV_STATUSES,
    RELATIVE_VALUE_CANDIDATE_COLUMNS,
    build_relative_value_candidates,
)

AUDIT_COLUMNS = [
    "candidate_id",
    "snapshot_timestamp",
    "relative_value_source",
    "empirical_evidence_eligible",
    "relation_type",
    "status",
    "source_status",
    "theoretical_violation",
    "executable_violation",
    "leg_count",
    "all_legs_present",
    "all_token_ids_present",
    "all_books_present",
    "all_bid_ask_present",
    "min_leg_liquidity",
    "max_leg_spread",
    "stale_leg_count",
    "suspicious_leg_count",
    "package_price_available",
    "paper_trade_eligible",
    "final_blocker",
    "leg_level_reasons",
]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Audit relative-value package pricing and eligibility")
    parser.add_argument("--snapshot-dir", type=Path)
    parser.add_argument("--input", type=Path, default=Path("reports/relative_value_candidates.csv"))
    parser.add_argument("--output", type=Path, default=Path("reports/relative_value_candidate_audit.md"))
    parser.add_argument("--csv-output", type=Path)
    parser.add_argument("--candidates-output", type=Path, default=Path("reports/relative_value_candidates.csv"))
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    settings = Settings()
    candidates = load_candidates(args.snapshot_dir, args.input, settings)
    previous_executable = count_previous_executable(args.snapshot_dir, args.input)
    audit_rows = build_audit_rows(candidates)
    csv_output = args.csv_output or args.output.with_suffix(".csv")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    csv_output.parent.mkdir(parents=True, exist_ok=True)
    args.candidates_output.parent.mkdir(parents=True, exist_ok=True)
    write_csv(csv_output, audit_rows, AUDIT_COLUMNS)
    write_csv(args.candidates_output, candidates, RELATIVE_VALUE_CANDIDATE_COLUMNS)
    args.output.write_text(
        render_report(audit_rows, candidates, previous_executable, args.output, csv_output),
        encoding="utf-8",
    )
    print(f"relative_value_candidates_analyzed={len(audit_rows)}")
    print(f"previously_labeled_executable={previous_executable}")
    print(f"fully_priced_packages={sum(1 for row in audit_rows if row['status'] in FULLY_PRICED_RV_STATUSES)}")
    print(f"paper_eligible_packages={sum(1 for row in audit_rows if row['paper_trade_eligible'] == 'true')}")
    print(f"report={args.output}")
    print(f"csv={csv_output}")


def load_candidates(snapshot_dir: Path | None, input_path: Path, settings: Settings) -> list[dict[str, str]]:
    if snapshot_dir is not None:
        rows: list[dict[str, str]] = []
        for iteration_dir in sorted(snapshot_dir.glob("iteration_*")):
            opportunity_path = iteration_dir / "relative_value_opportunities.csv"
            if not opportunity_path.exists():
                continue
            rows.extend(
                build_relative_value_candidates(
                    read_csv_rows(opportunity_path),
                    snapshot_timestamp=snapshot_timestamp(iteration_dir),
                    settings=settings,
                )
            )
        if rows:
            return rows
    existing = read_csv_rows(input_path)
    if existing and "paper_trade_eligible" in existing[0]:
        return existing
    return build_relative_value_candidates(existing, settings=settings)


def count_previous_executable(snapshot_dir: Path | None, input_path: Path) -> int:
    rows: list[dict[str, str]] = []
    if snapshot_dir is not None:
        for path in sorted(snapshot_dir.glob("iteration_*/relative_value_candidates.csv")):
            rows.extend(read_csv_rows(path))
        if rows:
            return sum(1 for row in rows if row.get("status") == "EXECUTABLE")
        for path in sorted(snapshot_dir.glob("iteration_*/relative_value_opportunities.csv")):
            rows.extend(read_csv_rows(path))
    else:
        rows = read_csv_rows(input_path)
    return sum(1 for row in rows if row.get("status") == "EXECUTABLE")


def build_audit_rows(candidates: list[dict[str, str]]) -> list[dict[str, str]]:
    output = []
    for row in candidates:
        legs = parse_legs(row.get("legs_json"))
        spreads = [value for leg in legs if (value := parse_optional_float(leg.get("spread"))) is not None]
        leg_reasons = leg_level_reasons(legs)
        output.append(
            {
                "candidate_id": row.get("candidate_id", ""),
                "snapshot_timestamp": row.get("snapshot_timestamp", ""),
                "relative_value_source": row.get("relative_value_source", ""),
                "empirical_evidence_eligible": row.get("empirical_evidence_eligible", "false"),
                "relation_type": row.get("relation_type", ""),
                "status": row.get("status", ""),
                "source_status": row.get("source_status", ""),
                "theoretical_violation": row.get("theoretical_violation", ""),
                "executable_violation": row.get("executable_violation", ""),
                "leg_count": row.get("leg_count", str(len(legs))),
                "all_legs_present": bool_text(len(legs) >= 2),
                "all_token_ids_present": bool_text(bool(legs) and all(leg.get("token_id") for leg in legs)),
                "all_books_present": bool_text(bool(legs) and all(leg.get("bid") or leg.get("ask") for leg in legs)),
                "all_bid_ask_present": bool_text(
                    bool(legs) and all(leg.get("bid") not in {None, ""} and leg.get("ask") not in {None, ""} for leg in legs)
                ),
                "min_leg_liquidity": row.get("min_leg_liquidity", ""),
                "max_leg_spread": format_float(max(spreads) if spreads else None),
                "stale_leg_count": str(sum(1 for leg in legs if leg.get("freshness_status") == "stale")),
                "suspicious_leg_count": str(sum(1 for leg in legs if has_suspicious_warning(leg))),
                "package_price_available": bool_text(bool(legs) and all(leg.get("entry_price") for leg in legs)),
                "paper_trade_eligible": row.get("paper_trade_eligible", "false"),
                "final_blocker": row.get("paper_trade_blocker") or row.get("risk_reasons", ""),
                "leg_level_reasons": "; ".join(leg_reasons),
            }
        )
    return output


def render_report(
    audit_rows: list[dict[str, str]],
    candidates: list[dict[str, str]],
    previous_executable: int,
    output_path: Path,
    csv_output: Path,
) -> str:
    fully_priced = [row for row in audit_rows if row["status"] in FULLY_PRICED_RV_STATUSES]
    empirical_fully_priced = [row for row in fully_priced if row["empirical_evidence_eligible"] == "true"]
    paper_eligible = [row for row in audit_rows if row["paper_trade_eligible"] == "true"]
    source_counts = Counter(row["relative_value_source"] or "unknown" for row in audit_rows)
    blockers = Counter(row["final_blocker"] or "none" for row in audit_rows if row["paper_trade_eligible"] != "true")
    leg_blockers = Counter(
        reason
        for row in audit_rows
        for reason in [part.strip() for part in row["leg_level_reasons"].split(";") if part.strip()]
    )
    lines = [
        "# EdgeLab Relative-Value Candidate Audit",
        "",
        "This audit rebuilds relative-value packages from candidate/opportunity rows and validates every leg before paper trading.",
        "",
        "## Summary",
        "",
        f"- candidates analyzed: {len(audit_rows)}",
        f"- previously labeled executable: {previous_executable}",
        f"- fully priced packages: {len(fully_priced)}",
        f"- empirical fully priced packages: {len(empirical_fully_priced)}",
        f"- paper-eligible packages: {len(paper_eligible)}",
        f"- source counts: {', '.join(f'{key}={value}' for key, value in source_counts.most_common()) or 'none'}",
        f"- report: `{output_path}`",
        f"- csv: `{csv_output}`",
        "",
        "## Required Answers",
        "",
        answer_zero_trades(previous_executable, fully_priced, empirical_fully_priced, paper_eligible, source_counts, blockers),
        "",
        f"- packages reconstructable at bid/ask: {'yes' if fully_priced else 'no'}",
        f"- empirical packages truly executable: {'yes' if empirical_fully_priced else 'no'}",
        f"- fields most often missing: {missing_fields_summary(audit_rows)}",
        f"- most common leg blocker: {leg_blockers.most_common(1)[0][0] if leg_blockers else 'none'}",
        "",
        "## Package Blockers",
        "",
    ]
    if blockers:
        lines.extend(markdown_table(["blocker", "count"], [[key, str(value)] for key, value in blockers.most_common()]))
    else:
        lines.append("No package blockers were found.")
    lines.extend(["", "## Leg Blockers", ""])
    if leg_blockers:
        lines.extend(markdown_table(["leg_blocker", "count"], [[key, str(value)] for key, value in leg_blockers.most_common(12)]))
    else:
        lines.append("No leg blockers were found.")
    lines.extend(["", "## Candidate Status Counts", ""])
    status_counts = Counter(row.get("status", "") for row in candidates)
    lines.extend(markdown_table(["status", "count"], [[key, str(value)] for key, value in status_counts.items()]))
    return "\n".join(lines)


def answer_zero_trades(
    previous_executable: int,
    fully_priced: list[dict[str, str]],
    empirical_fully_priced: list[dict[str, str]],
    paper_eligible: list[dict[str, str]],
    source_counts: Counter[str],
    blockers: Counter[str],
) -> str:
    if previous_executable and not fully_priced:
        return (
            f"- why 0 paper trades: {previous_executable} rows were previously labeled executable, "
            "but none were reconstructable as fully priced packages."
        )
    if fully_priced and not empirical_fully_priced:
        sources = ", ".join(f"{key}={value}" for key, value in source_counts.most_common()) or "unknown"
        return (
            "- why 0 paper trades: packages are priced, but none are empirical live-snapshot packages. "
            f"Current relative-value sources are `{sources}`, so they remain research-only."
        )
    if fully_priced and not paper_eligible:
        top = blockers.most_common(1)[0][0] if blockers else "risk checks"
        return f"- why 0 paper trades: packages are priced, but paper eligibility is blocked by `{top}`."
    if paper_eligible:
        return "- why 0 paper trades: paper-eligible packages exist; run the relative-value paper trader."
    return "- why 0 paper trades: no relative-value packages passed leg validation."


def missing_fields_summary(rows: list[dict[str, str]]) -> str:
    counts = Counter()
    for row in rows:
        if row["all_token_ids_present"] != "true":
            counts["token_id"] += 1
        if row["all_books_present"] != "true":
            counts["book"] += 1
        if row["all_bid_ask_present"] != "true":
            counts["bid/ask"] += 1
    return ", ".join(f"{key}={value}" for key, value in counts.most_common()) or "none"


def leg_level_reasons(legs: list[dict[str, Any]]) -> list[str]:
    reasons: list[str] = []
    for leg in legs:
        reason = str(leg.get("leg_rejection_reason", ""))
        if reason:
            reasons.append(f"{leg.get('market_id')}:{reason}")
        for warning in parse_leg_warnings(leg):
            if warning.get("blocks_paper") and warning.get("message"):
                reasons.append(f"{leg.get('market_id')}:{warning['message']}")
    return sorted(set(reasons))


def has_suspicious_warning(leg: dict[str, Any]) -> bool:
    return any(str(warning.get("code", "")).startswith("SUSPICIOUS") for warning in parse_leg_warnings(leg))


def parse_leg_warnings(leg: dict[str, Any]) -> list[dict[str, object]]:
    value = str(leg.get("sanity_warnings", ""))
    if not value:
        return []
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        return []
    if not isinstance(parsed, list):
        return []
    return [item for item in parsed if isinstance(item, dict)]


def parse_legs(value: str | None) -> list[dict[str, Any]]:
    if not value:
        return []
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        return []
    if not isinstance(parsed, list):
        return []
    return [item for item in parsed if isinstance(item, dict)]


def snapshot_timestamp(iteration_dir: Path) -> str:
    summary_path = iteration_dir / "summary.json"
    if not summary_path.exists():
        return ""
    try:
        payload = json.loads(summary_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return ""
    return str(payload.get("timestamp", ""))


def read_csv_rows(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    with path.open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def write_csv(path: Path, rows: list[dict[str, str]], columns: list[str]) -> None:
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


def parse_optional_float(value: str | None) -> float | None:
    if value in {None, ""}:
        return None
    try:
        return float(value)
    except ValueError:
        return None


def format_float(value: float | None) -> str:
    return "" if value is None else f"{value:.10g}"


def bool_text(value: bool) -> str:
    return "true" if value else "false"


def markdown_table(headers: list[str], rows: list[list[str]]) -> list[str]:
    output = ["| " + " | ".join(headers) + " |", "| " + " | ".join("---" for _ in headers) + " |"]
    for row in rows:
        output.append("| " + " | ".join(str(cell).replace("|", "\\|") for cell in row) + " |")
    return output


if __name__ == "__main__":
    main()
