#!/usr/bin/env python
from __future__ import annotations

import argparse
import csv
import math
from pathlib import Path

try:
    import _bootstrap  # noqa: F401
except ModuleNotFoundError:
    from scripts import _bootstrap  # noqa: F401

from edgelab.config.settings import Settings

PNL_COLUMNS = [
    "paper_trade_id",
    "market_id",
    "outcome",
    "side",
    "entry_price",
    "final_payout",
    "size",
    "fees",
    "realized_pnl",
    "return_on_capital",
    "holding_period_seconds",
    "clv_5m",
    "clv_1h",
    "clv_vs_pnl_same_sign",
    "status",
]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Analyze settlement PnL for resolved prediction-market paper trades")
    parser.add_argument("--ledger", type=Path, default=Path("data/paper_trades/ledger.csv"))
    parser.add_argument("--settlements", type=Path, default=Path("data/settlements.csv"))
    parser.add_argument("--output", type=Path, default=Path("reports/settlement_pnl.md"))
    parser.add_argument("--csv-output", type=Path)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    settings = Settings()
    settlements = load_settlements(args.settlements)
    rows = [pnl_row(row, settlements, settings) for row in read_csv_rows(args.ledger)]
    csv_output = args.csv_output or args.output.with_suffix(".csv")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    csv_output.parent.mkdir(parents=True, exist_ok=True)
    write_csv(csv_output, rows, PNL_COLUMNS)
    args.output.write_text(render_report(rows, args.ledger, args.settlements, csv_output), encoding="utf-8")
    print(f"ledger_rows={len(rows)}")
    print(f"settled_rows={sum(1 for row in rows if row['status'] == 'settled')}")
    print(f"report={args.output}")


def load_settlements(path: Path) -> dict[tuple[str, str], dict[str, str]]:
    settlements: dict[tuple[str, str], dict[str, str]] = {}
    for row in read_csv_rows(path):
        market_id = row.get("market_id", "")
        outcome = row.get("outcome", "")
        if market_id:
            settlements[(market_id, outcome)] = row
            settlements[(market_id, "")] = row
    return settlements


def pnl_row(row: dict[str, str], settlements: dict[tuple[str, str], dict[str, str]], settings: Settings) -> dict[str, str]:
    market_id = row.get("market_id", "")
    outcome = row.get("outcome", "")
    settlement = settlements.get((market_id, outcome)) or settlements.get((market_id, ""))
    entry_price = parse_optional_float(row.get("execution_price"))
    size = parse_optional_float(row.get("size"))
    side = row.get("side", "")
    final_payout = parse_optional_float(settlement.get("final_payout") if settlement else None)
    if settlement is None or entry_price is None or size is None or side not in {"BUY_YES", "SELL_YES"}:
        realized = None
        fees = None
        status = "missing_settlement_or_trade"
    else:
        notional = entry_price * size
        fees = notional * settings.fee_bps / 10000
        if side == "BUY_YES":
            realized = (final_payout - entry_price) * size - fees
        else:
            realized = (entry_price - final_payout) * size - fees
        status = "settled"
    clv_5m = parse_optional_float(row.get("clv_5m"))
    clv_1h = parse_optional_float(row.get("clv_1h"))
    return {
        "paper_trade_id": row.get("paper_trade_id", ""),
        "market_id": market_id,
        "outcome": outcome,
        "side": side,
        "entry_price": format_optional(entry_price),
        "final_payout": format_optional(final_payout),
        "size": format_optional(size),
        "fees": format_optional(fees),
        "realized_pnl": format_optional(realized),
        "return_on_capital": format_optional(return_on_capital(realized, entry_price, size)),
        "holding_period_seconds": settlement.get("holding_period_seconds", "") if settlement else "",
        "clv_5m": format_optional(clv_5m),
        "clv_1h": format_optional(clv_1h),
        "clv_vs_pnl_same_sign": same_sign_text(clv_5m, realized),
        "status": status,
    }


def render_report(rows: list[dict[str, str]], ledger: Path, settlements: Path, csv_output: Path) -> str:
    settled = [row for row in rows if row["status"] == "settled"]
    pnl_values = [value for row in settled if (value := parse_optional_float(row.get("realized_pnl"))) is not None]
    clv_values = [parse_optional_float(row.get("clv_5m")) for row in settled]
    clv_pnl_pairs = [
        (clv, pnl)
        for clv, pnl in zip(clv_values, pnl_values, strict=False)
        if clv is not None and pnl is not None
    ]
    lines = [
        "# EdgeLab Settlement PnL Report",
        "",
        "Settlement PnL is the final truth for resolved prediction-market paper trades. This report does not place orders or approve live trading.",
        "",
        f"- ledger: `{ledger}`",
        f"- settlements: `{settlements}`",
        f"- csv: `{csv_output}`",
        f"- ledger rows: {len(rows)}",
        f"- settled rows: {len(settled)}",
        f"- total realized PnL: {format_optional(sum(pnl_values) if pnl_values else None)}",
        f"- CLV/PnL correlation: {format_optional(correlation(clv_pnl_pairs))}",
        "",
        "## Interpretation",
        "",
        interpretation(settled),
    ]
    return "\n".join(lines)


def interpretation(settled: list[dict[str, str]]) -> str:
    if not settled:
        return "No settlement PnL exists yet. CLV remains a research metric until resolved outcomes are available."
    return "Resolved trades exist. Compare CLV direction with realized PnL before increasing paper size."


def return_on_capital(realized: float | None, entry_price: float | None, size: float | None) -> float | None:
    if realized is None or entry_price is None or size is None or entry_price * size == 0:
        return None
    return realized / (entry_price * size)


def same_sign_text(clv: float | None, pnl: float | None) -> str:
    if clv is None or pnl is None:
        return ""
    return "true" if (clv >= 0 and pnl >= 0) or (clv < 0 and pnl < 0) else "false"


def correlation(pairs: list[tuple[float, float]]) -> float | None:
    if len(pairs) < 2:
        return None
    xs = [pair[0] for pair in pairs]
    ys = [pair[1] for pair in pairs]
    x_bar = sum(xs) / len(xs)
    y_bar = sum(ys) / len(ys)
    numerator = sum((x - x_bar) * (y - y_bar) for x, y in pairs)
    x_var = sum((x - x_bar) ** 2 for x in xs)
    y_var = sum((y - y_bar) ** 2 for y in ys)
    if x_var == 0 or y_var == 0:
        return None
    return numerator / math.sqrt(x_var * y_var)


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


if __name__ == "__main__":
    main()
