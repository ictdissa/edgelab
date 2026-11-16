"""Small SQLite storage layer for reports and paper ledgers."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Iterable

from edgelab.data.schemas import Signal, TradeDecision


class Storage:
    def __init__(self, sqlite_path: Path) -> None:
        sqlite_path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(sqlite_path)
        self.conn.row_factory = sqlite3.Row
        self._init()

    def _init(self) -> None:
        self.conn.execute(
            """
            CREATE TABLE IF NOT EXISTS signals (
                signal_id TEXT PRIMARY KEY,
                timestamp TEXT,
                market_id TEXT,
                event_id TEXT,
                outcome TEXT,
                category TEXT,
                fair_probability REAL,
                prediction_bid REAL,
                prediction_ask REAL,
                adjusted_edge REAL,
                action TEXT,
                reason TEXT
            )
            """
        )
        self.conn.execute(
            """
            CREATE TABLE IF NOT EXISTS paper_trades (
                timestamp TEXT,
                signal_id TEXT,
                market_id TEXT,
                event_id TEXT,
                outcome TEXT,
                side TEXT,
                price REAL,
                size REAL,
                fees REAL,
                approved INTEGER,
                reason TEXT
            )
            """
        )
        self.conn.commit()

    def insert_signals(self, signals: Iterable[Signal]) -> None:
        self.conn.executemany(
            """
            INSERT OR REPLACE INTO signals VALUES (
                :signal_id, :timestamp, :market_id, :event_id, :outcome, :category,
                :fair_probability, :prediction_bid, :prediction_ask, :adjusted_edge,
                :action, :reason
            )
            """,
            [
                {
                    **signal.model_dump(mode="json"),
                    "timestamp": signal.timestamp.isoformat(),
                }
                for signal in signals
            ],
        )
        self.conn.commit()

    def insert_trades(self, trades: Iterable[TradeDecision]) -> None:
        self.conn.executemany(
            """
            INSERT INTO paper_trades VALUES (
                :timestamp, :signal_id, :market_id, :event_id, :outcome, :side,
                :price, :size, :fees, :approved, :reason
            )
            """,
            [
                {
                    "timestamp": trade.timestamp.isoformat(),
                    "signal_id": trade.signal_id,
                    "market_id": trade.market_id,
                    "event_id": trade.event_id,
                    "outcome": trade.outcome,
                    "side": trade.side,
                    "price": trade.price,
                    "size": trade.size,
                    "fees": trade.fees,
                    "approved": int(trade.approved),
                    "reason": trade.reason,
                }
                for trade in trades
            ],
        )
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()
