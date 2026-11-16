"""SQLite-backed local storage for snapshots and research artifacts."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sharpedge.data.schemas import (
    ExternalOdds,
    FairValue,
    Market,
    OrderBook,
    Resolution,
    Signal,
    Trade,
)


def _json_default(value: Any) -> str:
    if isinstance(value, datetime):
        return value.astimezone(UTC).isoformat()
    raise TypeError(f"Object of type {type(value)!r} is not JSON serializable")


def _dumps(value: Any) -> str:
    return json.dumps(value, default=_json_default, sort_keys=True)


def _loads(value: str) -> Any:
    return json.loads(value)


def _dt(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)


class Storage:
    """Small persistence layer optimized for reproducible local research."""

    def __init__(self, database_url: str = "sqlite:///sharpedge.db") -> None:
        if database_url.startswith("sqlite:///"):
            path = database_url.removeprefix("sqlite:///")
        elif database_url.startswith("duckdb:///"):
            raise ValueError("DuckDB URL configured but sqlite Storage was requested")
        else:
            path = database_url
        self.path = Path(path)
        if self.path.parent and str(self.path.parent) != ".":
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path)
        self.conn.row_factory = sqlite3.Row
        self.init_schema()

    def init_schema(self) -> None:
        statements = [
            """
            CREATE TABLE IF NOT EXISTS markets (
                market_id TEXT PRIMARY KEY,
                event_id TEXT NOT NULL,
                sport TEXT NOT NULL,
                league TEXT,
                title TEXT NOT NULL,
                description TEXT,
                outcomes_json TEXT NOT NULL,
                token_ids_json TEXT NOT NULL,
                start_time TEXT,
                end_time TEXT,
                resolution_source TEXT,
                status TEXT NOT NULL,
                raw_metadata_json TEXT NOT NULL
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS orderbooks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                market_id TEXT NOT NULL,
                token_id TEXT NOT NULL,
                timestamp TEXT NOT NULL,
                bids_json TEXT NOT NULL,
                asks_json TEXT NOT NULL
            )
            """,
            "CREATE INDEX IF NOT EXISTS idx_orderbooks_market_time ON orderbooks(market_id, timestamp)",
            "CREATE INDEX IF NOT EXISTS idx_orderbooks_token_time ON orderbooks(token_id, timestamp)",
            """
            CREATE TABLE IF NOT EXISTS external_odds (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                event_id TEXT NOT NULL,
                sport TEXT NOT NULL,
                book TEXT NOT NULL,
                market_type TEXT NOT NULL,
                outcome TEXT NOT NULL,
                decimal_odds REAL NOT NULL,
                timestamp TEXT NOT NULL
            )
            """,
            "CREATE INDEX IF NOT EXISTS idx_external_odds_event_time ON external_odds(event_id, timestamp)",
            """
            CREATE TABLE IF NOT EXISTS fair_values (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                event_id TEXT NOT NULL,
                market_id TEXT NOT NULL,
                outcome TEXT NOT NULL,
                fair_probability REAL NOT NULL,
                source TEXT NOT NULL,
                timestamp TEXT NOT NULL,
                uncertainty REAL NOT NULL
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS signals (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL,
                market_id TEXT NOT NULL,
                event_id TEXT,
                outcome TEXT NOT NULL,
                signal_type TEXT NOT NULL,
                fair_probability REAL NOT NULL,
                polymarket_bid REAL,
                polymarket_ask REAL,
                raw_edge REAL NOT NULL,
                adjusted_edge REAL NOT NULL,
                available_size REAL NOT NULL,
                confidence REAL NOT NULL,
                action TEXT NOT NULL,
                reason TEXT NOT NULL
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS trades (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL,
                market_id TEXT NOT NULL,
                event_id TEXT,
                outcome TEXT NOT NULL,
                side TEXT NOT NULL,
                price REAL NOT NULL,
                size REAL NOT NULL,
                fees REAL NOT NULL,
                slippage REAL NOT NULL,
                signal_id TEXT,
                paper_or_live TEXT NOT NULL,
                notes TEXT
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS decisions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL,
                market_id TEXT NOT NULL,
                action TEXT NOT NULL,
                executed INTEGER NOT NULL,
                reasons_json TEXT NOT NULL,
                signal_json TEXT NOT NULL,
                trade_json TEXT
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS resolutions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                market_id TEXT NOT NULL,
                outcome TEXT NOT NULL,
                resolved_outcome TEXT NOT NULL,
                payout REAL NOT NULL,
                timestamp TEXT NOT NULL
            )
            """,
        ]
        with self.conn:
            for statement in statements:
                self.conn.execute(statement)

    def close(self) -> None:
        self.conn.close()

    def insert_market(self, market: Market) -> None:
        with self.conn:
            self.conn.execute(
                """
                INSERT OR REPLACE INTO markets VALUES (
                    :market_id, :event_id, :sport, :league, :title, :description,
                    :outcomes_json, :token_ids_json, :start_time, :end_time,
                    :resolution_source, :status, :raw_metadata_json
                )
                """,
                {
                    "market_id": market.market_id,
                    "event_id": market.event_id,
                    "sport": market.sport,
                    "league": market.league,
                    "title": market.title,
                    "description": market.description,
                    "outcomes_json": _dumps(market.outcomes),
                    "token_ids_json": _dumps(market.token_ids),
                    "start_time": market.start_time.isoformat()
                    if market.start_time
                    else None,
                    "end_time": market.end_time.isoformat() if market.end_time else None,
                    "resolution_source": market.resolution_source,
                    "status": market.status,
                    "raw_metadata_json": _dumps(market.raw_metadata),
                },
            )

    def insert_markets(self, markets: Iterable[Market]) -> None:
        for market in markets:
            self.insert_market(market)

    def insert_orderbook(self, orderbook: OrderBook) -> None:
        with self.conn:
            self.conn.execute(
                """
                INSERT INTO orderbooks (
                    market_id, token_id, timestamp, bids_json, asks_json
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (
                    orderbook.market_id,
                    orderbook.token_id,
                    orderbook.timestamp.isoformat(),
                    _dumps([level.model_dump() for level in orderbook.bids]),
                    _dumps([level.model_dump() for level in orderbook.asks]),
                ),
            )

    def insert_orderbooks(self, orderbooks: Iterable[OrderBook]) -> None:
        with self.conn:
            for orderbook in orderbooks:
                self.conn.execute(
                    """
                    INSERT INTO orderbooks (
                        market_id, token_id, timestamp, bids_json, asks_json
                    ) VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        orderbook.market_id,
                        orderbook.token_id,
                        orderbook.timestamp.isoformat(),
                        _dumps([level.model_dump() for level in orderbook.bids]),
                        _dumps([level.model_dump() for level in orderbook.asks]),
                    ),
                )

    def insert_external_odds(self, odds: Iterable[ExternalOdds]) -> None:
        with self.conn:
            for row in odds:
                self.conn.execute(
                    """
                    INSERT INTO external_odds (
                        event_id, sport, book, market_type, outcome, decimal_odds, timestamp
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        row.event_id,
                        row.sport,
                        row.book,
                        row.market_type,
                        row.outcome,
                        row.decimal_odds,
                        row.timestamp.isoformat(),
                    ),
                )

    def insert_fair_values(self, values: Iterable[FairValue]) -> None:
        with self.conn:
            for value in values:
                self.conn.execute(
                    """
                    INSERT INTO fair_values (
                        event_id, market_id, outcome, fair_probability, source,
                        timestamp, uncertainty
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        value.event_id,
                        value.market_id,
                        value.outcome,
                        value.fair_probability,
                        value.source,
                        value.timestamp.isoformat(),
                        value.uncertainty,
                    ),
                )

    def insert_signal(self, signal: Signal) -> int:
        with self.conn:
            cursor = self.conn.execute(
                """
                INSERT INTO signals (
                    timestamp, market_id, event_id, outcome, signal_type,
                    fair_probability, polymarket_bid, polymarket_ask, raw_edge,
                    adjusted_edge, available_size, confidence, action, reason
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    signal.timestamp.isoformat(),
                    signal.market_id,
                    signal.event_id,
                    signal.outcome,
                    signal.signal_type,
                    signal.fair_probability,
                    signal.polymarket_bid,
                    signal.polymarket_ask,
                    signal.raw_edge,
                    signal.adjusted_edge,
                    signal.available_size,
                    signal.confidence,
                    signal.action,
                    signal.reason,
                ),
            )
            return int(cursor.lastrowid)

    def insert_trade(self, trade: Trade) -> int:
        with self.conn:
            cursor = self.conn.execute(
                """
                INSERT INTO trades (
                    timestamp, market_id, event_id, outcome, side, price, size,
                    fees, slippage, signal_id, paper_or_live, notes
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    trade.timestamp.isoformat(),
                    trade.market_id,
                    trade.event_id,
                    trade.outcome,
                    trade.side,
                    trade.price,
                    trade.size,
                    trade.fees,
                    trade.slippage,
                    trade.signal_id,
                    trade.paper_or_live,
                    trade.notes,
                ),
            )
            return int(cursor.lastrowid)

    def insert_decision(
        self, signal: Signal, executed: bool, reasons: list[str], trade: Trade | None
    ) -> None:
        with self.conn:
            self.conn.execute(
                """
                INSERT INTO decisions (
                    timestamp, market_id, action, executed, reasons_json,
                    signal_json, trade_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    signal.timestamp.isoformat(),
                    signal.market_id,
                    signal.action,
                    int(executed),
                    _dumps(reasons),
                    signal.model_dump_json(),
                    trade.model_dump_json() if trade else None,
                ),
            )

    def insert_resolutions(self, resolutions: Iterable[Resolution]) -> None:
        with self.conn:
            for row in resolutions:
                self.conn.execute(
                    """
                    INSERT INTO resolutions (
                        market_id, outcome, resolved_outcome, payout, timestamp
                    ) VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        row.market_id,
                        row.outcome,
                        row.resolved_outcome,
                        row.payout,
                        row.timestamp.isoformat(),
                    ),
                )

    def list_markets(self) -> list[Market]:
        rows = self.conn.execute("SELECT * FROM markets ORDER BY market_id").fetchall()
        return [
            Market(
                market_id=row["market_id"],
                event_id=row["event_id"],
                sport=row["sport"],
                league=row["league"],
                title=row["title"],
                description=row["description"],
                outcomes=_loads(row["outcomes_json"]),
                token_ids=_loads(row["token_ids_json"]),
                start_time=_dt(row["start_time"]) if row["start_time"] else None,
                end_time=_dt(row["end_time"]) if row["end_time"] else None,
                resolution_source=row["resolution_source"],
                status=row["status"],
                raw_metadata=_loads(row["raw_metadata_json"]),
            )
            for row in rows
        ]

    def latest_orderbooks(self, as_of: datetime | None = None) -> list[OrderBook]:
        as_of = as_of or datetime.now(UTC)
        rows = self.conn.execute(
            """
            SELECT ob.*
            FROM orderbooks ob
            JOIN (
                SELECT market_id, token_id, MAX(timestamp) AS timestamp
                FROM orderbooks
                WHERE timestamp <= ?
                GROUP BY market_id, token_id
            ) latest
            ON ob.market_id = latest.market_id
               AND ob.token_id = latest.token_id
               AND ob.timestamp = latest.timestamp
            ORDER BY ob.market_id, ob.token_id
            """,
            (as_of.isoformat(),),
        ).fetchall()
        return [self._orderbook_from_row(row) for row in rows]

    def orderbooks_for_market(
        self, market_id: str, as_of: datetime | None = None
    ) -> list[OrderBook]:
        as_of = as_of or datetime.now(UTC)
        rows = self.conn.execute(
            """
            SELECT ob.*
            FROM orderbooks ob
            JOIN (
                SELECT token_id, MAX(timestamp) AS timestamp
                FROM orderbooks
                WHERE market_id = ? AND timestamp <= ?
                GROUP BY token_id
            ) latest
            ON ob.token_id = latest.token_id
               AND ob.timestamp = latest.timestamp
            WHERE ob.market_id = ?
            """,
            (market_id, as_of.isoformat(), market_id),
        ).fetchall()
        return [self._orderbook_from_row(row) for row in rows]

    def list_external_odds(self, as_of: datetime | None = None) -> list[ExternalOdds]:
        params: tuple[str, ...] = ()
        query = "SELECT * FROM external_odds"
        if as_of:
            query += " WHERE timestamp <= ?"
            params = (as_of.isoformat(),)
        query += " ORDER BY event_id, market_type, book, outcome, timestamp"
        rows = self.conn.execute(query, params).fetchall()
        return [
            ExternalOdds(
                event_id=row["event_id"],
                sport=row["sport"],
                book=row["book"],
                market_type=row["market_type"],
                outcome=row["outcome"],
                decimal_odds=row["decimal_odds"],
                timestamp=_dt(row["timestamp"]),
            )
            for row in rows
        ]

    def latest_external_odds(self, as_of: datetime | None = None) -> list[ExternalOdds]:
        as_of = as_of or datetime.now(UTC)
        rows = self.conn.execute(
            """
            SELECT eo.*
            FROM external_odds eo
            JOIN (
                SELECT event_id, book, market_type, outcome, MAX(timestamp) AS timestamp
                FROM external_odds
                WHERE timestamp <= ?
                GROUP BY event_id, book, market_type, outcome
            ) latest
            ON eo.event_id = latest.event_id
               AND eo.book = latest.book
               AND eo.market_type = latest.market_type
               AND eo.outcome = latest.outcome
               AND eo.timestamp = latest.timestamp
            ORDER BY eo.event_id, eo.market_type, eo.book, eo.outcome
            """,
            (as_of.isoformat(),),
        ).fetchall()
        return [
            ExternalOdds(
                event_id=row["event_id"],
                sport=row["sport"],
                book=row["book"],
                market_type=row["market_type"],
                outcome=row["outcome"],
                decimal_odds=row["decimal_odds"],
                timestamp=_dt(row["timestamp"]),
            )
            for row in rows
        ]

    def list_signals(self) -> list[Signal]:
        rows = self.conn.execute("SELECT * FROM signals ORDER BY timestamp").fetchall()
        return [
            Signal(
                timestamp=_dt(row["timestamp"]),
                market_id=row["market_id"],
                event_id=row["event_id"],
                outcome=row["outcome"],
                signal_type=row["signal_type"],
                fair_probability=row["fair_probability"],
                polymarket_bid=row["polymarket_bid"],
                polymarket_ask=row["polymarket_ask"],
                raw_edge=row["raw_edge"],
                adjusted_edge=row["adjusted_edge"],
                available_size=row["available_size"],
                confidence=row["confidence"],
                action=row["action"],
                reason=row["reason"],
            )
            for row in rows
        ]

    def list_trades(self) -> list[Trade]:
        rows = self.conn.execute("SELECT * FROM trades ORDER BY timestamp").fetchall()
        return [
            Trade(
                timestamp=_dt(row["timestamp"]),
                market_id=row["market_id"],
                event_id=row["event_id"],
                outcome=row["outcome"],
                side=row["side"],
                price=row["price"],
                size=row["size"],
                fees=row["fees"],
                slippage=row["slippage"],
                signal_id=row["signal_id"],
                paper_or_live=row["paper_or_live"],
                notes=row["notes"],
            )
            for row in rows
        ]

    def _orderbook_from_row(self, row: sqlite3.Row) -> OrderBook:
        from sharpedge.data.schemas import PriceLevel

        return OrderBook(
            market_id=row["market_id"],
            token_id=row["token_id"],
            timestamp=_dt(row["timestamp"]),
            bids=[PriceLevel(**level) for level in _loads(row["bids_json"])],
            asks=[PriceLevel(**level) for level in _loads(row["asks_json"])],
        )
