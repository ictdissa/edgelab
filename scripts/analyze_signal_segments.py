#!/usr/bin/env python
from __future__ import annotations

import argparse
from collections import defaultdict
from pathlib import Path
from statistics import median

try:
    import _bootstrap  # noqa: F401
except ModuleNotFoundError:
    from scripts import _bootstrap  # noqa: F401

from scripts.empirical_audit_utils import (
    average,
    bootstrap_ci,
    default_experiment_dir,
    format_float,
    load_snapshot_signals,
    load_snapshot_warnings,
    markdown_table,
    parse_float,
    read_csv_rows,
    signal_lookup,
    trimmed_mean,
    warning_lookup,
    write_csv,
)

SEGMENT_COLUMNS = [
    "segment_type",
    "segment",
    "horizon",
    "evidence_type",
    "count",
    "mean_markout",
    "median_markout",
    "win_rate",
    "trimmed_mean",
    "bootstrap_ci_low",
    "bootstrap_ci_high",
    "missing_bid_ask_fraction",
    "stale_fair_value_fraction",
    "suspicious_edge_fraction",
    "reconstructable_fraction",
    "executable_at_signal_fraction",
]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Analyze markout quality by category/source/side/risk segment")
    parser.add_argument("--experiment-dir", type=Path, default=default_experiment_dir())
    parser.add_argument("--output", type=Path, default=Path("reports/signal_segment_report.md"))
    parser.add_argument("--csv-output", type=Path, default=Path("reports/signal_segment_stats.csv"))
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    rows, report = build_segment_report(args.experiment_dir, args.csv_output)
    write_csv(args.csv_output, rows, SEGMENT_COLUMNS)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(report, encoding="utf-8")
    strongest = strongest_segment(rows)
    weakest = weakest_segment(rows)
    print(f"segment_rows={len(rows)}")
    print(f"strongest_segment={strongest}")
    print(f"weakest_segment={weakest}")
    print(f"report={args.output}")


def build_segment_report(experiment_dir: Path, csv_output: Path | None = None) -> tuple[list[dict[str, object]], str]:
    snapshot_dir = experiment_dir / "snapshots"
    reports_dir = experiment_dir / "reports"
    markouts = read_csv_rows(reports_dir / "markout_observations.csv")
    signals = load_snapshot_signals(snapshot_dir)
    warnings = load_snapshot_warnings(snapshot_dir)
    lookup = signal_lookup(signals)
    warning_by_key = warning_lookup(warnings)
    observed = [row for row in markouts if row.get("status") == "observed" and parse_float(row.get("markout")) is not None]
    enriched = [enrich(row, lookup, warning_by_key) for row in observed]
    rows = aggregate_segments(enriched)
    report = render_report(rows, csv_output)
    return rows, report


def enrich(
    row: dict[str, str],
    lookup: dict[tuple[str, str, str], dict[str, str]],
    warning_by_key: dict[tuple[str, str], set[str]],
) -> dict[str, object]:
    signal = find_signal(row, lookup)
    future = find_future(row, lookup)
    warnings = warning_by_key.get((row.get("market_id", ""), row.get("outcome", "")), set())
    entry_bid = parse_float(signal.get("bid") if signal else None)
    entry_ask = parse_float(signal.get("ask") if signal else None)
    spread = parse_float(signal.get("spread") if signal else row.get("spread"))
    liquidity = parse_float(signal.get("liquidity") if signal else None)
    quote_age = parse_float(signal.get("data_age_seconds") if signal else None)
    side = row.get("side", "")
    executable = (side == "BUY_YES" and entry_ask is not None) or (side == "SELL_YES" and entry_bid is not None)
    return {
        **row,
        "_markout": parse_float(row.get("markout")) or 0.0,
        "_signal": signal,
        "_future": future,
        "_warnings": warnings,
        "_missing_bid_ask": entry_bid is None or entry_ask is None,
        "_stale_fair_value": "STALE_FAIR_VALUE" in warnings or (signal or {}).get("freshness_status") == "stale",
        "_suspicious_edge": "SUSPICIOUS_RAW_EDGE" in warnings or "SUSPICIOUS_ADJUSTED_EDGE" in warnings,
        "_reconstructable": signal is not None and future is not None,
        "_executable": executable,
        "spread_bucket": row.get("spread_bucket") or bucket(spread, [(0.01, "<=1%"), (0.03, "1-3%"), (0.08, "3-8%")], ">8%"),
        "liquidity_bucket": row.get("liquidity_bucket") or bucket(liquidity, [(10, "<10"), (50, "10-50"), (250, "50-250")], ">=250"),
        "quote_age_bucket": row.get("quote_age_bucket") or bucket(quote_age, [(60, "<=60s"), (300, "60-300s"), (900, "300-900s")], ">900s"),
        "warning_code": ";".join(sorted(warnings)) or "none",
        "rejection_reason": (signal or {}).get("rejection_reason", ""),
        "market_liquidity_bucket": bucket(liquidity, [(50, "<50"), (250, "50-250"), (1000, "250-1000")], ">=1000"),
        "market_volume_bucket": "missing",
        "time_to_expiry_bucket": "missing",
    }


def aggregate_segments(rows: list[dict[str, object]]) -> list[dict[str, object]]:
    dimensions = [
        "category",
        "fair_value_source",
        "side",
        "edge_bucket",
        "spread_bucket",
        "liquidity_bucket",
        "quote_age_bucket",
        "time_to_expiry_bucket",
        "rejection_reason",
        "warning_code",
        "market_volume_bucket",
        "market_liquidity_bucket",
    ]
    groups: dict[tuple[str, str, str, str], list[dict[str, object]]] = defaultdict(list)
    for row in rows:
        horizon = str(row.get("horizon", ""))
        evidence_type = str(row.get("evidence_type", ""))
        for dimension in dimensions:
            value = str(row.get(dimension, "") or "missing")
            groups[(dimension, value, horizon, evidence_type)].append(row)
    output = []
    for (segment_type, segment, horizon, evidence_type), group_rows in sorted(groups.items()):
        values = [float(row["_markout"]) for row in group_rows]
        low, high = bootstrap_ci(values)
        output.append(
            {
                "segment_type": segment_type,
                "segment": segment,
                "horizon": horizon,
                "evidence_type": evidence_type,
                "count": len(values),
                "mean_markout": format_float(average(values)),
                "median_markout": format_float(median(values) if values else None),
                "win_rate": format_float(sum(1 for value in values if value > 0) / len(values) if values else None),
                "trimmed_mean": format_float(trimmed_mean(values)),
                "bootstrap_ci_low": format_float(low),
                "bootstrap_ci_high": format_float(high),
                "missing_bid_ask_fraction": fraction(group_rows, "_missing_bid_ask"),
                "stale_fair_value_fraction": fraction(group_rows, "_stale_fair_value"),
                "suspicious_edge_fraction": fraction(group_rows, "_suspicious_edge"),
                "reconstructable_fraction": fraction(group_rows, "_reconstructable"),
                "executable_at_signal_fraction": fraction(group_rows, "_executable"),
            }
        )
    return output


def fraction(rows: list[dict[str, object]], key: str) -> str:
    return format_float(sum(1 for row in rows if row.get(key)) / len(rows) if rows else None)


def bucket(value: float | None, thresholds: list[tuple[float, str]], upper_label: str) -> str:
    if value is None:
        return "missing"
    for threshold, label in thresholds:
        if value <= threshold:
            return label
    return upper_label


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


def strongest_segment(rows: list[dict[str, object]]) -> str:
    candidates = [row for row in rows if int(row["count"]) >= 20 and row["evidence_type"] == "tradable_candidate"]
    if not candidates:
        return "none"
    best = max(candidates, key=lambda row: parse_float(row.get("median_markout")) or -999)
    return f"{best['segment_type']}={best['segment']} horizon={best['horizon']} median={best['median_markout']}"


def weakest_segment(rows: list[dict[str, object]]) -> str:
    candidates = [row for row in rows if int(row["count"]) >= 20 and row["evidence_type"] == "tradable_candidate"]
    if not candidates:
        return "none"
    worst = min(candidates, key=lambda row: parse_float(row.get("median_markout")) or 999)
    return f"{worst['segment_type']}={worst['segment']} horizon={worst['horizon']} median={worst['median_markout']}"


def render_report(rows: list[dict[str, object]], csv_output: Path | None) -> str:
    tradable = [row for row in rows if row["evidence_type"] == "tradable_candidate" and int(row["count"]) >= 20]
    positive_median = [row for row in tradable if (parse_float(row.get("median_markout")) or 0.0) > 0]
    high_warning = [
        row
        for row in rows
        if int(row["count"]) >= 20
        and (
            (parse_float(row.get("missing_bid_ask_fraction")) or 0.0) > 0.2
            or (parse_float(row.get("suspicious_edge_fraction")) or 0.0) > 0.2
        )
    ]
    by_category = [row for row in tradable if row["segment_type"] == "category"]
    strongest = strongest_segment(rows)
    weakest = weakest_segment(rows)
    lines = [
        "# Signal Segment Analysis",
        "",
        "Aggregate CLV is not enough. This report decomposes markouts by source, side, edge, liquidity, spread, quote age, and warning state.",
        "",
        f"- segment rows: {len(rows)}",
        f"- tradable segments with >=20 observations: {len(tradable)}",
        f"- tradable segments with positive median markout: {len(positive_median)}",
        f"- warning-contaminated segments with >=20 observations: {len(high_warning)}",
        f"- strongest tradable segment: {strongest}",
        f"- weakest tradable segment: {weakest}",
        "",
        "## Category Summary",
        "",
    ]
    if by_category:
        lines.extend(
            markdown_table(
                ["category", "horizon", "count", "mean", "median", "win_rate"],
                [
                    [
                        row["segment"],
                        row["horizon"],
                        row["count"],
                        row["mean_markout"],
                        row["median_markout"],
                        row["win_rate"],
                    ]
                    for row in by_category
                ],
            )
        )
    else:
        lines.append("No category segment has enough tradable observations.")
    lines.extend(
        [
            "",
            "## Interpretation",
            "",
            (
                "No segment has credible positive median markout yet."
                if not positive_median
                else "At least one segment has positive median markout, but check warning contamination and confidence intervals before acting."
            ),
            "Positive means should be treated as outlier-sensitive unless medians and bootstrap intervals agree.",
            "Sources/categories with high warning fractions should be disabled or downweighted in future experiments until data quality improves.",
            "",
            f"- segment stats csv: `{csv_output}`",
        ]
    )
    return "\n".join(lines)


if __name__ == "__main__":
    main()
