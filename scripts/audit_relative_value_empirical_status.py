#!/usr/bin/env python
from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path

try:
    import _bootstrap  # noqa: F401
except ModuleNotFoundError:
    from scripts import _bootstrap  # noqa: F401

from scripts.empirical_audit_utils import (
    default_experiment_dir,
    load_relative_value_candidates,
    markdown_table,
    parse_float,
    read_csv_rows,
    write_csv,
)

STATUS_STATIC_RESEARCH_ONLY = "STATIC_RESEARCH_ONLY"
STATUS_LIVE_SNAPSHOT_INCOMPLETE = "LIVE_SNAPSHOT_INCOMPLETE"
STATUS_LIVE_FULLY_PRICED_BUT_NOT_LIQUID = "LIVE_FULLY_PRICED_BUT_NOT_LIQUID"
STATUS_LIVE_FULLY_PRICED_LIQUID_BUT_NOT_PAYOFF_RECONSTRUCTABLE = "LIVE_FULLY_PRICED_LIQUID_BUT_NOT_PAYOFF_RECONSTRUCTABLE"
STATUS_LIVE_EXECUTABLE_PACKAGE = "LIVE_EXECUTABLE_PACKAGE"
STATUS_PAPER_TRADED_PACKAGE = "PAPER_TRADED_PACKAGE"

COLUMNS = [
    "candidate_id",
    "snapshot_timestamp",
    "relative_value_source",
    "input_status",
    "empirical_status",
    "paper_trade_eligible",
    "paper_traded",
    "leg_count",
    "all_legs_live_snapshot",
    "all_legs_fresh_bid_ask",
    "all_legs_sufficient_depth",
    "all_ids_reconstructable",
    "payoff_table_reconstructable",
    "worst_case_payoff_computed",
    "package_cost_computed",
    "fees_included",
    "legging_risk_flagged",
    "blocking_reasons",
]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Audit empirical executability status of relative-value candidates")
    parser.add_argument("--experiment-dir", type=Path, default=default_experiment_dir())
    parser.add_argument("--output", type=Path, default=Path("reports/relative_value_empirical_status.md"))
    parser.add_argument("--csv-output", type=Path, default=Path("reports/relative_value_empirical_status.csv"))
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    rows, report = build_relative_value_status(args.experiment_dir, args.csv_output)
    write_csv(args.csv_output, rows, COLUMNS)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(report, encoding="utf-8")
    print(f"rv_candidates={len(rows)}")
    print(f"rv_empirical_executable={sum(1 for row in rows if row['empirical_status'] == STATUS_LIVE_EXECUTABLE_PACKAGE)}")
    print(f"rv_paper_traded={sum(1 for row in rows if row['empirical_status'] == STATUS_PAPER_TRADED_PACKAGE)}")
    print(f"report={args.output}")


def build_relative_value_status(experiment_dir: Path, csv_output: Path | None = None) -> tuple[list[dict[str, object]], str]:
    snapshot_dir = experiment_dir / "snapshots"
    candidates = load_relative_value_candidates(snapshot_dir)
    rv_ledger = read_csv_rows(experiment_dir / "paper_trades" / "relative_value_ledger.csv")
    traded_ids = {row.get("candidate_id", "") for row in rv_ledger if row.get("execution_status") == "FILLED_PAPER"}
    rows = [classify_candidate(row, traded_ids) for row in candidates]
    report = render_report(rows, csv_output)
    return rows, report


def classify_candidate(row: dict[str, str], traded_ids: set[str]) -> dict[str, object]:
    legs = parse_legs(row.get("legs_json", ""))
    candidate_id = row.get("candidate_id", "")
    paper_traded = candidate_id in traded_ids
    all_legs_live_snapshot = row.get("relative_value_source") == "live_snapshot" and row.get("empirical_evidence_eligible") == "true"
    all_legs_fresh_bid_ask = bool(legs) and all(leg_has_fresh_bid_ask(leg) for leg in legs)
    all_legs_sufficient_depth = bool(legs) and all((parse_float(leg.get("size_available")) or 0.0) >= 25 for leg in legs)
    all_ids_reconstructable = bool(legs) and all(leg.get("market_id") and leg.get("token_id") for leg in legs)
    package_cost_computed = parse_float(row.get("package_entry_cost")) is not None
    worst_case_payoff_computed = parse_float(row.get("package_expected_payoff")) is not None
    payoff_table_reconstructable = worst_case_payoff_computed and package_cost_computed and bool(legs)
    fees_included = row.get("fees_included") in {"true", "True", "1"} or "fee" in row.get("risk_reasons", "").lower()
    legging_risk_flagged = row.get("legging_risk_flagged") in {"true", "True", "1"} or "legging" in row.get("risk_reasons", "").lower()
    reasons = []
    if not all_legs_live_snapshot:
        reasons.append("not live_snapshot empirical source")
    if not all_legs_fresh_bid_ask:
        reasons.append("missing or stale leg bid/ask")
    if not all_legs_sufficient_depth:
        reasons.append("insufficient leg depth")
    if not all_ids_reconstructable:
        reasons.append("missing market_id/token_id")
    if not payoff_table_reconstructable:
        reasons.append("payoff/cost not reconstructable")
    if not fees_included:
        reasons.append("fees not explicitly included")
    if not legging_risk_flagged:
        reasons.append("legging risk not explicitly flagged")

    if paper_traded:
        empirical_status = STATUS_PAPER_TRADED_PACKAGE
    elif row.get("relative_value_source") == "static_report" or row.get("empirical_evidence_eligible") != "true":
        empirical_status = STATUS_STATIC_RESEARCH_ONLY
    elif not all_legs_fresh_bid_ask or not all_ids_reconstructable:
        empirical_status = STATUS_LIVE_SNAPSHOT_INCOMPLETE
    elif not all_legs_sufficient_depth:
        empirical_status = STATUS_LIVE_FULLY_PRICED_BUT_NOT_LIQUID
    elif not payoff_table_reconstructable or not fees_included or not legging_risk_flagged:
        empirical_status = STATUS_LIVE_FULLY_PRICED_LIQUID_BUT_NOT_PAYOFF_RECONSTRUCTABLE
    else:
        empirical_status = STATUS_LIVE_EXECUTABLE_PACKAGE
    return {
        "candidate_id": candidate_id,
        "snapshot_timestamp": row.get("snapshot_timestamp", ""),
        "relative_value_source": row.get("relative_value_source", ""),
        "input_status": row.get("status", ""),
        "empirical_status": empirical_status,
        "paper_trade_eligible": row.get("paper_trade_eligible", ""),
        "paper_traded": str(paper_traded).lower(),
        "leg_count": row.get("leg_count", ""),
        "all_legs_live_snapshot": str(all_legs_live_snapshot).lower(),
        "all_legs_fresh_bid_ask": str(all_legs_fresh_bid_ask).lower(),
        "all_legs_sufficient_depth": str(all_legs_sufficient_depth).lower(),
        "all_ids_reconstructable": str(all_ids_reconstructable).lower(),
        "payoff_table_reconstructable": str(payoff_table_reconstructable).lower(),
        "worst_case_payoff_computed": str(worst_case_payoff_computed).lower(),
        "package_cost_computed": str(package_cost_computed).lower(),
        "fees_included": str(fees_included).lower(),
        "legging_risk_flagged": str(legging_risk_flagged).lower(),
        "blocking_reasons": "; ".join(reasons),
    }


def leg_has_fresh_bid_ask(leg: dict[str, object]) -> bool:
    bid = parse_float(leg.get("bid"))
    ask = parse_float(leg.get("ask"))
    age = parse_float(leg.get("data_age_seconds"))
    return bid is not None and ask is not None and bid <= ask and age is not None and age <= 300


def parse_legs(value: str) -> list[dict[str, object]]:
    if not value:
        return []
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        return []
    return parsed if isinstance(parsed, list) else []


def render_report(rows: list[dict[str, object]], csv_output: Path | None) -> str:
    counts = Counter(str(row["empirical_status"]) for row in rows)
    empirical_executable = counts.get(STATUS_LIVE_EXECUTABLE_PACKAGE, 0)
    paper_traded = counts.get(STATUS_PAPER_TRADED_PACKAGE, 0)
    lines = [
        "# Relative-Value Empirical Status",
        "",
        "Relative-value packages are demoted unless they are empirically live-snapshot executable.",
        "",
        f"- candidates audited: {len(rows)}",
        f"- live executable packages: {empirical_executable}",
        f"- paper-traded packages: {paper_traded}",
        "",
        "## Status Counts",
        "",
    ]
    lines.extend(markdown_table(["empirical_status", "count"], counts.most_common()))
    lines.extend(
        [
            "",
            "## Interpretation",
            "",
            (
                "RV is research-only because empirical executable count is zero."
                if empirical_executable == 0 and paper_traded == 0
                else "Some RV packages have empirical executable status; review leg-level files before any paper scaling."
            ),
            "Do not market RV as a trading module until paper-traded live-snapshot packages exist. It is valid to describe RV as a logical-relationship discovery and audit engine.",
            "",
            f"- status csv: `{csv_output}`",
        ]
    )
    return "\n".join(lines)


if __name__ == "__main__":
    main()
