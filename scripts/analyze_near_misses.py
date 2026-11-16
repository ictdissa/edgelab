#!/usr/bin/env python
from __future__ import annotations

import argparse
import csv
from collections import Counter
from pathlib import Path
from statistics import median

try:
    import _bootstrap  # noqa: F401
except ModuleNotFoundError:
    from scripts import _bootstrap  # noqa: F401

from edgelab.config.settings import Settings
from edgelab.research.signal_lifecycle import FINAL_PAPER_CANDIDATE, enrich_signal_row
from scripts.audit_signal_generation import (
    inferred_action_side,
    is_fresh,
    load_snapshot_signals,
    paper_candidate_rejection_reason,
    parse_optional_float,
    raw_edge,
    row_spread,
    signal_blockers,
)

NEAR_MISS_COLUMNS = [
    "near_miss_id",
    "timestamp",
    "source_snapshot",
    "market",
    "outcome",
    "category",
    "action",
    "inferred_side",
    "fair_probability",
    "bid",
    "ask",
    "raw_edge",
    "adjusted_edge",
    "cost_drag",
    "spread",
    "liquidity",
    "reason_not_traded",
    "what_would_need_to_improve",
]


def main() -> None:
    parser = argparse.ArgumentParser(description="Analyze EdgeLab near-miss research signals")
    parser.add_argument("--snapshot-dir", type=Path, default=Path("data/snapshots"))
    parser.add_argument("--output", type=Path, default=Path("reports/near_misses.md"))
    parser.add_argument("--csv-output", type=Path)
    args = parser.parse_args()

    settings = Settings()
    rows = load_snapshot_signals(args.snapshot_dir)
    near_misses = build_near_misses(rows, settings)
    csv_output = args.csv_output or args.output.with_suffix(".csv")

    args.output.parent.mkdir(parents=True, exist_ok=True)
    csv_output.parent.mkdir(parents=True, exist_ok=True)
    write_csv(csv_output, near_misses, NEAR_MISS_COLUMNS)
    args.output.write_text(
        render_report(near_misses, rows, args.snapshot_dir, csv_output),
        encoding="utf-8",
    )

    print(f"signals_analyzed={len(rows)}")
    print(f"near_misses={len(near_misses)}")
    print(f"best_raw_edge={format_float(best_value(near_misses, 'raw_edge'))}")
    print(f"best_adjusted_edge={format_float(best_value(near_misses, 'adjusted_edge'))}")
    print(f"report={args.output}")
    print(f"csv={csv_output}")


def build_near_misses(rows: list[dict[str, str]], settings: Settings) -> list[dict[str, str]]:
    indexed_rows = [
        dict(enrich_signal_row(row, settings), _row_index=str(index)) for index, row in enumerate(rows)
    ]
    top_adjusted = top_positive_adjusted_keys(indexed_rows, limit=20)
    output: list[dict[str, str]] = []
    for row in indexed_rows:
        if is_near_miss(row, top_adjusted, settings):
            output.append(near_miss_row(row, settings, len(output) + 1))
    return sorted(
        output,
        key=lambda row: (
            parse_optional_float(row.get("raw_edge")) or float("-inf"),
            parse_optional_float(row.get("adjusted_edge")) or float("-inf"),
        ),
        reverse=True,
    )


def is_near_miss(
    row: dict[str, str],
    top_adjusted_keys: set[str],
    settings: Settings,
) -> bool:
    if parse_optional_float(row.get("fair_probability")) is None:
        return False
    bid = parse_optional_float(row.get("bid") or row.get("prediction_bid"))
    ask = parse_optional_float(row.get("ask") or row.get("prediction_ask"))
    if bid is None or ask is None:
        return False
    if not is_fresh(row, settings):
        return False
    raw = raw_edge(row)
    adjusted = adjusted_value(row)
    has_edge = raw is not None and raw > 0
    is_top_adjusted = row_key(row) in top_adjusted_keys and adjusted is not None and adjusted > 0
    if not (has_edge or is_top_adjusted):
        return False
    return row.get("final_decision") != FINAL_PAPER_CANDIDATE


def near_miss_row(row: dict[str, str], settings: Settings, index: int) -> dict[str, str]:
    raw = raw_edge(row)
    adjusted = parse_optional_float(row.get("best_adjusted_edge") or row.get("adjusted_edge"))
    cost_drag = raw - adjusted if raw is not None and adjusted is not None else None
    accepted, reason = paper_candidate_rejection_reason(row, settings)
    reason_not_traded = "paper candidate" if accepted else reason
    blockers = signal_blockers(row, settings)
    if blockers:
        reason_not_traded = "; ".join(dict.fromkeys([reason_not_traded, *blockers]))
    return {
        "near_miss_id": f"near_miss:{index}",
        "timestamp": row.get("timestamp", ""),
        "source_snapshot": row.get("_source_snapshot", ""),
        "market": row.get("market_id", ""),
        "outcome": row.get("outcome", ""),
        "category": row.get("category", ""),
        "action": row.get("action", ""),
        "inferred_side": row.get("best_side") or inferred_action_side(row),
        "fair_probability": row.get("fair_probability", ""),
        "bid": row.get("bid") or row.get("prediction_bid") or "",
        "ask": row.get("ask") or row.get("prediction_ask") or "",
        "raw_edge": format_float(raw),
        "adjusted_edge": row.get("best_adjusted_edge") or row.get("adjusted_edge", ""),
        "cost_drag": format_float(cost_drag),
        "spread": format_float(row_spread(row)),
        "liquidity": row.get("liquidity") or row.get("available_size") or "",
        "reason_not_traded": reason_not_traded,
        "what_would_need_to_improve": improvement_text(row, settings),
    }


def top_positive_adjusted_keys(rows: list[dict[str, str]], limit: int) -> set[str]:
    candidates = [
        (row_key(row), adjusted)
        for row in rows
        if (adjusted := adjusted_value(row)) is not None
        and adjusted > 0
    ]
    candidates.sort(key=lambda item: item[1], reverse=True)
    return {key for key, _adjusted in candidates[:limit]}


def row_key(row: dict[str, str]) -> str:
    return row.get("signal_id") or row.get("_row_index", "")


def improvement_text(row: dict[str, str], settings: Settings) -> str:
    parts: list[str] = []
    if row.get("best_side") not in {"BUY_YES", "SELL_YES"}:
        parts.append("action generation must emit BUY_YES/SELL_YES")
    if not (
        row.get("risk_decision") == "APPROVED" or row.get("risk_approved") in {"True", "true", "1"}
    ):
        reason = row.get("rejection_reason") or row.get("risk_reasons") or "risk rejection must clear"
        parts.append(f"risk gate must clear ({reason})")
    liquidity = parse_optional_float(row.get("liquidity") or row.get("available_size"))
    if liquidity is None or liquidity < settings.min_liquidity:
        parts.append("liquidity must increase")
    spread = row_spread(row)
    if spread is None or spread <= 0 or spread > settings.max_spread:
        parts.append("spread must tighten and both sides must be present")
    adjusted = adjusted_value(row)
    if adjusted is None or adjusted < settings.min_adjusted_edge:
        gap = settings.min_adjusted_edge - (adjusted or 0.0)
        parts.append(f"adjusted edge must improve by at least {gap:.4f}")
    if not is_fresh(row, settings):
        parts.append("fresh data is required")
    return "; ".join(parts) if parts else "strict paper-candidate gates must all pass"


def render_report(
    near_misses: list[dict[str, str]],
    all_rows: list[dict[str, str]],
    snapshot_dir: Path,
    csv_output: Path,
) -> str:
    raw_values = values_for(near_misses, "raw_edge")
    adjusted_values = values_for(near_misses, "adjusted_edge")
    spreads = values_for(near_misses, "spread")
    liquidities = values_for(near_misses, "liquidity")
    blockers = Counter(
        part.strip()
        for row in near_misses
        for part in row.get("reason_not_traded", "").split(";")
        if part.strip()
    )
    dominant = blockers.most_common(1)[0][0] if blockers else "n/a"
    lines = [
        "# EdgeLab Near-Miss Report",
        "",
        "Near misses are research rows with fair value, bid/ask, fresh data, and edge evidence "
        "that still fail strict paper-candidate gates.",
        "",
        "## Summary",
        "",
        f"- snapshot directory: `{snapshot_dir}`",
        f"- csv: `{csv_output}`",
        f"- signals scanned: {len(all_rows)}",
        f"- near misses: {len(near_misses)}",
        f"- best raw edge: {format_float(max(raw_values) if raw_values else None) or 'n/a'}",
        f"- best adjusted edge: {format_float(max(adjusted_values) if adjusted_values else None) or 'n/a'}",
        f"- median spread of near misses: {format_float(median(spreads)) if spreads else 'n/a'}",
        f"- median liquidity of near misses: {format_float(median(liquidities)) if liquidities else 'n/a'}",
        f"- dominant blocker: {dominant}",
        "",
    ]
    if near_misses:
        lines.extend(["## Top Near Misses", ""])
        lines.extend(
            markdown_table(
                ["market", "outcome", "action", "raw", "adjusted", "spread", "liquidity", "reason"],
                [
                    [
                        row["market"],
                        row["outcome"],
                        row["action"],
                        row["raw_edge"],
                        row["adjusted_edge"],
                        row["spread"],
                        row["liquidity"],
                        row["reason_not_traded"],
                    ]
                    for row in near_misses[:20]
                ],
            )
        )
    else:
        lines.append("No near misses met the research criteria.")
    lines.extend(["", "## Blockers", ""])
    if blockers:
        lines.extend(markdown_table(["blocker", "near_misses"], [[key, str(value)] for key, value in blockers.most_common(12)]))
    else:
        lines.append("No blockers were recorded.")
    return "\n".join(lines)


def values_for(rows: list[dict[str, str]], column: str) -> list[float]:
    return [
        value
        for row in rows
        if (value := parse_optional_float(row.get(column))) is not None
    ]


def best_value(rows: list[dict[str, str]], column: str) -> float | None:
    values = values_for(rows, column)
    return max(values) if values else None


def adjusted_value(row: dict[str, str]) -> float | None:
    return parse_optional_float(row.get("best_adjusted_edge") or row.get("adjusted_edge"))


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
