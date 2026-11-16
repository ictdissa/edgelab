#!/usr/bin/env python
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from datetime import timedelta
from pathlib import Path

try:
    import _bootstrap  # noqa: F401
except ModuleNotFoundError:
    from scripts import _bootstrap  # noqa: F401

from scripts.empirical_audit_utils import (
    default_experiment_dir,
    load_snapshot_signals,
    markdown_table,
    parse_float,
    parse_time,
    read_csv_rows,
    signal_lookup,
    write_csv,
)

HORIZON_SECONDS = {"1m": 60, "5m": 300, "15m": 900, "1h": 3600, "6h": 21600, "30m": 1800}

AUDIT_COLUMNS = [
    "row_id",
    "signal_id",
    "market_id",
    "outcome",
    "side",
    "horizon",
    "evidence_type",
    "status",
    "markout_type",
    "violation_type",
    "severity",
    "detail",
    "entry_price",
    "entry_bid",
    "entry_ask",
    "future_bid",
    "future_ask",
    "future_mid",
    "later_value",
    "later_value_source",
    "markout",
]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Audit markout computation for conservative bid/ask semantics")
    parser.add_argument("--experiment-dir", type=Path, default=default_experiment_dir())
    parser.add_argument("--output", type=Path, default=Path("reports/markout_computation_audit.md"))
    parser.add_argument("--csv-output", type=Path, default=Path("reports/markout_computation_audit.csv"))
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    rows, report = build_audit(args.experiment_dir, args.csv_output)
    write_csv(args.csv_output, rows, AUDIT_COLUMNS)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(report, encoding="utf-8")
    print(f"markout_audit_violations={sum(1 for row in rows if row['severity'] == 'VIOLATION')}")
    print(f"report={args.output}")
    print(f"csv={args.csv_output}")


def build_audit(experiment_dir: Path, csv_output: Path | None = None) -> tuple[list[dict[str, object]], str]:
    reports_dir = experiment_dir / "reports"
    snapshot_dir = experiment_dir / "snapshots"
    markouts = read_csv_rows(reports_dir / "markout_observations.csv")
    signals = load_snapshot_signals(snapshot_dir)
    lookup = signal_lookup(signals)
    audit_rows = audit_markout_rows(markouts, lookup)
    report = render_report(markouts, audit_rows, csv_output)
    return audit_rows, report


def audit_markout_rows(
    markouts: list[dict[str, str]],
    lookup: dict[tuple[str, str, str], dict[str, str]],
) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    seen_pairs: dict[tuple[str, str, str], int] = defaultdict(int)
    for index, row in enumerate(markouts, start=1):
        signal = find_signal(row, lookup)
        future = find_future(row, lookup)
        pair_key = (row.get("signal_id", ""), row.get("later_timestamp", ""), row.get("horizon", ""))
        seen_pairs[pair_key] += 1
        rows.extend(audit_single_row(index, row, signal, future))
    for index, row in enumerate(markouts, start=1):
        pair_key = (row.get("signal_id", ""), row.get("later_timestamp", ""), row.get("horizon", ""))
        if seen_pairs[pair_key] > 1 and row.get("status") == "observed":
            rows.append(audit_row(index, row, "DUPLICATE_SNAPSHOT_PAIR", "VIOLATION", "same signal/horizon/future pair appears more than once"))
    return rows


def audit_single_row(
    index: int,
    row: dict[str, str],
    signal: dict[str, str] | None,
    future: dict[str, str] | None,
) -> list[dict[str, object]]:
    output: list[dict[str, object]] = []
    side = row.get("side", "")
    entry_price = parse_float(row.get("entry_price"))
    entry_bid = parse_float(signal.get("bid") if signal else None)
    entry_ask = parse_float(signal.get("ask") if signal else None)
    future_bid = parse_float(row.get("later_bid") or (future.get("bid") if future else None))
    future_ask = parse_float(row.get("later_ask") or (future.get("ask") if future else None))
    later_value = parse_float(row.get("later_value"))
    markout = parse_float(row.get("markout"))
    status = row.get("status", "")
    markout_type = row.get("markout_type", "")

    for field, value in {
        "entry_price": entry_price,
        "entry_bid": entry_bid,
        "entry_ask": entry_ask,
        "future_bid": future_bid,
        "future_ask": future_ask,
        "later_value": later_value,
        "markout": markout,
    }.items():
        if value is not None and not 0 <= value <= 1 and field != "markout":
            output.append(audit_row(index, row, "PRICE_OUT_OF_RANGE", "VIOLATION", f"{field}={value} outside [0,1]"))

    if side == "BUY_YES" and entry_price is not None and entry_ask is not None and entry_price < entry_ask - 1e-12:
        output.append(audit_row(index, row, "BUY_ENTRY_BELOW_ASK", "VIOLATION", "BUY executable entry is below entry ask"))
    if side == "SELL_YES" and entry_price is not None and entry_bid is not None and entry_price > entry_bid + 1e-12:
        output.append(audit_row(index, row, "SELL_ENTRY_ABOVE_BID", "VIOLATION", "SELL executable entry is above entry bid"))

    if status == "observed":
        if markout_type != "tradable_bid_ask":
            output.append(audit_row(index, row, "EXECUTABLE_CLV_NOT_BID_ASK", "VIOLATION", "observed executable markout is not bid/ask labeled"))
        if side == "BUY_YES" and row.get("later_value_source") != "FUTURE_BID":
            output.append(audit_row(index, row, "BUY_EXIT_NOT_FUTURE_BID", "VIOLATION", "BUY tradable markout must exit at future bid"))
        if side == "SELL_YES" and row.get("later_value_source") != "FUTURE_ASK":
            output.append(audit_row(index, row, "SELL_EXIT_NOT_FUTURE_ASK", "VIOLATION", "SELL tradable markout must exit at future ask"))
        if later_value is None or markout is None:
            output.append(audit_row(index, row, "MISSING_FUTURE_PRICE_USED", "VIOLATION", "observed row has missing future price or markout"))
        if future is None:
            output.append(audit_row(index, row, "FUTURE_ROW_NOT_RECONSTRUCTABLE", "VIOLATION", "future markout row not found in snapshots"))

    if signal and future and signal.get("market_id") != future.get("market_id"):
        output.append(audit_row(index, row, "MARKET_MISMATCH", "VIOLATION", "entry and future market_id differ"))
    if signal and future and signal.get("token_id") and future.get("token_id") and signal.get("token_id") != future.get("token_id"):
        output.append(audit_row(index, row, "TOKEN_MISMATCH", "VIOLATION", "entry and future token_id differ"))

    if status == "observed" and stale_followup(row):
        output.append(audit_row(index, row, "STALE_FUTURE_SNAPSHOT", "WARNING", "future snapshot is much later than target horizon"))

    if not output:
        output.append(audit_row(index, row, "OK", "OK", "markout row passes audit checks"))
    return output


def stale_followup(row: dict[str, str]) -> bool:
    start = parse_time(row.get("timestamp"))
    later = parse_time(row.get("later_timestamp"))
    horizon = HORIZON_SECONDS.get(row.get("horizon", ""), 0)
    if start is None or later is None or horizon <= 0:
        return False
    lag = (later - (start + timedelta(seconds=horizon))).total_seconds()
    return lag > max(180.0, horizon * 0.5)


def audit_row(index: int, row: dict[str, str], violation_type: str, severity: str, detail: str) -> dict[str, object]:
    return {
        "row_id": str(index),
        "signal_id": row.get("signal_id", ""),
        "market_id": row.get("market_id", ""),
        "outcome": row.get("outcome", ""),
        "side": row.get("side", ""),
        "horizon": row.get("horizon", ""),
        "evidence_type": row.get("evidence_type", ""),
        "status": row.get("status", ""),
        "markout_type": row.get("markout_type", ""),
        "violation_type": violation_type,
        "severity": severity,
        "detail": detail,
        "entry_price": row.get("entry_price", ""),
        "entry_bid": "",
        "entry_ask": "",
        "future_bid": row.get("later_bid", ""),
        "future_ask": row.get("later_ask", ""),
        "future_mid": row.get("later_mid", ""),
        "later_value": row.get("later_value", ""),
        "later_value_source": row.get("later_value_source", ""),
        "markout": row.get("markout", ""),
    }


def find_signal(row: dict[str, str], lookup: dict[tuple[str, str, str], dict[str, str]]) -> dict[str, str] | None:
    signal_id = row.get("signal_id", "")
    market_id = row.get("market_id", "")
    outcome = row.get("outcome", "")
    if signal_id and (signal_id, market_id, outcome) in lookup:
        return lookup[(signal_id, market_id, outcome)]
    return lookup.get((row.get("timestamp", "").replace("Z", "+00:00"), market_id, outcome))


def find_future(row: dict[str, str], lookup: dict[tuple[str, str, str], dict[str, str]]) -> dict[str, str] | None:
    later = row.get("later_timestamp", "").replace("Z", "+00:00")
    if not later:
        return None
    return lookup.get((later, row.get("market_id", ""), row.get("outcome", "")))


def render_report(markouts: list[dict[str, str]], audit_rows: list[dict[str, object]], csv_output: Path | None) -> str:
    violations = [row for row in audit_rows if row["severity"] == "VIOLATION"]
    warnings = [row for row in audit_rows if row["severity"] == "WARNING"]
    counts = Counter(str(row["violation_type"]) for row in violations)
    shadow_rows = sum(1 for row in markouts if row.get("status") == "shadow_observed")
    observed = sum(1 for row in markouts if row.get("status") == "observed")
    rejected_violations = sum(
        1 for row in violations if row.get("evidence_type") == "rejected_directional"
    )
    accepted_violations = sum(
        1 for row in violations if row.get("evidence_type") == "tradable_candidate"
    )
    lines = [
        "# Markout Computation Audit",
        "",
        "This audit checks whether executable CLV is separated from shadow midpoint/model marks.",
        "",
        f"- raw markout rows: {len(markouts)}",
        f"- executable observed rows: {observed}",
        f"- shadow-only observed rows: {shadow_rows}",
        f"- violations: {len(violations)}",
        f"- warnings: {len(warnings)}",
        f"- accepted-candidate violations: {accepted_violations}",
        f"- rejected-directional violations: {rejected_violations}",
        "",
        "## Separation Statement",
        "",
        (
            "Tradable CLV and shadow midpoint CLV are separated."
            if not any(row["violation_type"] == "EXECUTABLE_CLV_NOT_BID_ASK" for row in violations)
            else "Tradable CLV and shadow midpoint CLV are not cleanly separated."
        ),
        "",
        "## Violations By Type",
        "",
    ]
    if counts:
        lines.extend(markdown_table(["violation_type", "count"], counts.most_common()))
    else:
        lines.append("No markout computation violations were found.")
    lines.extend(
        [
            "",
            "## Trust Assessment",
            "",
            (
                "Accepted-candidate CLV numbers are mechanically trustworthy under the current bid/ask audit."
                if accepted_violations == 0
                else "Accepted-candidate CLV numbers need regeneration before interpretation."
            ),
            (
                "Rejected-directional CLV numbers should be treated skeptically unless they are `tradable_bid_ask` rows."
                if rejected_violations == 0
                else "Rejected-directional CLV numbers contain computation violations and should not be used as alpha evidence."
            ),
            "",
            f"- audit csv: `{csv_output}`",
        ]
    )
    return "\n".join(lines)


if __name__ == "__main__":
    main()
