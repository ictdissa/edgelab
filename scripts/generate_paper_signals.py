#!/usr/bin/env python
from __future__ import annotations

import argparse
import csv
import json
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

try:
    import _bootstrap  # noqa: F401
except ModuleNotFoundError:
    from scripts import _bootstrap  # noqa: F401

from edgelab.config.settings import Settings
from edgelab.data.schemas import OrderBook, PredictionMarket
from edgelab.research.signal_lifecycle import (
    FINAL_PAPER_CANDIDATE,
    FINAL_RELATIVE_VALUE_MULTI_LEG,
    FINAL_THEORETICAL_ONLY,
    enrich_signal_row,
    row_spread,
)
from edgelab.strategies.relative_value_graph import mock_relative_value_inputs

PAPER_CANDIDATE_COLUMNS = [
    "candidate_id",
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
    "mid_at_entry",
    "spread_at_entry",
    "liquidity_at_entry",
    "fair_value_source",
    "risk_reason",
    "data_age_seconds",
    "source_snapshot",
    "candidate_status",
]
PAPER_CANDIDATE_COLUMNS += [
    "token_id",
    "event_id",
    "fair_value_timestamp",
    "orderbook_timestamp",
    "uncertainty",
    "match_decision",
    "settlement_verified",
    "fair_value_available_at",
    "orderbook_available_at",
    "freshness_status",
]

REJECTED_DIRECTIONAL_COLUMNS = [
    "market_id",
    "outcome",
    "best_side",
    "best_adjusted_edge",
    "bid",
    "ask",
    "spread",
    "liquidity",
    "risk_reasons",
    "final_decision",
]

RELATIVE_VALUE_CANDIDATE_COLUMNS = [
    "candidate_id",
    "snapshot_timestamp",
    "relative_value_source",
    "empirical_evidence_eligible",
    "relation_type",
    "status",
    "source_status",
    "final_decision",
    "paper_trade_eligible",
    "paper_trade_blocker",
    "theoretical_violation",
    "executable_violation",
    "leg_count",
    "legs_json",
    "min_leg_liquidity",
    "max_trade_size",
    "risk_reasons",
    "sanity_warning_count",
    "package_entry_cost",
    "package_expected_payoff",
    "package_expected_edge",
]

RV_STATUS_THEORETICAL_VIOLATION = "THEORETICAL_VIOLATION"
RV_STATUS_PRICING_INCOMPLETE = "PRICING_INCOMPLETE"
RV_STATUS_INCOMPLETE_PACKAGE = "INCOMPLETE_PACKAGE"
RV_STATUS_REJECTED_BY_LEG_DATA = "REJECTED_BY_LEG_DATA"
RV_STATUS_INSUFFICIENT_LIQUIDITY = "INSUFFICIENT_LIQUIDITY"
RV_STATUS_EXECUTABLE_PACKAGE = "EXECUTABLE_PACKAGE"
RV_STATUS_PAPER_ELIGIBLE = "PAPER_ELIGIBLE"
RV_STATUS_NO_VIOLATION = "NO_VIOLATION"

FULLY_PRICED_RV_STATUSES = {RV_STATUS_EXECUTABLE_PACKAGE, RV_STATUS_PAPER_ELIGIBLE}


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Generate strict paper-trading candidates from snapshots"
    )
    parser.add_argument("--snapshot-dir", type=Path, default=Path("data/snapshots"))
    parser.add_argument("--output", type=Path, default=Path("reports/paper_candidates.csv"))
    parser.add_argument("--markdown-output", type=Path)
    parser.add_argument(
        "--rejected-output", type=Path, default=Path("reports/rejected_directional_edges.csv")
    )
    parser.add_argument(
        "--relative-value-output", type=Path, default=Path("reports/relative_value_candidates.csv")
    )
    args = parser.parse_args()

    settings = Settings()
    snapshot_path = latest_snapshot_signals(args.snapshot_dir)
    rows = read_csv_rows(snapshot_path) if snapshot_path else []
    candidates, rejection_counts, rejected_directional = build_candidates(
        rows, settings, snapshot_path
    )
    relative_value_rows = latest_relative_value_rows(args.snapshot_dir)
    relative_value_candidates = build_relative_value_candidates(relative_value_rows)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    write_csv(args.output, candidates, PAPER_CANDIDATE_COLUMNS)
    write_csv(args.rejected_output, rejected_directional, REJECTED_DIRECTIONAL_COLUMNS)
    write_csv(
        args.relative_value_output, relative_value_candidates, RELATIVE_VALUE_CANDIDATE_COLUMNS
    )
    markdown_path = args.markdown_output or args.output.with_suffix(".md")
    markdown_path.write_text(
        render_report(
            candidates,
            rejection_counts,
            rejected_directional,
            relative_value_candidates,
            snapshot_path,
            settings,
        ),
        encoding="utf-8",
    )
    print(f"paper_candidates={len(candidates)}")
    print(f"rejected_directional_edges={len(rejected_directional)}")
    print(f"relative_value_candidates={len(relative_value_candidates)}")
    print(f"source_snapshot={snapshot_path or ''}")
    print(f"csv={args.output}")
    print(f"report={markdown_path}")


def latest_snapshot_signals(snapshot_dir: Path) -> Path | None:
    candidates = sorted(snapshot_dir.glob("iteration_*/signals.csv"))
    return candidates[-1] if candidates else None


def latest_relative_value_rows(snapshot_dir: Path) -> list[dict[str, str]]:
    paths = sorted(snapshot_dir.glob("iteration_*/relative_value_opportunities.csv"))
    if paths:
        return read_csv_rows(paths[-1])
    fallback = Path("reports/relative_value_opportunities.csv")
    if not fallback.exists():
        return []
    return mark_relative_value_source(read_csv_rows(fallback), "static_report")


def build_candidates(
    rows: list[dict[str, str]],
    settings: Settings,
    snapshot_path: Path | None,
) -> tuple[list[dict[str, str]], Counter[str], list[dict[str, str]]]:
    output: list[dict[str, str]] = []
    rejected_directional: list[dict[str, str]] = []
    rejection_counts: Counter[str] = Counter()
    for index, row in enumerate(rows, start=1):
        row = enrich_signal_row(row, settings)
        if row.get("final_decision") in {FINAL_RELATIVE_VALUE_MULTI_LEG, FINAL_THEORETICAL_ONLY}:
            continue
        accepted, reason = candidate_rejection_reason(row, settings)
        if not accepted:
            rejection_counts[reason] += 1
            if is_rejected_directional_edge(row, settings):
                rejected_directional.append(rejected_directional_row(row))
            continue
        side = row.get("best_side", "")
        bid = parse_float(row.get("bid") or row.get("prediction_bid"))
        ask = parse_float(row.get("ask") or row.get("prediction_ask"))
        liquidity = parse_float(row.get("liquidity") or row.get("available_size"))
        execution_price = ask if side == "BUY_YES" else bid
        source = "ASK" if side == "BUY_YES" else "BID"
        output.append(
            {
                "candidate_id": f"paper:{index}:{row.get('market_id', '')}:{side}",
                "timestamp": row.get("timestamp", ""),
                "category": row.get("category", ""),
                "market_id": row.get("market_id", ""),
                "outcome": row.get("outcome", ""),
                "side": side,
                "execution_price": format_float(execution_price),
                "execution_price_source": source,
                "size": format_float(min(settings.max_trade_size, liquidity)),
                "fair_probability_at_entry": row.get("fair_probability", ""),
                "adjusted_edge_at_entry": row.get("best_adjusted_edge")
                or row.get("adjusted_edge", ""),
                "bid_at_entry": format_float(bid),
                "ask_at_entry": format_float(ask),
                "mid_at_entry": row.get("mid", ""),
                "spread_at_entry": row.get("spread", ""),
                "liquidity_at_entry": format_float(liquidity),
                "fair_value_source": row.get("fair_value_source", ""),
                "risk_reason": row.get("risk_reasons") or row.get("rejection_reason", ""),
                "data_age_seconds": row.get("data_age_seconds", ""),
                "source_snapshot": str(snapshot_path or ""),
                "candidate_status": "PAPER_CANDIDATE",
                **{
                    key: row.get(key, "")
                    for key in (
                        "token_id",
                        "event_id",
                        "fair_value_timestamp",
                        "orderbook_timestamp",
                        "uncertainty",
                        "match_decision",
                        "settlement_verified",
                        "fair_value_available_at",
                        "orderbook_available_at",
                        "freshness_status",
                    )
                },
            }
        )
    return output, rejection_counts, rejected_directional


def candidate_rejection_reason(row: dict[str, str], settings: Settings) -> tuple[bool, str]:
    side = row.get("best_side", "")
    if side not in {"BUY_YES", "SELL_YES"}:
        return False, "no BUY_YES or SELL_YES best side"
    if row.get("edge_passes_threshold") != "true":
        return False, "edge does not pass threshold"
    if row.get("risk_decision") != "APPROVED":
        return False, "risk_approved is false"
    adjusted_edge = parse_float(row.get("best_adjusted_edge") or row.get("adjusted_edge"))
    if adjusted_edge < settings.min_adjusted_edge:
        return False, "adjusted edge below threshold"
    liquidity = parse_float(row.get("liquidity") or row.get("available_size"))
    if liquidity < settings.min_liquidity:
        return False, "liquidity below threshold"
    spread = parse_float(row.get("spread"))
    if spread <= 0 or spread > settings.max_spread:
        return False, "spread missing or above threshold"
    age = parse_optional_float(row.get("data_age_seconds"))
    if age is None or age > settings.max_data_age_seconds:
        return False, "data is not fresh"
    bid = parse_optional_float(row.get("bid") or row.get("prediction_bid"))
    ask = parse_optional_float(row.get("ask") or row.get("prediction_ask"))
    if side == "BUY_YES" and ask is None:
        return False, "BUY_YES missing executable ask"
    if side == "SELL_YES" and bid is None:
        return False, "SELL_YES missing executable bid"
    if row.get("final_decision") != FINAL_PAPER_CANDIDATE:
        return False, row.get("final_decision") or "not PAPER_CANDIDATE"
    return True, ""


def is_rejected_directional_edge(row: dict[str, str], settings: Settings) -> bool:
    best_edge = parse_optional_float(row.get("best_adjusted_edge"))
    return (
        row.get("best_side") in {"BUY_YES", "SELL_YES"}
        and best_edge is not None
        and best_edge >= settings.min_adjusted_edge
    )


def rejected_directional_row(row: dict[str, str]) -> dict[str, str]:
    return {
        "market_id": row.get("market_id", ""),
        "outcome": row.get("outcome", ""),
        "best_side": row.get("best_side", ""),
        "best_adjusted_edge": row.get("best_adjusted_edge", ""),
        "bid": row.get("bid") or row.get("prediction_bid") or "",
        "ask": row.get("ask") or row.get("prediction_ask") or "",
        "spread": row.get("spread") or format_float(row_spread(row)),
        "liquidity": row.get("liquidity") or row.get("available_size") or "",
        "risk_reasons": row.get("risk_reasons") or row.get("rejection_reason") or "",
        "final_decision": row.get("final_decision", ""),
    }


def build_relative_value_candidates(
    rows: list[dict[str, str]],
    *,
    snapshot_timestamp: str = "",
    markets: list[PredictionMarket] | None = None,
    orderbooks: list[OrderBook] | None = None,
    settings: Settings | None = None,
) -> list[dict[str, str]]:
    settings = settings or Settings()
    explicit_pricing_context = markets is not None and orderbooks is not None
    if markets is None or orderbooks is None:
        markets, orderbooks = load_relative_value_pricing_context()
    market_lookup = {market.market_id: market for market in markets}
    bba_lookup = {(book.market_id, book.token_id): book.best_bid_ask() for book in orderbooks}
    output = []
    for index, row in enumerate(rows, start=1):
        timestamp = (
            snapshot_timestamp or row.get("snapshot_timestamp", "") or row.get("timestamp", "")
        )
        source = relative_value_source(row, explicit_pricing_context)
        legs = relative_value_legs(row, market_lookup, bba_lookup, settings, timestamp)
        status, blocker = relative_value_package_status(row, legs, settings)
        source_eligible = source == "live_snapshot"
        if not source_eligible and status == RV_STATUS_PAPER_ELIGIBLE:
            status = RV_STATUS_EXECUTABLE_PACKAGE
            blocker = f"relative_value_source={source} is research-only; requires live_snapshot"
        paper_eligible = status == RV_STATUS_PAPER_ELIGIBLE
        min_liquidity = min_leg_liquidity(legs)
        max_trade_size = min(settings.max_trade_size, min_liquidity) if min_liquidity > 0 else 0.0
        package_entry_cost = (
            unit_package_entry_cost(legs) if package_price_available(legs) else None
        )
        package_expected_edge = parse_optional_float(row.get("executable_violation")) or 0.0
        package_expected_payoff = (
            package_entry_cost + package_expected_edge if package_entry_cost is not None else None
        )
        output.append(
            {
                "candidate_id": f"rv_candidate:{index}:{row.get('relation_type', '')}",
                "snapshot_timestamp": timestamp,
                "relative_value_source": source,
                "empirical_evidence_eligible": format_bool(source_eligible),
                "relation_type": row.get("relation_type", ""),
                "source_status": row.get("status", ""),
                "status": status,
                "final_decision": FINAL_RELATIVE_VALUE_MULTI_LEG
                if status in FULLY_PRICED_RV_STATUSES
                else "REJECTED_BY_LEG_DATA"
                if status
                in {
                    RV_STATUS_PRICING_INCOMPLETE,
                    RV_STATUS_INCOMPLETE_PACKAGE,
                    RV_STATUS_REJECTED_BY_LEG_DATA,
                }
                else FINAL_THEORETICAL_ONLY
                if status == RV_STATUS_THEORETICAL_VIOLATION
                else "NO_VIOLATION",
                "paper_trade_eligible": format_bool(paper_eligible),
                "paper_trade_blocker": blocker,
                "theoretical_violation": row.get("theoretical_violation", ""),
                "executable_violation": row.get("executable_violation", ""),
                "leg_count": str(len(legs)),
                "legs_json": json.dumps(legs, sort_keys=True),
                "min_leg_liquidity": format_float(min_liquidity),
                "max_trade_size": format_float(max_trade_size),
                "risk_reasons": "" if paper_eligible else blocker,
                "sanity_warning_count": str(sum(len(parse_leg_warnings(leg)) for leg in legs)),
                "package_entry_cost": format_float(package_entry_cost),
                "package_expected_payoff": format_float(package_expected_payoff),
                "package_expected_edge": format_float(package_expected_edge),
            }
        )
    return output


def relative_value_source(row: dict[str, str], explicit_pricing_context: bool = False) -> str:
    source = row.get("relative_value_source") or row.get("source") or ""
    if source:
        return source
    return "live_snapshot" if explicit_pricing_context else "unknown"


def mark_relative_value_source(rows: list[dict[str, str]], source: str) -> list[dict[str, str]]:
    marked_rows = []
    for row in rows:
        marked = dict(row)
        marked["relative_value_source"] = marked.get("relative_value_source") or source
        marked_rows.append(marked)
    return marked_rows


def is_empirical_relative_value_candidate(row: dict[str, str]) -> bool:
    return row.get("relative_value_source") == "live_snapshot"


def is_empirical_fully_priced_relative_value_candidate(row: dict[str, str]) -> bool:
    return (
        is_empirical_relative_value_candidate(row) and row.get("status") in FULLY_PRICED_RV_STATUSES
    )


def relative_value_legs(
    row: dict[str, str],
    market_lookup: dict[str, PredictionMarket] | None = None,
    bba_lookup: dict[tuple[str, str], Any] | None = None,
    settings: Settings | None = None,
    snapshot_timestamp: str = "",
) -> list[dict[str, str]]:
    market_lookup = market_lookup or {}
    bba_lookup = bba_lookup or {}
    settings = settings or Settings()
    snapshot_time = parse_time(snapshot_timestamp)
    legs = []
    required = row.get("required_legs", "")
    involved = [
        part.strip() for part in row.get("involved_outcomes", "").split("|") if part.strip()
    ]
    for item in involved:
        market_id, _, outcome = item.partition(":")
        if not market_id:
            continue
        side = infer_relative_value_side(item, market_id, required)
        price_source = "BID" if side.startswith("SELL") else "ASK"
        market = market_lookup.get(market_id)
        token_id = token_id_for_outcome(market, outcome or "Yes")
        bba = bba_lookup.get((market_id, token_id)) if token_id else None
        bid = bba.best_bid if bba is not None else None
        ask = bba.best_ask if bba is not None else None
        mid = bba.mid if bba is not None else None
        spread = bba.spread if bba is not None else None
        entry_price = ask if price_source == "ASK" else bid
        size_available = (
            bba.best_ask_size
            if price_source == "ASK" and bba is not None
            else (bba.best_bid_size if bba is not None else None)
        )
        age = data_age_seconds(snapshot_time, bba.timestamp if bba is not None else None)
        freshness = freshness_status(age, settings)
        warnings = leg_sanity_warnings(
            token_id=token_id,
            bba_present=bba is not None,
            bid=bid,
            ask=ask,
            spread=spread,
            entry_price=entry_price,
            size_available=size_available,
            age_seconds=age,
            settings=settings,
        )
        executable = leg_is_executable(entry_price, size_available, spread, settings, warnings)
        legs.append(
            {
                "market_id": market_id,
                "token_id": token_id,
                "outcome": outcome or "Yes",
                "side": side,
                "bid": format_float(bid),
                "ask": format_float(ask),
                "mid": format_float(mid),
                "spread": format_float(spread),
                "size_available": format_float(size_available),
                "price_source_for_entry": price_source,
                "entry_price": format_float(entry_price),
                "data_age_seconds": format_float(age),
                "freshness_status": freshness,
                "sanity_warnings": json.dumps(warnings, sort_keys=True),
                "leg_is_executable": format_bool(executable),
                "leg_rejection_reason": "; ".join(
                    warning["message"] for warning in warnings if warning["blocks_paper"]
                ),
                "price_source": price_source,
                "price": format_float(entry_price),
            }
        )
    return legs


def relative_value_package_status(
    row: dict[str, str],
    legs: list[dict[str, str]],
    settings: Settings,
) -> tuple[str, str]:
    source_status = row.get("status", "")
    theoretical = parse_float(row.get("theoretical_violation"))
    executable = parse_float(row.get("executable_violation"))
    if (
        source_status in {"NO_VIOLATION", RV_STATUS_NO_VIOLATION}
        and theoretical <= 0
        and executable <= 0
    ):
        return RV_STATUS_NO_VIOLATION, "NO_VIOLATION"
    if not legs:
        return RV_STATUS_INCOMPLETE_PACKAGE, "missing relative-value legs"
    if any(not leg.get("token_id") for leg in legs):
        return RV_STATUS_INCOMPLETE_PACKAGE, "missing token_id"
    if not all_legs_have_books(legs):
        return RV_STATUS_PRICING_INCOMPLETE, "missing orderbook"
    if not package_price_available(legs):
        return RV_STATUS_PRICING_INCOMPLETE, "missing executable bid/ask"
    if min_leg_liquidity(legs) < settings.min_liquidity:
        return RV_STATUS_INSUFFICIENT_LIQUIDITY, "minimum leg liquidity below threshold"
    if any(
        parse_optional_float(leg.get("spread")) is None
        or parse_float(leg.get("spread")) > settings.max_spread
        for leg in legs
    ):
        return RV_STATUS_REJECTED_BY_LEG_DATA, "leg spread above threshold"
    blocking = blocking_leg_warnings(legs)
    if blocking:
        return RV_STATUS_EXECUTABLE_PACKAGE, "; ".join(blocking)
    if executable > 0 or source_status in {
        "EXECUTABLE",
        RV_STATUS_EXECUTABLE_PACKAGE,
        RV_STATUS_PAPER_ELIGIBLE,
    }:
        return RV_STATUS_PAPER_ELIGIBLE, ""
    if theoretical > 0:
        return (
            RV_STATUS_THEORETICAL_VIOLATION,
            "theoretical violation without executable package edge",
        )
    return RV_STATUS_NO_VIOLATION, "NO_VIOLATION"


def load_relative_value_pricing_context(
    live_data_dir: Path = Path("data/live"),
) -> tuple[list[PredictionMarket], list[OrderBook]]:
    markets: list[PredictionMarket] = []
    orderbooks: list[OrderBook] = []
    if live_data_dir.exists():
        for path in sorted(live_data_dir.glob("*_markets.json")):
            markets.extend(load_models(path, PredictionMarket, "markets"))
        for path in sorted(live_data_dir.glob("*_orderbooks.json")):
            orderbooks.extend(load_models(path, OrderBook, "orderbooks"))
    mock_markets, mock_orderbooks = mock_relative_value_inputs()
    markets.extend(mock_markets)
    orderbooks.extend(mock_orderbooks)
    return dedupe_markets(markets), dedupe_orderbooks(orderbooks)


def load_models(
    path: Path, model_type: type[PredictionMarket] | type[OrderBook], key: str
) -> list[Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    rows = payload.get(key, payload) if isinstance(payload, dict) else payload
    if not isinstance(rows, list):
        return []
    output = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        try:
            output.append(model_type(**row))
        except ValueError:
            continue
    return output


def dedupe_markets(markets: list[PredictionMarket]) -> list[PredictionMarket]:
    by_id = {market.market_id: market for market in markets}
    return list(by_id.values())


def dedupe_orderbooks(orderbooks: list[OrderBook]) -> list[OrderBook]:
    by_key: dict[tuple[str, str], OrderBook] = {}
    for book in orderbooks:
        key = (book.market_id, book.token_id)
        existing = by_key.get(key)
        if existing is None or book.timestamp >= existing.timestamp:
            by_key[key] = book
    return list(by_key.values())


def infer_relative_value_side(item: str, market_id: str, required: str) -> str:
    normalized = required.upper()
    item_upper = item.upper()
    market_upper = market_id.upper()
    if f"BUY {item_upper}" in normalized or f"BUY {market_upper}" in normalized:
        return "BUY_YES"
    if f"SELL {item_upper}" in normalized or f"SELL {market_upper}" in normalized:
        return "SELL_YES"
    if normalized.startswith("SELL") or " SELL " in normalized:
        return "SELL_YES"
    if normalized.startswith("BUY") or " BUY " in normalized:
        return "BUY_YES"
    return "BUY_YES"


def token_id_for_outcome(market: PredictionMarket | None, outcome: str) -> str:
    if market is None:
        return ""
    for market_outcome, token_id in zip(market.outcomes, market.token_ids, strict=False):
        if market_outcome.lower() == outcome.lower():
            return token_id
    if outcome.lower() == "yes" and market.token_ids:
        return market.token_ids[0]
    return ""


def leg_sanity_warnings(
    *,
    token_id: str,
    bba_present: bool,
    bid: float | None,
    ask: float | None,
    spread: float | None,
    entry_price: float | None,
    size_available: float | None,
    age_seconds: float | None,
    settings: Settings,
) -> list[dict[str, object]]:
    warnings: list[dict[str, object]] = []
    if not token_id:
        warnings.append(_leg_warning("MISSING_TOKEN_ID", "missing token_id", True))
    if not bba_present:
        warnings.append(_leg_warning("MISSING_BID_ASK", "missing orderbook", True))
    if bid is None or ask is None:
        warnings.append(_leg_warning("MISSING_BID_ASK", "missing bid or ask", True))
    if bid is not None and ask is not None and bid > ask:
        warnings.append(_leg_warning("CROSSED_BOOK", "bid > ask", True))
    if bid is not None and not 0 <= bid <= 1:
        warnings.append(_leg_warning("BID_OUT_OF_RANGE", "bid outside [0, 1]", True))
    if ask is not None and not 0 <= ask <= 1:
        warnings.append(_leg_warning("ASK_OUT_OF_RANGE", "ask outside [0, 1]", True))
    if age_seconds is None or age_seconds > settings.max_data_age_seconds:
        warnings.append(_leg_warning("STALE_FAIR_VALUE", "stale leg quote", True))
    if spread is not None and spread > settings.max_spread:
        warnings.append(_leg_warning("WIDE_SPREAD", "leg spread above configured max", True))
    elif spread is not None and spread > 0.25:
        warnings.append(_leg_warning("WIDE_SPREAD", "wide spread diagnostic", False))
    if (
        size_available is not None
        and size_available < settings.min_liquidity
        and size_available > 0
    ):
        warnings.append(_leg_warning("LOW_LIQUIDITY_DIAGNOSTIC", "low liquidity diagnostic", False))
    if entry_price is None:
        warnings.append(_leg_warning("MISSING_BID_ASK", "missing entry price", True))
    return dedupe_warnings(warnings)


def _leg_warning(code: str, message: str, blocks_paper: bool) -> dict[str, object]:
    return {"code": code, "message": message, "blocks_paper": blocks_paper}


def dedupe_warnings(warnings: list[dict[str, object]]) -> list[dict[str, object]]:
    seen = set()
    output = []
    for warning in warnings:
        key = (warning["code"], warning["message"])
        if key in seen:
            continue
        seen.add(key)
        output.append(warning)
    return output


def leg_is_executable(
    entry_price: float | None,
    size_available: float | None,
    spread: float | None,
    settings: Settings,
    warnings: list[dict[str, object]],
) -> bool:
    if entry_price is None or not 0 <= entry_price <= 1:
        return False
    if size_available is None or size_available < settings.min_liquidity:
        return False
    if spread is None or spread > settings.max_spread:
        return False
    fatal_codes = {
        "MISSING_TOKEN_ID",
        "MISSING_BID_ASK",
        "CROSSED_BOOK",
        "BID_OUT_OF_RANGE",
        "ASK_OUT_OF_RANGE",
    }
    return not any(
        warning["code"] in fatal_codes and warning["blocks_paper"] for warning in warnings
    )


def all_legs_have_books(legs: list[dict[str, str]]) -> bool:
    return all(leg.get("bid") != "" or leg.get("ask") != "" for leg in legs)


def package_price_available(legs: list[dict[str, str]]) -> bool:
    return bool(legs) and all(
        parse_optional_float(leg.get("entry_price")) is not None for leg in legs
    )


def min_leg_liquidity(legs: list[dict[str, str]]) -> float:
    values = [
        parse_float(leg.get("size_available"))
        for leg in legs
        if parse_float(leg.get("size_available")) > 0
    ]
    return min(values) if values else 0.0


def unit_package_entry_cost(legs: list[dict[str, str]]) -> float:
    total = 0.0
    for leg in legs:
        price = parse_float(leg.get("entry_price") or leg.get("price"))
        if leg.get("side", "").startswith("BUY"):
            total += price
        elif leg.get("side", "").startswith("SELL"):
            total -= price
    return total


def blocking_leg_warnings(legs: list[dict[str, str]]) -> list[str]:
    reasons: list[str] = []
    for leg in legs:
        for warning in parse_leg_warnings(leg):
            if warning.get("blocks_paper"):
                reasons.append(f"{leg.get('market_id')}:{warning.get('message')}")
    return reasons


def parse_leg_warnings(leg: dict[str, str]) -> list[dict[str, object]]:
    value = leg.get("sanity_warnings", "")
    if not value:
        return []
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        return []
    if not isinstance(parsed, list):
        return []
    return [item for item in parsed if isinstance(item, dict)]


def data_age_seconds(snapshot_time: datetime | None, quote_time: datetime | None) -> float | None:
    if snapshot_time is None or quote_time is None:
        return None
    return max(0.0, (snapshot_time - quote_time).total_seconds())


def freshness_status(age_seconds: float | None, settings: Settings) -> str:
    if age_seconds is None:
        return "unknown"
    return "fresh" if age_seconds <= settings.max_data_age_seconds else "stale"


def parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.astimezone(UTC) if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def render_report(
    candidates: list[dict[str, str]],
    rejection_counts: Counter[str],
    rejected_directional: list[dict[str, str]],
    relative_value_candidates: list[dict[str, str]],
    snapshot_path: Path | None,
    settings: Settings,
) -> str:
    lines = [
        "# EdgeLab Paper Candidates",
        "",
        f"- source snapshot: `{snapshot_path or ''}`",
        f"- candidates: {len(candidates)}",
        f"- rejected directional edges: {len(rejected_directional)}",
        f"- relative-value multi-leg candidates: {len(relative_value_candidates)}",
        f"- min adjusted edge: `{settings.min_adjusted_edge}`",
        f"- min liquidity: `{settings.min_liquidity}`",
        f"- max spread: `{settings.max_spread}`",
        "",
    ]
    if candidates:
        lines.extend(["## Candidates", ""])
        lines.extend(
            markdown_table(
                ["market", "side", "price_source", "price", "edge", "liquidity", "spread"],
                [
                    [
                        row["market_id"],
                        row["side"],
                        row["execution_price_source"],
                        row["execution_price"],
                        row["adjusted_edge_at_entry"],
                        row["liquidity_at_entry"],
                        row["spread_at_entry"],
                    ]
                    for row in candidates
                ],
            )
        )
    else:
        lines.extend(
            [
                "No paper candidates survived the configured filters.",
                "",
                "This is expected when signals are research-only, risk-rejected, stale, too wide, "
                "too illiquid, or below the adjusted-edge threshold.",
            ]
        )
    lines.extend(["", "## Rejection Reasons", ""])
    if rejection_counts:
        lines.extend(
            markdown_table(
                ["reason", "count"],
                [[reason, str(count)] for reason, count in rejection_counts.items()],
            )
        )
    else:
        lines.append("No rejected rows were available.")
    lines.extend(["", "## Rejected Directional Edges", ""])
    if rejected_directional:
        lines.extend(
            markdown_table(
                ["market", "outcome", "side", "edge", "risk", "final"],
                [
                    [
                        row["market_id"],
                        row["outcome"],
                        row["best_side"],
                        row["best_adjusted_edge"],
                        row["risk_reasons"],
                        row["final_decision"],
                    ]
                    for row in rejected_directional[:20]
                ],
            )
        )
    else:
        lines.append("No rejected directional edges were found.")
    return "\n".join(lines)


def read_csv_rows(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def write_csv(path: Path, rows: list[dict[str, str]], columns: list[str]) -> None:
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns)
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


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


def format_float(value: float | None) -> str:
    return "" if value is None else f"{value:.10g}"


def format_bool(value: bool) -> str:
    return "true" if value else "false"


def markdown_table(headers: list[str], rows: list[list[str]]) -> list[str]:
    output = ["| " + " | ".join(headers) + " |", "| " + " | ".join("---" for _ in headers) + " |"]
    for row in rows:
        output.append("| " + " | ".join(str(cell).replace("|", "\\|") for cell in row) + " |")
    return output


if __name__ == "__main__":
    main()
