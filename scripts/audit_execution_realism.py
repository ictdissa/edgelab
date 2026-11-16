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

AUDIT_COLUMNS = [
    "record_id",
    "record_type",
    "market_id",
    "outcome",
    "side",
    "execution_status",
    "execution_price",
    "execution_price_source",
    "quote_price",
    "size",
    "liquidity",
    "capacity_utilization",
    "vwap_price",
    "partial_fill_size",
    "depth_sufficient",
    "spread",
    "fill_realistic",
    "blocker",
    "live_order_called",
]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Audit paper-fill execution realism")
    parser.add_argument("--ledger", type=Path, default=Path("data/paper_trades/ledger.csv"))
    parser.add_argument("--candidates", type=Path, default=Path("reports/paper_candidates.csv"))
    parser.add_argument("--output", type=Path, default=Path("reports/execution_realism.md"))
    parser.add_argument("--csv-output", type=Path)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    settings = Settings()
    ledger_rows = read_csv_rows(args.ledger)
    candidate_rows = read_csv_rows(args.candidates) if not ledger_rows else []
    rows = audit_rows(ledger_rows, "ledger", settings) if ledger_rows else audit_rows(candidate_rows, "candidate", settings)
    csv_output = args.csv_output or args.output.with_suffix(".csv")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    csv_output.parent.mkdir(parents=True, exist_ok=True)
    write_csv(csv_output, rows, AUDIT_COLUMNS)
    args.output.write_text(render_report(rows, args.ledger, args.candidates, csv_output), encoding="utf-8")
    print(f"records_audited={len(rows)}")
    print(f"realistic_fills={sum(1 for row in rows if row['fill_realistic'] == 'true')}")
    print(f"report={args.output}")
    print(f"csv={csv_output}")


def audit_rows(rows: list[dict[str, str]], record_type: str, settings: Settings) -> list[dict[str, str]]:
    return [audit_row(row, record_type, settings) for row in rows]


def audit_row(row: dict[str, str], record_type: str, settings: Settings) -> dict[str, str]:
    side = row.get("side", "")
    source = row.get("execution_price_source", "")
    execution_price = parse_optional_float(row.get("execution_price"))
    bid = parse_optional_float(row.get("bid_at_entry"))
    ask = parse_optional_float(row.get("ask_at_entry"))
    size = parse_optional_float(row.get("size"))
    liquidity = parse_optional_float(row.get("liquidity_at_entry"))
    spread = parse_optional_float(row.get("spread_at_entry"))
    execution_status = execution_status_for(row, record_type)
    quote_price = ask if side == "BUY_YES" else bid if side == "SELL_YES" else None
    blocker = realism_blocker(
        side=side,
        source=source,
        execution_price=execution_price,
        quote_price=quote_price,
        size=size,
        liquidity=liquidity,
        spread=spread,
        execution_status=execution_status,
        settings=settings,
    )
    utilization = size / liquidity if size is not None and liquidity and liquidity > 0 else None
    partial_fill_size = min(size, liquidity) if size is not None and liquidity is not None else None
    depth_sufficient = size is not None and liquidity is not None and size <= liquidity
    return {
        "record_id": row.get("paper_trade_id") or row.get("candidate_id", ""),
        "record_type": record_type,
        "market_id": row.get("market_id", ""),
        "outcome": row.get("outcome", ""),
        "side": side,
        "execution_status": execution_status,
        "execution_price": format_optional(execution_price),
        "execution_price_source": source,
        "quote_price": format_optional(quote_price),
        "size": format_optional(size),
        "liquidity": format_optional(liquidity),
        "capacity_utilization": format_optional(utilization),
        "vwap_price": format_optional(quote_price if depth_sufficient else None),
        "partial_fill_size": format_optional(partial_fill_size),
        "depth_sufficient": "true" if depth_sufficient else "false",
        "spread": format_optional(spread),
        "fill_realistic": "true" if blocker == "" else "false",
        "blocker": blocker,
        "live_order_called": "false",
    }


def execution_status_for(row: dict[str, str], record_type: str) -> str:
    if record_type == "candidate":
        return "CANDIDATE"
    if row.get("clv_status") == "skipped" or row.get("execution_price_source") == "NONE":
        return "SKIPPED"
    return "FILLED_PAPER"


def realism_blocker(
    *,
    side: str,
    source: str,
    execution_price: float | None,
    quote_price: float | None,
    size: float | None,
    liquidity: float | None,
    spread: float | None,
    execution_status: str,
    settings: Settings,
) -> str:
    if execution_status == "SKIPPED":
        return "skipped candidate"
    if side not in {"BUY_YES", "SELL_YES"}:
        return "unsupported side"
    if side == "BUY_YES" and source != "ASK":
        return "BUY_YES not filled at ask"
    if side == "SELL_YES" and source != "BID":
        return "SELL_YES not filled at bid"
    if execution_price is None or quote_price is None:
        return "missing executable quote"
    if abs(execution_price - quote_price) > 1e-9:
        return "execution price does not match bid/ask quote"
    if size is None or size <= 0:
        return "missing or non-positive size"
    if liquidity is None or liquidity < settings.min_liquidity:
        return "liquidity below threshold"
    if size > min(liquidity, settings.max_trade_size):
        return "size exceeds available conservative liquidity"
    if spread is None or spread <= 0 or spread > settings.max_spread:
        return "spread missing or above threshold"
    return ""


def render_report(rows: list[dict[str, str]], ledger_path: Path, candidates_path: Path, csv_output: Path) -> str:
    realistic = [row for row in rows if row["fill_realistic"] == "true"]
    blockers = Counter(row["blocker"] or "none" for row in rows if row["fill_realistic"] != "true")
    lines = [
        "# EdgeLab Execution Realism Audit",
        "",
        "This report checks whether paper fills are reconstructable from executable bid/ask quotes. "
        "It does not place live orders and never treats midpoint fills as tradable.",
        "",
        "## Inputs",
        "",
        f"- ledger: `{ledger_path}`",
        f"- candidates fallback: `{candidates_path}`",
        f"- csv: `{csv_output}`",
        "",
        "## Summary",
        "",
        f"- records audited: {len(rows)}",
        f"- realistic bid/ask fills: {len(realistic)}",
        "- live orders called: `false`",
        "",
        "## Blockers",
        "",
    ]
    if blockers:
        lines.extend(markdown_table(["blocker", "count"], [[key, str(value)] for key, value in blockers.most_common()]))
    else:
        lines.append("No execution realism blockers were found.")
    lines.extend(
        [
            "",
            "## Interpretation",
            "",
            "A fill is realistic only if BUY_YES uses ask, SELL_YES uses bid, execution price equals the quote, size is inside conservative displayed liquidity, and spread is within configured limits.",
        ]
    )
    return "\n".join(lines)


def parse_optional_float(value: str | None) -> float | None:
    if value in {None, ""}:
        return None
    try:
        return float(value)
    except ValueError:
        return None


def format_optional(value: float | None) -> str:
    return "" if value is None else f"{value:.10g}"


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


def markdown_table(headers: list[str], rows: list[list[str]]) -> list[str]:
    output = ["| " + " | ".join(headers) + " |", "| " + " | ".join("---" for _ in headers) + " |"]
    for row in rows:
        output.append("| " + " | ".join(str(cell).replace("|", "\\|") for cell in row) + " |")
    return output


if __name__ == "__main__":
    main()
