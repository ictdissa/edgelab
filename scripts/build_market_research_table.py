#!/usr/bin/env python
from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path
from typing import Any

try:
    import _bootstrap  # noqa: F401
except ModuleNotFoundError:
    from scripts import _bootstrap  # noqa: F401

from edgelab.data.schemas import PredictionMarket

RESEARCH_COLUMNS = [
    "timestamp",
    "market_id",
    "condition_id",
    "token_id",
    "title",
    "category",
    "expiry",
    "outcome",
    "best_bid",
    "best_ask",
    "bid_size",
    "ask_size",
    "mid",
    "spread",
    "external_anchor",
    "fair_value",
    "fair_value_source",
    "fair_value_age",
    "edge_buy",
    "edge_sell",
    "decision",
    "rejection_reason",
    "config_version",
]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build normalized market research table from snapshots")
    parser.add_argument("--snapshot-dir", type=Path, default=Path("data/snapshots"))
    parser.add_argument("--live-data-dir", type=Path, default=Path("data/live"))
    parser.add_argument("--output", type=Path, default=Path("data/processed/market_research_table.csv"))
    parser.add_argument("--config-version", default="default")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    rows = build_research_rows(args.snapshot_dir, args.live_data_dir, args.config_version)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    write_csv(args.output, rows, RESEARCH_COLUMNS)
    print(f"research_rows={len(rows)}")
    print(f"output={args.output}")


def build_research_rows(snapshot_dir: Path, live_data_dir: Path, config_version: str) -> list[dict[str, str]]:
    markets = load_market_lookup(live_data_dir)
    rows = []
    for path in snapshot_signal_paths(snapshot_dir):
        for signal in read_csv_rows(path):
            rows.append(research_row(signal, markets.get(signal.get("market_id", "")), config_version))
    return rows


def research_row(signal: dict[str, str], market: PredictionMarket | None, config_version: str) -> dict[str, str]:
    outcome = signal.get("outcome", "")
    bid = signal.get("bid") or signal.get("prediction_bid") or ""
    ask = signal.get("ask") or signal.get("prediction_ask") or ""
    title = market.title if market else signal.get("title", "")
    token_id = token_id_for_outcome(market, outcome)
    condition_id = ""
    if market and isinstance(market.raw_metadata, dict):
        condition_id = str(market.raw_metadata.get("conditionId") or market.raw_metadata.get("condition_id") or "")
    return {
        "timestamp": signal.get("timestamp", ""),
        "market_id": signal.get("market_id", ""),
        "condition_id": condition_id,
        "token_id": token_id,
        "title": title,
        "category": signal.get("category") or (market.category if market else ""),
        "expiry": "" if market is None or market.end_time is None else market.end_time.isoformat(),
        "outcome": outcome,
        "best_bid": bid,
        "best_ask": ask,
        "bid_size": signal.get("bid_size") or "",
        "ask_size": signal.get("ask_size") or "",
        "mid": signal.get("mid", ""),
        "spread": signal.get("spread", ""),
        "external_anchor": signal.get("fair_value_provider", ""),
        "fair_value": signal.get("fair_probability", ""),
        "fair_value_source": signal.get("fair_value_source", ""),
        "fair_value_age": signal.get("data_age_seconds", ""),
        "edge_buy": signal.get("buy_edge_adjusted", ""),
        "edge_sell": signal.get("sell_edge_adjusted", ""),
        "decision": signal.get("final_decision") or signal.get("action", ""),
        "rejection_reason": signal.get("rejection_reason") or signal.get("risk_reasons", ""),
        "config_version": config_version,
    }


def snapshot_signal_paths(snapshot_dir: Path) -> list[Path]:
    index_path = snapshot_dir / "snapshot_index.csv"
    if index_path.exists():
        paths = []
        for row in read_csv_rows(index_path):
            value = row.get("signals_path", "")
            if not value:
                continue
            path = Path(value)
            if not path.is_absolute() and not path.exists():
                path = snapshot_dir / path
            if path.exists():
                paths.append(path)
        if paths:
            return paths
    return sorted(snapshot_dir.glob("iteration_*/signals.csv"))


def load_market_lookup(live_data_dir: Path) -> dict[str, PredictionMarket]:
    markets: dict[str, PredictionMarket] = {}
    if not live_data_dir.exists():
        return markets
    for path in sorted(live_data_dir.glob("*_markets.json")):
        for row in load_json_rows(path, "markets"):
            try:
                market = PredictionMarket(**row)
            except ValueError:
                continue
            markets[market.market_id] = market
    return markets


def load_json_rows(path: Path, key: str) -> list[dict[str, Any]]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    rows = payload.get(key, payload) if isinstance(payload, dict) else payload
    return [row for row in rows if isinstance(row, dict)] if isinstance(rows, list) else []


def token_id_for_outcome(market: PredictionMarket | None, outcome: str) -> str:
    if market is None:
        return ""
    for market_outcome, token_id in zip(market.outcomes, market.token_ids, strict=False):
        if market_outcome.lower() == outcome.lower():
            return token_id
    return ""


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
