from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from _bootstrap import ROOT  # noqa: F401

from edgelab.config.settings import Settings
from edgelab.data.schemas import OrderBook, PredictionMarket
from edgelab.strategies.relative_value_graph import (
    RV_STATUS_EXECUTABLE,
    RV_STATUS_THEORETICAL_ONLY,
    find_relative_value_opportunities,
    mock_relative_value_inputs,
    write_relative_value_outputs,
)


def main() -> None:
    parser = argparse.ArgumentParser(description="Run internal relative-value graph analysis")
    parser.add_argument("--mock", action="store_true", help="Use deterministic local mock markets")
    parser.add_argument("--output", type=Path, default=Path("reports/relative_value_report.md"))
    parser.add_argument("--csv", type=Path, default=Path("reports/relative_value_opportunities.csv"))
    parser.add_argument(
        "--live-data-dir",
        type=Path,
        default=None,
        help="Directory containing already-collected *_markets.json and *_orderbooks.json files",
    )
    args = parser.parse_args()

    settings = Settings()
    if args.mock:
        markets, orderbooks = mock_relative_value_inputs()
    else:
        live_data_dir = args.live_data_dir or settings.live_data_dir
        markets, orderbooks = load_live_inputs(live_data_dir)

    opportunities = find_relative_value_opportunities(markets, orderbooks, settings)
    write_relative_value_outputs(args.output, args.csv, opportunities, markets, orderbooks)

    executable = sum(1 for row in opportunities if row.status == RV_STATUS_EXECUTABLE)
    theoretical = sum(1 for row in opportunities if row.status == RV_STATUS_THEORETICAL_ONLY)
    print(f"relative_value_opportunities={len(opportunities)}")
    print(f"executable={executable}")
    print(f"theoretical_only={theoretical}")
    print(f"report={args.output}")
    print(f"csv={args.csv}")


def load_live_inputs(live_data_dir: Path) -> tuple[list[PredictionMarket], list[OrderBook]]:
    if not live_data_dir.exists():
        raise SystemExit(f"Live data directory does not exist: {live_data_dir}")
    markets: list[PredictionMarket] = []
    orderbooks: list[OrderBook] = []
    for path in sorted(live_data_dir.glob("*_markets.json")):
        markets.extend(_load_models(path, "markets", PredictionMarket))
    for path in sorted(live_data_dir.glob("*_orderbooks.json")):
        orderbooks.extend(_load_models(path, "orderbooks", OrderBook))
    markets = _dedupe_markets(markets)
    orderbooks = _dedupe_orderbooks(orderbooks)
    if not markets:
        raise SystemExit(f"No *_markets.json files with valid markets found in {live_data_dir}")
    if not orderbooks:
        raise SystemExit(f"No *_orderbooks.json files with valid order books found in {live_data_dir}")
    return markets, orderbooks


def _load_models(path: Path, key: str, model_type: type[PredictionMarket] | type[OrderBook]) -> list[Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    rows = payload.get(key, payload) if isinstance(payload, dict) else payload
    if not isinstance(rows, list):
        return []
    models = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        try:
            models.append(model_type(**row))
        except ValueError:
            continue
    return models


def _dedupe_markets(markets: list[PredictionMarket]) -> list[PredictionMarket]:
    by_id = {market.market_id: market for market in markets}
    return list(by_id.values())


def _dedupe_orderbooks(orderbooks: list[OrderBook]) -> list[OrderBook]:
    by_key: dict[tuple[str, str], OrderBook] = {}
    for book in orderbooks:
        key = (book.market_id, book.token_id)
        existing = by_key.get(key)
        if existing is None or book.timestamp >= existing.timestamp:
            by_key[key] = book
    return list(by_key.values())


if __name__ == "__main__":
    main()
