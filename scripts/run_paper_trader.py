#!/usr/bin/env python
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import logging
import math
import os
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import get_args

try:
    import _bootstrap  # noqa: F401
except ModuleNotFoundError:
    from scripts import _bootstrap  # noqa: F401

from edgelab.config.settings import Settings
from edgelab.data.schemas import PredictionMarket, Signal, FairValue, BestBidAsk
from edgelab.execution.paper_trader import PaperTrader
from edgelab.research.pipeline import run_mock_paper_trader
from edgelab.strategies.taker_edge import compute_edge_signal
from edgelab.research.ledger_io import ledger_lock

LOGGER = logging.getLogger(__name__)

LEDGER_COLUMNS = [
    "paper_trade_id",
    "timestamp",
    "category",
    "market_id",
    "outcome",
    "side",
    "execution_price",
    "execution_price_source",
    "size",
    "fair_probability_at_entry",
    "adjusted_edge_at_entry",
    "bid_at_entry",
    "ask_at_entry",
    "spread_at_entry",
    "liquidity_at_entry",
    "fair_value_source",
    "risk_reason",
    "clv_5m",
    "clv_30m",
    "clv_1h",
    "clv_status",
]
LEDGER_COLUMNS += [
    "token_id",
    "event_id",
    "risk_event_id",
    "fees",
    "slippage",
    "cash_change",
    "realized_pnl",
    "fair_value_timestamp",
    "orderbook_timestamp",
    "net_entry_price",
    "fee_bps_at_entry",
    "slippage_bps_at_entry",
]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run EdgeLab paper trader")
    parser.add_argument("--mock", action="store_true")
    parser.add_argument(
        "--signals",
        "--input",
        dest="signals",
        type=Path,
        default=Path("reports/paper_candidates.csv"),
    )
    parser.add_argument("--ledger", type=Path)
    return parser.parse_args()


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
    args = parse_args()
    args.ledger = args.ledger or Path(
        "data/paper_trades/mock_ledger.csv" if args.mock else "data/paper_trades/ledger.csv"
    )
    if args.mock:
        trades = run_mock_paper_trader(args.ledger, Settings())
        entered = sum(1 for trade in trades if trade.approved)
        skipped = len(trades) - entered
        print(f"Paper trades entered: {entered}; skipped: {skipped}")
        print(f"Wrote {args.ledger}")
        return

    rows = read_csv_rows(args.signals)
    if rows and "candidate_id" not in rows[0]:
        signals = read_signals(args.signals)
        markets = load_live_markets()
        trader = persistent_trader(args.ledger, Settings())
        trades = trader.execute(signals, markets)
        ensure_ledger_schema(args.ledger)
        sync_portfolio_ledger(args.ledger, trader)
        entered = sum(1 for trade in trades if trade.approved)
        skipped = len(trades) - entered
        print(f"Paper trades entered: {entered}; skipped: {skipped}")
        print(f"Wrote {args.ledger}")
        print_portfolio_summary(trader)
        return

    trader = persistent_trader(args.ledger, Settings())
    ledger_rows = build_ledger_rows(rows, Settings(), trader=trader)
    ensure_ledger_schema(args.ledger)
    sync_portfolio_ledger(args.ledger, trader, ledger_rows)
    entered = sum(1 for row in ledger_rows if row["clv_status"] == "pending")
    skipped = len(ledger_rows) - entered
    print(f"Paper trades entered: {entered}; skipped: {skipped}")
    print(f"Wrote {args.ledger}")
    print_portfolio_summary(trader)


def print_portfolio_summary(trader: PaperTrader) -> None:
    markets, events, total = trader.portfolio.exposures()
    print(
        f"Paper cash: {trader.portfolio.cash:.2f}; unresolved shares: {total:.2f}; markets: {len(markets)}; events: {len(events)}"
    )
    print(
        f"Conservative bid-marked unrealized PnL: {trader.portfolio.unrealized_pnl():.2f}; CLV needs later verified snapshots"
    )


def persistent_trader(ledger: Path, settings: Settings) -> PaperTrader:
    state = ledger.with_suffix(".sqlite")
    if (
        ledger.exists()
        and ledger.stat().st_size > 0
        and not state.exists()
        and read_csv_rows(ledger)
    ):
        raise ValueError(
            "Legacy ledger has no verified portfolio state. Archive it and use a new ledger path; do not silently reset exposure."
        )
    return PaperTrader(settings, portfolio_path=state)


def build_ledger_rows(
    rows: list[dict[str, str]], settings: Settings, trader: PaperTrader | None = None
) -> list[dict[str, str]]:
    output = []
    trader = trader or PaperTrader(settings)
    markets = load_live_markets(settings.live_data_dir)
    for row in rows:
        accepted, reason = validate_candidate(row, settings)
        decision = None
        if accepted:
            market = markets.get(row.get("market_id", ""))
            if market is None:
                accepted, reason = False, "missing verified market metadata"
            elif row.get("event_id") != market.event_id:
                accepted, reason = False, "candidate event identity mismatch"
            else:
                fair = FairValue(
                    event_id=market.event_id,
                    market_id=market.market_id,
                    outcome=row["outcome"],
                    fair_probability=float(row["fair_probability_at_entry"]),
                    source=row["fair_value_source"],
                    timestamp=row["fair_value_timestamp"],
                    available_at=row.get("fair_value_available_at") or None,
                    uncertainty=float(row["uncertainty"]),
                    metadata={
                        "match_decision": row["match_decision"],
                        "settlement_verified": row.get("settlement_verified", "").lower() == "true",
                    },
                )
                book = BestBidAsk(
                    market_id=market.market_id,
                    token_id=row["token_id"],
                    timestamp=row["orderbook_timestamp"],
                    available_at=row.get("orderbook_available_at") or None,
                    best_bid=float(row["bid_at_entry"]),
                    best_ask=float(row["ask_at_entry"]),
                    best_bid_size=float(row["liquidity_at_entry"]),
                    best_ask_size=float(row["liquidity_at_entry"]),
                    spread=float(row["ask_at_entry"]) - float(row["bid_at_entry"]),
                )
                signal = compute_edge_signal(market, fair, book, settings)
                signal.signal_id = paper_trade_id(row, row["timestamp"])
                signal.available_size = min(signal.available_size, float(row["size"]))
                if signal.action != row["side"]:
                    accepted, reason = False, "recomputed edge/action does not support candidate"
                else:
                    decision = trader.execute([signal], markets)[0]
                    accepted, reason = decision.approved, decision.reason
        now = datetime.now(UTC).isoformat()
        side = row.get("side", "")
        source = row.get("execution_price_source", "")
        status = "pending" if accepted else "skipped"
        risk_reason = reason
        output.append(
            {
                "paper_trade_id": paper_trade_id(row, row.get("timestamp") or now),
                "timestamp": now,
                "category": row.get("category", ""),
                "market_id": row.get("market_id", ""),
                "outcome": row.get("outcome", ""),
                "side": side,
                "execution_price": row.get("execution_price", "") if accepted else "",
                "execution_price_source": source if accepted else "NONE",
                "size": row.get("size", "") if accepted else "0",
                "fair_probability_at_entry": row.get("fair_probability_at_entry", ""),
                "adjusted_edge_at_entry": row.get("adjusted_edge_at_entry", ""),
                "bid_at_entry": row.get("bid_at_entry", ""),
                "ask_at_entry": row.get("ask_at_entry", ""),
                "spread_at_entry": row.get("spread_at_entry", ""),
                "liquidity_at_entry": row.get("liquidity_at_entry", ""),
                "fair_value_source": row.get("fair_value_source", ""),
                "risk_reason": risk_reason,
                "clv_5m": "",
                "clv_30m": "",
                "clv_1h": "",
                "clv_status": status,
            }
        )
        output[-1].update(
            {key: row.get(key, "") for key in LEDGER_COLUMNS if key not in output[-1]}
        )
        if decision:
            output[-1].update(
                {
                    key: str(getattr(decision, key) or 0)
                    for key in (
                        "fees",
                        "slippage",
                        "cash_change",
                        "realized_pnl",
                        "net_entry_price",
                        "fee_bps_at_entry",
                        "slippage_bps_at_entry",
                    )
                }
            )
            output[-1]["size"] = str(decision.size)
    return output


def validate_candidate(row: dict[str, str], settings: Settings) -> tuple[bool, str]:
    required = (
        "token_id",
        "event_id",
        "fair_value_timestamp",
        "orderbook_timestamp",
        "uncertainty",
        "timestamp",
    )
    if any(not row.get(key) for key in required):
        return False, "missing contract identity, source timestamps, or uncertainty"
    if row.get("match_decision") != "MATCH" or row.get("freshness_status") != "fresh":
        return False, "unverified mapping or stale data"
    try:
        for key in ("fair_probability_at_entry", "uncertainty", "bid_at_entry", "ask_at_entry"):
            value = float(row[key])
            if not math.isfinite(value) or not 0 <= value <= 1:
                return False, f"invalid probability/quote field: {key}"
        for key in ("adjusted_edge_at_entry", "liquidity_at_entry", "spread_at_entry"):
            if not math.isfinite(float(row[key])):
                return False, f"non-finite candidate field: {key}"
        if not row.get("fair_value_source") or not row.get("outcome"):
            return False, "missing fair-value provenance or outcome"
        size = float(row["size"])
        if not math.isfinite(size) or not 0 < size <= settings.max_trade_size:
            return False, "invalid or excessive trade size"
        for key in ("fair_value_timestamp", "orderbook_timestamp", "timestamp"):
            observed = datetime.fromisoformat(row[key].replace("Z", "+00:00"))
            age = (datetime.now(UTC) - observed).total_seconds()
            if not 0 <= age <= settings.max_data_age_seconds:
                return False, "stale or future candidate data"
        quote = float(row["ask_at_entry"] if row.get("side") == "BUY_YES" else row["bid_at_entry"])
        if float(row["execution_price"]) != quote or not 0 <= quote <= 1:
            return False, "execution price is not the executable quote"
    except (KeyError, ValueError, TypeError):
        return False, "malformed candidate data"
    side = row.get("side", "")
    if side not in {"BUY_YES", "SELL_YES"}:
        return False, "candidate side is not BUY_YES or SELL_YES"
    edge = parse_float(row.get("adjusted_edge_at_entry"))
    if edge < settings.min_adjusted_edge:
        return False, "adjusted edge below threshold"
    liquidity = parse_float(row.get("liquidity_at_entry"))
    if liquidity < settings.min_liquidity:
        return False, "liquidity below threshold"
    spread = parse_float(row.get("spread_at_entry"))
    if spread <= 0 or spread > settings.max_spread:
        return False, "spread missing or above threshold"
    source = row.get("execution_price_source")
    if side == "BUY_YES" and source != "ASK":
        return False, "BUY_YES must execute at ASK"
    if side == "SELL_YES" and source != "BID":
        return False, "SELL_YES must execute at BID"
    if parse_optional_float(row.get("execution_price")) is None:
        return False, "missing executable price"
    return True, ""


def ensure_ledger_schema(path: Path) -> None:
    with ledger_lock(path):
        _ensure_ledger_schema_locked(path)


def _ensure_ledger_schema_locked(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        write_csv(path, [], LEDGER_COLUMNS)
        return
    with path.open(newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        existing_columns = reader.fieldnames or []
        rows = list(reader)
    if existing_columns == LEDGER_COLUMNS:
        return
    migrated = [
        {key: row.get(key, "") for key in LEDGER_COLUMNS}
        if "clv_status" in row
        else migrate_legacy_row(row)
        for row in rows
    ]
    write_csv(path, migrated, LEDGER_COLUMNS)


def migrate_legacy_row(row: dict[str, str]) -> dict[str, str]:
    timestamp = row.get("timestamp", "")
    source = (row.get("execution_price_source") or "none").upper()
    approved = row.get("approved") in {"True", "true", "1"}
    side = row.get("side", "")
    return {
        "paper_trade_id": paper_trade_id(row, timestamp or datetime.now(UTC).isoformat()),
        "timestamp": timestamp,
        "category": row.get("category", ""),
        "market_id": row.get("market_id", ""),
        "outcome": row.get("outcome", ""),
        "side": side,
        "execution_price": row.get("price", ""),
        "execution_price_source": source if source in {"ASK", "BID"} else "NONE",
        "size": row.get("size", ""),
        "fair_probability_at_entry": row.get("fair_probability", ""),
        "adjusted_edge_at_entry": row.get("adjusted_edge", ""),
        "bid_at_entry": row.get("prediction_bid", ""),
        "ask_at_entry": row.get("prediction_ask", ""),
        "spread_at_entry": spread_from(row.get("prediction_bid"), row.get("prediction_ask")),
        "liquidity_at_entry": row.get("available_size", ""),
        "fair_value_source": row.get("fair_value_source", ""),
        "risk_reason": row.get("reason", ""),
        "clv_5m": row.get("clv_5m", ""),
        "clv_30m": row.get("clv_30m", ""),
        "clv_1h": row.get("clv_1h", ""),
        "clv_status": "pending" if approved and side in {"BUY_YES", "SELL_YES"} else "skipped",
    }


def append_ledger_rows(path: Path, rows: list[dict[str, str]]) -> None:
    if not rows:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with ledger_lock(path):
        existing = read_csv_rows(path)
        seen = {r.get("paper_trade_id") for r in existing}
        for row in rows:
            if row["paper_trade_id"] not in seen:
                existing.append(row)
                seen.add(row["paper_trade_id"])
        write_csv(path, existing, LEDGER_COLUMNS)


def sync_portfolio_ledger(
    path: Path,
    trader: PaperTrader,
    supplemental: list[dict[str, str]] | None = None,
) -> None:
    """Rebuild missing exports from committed accounting decisions after a crash.

    Existing follow-ups survive only when entry identity/price/size agree with
    the accounting authority. Pre-validation skips have no cash-flow authority.
    """
    with ledger_lock(path):
        existing = {r["paper_trade_id"]: r for r in read_csv_rows(path)}
        for decision in trader.portfolio.decisions():
            raw = {
                k: "" if v is None else str(v) for k, v in decision.model_dump(mode="json").items()
            }
            row = migrate_legacy_row(raw)
            row["paper_trade_id"] = decision.signal_id
            row.update({k: raw.get(k, "") for k in LEDGER_COLUMNS if k not in row})
            prior = existing.get(decision.signal_id)
            if prior and prior.get("clv_status") != "skipped" and decision.approved:
                identity = (
                    "market_id",
                    "token_id",
                    "outcome",
                    "side",
                    "timestamp",
                    "risk_event_id",
                )
                numbers = ("execution_price", "size", "fees", "slippage")
                if any(prior.get(k, "") != row.get(k, "") for k in identity) or any(
                    parse_optional_float(prior.get(k)) != parse_optional_float(row.get(k))
                    for k in numbers
                ):
                    raise ValueError("ledger entry conflicts with authoritative accounting journal")
                row.update({k: prior.get(k, "") for k in LEDGER_COLUMNS if k.startswith("clv_")})
            existing[decision.signal_id] = row
        for row in supplemental or []:
            existing.setdefault(row["paper_trade_id"], row)
        write_csv(path, list(existing.values()), LEDGER_COLUMNS)


def read_signals(path: Path) -> list[Signal]:
    if not path.exists():
        raise FileNotFoundError(f"Signal file does not exist: {path}")
    with path.open(newline="", encoding="utf-8") as handle:
        rows = [Signal(**_clean_signal_row(row)) for row in csv.DictReader(handle)]
    LOGGER.info("loaded_signals path=%s rows=%d", path, len(rows))
    return rows


def load_live_markets(live_dir: Path = Path("data/live")) -> dict[str, PredictionMarket]:
    markets: dict[str, PredictionMarket] = {}
    for path in sorted(live_dir.glob("*_markets.json")):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(payload, list):
            continue
        for row in payload:
            if not isinstance(row, dict):
                continue
            try:
                market = PredictionMarket(**row)
            except Exception as exc:
                LOGGER.warning("skip_live_market path=%s error=%s", path, exc)
                continue
            markets[market.market_id] = market
    LOGGER.info("loaded_live_markets rows=%d", len(markets))
    return markets


def _clean_signal_row(row: dict[str, str]) -> dict[str, object]:
    clean: dict[str, object] = {}
    fields = Signal.model_fields
    optional_blanks = {
        name for name, field in fields.items() if type(None) in get_args(field.annotation)
    } | {
        "prediction_bid",
        "prediction_ask",
        "fair_value_provider",
        "fair_value_timestamp",
        "orderbook_timestamp",
        "data_age_seconds",
        "match_score",
        "risk_approved",
    }
    for key in fields:
        value = row.get(key)
        if value == "" and key in optional_blanks:
            clean[key] = None
        elif key == "risk_approved" and value is not None:
            clean[key] = value.lower() in {"1", "true", "yes"}
        elif value is not None:
            clean[key] = value
    return clean


def read_csv_rows(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    with path.open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def write_csv(path: Path, rows: list[dict[str, str]], columns: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(dir=path.parent, suffix=".csv.tmp")
    with os.fdopen(fd, "w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns)
        writer.writeheader()
        for row in rows:
            writer.writerow(row)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(name, path)


def paper_trade_id(row: dict[str, str], timestamp: str) -> str:
    basis = "|".join(
        [
            row.get("candidate_id") or row.get("signal_id", ""),
            row.get("market_id", ""),
            row.get("side", ""),
            timestamp,
        ]
    )
    return hashlib.sha256(basis.encode("utf-8")).hexdigest()[:24]


def spread_from(bid: str | None, ask: str | None) -> str:
    bid_value = parse_optional_float(bid)
    ask_value = parse_optional_float(ask)
    if bid_value is None or ask_value is None:
        return ""
    return f"{max(0.0, ask_value - bid_value):.10g}"


def parse_float(value: str | None) -> float:
    parsed = parse_optional_float(value)
    return 0.0 if parsed is None else parsed


def parse_optional_float(value: str | None) -> float | None:
    if value in {None, ""}:
        return None
    try:
        return float(value)
    except ValueError:
        return None


if __name__ == "__main__":
    main()
