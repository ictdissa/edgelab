#!/usr/bin/env python
from __future__ import annotations

import argparse
import csv
from pathlib import Path

try:
    import _bootstrap  # noqa: F401
except ModuleNotFoundError:
    from scripts import _bootstrap  # noqa: F401

from edgelab.config.settings import Settings
from edgelab.research.signal_lifecycle import enrich_signal_row
from scripts.analyze_near_misses import build_near_misses
from scripts.audit_signal_generation import (
    inferred_action_side,
    load_snapshot_signals,
    paper_candidate_rejection_reason,
    parse_optional_float,
    raw_edge,
    row_spread,
)

WATCHLIST_COLUMNS = [
    "watchlist_id",
    "timestamp",
    "source_signal_id",
    "source_snapshot",
    "watchlist_type",
    "category",
    "market_id",
    "outcome",
    "action",
    "side",
    "fair_probability",
    "bid",
    "ask",
    "mid",
    "raw_edge",
    "adjusted_edge",
    "spread",
    "liquidity",
    "reason",
    "TRADEABLE",
    "RESEARCH_ONLY",
]


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate non-tradeable EdgeLab research watchlist")
    parser.add_argument("--snapshot-dir", type=Path, default=Path("data/snapshots"))
    parser.add_argument("--output", type=Path, default=Path("reports/research_watchlist.csv"))
    parser.add_argument("--markdown-output", type=Path)
    parser.add_argument("--limit", type=int, default=20)
    args = parser.parse_args()

    settings = Settings()
    rows = load_snapshot_signals(args.snapshot_dir)
    watchlist = build_watchlist(rows, settings, limit=args.limit)
    markdown_output = args.markdown_output or args.output.with_suffix(".md")

    args.output.parent.mkdir(parents=True, exist_ok=True)
    markdown_output.parent.mkdir(parents=True, exist_ok=True)
    write_csv(args.output, watchlist, WATCHLIST_COLUMNS)
    markdown_output.write_text(
        render_report(watchlist, rows, args.snapshot_dir, args.output),
        encoding="utf-8",
    )

    print(f"signals_analyzed={len(rows)}")
    print(f"watchlist_rows={len(watchlist)}")
    print(f"csv={args.output}")
    print(f"report={markdown_output}")


def build_watchlist(
    rows: list[dict[str, str]],
    settings: Settings,
    limit: int = 20,
) -> list[dict[str, str]]:
    rows = [enrich_signal_row(row, settings) for row in rows]
    output: list[dict[str, str]] = []
    seen: set[tuple[str, str, str]] = set()

    for row in top_positive_raw_edge_rows(rows, limit):
        append_watchlist_row(output, seen, row, "POSITIVE_RAW_EDGE", "top positive raw-edge signal")

    near_misses = build_near_misses(rows, settings)
    for near in sorted(
        near_misses,
        key=lambda row: parse_optional_float(row.get("adjusted_edge")) or float("-inf"),
        reverse=True,
    )[:limit]:
        source_row = near_miss_to_signal_row(near)
        append_watchlist_row(
            output,
            seen,
            source_row,
            "ADJUSTED_EDGE_NEAR_MISS",
            near.get("reason_not_traded", "top adjusted-edge near miss"),
        )

    for row in relative_value_theoretical_rows(rows, limit):
        append_watchlist_row(
            output,
            seen,
            row,
            "RELATIVE_VALUE_THEORETICAL",
            row.get("rejection_reason") or "relative-value theoretical opportunity",
        )

    for row in crypto_closest_to_passing(rows, settings, limit):
        append_watchlist_row(
            output,
            seen,
            row,
            "CRYPTO_CLOSE_TO_PASSING",
            closest_to_passing_reason(row, settings),
        )

    return output


def append_watchlist_row(
    output: list[dict[str, str]],
    seen: set[tuple[str, str, str]],
    row: dict[str, str],
    watchlist_type: str,
    reason: str,
) -> None:
    key = (watchlist_type, row.get("market_id", ""), row.get("outcome", ""))
    if key in seen:
        return
    seen.add(key)
    output.append(watchlist_row(row, watchlist_type, reason, len(output) + 1))


def watchlist_row(
    row: dict[str, str],
    watchlist_type: str,
    reason: str,
    index: int,
) -> dict[str, str]:
    return {
        "watchlist_id": f"watch:{index}",
        "timestamp": row.get("timestamp", ""),
        "source_signal_id": row.get("signal_id", ""),
        "source_snapshot": row.get("_source_snapshot", ""),
        "watchlist_type": watchlist_type,
        "category": row.get("category", ""),
        "market_id": row.get("market_id", ""),
        "outcome": row.get("outcome", ""),
        "action": row.get("action", ""),
        "side": row.get("best_side") or inferred_action_side(row),
        "fair_probability": row.get("fair_probability", ""),
        "bid": row.get("bid") or row.get("prediction_bid") or "",
        "ask": row.get("ask") or row.get("prediction_ask") or "",
        "mid": row.get("mid", ""),
        "raw_edge": format_float(raw_edge(row)),
        "adjusted_edge": row.get("best_adjusted_edge") or row.get("adjusted_edge", ""),
        "spread": format_float(row_spread(row)),
        "liquidity": row.get("liquidity") or row.get("available_size") or "",
        "reason": reason,
        "TRADEABLE": "false",
        "RESEARCH_ONLY": "true",
    }


def top_positive_raw_edge_rows(rows: list[dict[str, str]], limit: int) -> list[dict[str, str]]:
    candidates = [row for row in rows if (raw_edge(row) or 0.0) > 0]
    return sorted(candidates, key=lambda row: raw_edge(row) or float("-inf"), reverse=True)[:limit]


def near_miss_to_signal_row(row: dict[str, str]) -> dict[str, str]:
    return {
        "timestamp": row.get("timestamp", ""),
        "signal_id": row.get("near_miss_id", ""),
        "_source_snapshot": row.get("source_snapshot", ""),
        "category": row.get("category", ""),
        "market_id": row.get("market", ""),
        "outcome": row.get("outcome", ""),
        "action": row.get("action", ""),
        "fair_probability": row.get("fair_probability", ""),
        "bid": row.get("bid", ""),
        "ask": row.get("ask", ""),
        "mid": "",
        "adjusted_edge": row.get("adjusted_edge", ""),
        "liquidity": row.get("liquidity", ""),
    }


def relative_value_theoretical_rows(rows: list[dict[str, str]], limit: int) -> list[dict[str, str]]:
    candidates = [
        row
        for row in rows
        if row.get("category") == "relative_value"
        and (
            "THEORETICAL" in row.get("action", "")
            or "THEORETICAL" in row.get("rejection_reason", "")
        )
    ]
    return sorted(
        candidates,
        key=lambda row: parse_optional_float(row.get("adjusted_edge")) or float("-inf"),
        reverse=True,
    )[:limit]


def crypto_closest_to_passing(
    rows: list[dict[str, str]],
    settings: Settings,
    limit: int,
) -> list[dict[str, str]]:
    candidates = []
    for row in rows:
        if row.get("category") != "crypto":
            continue
        accepted, _reason = paper_candidate_rejection_reason(row, settings)
        if accepted:
            continue
        raw = raw_edge(row)
        adjusted = parse_optional_float(row.get("adjusted_edge"))
        if raw is None and adjusted is None:
            continue
        if (raw or 0.0) <= 0 and (adjusted or float("-inf")) < settings.min_adjusted_edge - 0.05:
            continue
        candidates.append(row)
    return sorted(
        candidates,
        key=lambda row: (
            parse_optional_float(row.get("adjusted_edge")) or float("-inf"),
            raw_edge(row) or float("-inf"),
        ),
        reverse=True,
    )[:limit]


def closest_to_passing_reason(row: dict[str, str], settings: Settings) -> str:
    accepted, reason = paper_candidate_rejection_reason(row, settings)
    if accepted:
        return "already passes strict candidate gates"
    adjusted = parse_optional_float(row.get("adjusted_edge"))
    if adjusted is not None and adjusted >= settings.min_adjusted_edge:
        return f"passes adjusted-edge threshold but blocked by {reason}"
    return reason


def render_report(
    watchlist: list[dict[str, str]],
    rows: list[dict[str, str]],
    snapshot_dir: Path,
    csv_output: Path,
) -> str:
    counts: dict[str, int] = {}
    for row in watchlist:
        counts[row["watchlist_type"]] = counts.get(row["watchlist_type"], 0) + 1
    lines = [
        "# EdgeLab Research Watchlist",
        "",
        "This watchlist is not a trade blotter and not a paper-candidate file. Every row is "
        "research-only and non-tradeable.",
        "",
        "## Summary",
        "",
        f"- snapshot directory: `{snapshot_dir}`",
        f"- source signals: {len(rows)}",
        f"- watchlist rows: {len(watchlist)}",
        f"- csv: `{csv_output}`",
        "- TRADEABLE: false for every row",
        "- RESEARCH_ONLY: true for every row",
        "",
        "## Rows By Type",
        "",
    ]
    if counts:
        lines.extend(markdown_table(["type", "rows"], [[key, str(value)] for key, value in sorted(counts.items())]))
    else:
        lines.append("No watchlist rows were produced.")
    lines.extend(["", "## Top Watchlist Rows", ""])
    if watchlist:
        lines.extend(
            markdown_table(
                ["type", "market", "outcome", "action", "side", "raw", "adjusted", "reason"],
                [
                    [
                        row["watchlist_type"],
                        row["market_id"],
                        row["outcome"],
                        row["action"],
                        row["side"],
                        row["raw_edge"],
                        row["adjusted_edge"],
                        row["reason"],
                    ]
                    for row in watchlist[:25]
                ],
            )
        )
    else:
        lines.append("No research rows met the watchlist criteria.")
    return "\n".join(lines)


def write_csv(path: Path, rows: list[dict[str, str]], columns: list[str]) -> None:
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns)
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


def format_float(value: float | int | None) -> str:
    if value is None:
        return ""
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
