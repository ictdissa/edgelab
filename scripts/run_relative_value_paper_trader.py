#!/usr/bin/env python
from __future__ import annotations

import argparse
import csv
import hashlib
import json
from collections import Counter, defaultdict
from datetime import UTC, datetime
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
    RV_STATUS_PAPER_ELIGIBLE,
    build_relative_value_candidates,
    is_empirical_relative_value_candidate,
)

RELATIVE_VALUE_LEDGER_COLUMNS = [
    "paper_trade_id",
    "timestamp",
    "candidate_id",
    "snapshot_timestamp",
    "relation_type",
    "leg_count",
    "legs_json",
    "package_entry_cost",
    "package_expected_payoff",
    "package_expected_edge",
    "max_trade_size",
    "executable_violation",
    "theoretical_violation",
    "risk_decision",
    "risk_reasons",
    "execution_status",
    "clv_5m",
    "clv_30m",
    "clv_1h",
    "clv_status",
]

RELATIVE_VALUE_REJECTION_COLUMNS = [
    "candidate_id",
    "snapshot_timestamp",
    "relation_type",
    "package_rejection_reason",
    "leg_market_id",
    "leg_outcome",
    "leg_side",
    "leg_rejection_reason",
    "leg_sanity_warnings",
]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Paper-simulate multi-leg relative-value candidates"
    )
    parser.add_argument(
        "--candidates", type=Path, default=Path("reports/relative_value_candidates.csv")
    )
    parser.add_argument("--snapshot-dir", type=Path)
    parser.add_argument(
        "--ledger", type=Path, default=Path("data/paper_trades/relative_value_ledger.csv")
    )
    parser.add_argument(
        "--output", type=Path, default=Path("reports/relative_value_paper_trades.md")
    )
    parser.add_argument(
        "--rejections-output",
        type=Path,
        default=Path("reports/relative_value_paper_rejections.csv"),
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    settings = Settings()
    candidate_rows = load_candidate_rows(args.candidates, args.snapshot_dir)
    ledger_rows, rejection_rows = build_relative_value_ledger_rows(candidate_rows, settings)
    ensure_ledger_schema(args.ledger)
    append_ledger_rows(args.ledger, ledger_rows)
    args.rejections_output.parent.mkdir(parents=True, exist_ok=True)
    write_csv(args.rejections_output, rejection_rows, RELATIVE_VALUE_REJECTION_COLUMNS)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        render_report(ledger_rows, args.ledger, args.rejections_output), encoding="utf-8"
    )
    entered = sum(1 for row in ledger_rows if row["execution_status"] == "FILLED_PAPER")
    skipped = len(ledger_rows) - entered
    print(f"relative_value_paper_trades={entered}")
    print(f"skipped_candidates={skipped}")
    print(f"ledger={args.ledger}")
    print(f"report={args.output}")
    print(f"rejections={args.rejections_output}")


def load_candidate_rows(candidates_path: Path, snapshot_dir: Path | None) -> list[dict[str, str]]:
    if snapshot_dir is not None:
        rows = candidate_rows_from_opportunities(snapshot_dir)
        if rows:
            return rows
        paths = relative_value_candidate_paths(snapshot_dir)
        rows = []
        for path in paths:
            for row in read_csv_rows(path):
                row["_source_path"] = str(path)
                rows.append(normalize_candidate_row(row))
        if rows:
            return rows
    return [normalize_candidate_row(row) for row in read_csv_rows(candidates_path)]


def candidate_rows_from_opportunities(snapshot_dir: Path) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    for iteration_dir in sorted(snapshot_dir.glob("iteration_*")):
        opportunity_path = iteration_dir / "relative_value_opportunities.csv"
        if not opportunity_path.exists():
            continue
        timestamp = snapshot_timestamp(iteration_dir)
        rows.extend(
            build_relative_value_candidates(
                read_csv_rows(opportunity_path),
                snapshot_timestamp=timestamp,
                settings=Settings(),
            )
        )
    return rows


def relative_value_candidate_paths(snapshot_dir: Path) -> list[Path]:
    index_path = snapshot_dir / "snapshot_index.csv"
    if index_path.exists():
        paths: list[Path] = []
        for row in read_csv_rows(index_path):
            value = row.get("relative_value_candidates_path", "")
            if not value:
                continue
            path = Path(value)
            if not path.is_absolute() and not path.exists():
                path = snapshot_dir / path
            if path.exists():
                paths.append(path)
        if paths:
            return paths
    return sorted(snapshot_dir.glob("iteration_*/relative_value_candidates.csv"))


def snapshot_timestamp(iteration_dir: Path) -> str:
    summary_path = iteration_dir / "summary.json"
    if not summary_path.exists():
        return ""
    try:
        payload = json.loads(summary_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return ""
    return str(payload.get("timestamp", ""))


def normalize_candidate_row(row: dict[str, str]) -> dict[str, str]:
    if "paper_trade_eligible" in row and "package_expected_edge" in row:
        normalized = dict(row)
        normalized["relative_value_source"] = normalized.get("relative_value_source") or "unknown"
        normalized["empirical_evidence_eligible"] = (
            normalized.get("empirical_evidence_eligible") or "false"
        )
        return normalized
    normalized = {column: row.get(column, "") for column in RELATIVE_VALUE_CANDIDATE_COLUMNS}
    normalized["paper_trade_eligible"] = normalized.get("paper_trade_eligible") or "false"
    normalized["package_expected_edge"] = normalized.get("package_expected_edge") or row.get(
        "executable_violation", ""
    )
    normalized["package_expected_payoff"] = normalized.get("package_expected_payoff", "")
    normalized["snapshot_timestamp"] = normalized.get("snapshot_timestamp", "")
    normalized["relative_value_source"] = normalized.get("relative_value_source") or "unknown"
    normalized["empirical_evidence_eligible"] = (
        normalized.get("empirical_evidence_eligible") or "false"
    )
    return normalized


def build_relative_value_ledger_rows(
    rows: list[dict[str, str]],
    settings: Settings,
    blocking_warning_markets: set[str] | None = None,
) -> tuple[list[dict[str, str]], list[dict[str, str]]]:
    output: list[dict[str, str]] = []
    rejections: list[dict[str, str]] = []
    for row in rows:
        now = datetime.now(UTC).isoformat()
        accepted, reason, legs, size, package_cost = validate_candidate(row, settings)
        rejections.extend(rejection_rows(row, legs, reason) if not accepted else [])
        risk_decision = "APPROVED" if accepted else "REJECTED"
        expected_edge = parse_float(
            row.get("package_expected_edge") or row.get("executable_violation")
        )
        unit_entry_cost = parse_float(row.get("package_entry_cost"))
        package_expected_payoff = parse_optional_float(row.get("package_expected_payoff"))
        output.append(
            {
                "paper_trade_id": relative_value_trade_id(row, now),
                "timestamp": now,
                "candidate_id": row.get("candidate_id", ""),
                "snapshot_timestamp": row.get("snapshot_timestamp", ""),
                "relation_type": row.get("relation_type", ""),
                "leg_count": str(len(legs) if legs else parse_int(row.get("leg_count"))),
                "legs_json": json.dumps(legs, sort_keys=True),
                "package_entry_cost": format_float(package_cost if accepted else None),
                "package_expected_payoff": format_float(
                    package_expected_payoff * size
                    if accepted and package_expected_payoff is not None
                    else None
                ),
                "package_expected_edge": format_float(
                    expected_edge * size if accepted else expected_edge
                ),
                "max_trade_size": format_float(size if accepted else 0.0),
                "executable_violation": row.get("executable_violation", ""),
                "theoretical_violation": row.get("theoretical_violation", ""),
                "risk_decision": risk_decision,
                "risk_reasons": "" if accepted else reason,
                "execution_status": "FILLED_PAPER" if accepted else "SKIPPED",
                "clv_5m": "",
                "clv_30m": "",
                "clv_1h": "",
                "clv_status": "pending" if accepted else "skipped",
            }
        )
        if accepted and unit_entry_cost == 0:
            output[-1]["risk_reasons"] = ""
    return output, rejections


def validate_candidate(
    row: dict[str, str],
    settings: Settings,
) -> tuple[bool, str, list[dict[str, Any]], float, float | None]:
    if not is_empirical_relative_value_candidate(row):
        source = row.get("relative_value_source") or "unknown"
        return (
            False,
            f"relative_value_source={source} is research-only; requires live_snapshot",
            parse_legs(row.get("legs_json")),
            0.0,
            None,
        )
    if row.get("paper_trade_eligible") != "true":
        return (
            False,
            row.get("paper_trade_blocker") or "candidate is not paper_trade_eligible",
            parse_legs(row.get("legs_json")),
            0.0,
            None,
        )
    if row.get("status") not in {RV_STATUS_PAPER_ELIGIBLE, *FULLY_PRICED_RV_STATUSES, "EXECUTABLE"}:
        return (
            False,
            "candidate is not an executable package",
            parse_legs(row.get("legs_json")),
            0.0,
            None,
        )
    legs = parse_legs(row.get("legs_json"))
    if len(legs) < 2:
        return False, "relative-value candidate must be multi-leg", legs, 0.0, None
    for leg in legs:
        valid, reason = validate_leg(leg)
        if not valid:
            return False, reason, legs, 0.0, None
    min_leg_liquidity = min(parse_float(str(leg.get("size_available", ""))) for leg in legs)
    declared_liquidity = parse_float(row.get("min_leg_liquidity"))
    if declared_liquidity > 0:
        min_leg_liquidity = min(min_leg_liquidity, declared_liquidity)
    if min_leg_liquidity < settings.min_liquidity:
        return False, "minimum leg liquidity below threshold", legs, 0.0, None
    max_trade_size = parse_float(row.get("max_trade_size")) or settings.max_trade_size
    size = min(settings.max_trade_size, max_trade_size, min_leg_liquidity)
    if size <= 0:
        return False, "non-positive package size", legs, 0.0, None
    return (
        False,
        "package execution disabled: persistent funding, atomic leg risk and settlement semantics are not verified",
        legs,
        0.0,
        None,
    )


def validate_leg(leg: dict[str, Any]) -> tuple[bool, str]:
    side = str(leg.get("side", ""))
    source = str(leg.get("price_source_for_entry") or leg.get("price_source", ""))
    price = parse_float(str(leg.get("entry_price") or leg.get("price", "")))
    bid = parse_optional_float(str(leg.get("bid", "")))
    ask = parse_optional_float(str(leg.get("ask", "")))
    if side not in {"BUY_YES", "SELL_YES", "BUY_NO", "SELL_NO"}:
        return False, "leg side is not supported"
    if bid is None or ask is None:
        return False, "missing bid/ask"
    if side.startswith("BUY") and source != "ASK":
        return False, "BUY_YES leg must execute at ASK"
    if side.startswith("SELL") and source != "BID":
        return False, "SELL_YES leg must execute at BID"
    if source not in {"ASK", "BID"}:
        return False, "midpoint or non-executable price source is not allowed"
    expected_price = ask if source == "ASK" else bid
    if abs(price - expected_price) > 1e-9:
        return False, f"{source} leg entry price does not match executable quote"
    if price <= 0 or price > 1:
        return False, "missing or invalid executable leg price"
    if parse_float(str(leg.get("size_available", ""))) <= 0:
        return False, "missing leg liquidity"
    if leg.get("freshness_status") == "stale":
        return False, "stale leg quote"
    if leg.get("leg_is_executable") == "false":
        return False, leg.get("leg_rejection_reason") or "leg is not executable"
    for warning in parse_leg_warnings(leg):
        if warning.get("blocks_paper"):
            return False, str(warning.get("message") or warning.get("code"))
    return True, ""


def package_entry_cost(legs: list[dict[str, Any]], size: float) -> float:
    total = 0.0
    for leg in legs:
        price = parse_float(str(leg.get("entry_price") or leg.get("price", "")))
        if str(leg.get("side", "")).startswith("BUY"):
            total += price * size
        elif str(leg.get("side", "")).startswith("SELL"):
            total -= price * size
    return total


def rejection_rows(
    row: dict[str, str], legs: list[dict[str, Any]], package_reason: str
) -> list[dict[str, str]]:
    if not legs:
        return [
            {
                "candidate_id": row.get("candidate_id", ""),
                "snapshot_timestamp": row.get("snapshot_timestamp", ""),
                "relation_type": row.get("relation_type", ""),
                "package_rejection_reason": package_reason,
                "leg_market_id": "",
                "leg_outcome": "",
                "leg_side": "",
                "leg_rejection_reason": "missing legs",
                "leg_sanity_warnings": "",
            }
        ]
    output = []
    for leg in legs:
        leg_reason = leg.get("leg_rejection_reason") or package_reason
        output.append(
            {
                "candidate_id": row.get("candidate_id", ""),
                "snapshot_timestamp": row.get("snapshot_timestamp", ""),
                "relation_type": row.get("relation_type", ""),
                "package_rejection_reason": package_reason,
                "leg_market_id": str(leg.get("market_id", "")),
                "leg_outcome": str(leg.get("outcome", "")),
                "leg_side": str(leg.get("side", "")),
                "leg_rejection_reason": str(leg_reason),
                "leg_sanity_warnings": str(leg.get("sanity_warnings", "")),
            }
        )
    return output


def ensure_ledger_schema(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        write_csv(path, [], RELATIVE_VALUE_LEDGER_COLUMNS)
        return
    with path.open(newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        rows = list(reader)
        fieldnames = reader.fieldnames or []
    if fieldnames == RELATIVE_VALUE_LEDGER_COLUMNS:
        return
    migrated = [
        {column: row.get(column, "") for column in RELATIVE_VALUE_LEDGER_COLUMNS} for row in rows
    ]
    write_csv(path, migrated, RELATIVE_VALUE_LEDGER_COLUMNS)


def append_ledger_rows(path: Path, rows: list[dict[str, str]]) -> None:
    if not rows:
        return
    with path.open("a", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=RELATIVE_VALUE_LEDGER_COLUMNS)
        for row in rows:
            writer.writerow(row)


def render_report(rows: list[dict[str, str]], ledger_path: Path, rejections_path: Path) -> str:
    entered = [row for row in rows if row.get("execution_status") == "FILLED_PAPER"]
    skipped = [row for row in rows if row.get("execution_status") == "SKIPPED"]
    rejection_counts = Counter(row.get("risk_reasons", "") for row in skipped)
    exposure_by_relation = defaultdict(float)
    for row in entered:
        exposure_by_relation[row.get("relation_type", "")] += abs(
            parse_float(row.get("package_entry_cost"))
        )
    lines = [
        "# EdgeLab Relative-Value Paper Trades",
        "",
        "This report is a multi-leg paper simulation only. It never uses midpoint fills and does not place live orders.",
        "",
        f"- ledger: `{ledger_path}`",
        f"- rejections: `{rejections_path}`",
        f"- candidates processed: {len(rows)}",
        f"- relative-value paper trades: {len(entered)}",
        f"- skipped candidates: {len(skipped)}",
        "",
        "## Exposure By Relation",
        "",
    ]
    if exposure_by_relation:
        lines.extend(
            markdown_table(
                ["relation_type", "absolute_entry_cost"],
                [[key, format_float(value)] for key, value in exposure_by_relation.items()],
            )
        )
    else:
        lines.append("No package exposure was opened.")
    lines.extend(["", "## Skips", ""])
    if rejection_counts:
        lines.extend(
            markdown_table(
                ["reason", "count"],
                [[reason or "n/a", str(count)] for reason, count in rejection_counts.items()],
            )
        )
    else:
        lines.append("No candidates were skipped.")
    return "\n".join(lines)


def relative_value_trade_id(row: dict[str, str], timestamp: str) -> str:
    basis = "|".join(
        [
            row.get("candidate_id", ""),
            row.get("relation_type", ""),
            row.get("legs_json", ""),
            timestamp,
        ]
    )
    return hashlib.sha256(basis.encode("utf-8")).hexdigest()[:24]


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


def parse_float(value: str | None) -> float:
    if value in {None, ""}:
        return 0.0
    try:
        return float(value)
    except ValueError:
        return 0.0


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


def format_float(value: float | None) -> str:
    return "" if value is None else f"{value:.10g}"


def markdown_table(headers: list[str], rows: list[list[str]]) -> list[str]:
    output = ["| " + " | ".join(headers) + " |", "| " + " | ".join("---" for _ in headers) + " |"]
    for row in rows:
        output.append("| " + " | ".join(str(cell).replace("|", "\\|") for cell in row) + " |")
    return output


if __name__ == "__main__":
    main()
