#!/usr/bin/env python
from __future__ import annotations

import argparse
import random
from collections import Counter
from pathlib import Path

try:
    import _bootstrap  # noqa: F401
except ModuleNotFoundError:
    from scripts import _bootstrap  # noqa: F401

from scripts.empirical_audit_utils import (
    default_experiment_dir,
    format_float,
    load_snapshot_signals,
    load_snapshot_warnings,
    markdown_table,
    parse_float,
    pct,
    read_csv_rows,
    signal_lookup,
    warning_lookup,
    write_csv,
)

FORENSICS_COLUMNS = [
    "sample_type",
    "diagnostic_classification",
    "market_id",
    "token_id",
    "market_title",
    "outcome",
    "category",
    "side",
    "signal_timestamp",
    "markout_timestamp",
    "horizon",
    "entry_bid",
    "entry_ask",
    "entry_mid",
    "future_bid",
    "future_ask",
    "future_mid",
    "markout_price_used",
    "markout_type",
    "fair_value",
    "fair_value_source",
    "fair_value_timestamp",
    "fair_value_age_seconds",
    "raw_edge",
    "adjusted_edge",
    "rejection_reason",
    "warning_code",
    "entry_bid_ask_available",
    "future_bid_ask_available",
    "token_ids_match",
    "market_ids_match",
    "order_book_existed",
    "executable_at_signal_time",
    "quote_was_fresh",
    "spread_was_sane",
    "depth_sufficient",
    "future_price_same_token",
    "reconstructable_from_snapshots",
    "analysis_markout",
    "status",
    "signal_id",
]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Forensically inspect rejected positive markouts")
    parser.add_argument("--experiment-dir", type=Path, default=default_experiment_dir())
    parser.add_argument("--output", type=Path, default=Path("reports/rejected_winner_forensics.md"))
    parser.add_argument("--csv-output", type=Path, default=Path("reports/rejected_winner_forensics.csv"))
    parser.add_argument(
        "--manual-review-output",
        type=Path,
        default=Path("reports/rejected_winner_manual_review_sample.csv"),
    )
    parser.add_argument("--sample-size", type=int, default=50)
    parser.add_argument("--random-seed", type=int, default=7)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    rows, manual_rows, report = build_forensics(
        args.experiment_dir,
        sample_size=args.sample_size,
        random_seed=args.random_seed,
        csv_output=args.csv_output,
        manual_review_output=args.manual_review_output,
    )
    write_csv(args.csv_output, rows, FORENSICS_COLUMNS)
    write_csv(args.manual_review_output, manual_rows, FORENSICS_COLUMNS)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(report, encoding="utf-8")
    rejected_rows = unique_rejected_rows(rows)
    print(f"rejected_winners_inspected={len(rejected_rows)}")
    print(f"reconstructable={sum(1 for row in rejected_rows if row['reconstructable_from_snapshots'] == 'true')}")
    print(f"executable_at_signal_time={sum(1 for row in rejected_rows if row['executable_at_signal_time'] == 'true')}")
    print(f"report={args.output}")
    print(f"csv={args.csv_output}")


def build_forensics(
    experiment_dir: Path,
    *,
    sample_size: int = 50,
    random_seed: int = 7,
    csv_output: Path | None = None,
    manual_review_output: Path | None = None,
) -> tuple[list[dict[str, object]], list[dict[str, object]], str]:
    reports_dir = experiment_dir / "reports"
    snapshot_dir = experiment_dir / "snapshots"
    markouts = read_csv_rows(reports_dir / "markout_observations.csv")
    signals = load_snapshot_signals(snapshot_dir)
    warnings = load_snapshot_warnings(snapshot_dir)
    by_signal = signal_lookup(signals)
    by_warning = warning_lookup(warnings)

    rejected = [
        row
        for row in markouts
        if row.get("evidence_type") == "rejected_directional"
        and analysis_markout(row) is not None
        and (analysis_markout(row) or 0.0) > 0
    ]
    accepted = [
        row
        for row in markouts
        if row.get("evidence_type") == "tradable_candidate"
        and analysis_markout(row) is not None
    ]
    rng = random.Random(random_seed)
    selected: list[tuple[str, dict[str, str]]] = []
    selected.extend(("top_abs_markout", row) for row in top_by(rejected, lambda row: abs(analysis_markout(row) or 0.0), sample_size))
    selected.extend(("top_adjusted_edge", row) for row in top_by(rejected, lambda row: signal_edge(row, by_signal, "best_adjusted_edge"), sample_size))
    selected.extend(("top_raw_edge", row) for row in top_by(rejected, lambda row: signal_edge(row, by_signal, "raw_edge"), sample_size))
    selected.extend(("random_rejected_positive", row) for row in random_sample(rejected, sample_size, rng))
    selected.extend(("random_accepted_comparison", row) for row in random_sample(accepted, sample_size, rng))

    output_rows: list[dict[str, object]] = []
    seen: set[tuple[str, str, str]] = set()
    for sample_type, markout in selected:
        key = (sample_type, markout.get("signal_id", ""), markout.get("horizon", ""))
        if key in seen:
            continue
        seen.add(key)
        output_rows.append(enrich_markout(sample_type, markout, by_signal, by_warning))

    manual_rows = [
        row
        for row in output_rows
        if row["diagnostic_classification"] in {"UNKNOWN_NEEDS_MANUAL_REVIEW", "VALID_BUT_REJECTED"}
    ][:sample_size]
    if not manual_rows:
        manual_rows = output_rows[: min(sample_size, len(output_rows))]
    report = render_report(output_rows, rejected, csv_output, manual_review_output)
    return output_rows, manual_rows, report


def enrich_markout(
    sample_type: str,
    markout: dict[str, str],
    by_signal: dict[tuple[str, str, str], dict[str, str]],
    by_warning: dict[tuple[str, str], set[str]],
) -> dict[str, object]:
    signal = find_signal(markout, by_signal)
    future = find_future(markout, by_signal)
    warning_codes = sorted(by_warning.get((markout.get("market_id", ""), markout.get("outcome", "")), set()))
    entry_bid = parse_float(signal.get("bid") if signal else None)
    entry_ask = parse_float(signal.get("ask") if signal else None)
    entry_mid = parse_float(signal.get("mid") if signal else None)
    future_bid = parse_float(future.get("bid") if future else markout.get("later_bid"))
    future_ask = parse_float(future.get("ask") if future else markout.get("later_ask"))
    future_mid = parse_float(future.get("mid") if future else markout.get("later_mid"))
    spread = parse_float(signal.get("spread") if signal else None)
    liquidity = parse_float(signal.get("liquidity") if signal else None)
    age = parse_float(signal.get("data_age_seconds") if signal else None)
    side = markout.get("side", "")
    entry_bid_ask_available = entry_bid is not None and entry_ask is not None
    future_bid_ask_available = future_bid is not None and future_ask is not None
    side_entry_available = (
        side == "BUY_YES"
        and entry_ask is not None
        or side == "SELL_YES"
        and entry_bid is not None
    )
    quote_fresh = age is not None and age <= 300
    spread_sane = spread is not None and 0 <= spread <= 0.10
    depth_ok = liquidity is not None and liquidity >= 25
    executable_at_signal = side_entry_available and entry_bid_ask_available and quote_fresh and spread_sane and depth_ok
    token_id = signal.get("token_id", "") if signal else ""
    future_token_id = future.get("token_id", "") if future else ""
    token_ids_match = "unknown" if not token_id or not future_token_id else str(token_id == future_token_id).lower()
    market_ids_match = str(bool(future) and future.get("market_id") == markout.get("market_id")).lower()
    classification = classify_row(
        signal=signal,
        future=future,
        markout=markout,
        warnings=warning_codes,
        entry_bid_ask_available=entry_bid_ask_available,
        future_bid_ask_available=future_bid_ask_available,
        executable_at_signal=executable_at_signal,
        token_ids_match=token_ids_match,
    )
    return {
        "sample_type": sample_type,
        "diagnostic_classification": classification,
        "market_id": markout.get("market_id", ""),
        "token_id": token_id,
        "market_title": signal.get("question") or signal.get("title") or signal.get("market_title") if signal else "",
        "outcome": markout.get("outcome", ""),
        "category": markout.get("category", ""),
        "side": side,
        "signal_timestamp": markout.get("timestamp", ""),
        "markout_timestamp": markout.get("later_timestamp", ""),
        "horizon": markout.get("horizon", ""),
        "entry_bid": format_float(entry_bid),
        "entry_ask": format_float(entry_ask),
        "entry_mid": format_float(entry_mid),
        "future_bid": format_float(future_bid),
        "future_ask": format_float(future_ask),
        "future_mid": format_float(future_mid),
        "markout_price_used": markout.get("later_value", "") or markout.get("shadow_value", ""),
        "markout_type": markout.get("markout_type", "") or "unknown",
        "fair_value": signal.get("fair_probability", "") if signal else "",
        "fair_value_source": markout.get("fair_value_source", "") or (signal.get("fair_value_source", "") if signal else ""),
        "fair_value_timestamp": signal.get("timestamp", "") if signal else "",
        "fair_value_age_seconds": "" if age is None else format_float(age),
        "raw_edge": signal.get("raw_edge", "") if signal else "",
        "adjusted_edge": signal.get("best_adjusted_edge") or signal.get("adjusted_edge", "") if signal else "",
        "rejection_reason": signal.get("rejection_reason") or signal.get("risk_reasons", "") if signal else "",
        "warning_code": ";".join(warning_codes),
        "entry_bid_ask_available": str(entry_bid_ask_available).lower(),
        "future_bid_ask_available": str(future_bid_ask_available).lower(),
        "token_ids_match": token_ids_match,
        "market_ids_match": market_ids_match,
        "order_book_existed": str(entry_bid_ask_available).lower(),
        "executable_at_signal_time": str(executable_at_signal).lower(),
        "quote_was_fresh": str(quote_fresh).lower(),
        "spread_was_sane": str(spread_sane).lower(),
        "depth_sufficient": "unknown" if liquidity is None else str(depth_ok).lower(),
        "future_price_same_token": token_ids_match,
        "reconstructable_from_snapshots": str(signal is not None and future is not None).lower(),
        "analysis_markout": format_float(analysis_markout(markout)),
        "status": markout.get("status", ""),
        "signal_id": markout.get("signal_id", ""),
    }


def classify_row(
    *,
    signal: dict[str, str] | None,
    future: dict[str, str] | None,
    markout: dict[str, str],
    warnings: list[str],
    entry_bid_ask_available: bool,
    future_bid_ask_available: bool,
    executable_at_signal: bool,
    token_ids_match: str,
) -> str:
    if signal is None:
        return "UNKNOWN_NEEDS_MANUAL_REVIEW"
    if signal.get("source") == "static_report" or signal.get("relative_value_source") == "static_report":
        return "STATIC_REPORT_NOT_EMPIRICAL"
    if token_ids_match == "false":
        return "TOKEN_MISMATCH"
    if future is None:
        return "FUTURE_PRICE_MISSING"
    if not entry_bid_ask_available or not executable_at_signal:
        return "NON_EXECUTABLE_MISSING_BID_ASK"
    if not future_bid_ask_available:
        return "FUTURE_PRICE_MISSING"
    if markout.get("markout_type") == "midpoint_shadow" or markout.get("status") == "shadow_observed":
        return "MIDPOINT_ONLY_NOT_TRADABLE"
    if "STALE_FAIR_VALUE" in warnings or signal.get("freshness_status") == "stale":
        return "STALE_FAIR_VALUE"
    if "SUSPICIOUS_ADJUSTED_EDGE" in warnings or "SUSPICIOUS_RAW_EDGE" in warnings:
        return "SUSPICIOUS_EDGE"
    if markout.get("market_id") and future.get("market_id") and markout["market_id"] != future["market_id"]:
        return "MARKET_MISMATCH"
    if executable_at_signal and markout.get("status") == "observed":
        return "VALID_BUT_REJECTED"
    return "UNKNOWN_NEEDS_MANUAL_REVIEW"


def find_signal(markout: dict[str, str], lookup: dict[tuple[str, str, str], dict[str, str]]) -> dict[str, str] | None:
    signal_id = markout.get("signal_id", "")
    market_id = markout.get("market_id", "")
    outcome = markout.get("outcome", "")
    if signal_id and (signal_id, market_id, outcome) in lookup:
        return lookup[(signal_id, market_id, outcome)]
    timestamp = markout.get("timestamp", "").replace("Z", "+00:00")
    return lookup.get((timestamp, market_id, outcome))


def find_future(markout: dict[str, str], lookup: dict[tuple[str, str, str], dict[str, str]]) -> dict[str, str] | None:
    timestamp = markout.get("later_timestamp", "").replace("Z", "+00:00")
    if not timestamp:
        return None
    return lookup.get((timestamp, markout.get("market_id", ""), markout.get("outcome", "")))


def signal_edge(markout: dict[str, str], lookup: dict[tuple[str, str, str], dict[str, str]], column: str) -> float:
    signal = find_signal(markout, lookup)
    return parse_float(signal.get(column) if signal else None) or 0.0


def analysis_markout(row: dict[str, str]) -> float | None:
    return parse_float(row.get("markout")) if row.get("markout") else parse_float(row.get("shadow_markout"))


def top_by(rows: list[dict[str, str]], key, count: int) -> list[dict[str, str]]:
    return sorted(rows, key=key, reverse=True)[:count]


def random_sample(rows: list[dict[str, str]], count: int, rng: random.Random) -> list[dict[str, str]]:
    if len(rows) <= count:
        return list(rows)
    return rng.sample(rows, count)


def render_report(
    rows: list[dict[str, object]],
    rejected_positive: list[dict[str, str]],
    csv_output: Path | None,
    manual_review_output: Path | None,
) -> str:
    rejected_rows = unique_rejected_rows(rows)
    total = len(rejected_rows)
    class_counts = Counter(str(row["diagnostic_classification"]) for row in rejected_rows)
    reconstructable = sum(1 for row in rejected_rows if row["reconstructable_from_snapshots"] == "true")
    executable = sum(1 for row in rejected_rows if row["executable_at_signal_time"] == "true")
    stale = sum(1 for row in rejected_rows if row["diagnostic_classification"] == "STALE_FAIR_VALUE")
    missing_bid_ask = sum(1 for row in rejected_rows if row["diagnostic_classification"] == "NON_EXECUTABLE_MISSING_BID_ASK")
    midpoint = sum(1 for row in rejected_rows if row["diagnostic_classification"] == "MIDPOINT_ONLY_NOT_TRADABLE")
    suspicious = sum(1 for row in rejected_rows if row["diagnostic_classification"] == "SUSPICIOUS_EDGE")
    likely_artifact = total - class_counts.get("VALID_BUT_REJECTED", 0)
    lines = [
        "# Rejected Directional Winner Forensics",
        "",
        "Rejected winners are treated as broken or contaminated until proven otherwise.",
        "",
        f"- rejected positive markout rows available: {len(rejected_positive)}",
        f"- rejected rows inspected: {total}",
        f"- accepted comparison rows inspected: {sum(1 for row in rows if row.get('sample_type') == 'random_accepted_comparison')}",
        f"- reconstructable from stored snapshots: {reconstructable} ({pct(reconstructable, total)})",
        f"- executable at signal time: {executable} ({pct(executable, total)})",
        f"- stale fair values: {stale} ({pct(stale, total)})",
        f"- missing bid/ask: {missing_bid_ask} ({pct(missing_bid_ask, total)})",
        f"- midpoint-only or non-tradable marks: {midpoint} ({pct(midpoint, total)})",
        f"- suspicious edges: {suspicious} ({pct(suspicious, total)})",
        f"- likely artifact / not proven missed alpha: {likely_artifact} ({pct(likely_artifact, total)})",
        "",
        "## Diagnostic Classifications",
        "",
    ]
    lines.extend(markdown_table(["classification", "count"], class_counts.most_common()))
    lines.extend(
        [
            "",
            "## Interpretation",
            "",
            (
                "Rejected winners are mostly data artifacts or non-executable research marks."
                if class_counts.get("VALID_BUT_REJECTED", 0) < max(3, total // 10)
                else "Some rejected winners may be real missed alpha, but they still require manual validation before changing thresholds."
            ),
            "",
            "Threshold loosening is not justified by this report. Missing bid/ask, stale fair values, suspicious edges, "
            "and shadow-only marks must be resolved before any rejected signal is considered missed alpha.",
            "",
            "## Outputs",
            "",
            f"- forensics csv: `{csv_output}`",
            f"- manual review sample: `{manual_review_output}`",
        ]
    )
    return "\n".join(lines)


def unique_rejected_rows(rows: list[dict[str, object]]) -> list[dict[str, object]]:
    unique: dict[tuple[object, object], dict[str, object]] = {}
    for row in rows:
        if row.get("sample_type") == "random_accepted_comparison":
            continue
        unique.setdefault((row.get("signal_id", ""), row.get("horizon", "")), row)
    return list(unique.values())


if __name__ == "__main__":
    main()
