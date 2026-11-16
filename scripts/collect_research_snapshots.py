#!/usr/bin/env python
from __future__ import annotations

import argparse
import csv
import json
import logging
import sqlite3
import time
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from statistics import median
from typing import Any, Iterable

try:
    import _bootstrap  # noqa: F401
except ModuleNotFoundError:
    from scripts import _bootstrap  # noqa: F401

from edgelab.config.settings import Settings
from edgelab.data.polymarket_collector import infer_resolution_source_from_text
from edgelab.data.schemas import ExternalAnchor, OrderBook, PredictionMarket, Signal
from edgelab.research.live_analysis import run_live_research
from edgelab.research.live_analysis import (
    add_avoid_signals,
    annotate_signals,
    compute_live_fair_values,
    resolve_events,
    rewrite_anchors_for_matches,
    risk_diagnostics,
)
from edgelab.research.signal_lifecycle import enrich_signal_row
from edgelab.strategies.relative_value_graph import RVOpportunity, find_relative_value_opportunities
from edgelab.strategies.taker_edge import generate_signals

LOGGER = logging.getLogger(__name__)

SNAPSHOT_SIGNAL_COLUMNS = [
    "signal_id",
    "timestamp",
    "category",
    "market_id",
    "event_id",
    "outcome",
    "fair_probability",
    "bid",
    "ask",
    "mid",
    "spread",
    "raw_edge",
    "adjusted_edge",
    "cost_drag",
    "buy_edge_raw",
    "sell_edge_raw",
    "buy_edge_adjusted",
    "sell_edge_adjusted",
    "best_side",
    "best_adjusted_edge",
    "edge_passes_threshold",
    "action",
    "signal_action",
    "risk_decision",
    "risk_approved",
    "risk_reasons",
    "final_decision",
    "rejection_reason",
    "liquidity",
    "fair_value_source",
    "fair_value_provider",
    "data_age_seconds",
    "freshness_status",
    "match_score",
    "source",
]
SNAPSHOT_SIGNAL_COLUMNS += [
    "token_id",
    "fair_value_timestamp",
    "orderbook_timestamp",
    "uncertainty",
    "match_decision",
    "settlement_verified",
    "fair_value_available_at",
    "orderbook_available_at",
    "book_bid_size",
    "book_ask_size",
    "market_status",
    "resolution_source",
    "market_end_time",
]

SNAPSHOT_FRESHNESS_COLUMNS = [
    "category",
    "source",
    "rows",
    "latest_timestamp",
    "age_seconds",
    "status",
    "reason",
]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Collect repeated EdgeLab research snapshots")
    parser.add_argument("--category", choices=["sports", "crypto", "weather"])
    parser.add_argument(
        "--categories",
        help="Comma-separated offline categories, e.g. sports,crypto,relative_value",
    )
    parser.add_argument("--interval-seconds", type=int, default=300)
    parser.add_argument("--iterations", type=int, default=12)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--limit", type=int, default=50)
    parser.add_argument("--refresh", action="store_true")
    parser.add_argument("--skip-provider-diagnosis", action="store_true")
    parser.add_argument(
        "--odds-provider", choices=["auto", "odds_api_io", "the_odds_api", "disabled"]
    )
    parser.add_argument("--sport")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    if not args.categories and not args.category:
        raise SystemExit(
            "Provide --categories for offline snapshots or --category for live research."
        )
    args.output_dir.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        handlers=[
            logging.StreamHandler(),
            logging.FileHandler(args.output_dir / "collection.log", encoding="utf-8"),
        ],
    )
    if args.categories:
        collect_offline_snapshots(args)
    else:
        collect_live_category_snapshots(args)


def collect_offline_snapshots(args: argparse.Namespace) -> None:
    categories = parse_categories(args.categories)
    db_path = args.output_dir / "snapshots.sqlite"
    init_db(db_path)
    for iteration in range(1, args.iterations + 1):
        started = datetime.now(UTC)
        iteration_dir = args.output_dir / f"iteration_{iteration:04d}"
        iteration_dir.mkdir(parents=True, exist_ok=True)
        snapshot = build_offline_snapshot(categories, started)
        write_snapshot_files(iteration_dir, snapshot)
        append_snapshot_to_db(db_path, iteration, started, categories, iteration_dir, snapshot)
        append_snapshot_index(
            args.output_dir / "snapshot_index.csv",
            iteration,
            started,
            categories,
            iteration_dir,
            snapshot,
        )
        print(
            f"Iteration {iteration} at {started.isoformat()}: "
            f"markets={snapshot['totals']['markets_scanned']}, "
            f"matches={snapshot['totals']['matched_markets']}, "
            f"signals={len(snapshot['signals'])}, "
            f"apparent_edges={snapshot['totals']['apparent_edges']}, "
            f"risk_approved={snapshot['totals']['risk_approved_paper_signals']}"
        )
        if iteration < args.iterations:
            time.sleep(max(0, args.interval_seconds))


def collect_live_category_snapshots(args: argparse.Namespace) -> None:
    db_path = args.output_dir / "snapshots.sqlite"
    init_db(db_path)
    settings = Settings()
    for iteration in range(1, args.iterations + 1):
        report_path = args.output_dir / f"snapshot_{iteration:04d}.md"
        run_live_research(
            category=args.category,
            output=report_path,
            settings=settings,
            limit=args.limit,
            refresh=args.refresh,
            skip_provider_diagnosis=args.skip_provider_diagnosis,
            odds_provider=args.odds_provider,
            sport=args.sport,
        )
        snapshot_time = datetime.now(UTC)
        categories = [args.category]
        snapshot = build_offline_snapshot(categories, snapshot_time)
        iteration_dir = args.output_dir / f"iteration_{iteration:04d}"
        iteration_dir.mkdir(parents=True, exist_ok=True)
        write_snapshot_files(iteration_dir, snapshot)
        append_snapshot_to_db(
            db_path, iteration, snapshot_time, categories, iteration_dir, snapshot
        )
        append_snapshot_index(
            args.output_dir / "snapshot_index.csv",
            iteration,
            snapshot_time,
            categories,
            iteration_dir,
            snapshot,
        )
        if iteration < args.iterations:
            time.sleep(max(0, args.interval_seconds))


def build_offline_snapshot(categories: list[str], timestamp: datetime) -> dict[str, Any]:
    settings = Settings()
    markets = load_markets(Path("data/live"))
    orderbooks = load_orderbooks(Path("data/live"))
    anchors = load_anchors(Path("data/live"))
    latest_signals = read_csv_rows(Path("reports/latest_signals.csv"))
    match_audit = read_csv_rows(Path("reports/event_match_audit.csv"))
    rv_build_error = ""
    try:
        rv_rows = build_current_relative_value_rows(markets, orderbooks, settings)
    except Exception as exc:
        rv_build_error = str(exc)
        rv_rows = []
    if not rv_rows:
        rv_rows = mark_relative_value_source(
            read_csv_rows(Path("reports/relative_value_opportunities.csv")),
            "static_report",
        )

    category_summaries = {}
    signals: list[dict[str, str]] = []
    freshness: list[dict[str, str]] = []
    matches: list[dict[str, str]] = []
    relative_value_rows: list[dict[str, str]] = []
    failures: dict[str, str] = {}
    for category in categories:
        normalized = normalize_category(category)
        try:
            if normalized == "relative_value":
                category_signals = relative_value_signal_rows(rv_rows, timestamp)
                category_freshness = relative_value_freshness_rows(rv_rows, timestamp)
                if rv_build_error:
                    category_freshness[0]["reason"] = (
                        f"live snapshot build failed; fallback used: {rv_build_error}"
                    )
                category_matches = []
                category_markets = {
                    value
                    for row in rv_rows
                    for value in (row.get("child_market"), row.get("parent_market"))
                    if value
                }
                liquidity_values = [parse_float(row.get("available_liquidity")) for row in rv_rows]
                spread_values: list[float] = []
                matched = len(rv_rows)
                relative_value_rows = rv_rows
            else:
                category_markets = {
                    market.market_id
                    for market in markets
                    if market_belongs_to_category(market, normalized)
                }
                category_orderbooks = [
                    book for book in orderbooks if book.market_id in category_markets
                ]
                category_anchors = [
                    anchor for anchor in anchors if anchor_belongs_to_category(anchor, normalized)
                ]
                category_signals = build_current_category_signal_rows(
                    normalized,
                    list(category_markets_by_id(markets, normalized).values()),
                    category_orderbooks,
                    category_anchors,
                    timestamp,
                    settings,
                )
                if not category_signals:
                    category_signals = [
                        normalize_signal_row(row, "latest_signals", normalized, settings)
                        for row in latest_signals
                        if signal_belongs_to_category(row, normalized)
                    ]
                category_freshness = freshness_rows(normalized)
                category_matches = [
                    row for row in match_audit if row.get("market_id") in category_markets
                ]
                liquidity_values = liquidity_distribution(category_orderbooks)
                spread_values = spread_distribution(category_orderbooks)
                matched = sum(1 for row in category_matches if row.get("decision") == "MATCH")
                if category_anchors and not category_freshness:
                    category_freshness = [
                        {
                            "category": normalized,
                            "source": "external_anchors",
                            "rows": str(len(category_anchors)),
                            "latest_timestamp": "",
                            "age_seconds": "",
                            "status": "present",
                            "reason": "anchors snapshot exists",
                        }
                    ]
        except Exception as exc:
            failures[normalized] = str(exc)
            category_signals = []
            category_freshness = [
                {
                    "category": normalized,
                    "source": "collector",
                    "rows": "0",
                    "latest_timestamp": timestamp.isoformat(),
                    "age_seconds": "0",
                    "status": "failed",
                    "reason": str(exc),
                }
            ]
            category_matches = []
            category_markets = set()
            liquidity_values = []
            spread_values = []
            matched = 0
        signals.extend(category_signals)
        freshness.extend(category_freshness)
        matches.extend(category_matches)
        category_summaries[normalized] = summarize_category(
            normalized,
            markets_scanned=len(category_markets),
            matched_markets=matched,
            signals=category_signals,
            matches=category_matches,
            liquidity_values=liquidity_values,
            spread_values=spread_values,
        )

    totals = combine_category_summaries(category_summaries.values())
    return {
        "snapshot_version": 1,
        "timestamp": timestamp.isoformat(),
        "categories": categories,
        "category_summaries": category_summaries,
        "totals": totals,
        "signals": signals,
        "freshness": freshness,
        "match_audit": matches,
        "relative_value_opportunities": relative_value_rows,
        "failures": failures,
    }


def write_snapshot_files(iteration_dir: Path, snapshot: dict[str, Any]) -> None:
    write_csv(iteration_dir / "signals.csv", snapshot["signals"], SNAPSHOT_SIGNAL_COLUMNS)
    write_csv(iteration_dir / "freshness.csv", snapshot["freshness"], SNAPSHOT_FRESHNESS_COLUMNS)
    if snapshot["match_audit"]:
        columns = sorted({key for row in snapshot["match_audit"] for key in row})
        write_csv(iteration_dir / "match_audit.csv", snapshot["match_audit"], columns)
    relative_value_rows = snapshot.get("relative_value_opportunities", [])
    if relative_value_rows:
        columns = sorted({key for row in relative_value_rows for key in row})
        write_csv(
            iteration_dir / "relative_value_opportunities.csv",
            relative_value_rows,
            columns,
        )
    summary = {
        key: value
        for key, value in snapshot.items()
        if key not in {"signals", "freshness", "match_audit", "relative_value_opportunities"}
    }
    (iteration_dir / "summary.json").write_text(
        json.dumps(summary, indent=2, sort_keys=True), encoding="utf-8"
    )


def append_snapshot_index(
    path: Path,
    iteration: int,
    started: datetime,
    categories: list[str],
    iteration_dir: Path,
    snapshot: dict[str, Any],
) -> None:
    columns = [
        "iteration",
        "timestamp",
        "categories",
        "snapshot_dir",
        "signals_path",
        "freshness_path",
        "match_audit_path",
        "relative_value_opportunities_path",
        "summary_path",
        "markets_scanned",
        "matched_markets",
        "apparent_edges",
        "risk_approved_paper_signals",
        "signals_count",
        "failures",
    ]
    row = {
        "iteration": iteration,
        "timestamp": started.isoformat(),
        "categories": ",".join(categories),
        "snapshot_dir": str(iteration_dir),
        "signals_path": str(iteration_dir / "signals.csv"),
        "freshness_path": str(iteration_dir / "freshness.csv"),
        "match_audit_path": str(iteration_dir / "match_audit.csv")
        if (iteration_dir / "match_audit.csv").exists()
        else "",
        "relative_value_opportunities_path": str(iteration_dir / "relative_value_opportunities.csv")
        if (iteration_dir / "relative_value_opportunities.csv").exists()
        else "",
        "summary_path": str(iteration_dir / "summary.json"),
        "markets_scanned": snapshot["totals"]["markets_scanned"],
        "matched_markets": snapshot["totals"]["matched_markets"],
        "apparent_edges": snapshot["totals"]["apparent_edges"],
        "risk_approved_paper_signals": snapshot["totals"]["risk_approved_paper_signals"],
        "signals_count": len(snapshot["signals"]),
        "failures": json.dumps(snapshot.get("failures", {}), sort_keys=True),
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    write_header = not path.exists()
    with path.open("a", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns)
        if write_header:
            writer.writeheader()
        writer.writerow(row)


def init_db(path: Path) -> None:
    with sqlite3.connect(path) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS snapshots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                iteration INTEGER NOT NULL,
                timestamp TEXT NOT NULL,
                category TEXT NOT NULL,
                report_path TEXT NOT NULL,
                markets_count INTEGER NOT NULL,
                anchors_count INTEGER NOT NULL,
                orderbooks_count INTEGER NOT NULL,
                signals_count INTEGER NOT NULL,
                matches_count INTEGER NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS signals (
                snapshot_id INTEGER NOT NULL,
                signal_id TEXT NOT NULL,
                timestamp TEXT,
                market_id TEXT,
                event_id TEXT,
                outcome TEXT,
                action TEXT,
                fair_probability REAL,
                prediction_bid REAL,
                prediction_ask REAL,
                adjusted_edge REAL,
                fair_value_source TEXT,
                fair_value_provider TEXT,
                fair_value_timestamp TEXT,
                orderbook_timestamp TEXT,
                match_score REAL,
                risk_approved TEXT,
                raw_json TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS match_audit (
                snapshot_id INTEGER NOT NULL,
                market_id TEXT,
                decision TEXT,
                score REAL,
                rejection_reason TEXT,
                raw_json TEXT NOT NULL
            )
            """
        )


def append_snapshot_to_db(
    db_path: Path,
    iteration: int,
    started: datetime,
    categories: list[str],
    iteration_dir: Path,
    snapshot: dict[str, Any],
) -> int:
    with sqlite3.connect(db_path) as conn:
        cursor = conn.execute(
            """
            INSERT INTO snapshots (
                iteration, timestamp, category, report_path, markets_count, anchors_count,
                orderbooks_count, signals_count, matches_count
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                iteration,
                started.isoformat(),
                ",".join(categories),
                str(iteration_dir / "summary.json"),
                snapshot["totals"]["markets_scanned"],
                sum(
                    int(row.get("rows") or 0)
                    for row in snapshot["freshness"]
                    if "anchors" in row.get("source", "")
                ),
                snapshot["totals"]["valid_books"],
                len(snapshot["signals"]),
                len(snapshot["match_audit"]),
            ),
        )
        snapshot_id = int(cursor.lastrowid)
        conn.executemany(
            """
            INSERT INTO signals (
                snapshot_id, signal_id, timestamp, market_id, event_id, outcome, action,
                fair_probability, prediction_bid, prediction_ask, adjusted_edge,
                fair_value_source, fair_value_provider, fair_value_timestamp,
                orderbook_timestamp, match_score, risk_approved, raw_json
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [_signal_tuple(snapshot_id, row) for row in snapshot["signals"]],
        )
        conn.executemany(
            """
            INSERT INTO match_audit (
                snapshot_id, market_id, decision, score, rejection_reason, raw_json
            )
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            [_match_tuple(snapshot_id, row) for row in snapshot["match_audit"]],
        )
    return snapshot_id


def summarize_category(
    category: str,
    markets_scanned: int,
    matched_markets: int,
    signals: list[dict[str, str]],
    matches: list[dict[str, str]],
    liquidity_values: list[float],
    spread_values: list[float],
) -> dict[str, Any]:
    apparent_edges = [row for row in signals if parse_float(row.get("adjusted_edge")) > 0]
    risk_approved = [
        row
        for row in signals
        if row.get("risk_decision") == "APPROVED"
        or row.get("risk_approved") in {"True", "true", "1"}
    ]
    rejection_reasons = Counter(
        reason
        for reason in (
            row.get("rejection_reason") or row.get("risk_reasons") or row.get("reason") or ""
            for row in [*signals, *matches]
        )
        if reason
    )
    return {
        "category": category,
        "markets_scanned": markets_scanned,
        "matched_markets": matched_markets,
        "apparent_edges": len(apparent_edges),
        "risk_approved_paper_signals": len(risk_approved),
        "average_adjusted_edge": average(
            [parse_float(row.get("adjusted_edge")) for row in apparent_edges]
        ),
        "liquidity": distribution(liquidity_values),
        "spread": distribution(spread_values),
        "top_rejection_reasons": dict(rejection_reasons.most_common(8)),
        "valid_books": len([value for value in liquidity_values if value > 0]),
    }


def combine_category_summaries(summaries: Iterable[dict[str, Any]]) -> dict[str, Any]:
    rows = list(summaries)
    adjusted_values = [
        row["average_adjusted_edge"]
        for row in rows
        if row["apparent_edges"] > 0 and row["average_adjusted_edge"] is not None
    ]
    return {
        "markets_scanned": sum(row["markets_scanned"] for row in rows),
        "matched_markets": sum(row["matched_markets"] for row in rows),
        "apparent_edges": sum(row["apparent_edges"] for row in rows),
        "risk_approved_paper_signals": sum(row["risk_approved_paper_signals"] for row in rows),
        "average_adjusted_edge": average(adjusted_values),
        "valid_books": sum(row["valid_books"] for row in rows),
    }


def parse_categories(value: str) -> list[str]:
    categories = [normalize_category(part) for part in value.split(",") if part.strip()]
    allowed = {"sports", "crypto", "weather", "politics", "macro", "relative_value"}
    invalid = [category for category in categories if category not in allowed]
    if invalid:
        raise SystemExit(f"Unsupported categories: {', '.join(invalid)}")
    return categories


def normalize_category(category: str) -> str:
    category = category.strip().lower()
    return (
        "relative_value" if category in {"internal_relative_value", "relative-value"} else category
    )


def normalize_signal_row(
    row: dict[str, Any],
    source: str,
    category: str,
    settings: Settings | None = None,
) -> dict[str, str]:
    row = stringify_row(row)
    bid = row.get("prediction_bid", "")
    ask = row.get("prediction_ask", "")
    bid_float = float_or_none(bid)
    ask_float = float_or_none(ask)
    mid = (bid_float + ask_float) / 2 if bid_float is not None and ask_float is not None else None
    spread = (
        max(0.0, ask_float - bid_float) if bid_float is not None and ask_float is not None else None
    )
    risk_approved = row.get("risk_approved", "")
    risk_decision = "APPROVED" if risk_approved.lower() in {"true", "1", "yes"} else "REJECTED"
    rejection_reason = row.get("risk_reasons") or row.get("reason") or ""
    normalized = {
        "signal_id": row.get("signal_id", ""),
        "timestamp": row.get("timestamp", ""),
        "category": category,
        "market_id": row.get("market_id", ""),
        "event_id": row.get("event_id", ""),
        "outcome": row.get("outcome", ""),
        "fair_probability": row.get("fair_probability", ""),
        "bid": bid,
        "ask": ask,
        "mid": "" if mid is None else str(mid),
        "spread": "" if spread is None else str(spread),
        "raw_edge": row.get("raw_edge", ""),
        "adjusted_edge": row.get("adjusted_edge", ""),
        "cost_drag": row.get("cost_drag", ""),
        "action": row.get("action", ""),
        "signal_action": row.get("signal_action") or row.get("action", ""),
        "risk_decision": risk_decision,
        "risk_approved": risk_approved,
        "risk_reasons": row.get("risk_reasons") or rejection_reason,
        "rejection_reason": rejection_reason,
        "liquidity": row.get("available_size", ""),
        "fair_value_source": row.get("fair_value_source", ""),
        "fair_value_provider": row.get("fair_value_provider", ""),
        "data_age_seconds": row.get("data_age_seconds", ""),
        "freshness_status": row.get("freshness_status", ""),
        "match_score": row.get("match_score", ""),
        "source": source,
    }
    normalized.update(
        {
            key: row.get(key, "")
            for key in (
                "token_id",
                "fair_value_timestamp",
                "orderbook_timestamp",
                "uncertainty",
                "match_decision",
                "settlement_verified",
                "fair_value_available_at",
                "orderbook_available_at",
                "book_bid_size",
                "book_ask_size",
                "market_status",
                "resolution_source",
                "market_end_time",
            )
        }
    )
    return enrich_signal_row(normalized, settings or Settings())


def build_current_category_signal_rows(
    category: str,
    markets: list[PredictionMarket],
    orderbooks: list[OrderBook],
    anchors: list[ExternalAnchor],
    timestamp: datetime,
    settings: Settings,
) -> list[dict[str, str]]:
    if category not in {"crypto", "weather", "sports"}:
        return []
    if not markets or not orderbooks or not anchors:
        return []
    matches = resolve_events(markets, anchors, as_of=timestamp)
    rewritten_anchors = rewrite_anchors_for_matches(anchors, matches)
    fair_values = compute_live_fair_values(
        category, markets, rewritten_anchors, orderbooks, settings, timestamp
    )
    if not fair_values:
        return []
    bbas = [book.best_bid_ask() for book in orderbooks]
    generated = generate_signals(markets, fair_values, bbas, settings, as_of=timestamp)
    signals = add_avoid_signals(
        markets, generated, fair_values, bbas, matches, anchors, settings, timestamp
    )
    risk_rows = risk_diagnostics(signals, markets, settings, timestamp)
    annotated = annotate_signals(signals, fair_values, matches, risk_rows)
    return [
        normalize_signal_row(signal_to_row(signal), "current_local_snapshot", category, settings)
        for signal in annotated
    ]


def signal_to_row(signal: Signal) -> dict[str, Any]:
    return signal.model_dump(mode="json")


def stringify_row(row: dict[str, Any]) -> dict[str, str]:
    output: dict[str, str] = {}
    for key, value in row.items():
        if value is None:
            output[str(key)] = ""
        elif isinstance(value, list | dict):
            output[str(key)] = json.dumps(value, sort_keys=True)
        else:
            output[str(key)] = str(value)
    return output


def build_current_relative_value_rows(
    markets: list[PredictionMarket],
    orderbooks: list[OrderBook],
    settings: Settings | None = None,
) -> list[dict[str, str]]:
    if not markets or not orderbooks:
        return []
    opportunities = find_relative_value_opportunities(markets, orderbooks, settings or Settings())
    return [relative_value_opportunity_to_row(row, "live_snapshot") for row in opportunities]


def relative_value_opportunity_to_row(opportunity: RVOpportunity, source: str) -> dict[str, str]:
    row = {
        "relation_type": opportunity.relation_type,
        "parent_market": opportunity.parent_market,
        "child_market": opportunity.child_market,
        "involved_outcomes": " | ".join(opportunity.involved_outcomes),
        "theoretical_violation": str(opportunity.theoretical_violation),
        "executable_violation": str(opportunity.executable_violation),
        "required_legs": opportunity.required_legs,
        "available_liquidity": str(opportunity.available_liquidity),
        "max_trade_size": str(opportunity.max_trade_size),
        "status": opportunity.status,
        "explanation": opportunity.explanation,
        "relative_value_source": source,
    }
    return row


def mark_relative_value_source(rows: list[dict[str, str]], source: str) -> list[dict[str, str]]:
    output = []
    for row in rows:
        marked = dict(row)
        marked["relative_value_source"] = marked.get("relative_value_source") or source
        output.append(marked)
    return output


def relative_value_signal_rows(
    rows: list[dict[str, str]], timestamp: datetime
) -> list[dict[str, str]]:
    output: list[dict[str, str]] = []
    for index, row in enumerate(rows, start=1):
        status = row.get("status", "")
        executable = status in {"EXECUTABLE", "EXECUTABLE_PACKAGE", "PAPER_ELIGIBLE"}
        edge = parse_float(row.get("executable_violation")) or parse_float(
            row.get("theoretical_violation")
        )
        normalized = {
            "signal_id": f"rv:{index}:{row.get('relation_type', '')}:{row.get('child_market', '')}",
            "timestamp": timestamp.isoformat(),
            "category": "relative_value",
            "market_id": row.get("child_market", ""),
            "event_id": row.get("parent_market", ""),
            "outcome": row.get("involved_outcomes", ""),
            "fair_probability": "",
            "bid": "",
            "ask": "",
            "mid": "",
            "spread": "",
            "raw_edge": "",
            "adjusted_edge": str(edge),
            "cost_drag": "",
            "buy_edge_raw": "",
            "sell_edge_raw": "",
            "buy_edge_adjusted": "",
            "sell_edge_adjusted": "",
            "best_side": "NONE",
            "best_adjusted_edge": str(edge),
            "edge_passes_threshold": "false",
            "action": "RELATIVE_VALUE_EXECUTABLE" if executable else f"RELATIVE_VALUE_{status}",
            "signal_action": "RELATIVE_VALUE",
            "risk_decision": "RESEARCH_ONLY",
            "risk_approved": "False",
            "risk_reasons": status,
            "final_decision": "RELATIVE_VALUE_MULTI_LEG"
            if executable
            else "THEORETICAL_ONLY"
            if status in {"THEORETICAL_ONLY", "THEORETICAL_VIOLATION"}
            else "NO_EDGE",
            "rejection_reason": "" if executable else status,
            "liquidity": row.get("available_liquidity", ""),
            "fair_value_source": "internal_relative_value",
            "fair_value_provider": "",
            "data_age_seconds": "0",
            "match_score": "",
            "source": row.get("relative_value_source") or "relative_value_opportunities",
        }
        output.append(normalized)
    return output


def relative_value_freshness_rows(
    rows: list[dict[str, str]], timestamp: datetime
) -> list[dict[str, str]]:
    source_counts = Counter(
        row.get("relative_value_source") or "relative_value_opportunities" for row in rows
    )
    source_label = ",".join(f"{source}:{count}" for source, count in sorted(source_counts.items()))
    return [
        {
            "category": "relative_value",
            "source": source_label or "relative_value_opportunities",
            "rows": str(len(rows)),
            "latest_timestamp": timestamp.isoformat(),
            "age_seconds": "0",
            "status": "fresh" if rows else "missing",
            "reason": "relative-value output exists" if rows else "relative-value output missing",
        }
    ]


def freshness_rows(category: str) -> list[dict[str, str]]:
    path = Path("reports") / f"data_freshness_{category}.csv"
    rows = read_csv_rows(path)
    output = []
    for row in rows:
        merged = {column: row.get(column, "") for column in SNAPSHOT_FRESHNESS_COLUMNS}
        merged["category"] = category
        output.append(merged)
    return output


def load_markets(live_data_dir: Path) -> list[PredictionMarket]:
    rows: list[PredictionMarket] = []
    for path in sorted(live_data_dir.glob("*_markets.json")) if live_data_dir.exists() else []:
        rows.extend(load_models(path, PredictionMarket, "markets"))
    return list({market.market_id: market for market in rows}.values())


def load_orderbooks(live_data_dir: Path) -> list[OrderBook]:
    rows: list[OrderBook] = []
    for path in sorted(live_data_dir.glob("*_orderbooks.json")) if live_data_dir.exists() else []:
        rows.extend(load_models(path, OrderBook, "orderbooks"))
    return rows


def load_anchors(live_data_dir: Path) -> list[ExternalAnchor]:
    rows: list[ExternalAnchor] = []
    for path in sorted(live_data_dir.glob("*_anchors.json")) if live_data_dir.exists() else []:
        rows.extend(load_models(path, ExternalAnchor, "anchors"))
    return rows


def load_models(path: Path, model_type: type[Any], key: str) -> list[Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    records = payload.get(key, payload) if isinstance(payload, dict) else payload
    if not isinstance(records, list):
        return []
    output = []
    for row in records:
        if not isinstance(row, dict):
            continue
        try:
            model = model_type(**row)
        except ValueError:
            continue
        if isinstance(model, PredictionMarket):
            model = normalize_loaded_market(model)
        output.append(model)
    return output


def normalize_loaded_market(market: PredictionMarket) -> PredictionMarket:
    if market.resolution_source:
        return market
    inferred = infer_resolution_source_from_text(market.description)
    if not inferred:
        return market
    return market.model_copy(update={"resolution_source": inferred})


def category_markets_by_id(
    markets: list[PredictionMarket], category: str
) -> dict[str, PredictionMarket]:
    return {
        market.market_id: market
        for market in markets
        if market_belongs_to_category(market, category)
    }


def read_csv_rows(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    with path.open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def write_csv(path: Path, rows: list[dict[str, str]], columns: list[str]) -> None:
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


def market_belongs_to_category(market: PredictionMarket, category: str) -> bool:
    market_category = str(market.category)
    if category == "sports":
        return market_category.startswith("sports")
    if category == "crypto":
        return market_category == "crypto_threshold"
    if category == "weather":
        return market_category == "weather_threshold"
    if category == "politics":
        return market_category == "politics_election"
    if category == "macro":
        text = f"{market.title} {market.description or ''}".lower()
        return any(term in text for term in ("cpi", "inflation", "fed", "gdp", "macro"))
    return False


def anchor_belongs_to_category(anchor: ExternalAnchor, category: str) -> bool:
    anchor_category = str(anchor.category)
    if category == "sports":
        return anchor_category.startswith("sports")
    if category == "crypto":
        return anchor_category == "crypto_threshold"
    if category == "weather":
        return anchor_category == "weather_threshold"
    if category == "politics":
        return anchor_category == "politics_election"
    if category == "macro":
        text = f"{anchor.outcome} {anchor.metadata}".lower()
        return any(term in text for term in ("cpi", "inflation", "fed", "gdp", "macro"))
    return False


def signal_belongs_to_category(row: dict[str, str], category: str) -> bool:
    signal_category = row.get("category", "")
    if category == "sports":
        return signal_category.startswith("sports")
    if category == "crypto":
        return signal_category == "crypto_threshold"
    if category == "weather":
        return signal_category == "weather_threshold"
    if category == "politics":
        return signal_category == "politics_election"
    if category == "macro":
        text = f"{row.get('market_id', '')} {row.get('reason', '')}".lower()
        return any(term in text for term in ("cpi", "inflation", "fed", "gdp", "macro"))
    return False


def liquidity_distribution(orderbooks: Iterable[OrderBook]) -> list[float]:
    values = []
    for book in orderbooks:
        bba = book.best_bid_ask()
        if bba.best_bid is None or bba.best_ask is None:
            continue
        values.append(min(float(bba.best_bid_size or 0.0), float(bba.best_ask_size or 0.0)))
    return values


def spread_distribution(orderbooks: Iterable[OrderBook]) -> list[float]:
    return [
        float(spread)
        for spread in (book.best_bid_ask().spread for book in orderbooks)
        if spread is not None
    ]


def distribution(values: list[float]) -> dict[str, float | None]:
    if not values:
        return {"count": 0, "min": None, "median": None, "max": None}
    ordered = sorted(values)
    return {
        "count": len(ordered),
        "min": ordered[0],
        "median": median(ordered),
        "max": ordered[-1],
    }


def average(values: list[float]) -> float | None:
    return sum(values) / len(values) if values else None


def _signal_tuple(snapshot_id: int, row: dict[str, str]) -> tuple[object, ...]:
    return (
        snapshot_id,
        row.get("signal_id", ""),
        row.get("timestamp", ""),
        row.get("market_id", ""),
        row.get("event_id", ""),
        row.get("outcome", ""),
        row.get("action", ""),
        float_or_none(row.get("fair_probability")),
        float_or_none(row.get("bid") or row.get("prediction_bid")),
        float_or_none(row.get("ask") or row.get("prediction_ask")),
        float_or_none(row.get("adjusted_edge")),
        row.get("fair_value_source", ""),
        row.get("fair_value_provider", ""),
        row.get("fair_value_timestamp", ""),
        row.get("orderbook_timestamp", ""),
        float_or_none(row.get("match_score")),
        row.get("risk_approved") or ("True" if row.get("risk_decision") == "APPROVED" else "False"),
        json.dumps(row, sort_keys=True),
    )


def _match_tuple(snapshot_id: int, row: dict[str, str]) -> tuple[object, ...]:
    return (
        snapshot_id,
        row.get("market_id", ""),
        row.get("decision", ""),
        float_or_none(row.get("score")),
        row.get("rejection_reason", ""),
        json.dumps(row, sort_keys=True),
    )


def parse_float(value: str | None) -> float:
    parsed = float_or_none(value)
    return 0.0 if parsed is None else parsed


def float_or_none(value: str | None) -> float | None:
    if value in {None, ""}:
        return None
    try:
        return float(value)
    except ValueError:
        return None


if __name__ == "__main__":
    main()
