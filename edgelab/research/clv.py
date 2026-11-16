"""Closing-line-value tracking helpers."""

from __future__ import annotations

import csv
import json
import hashlib
import math
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from edgelab.data.schemas import ensure_utc


def finite_number(value: float, name: str, low: float = 0, high: float = 1) -> None:
    if not math.isfinite(value) or not low <= value <= high:
        raise ValueError(f"invalid {name}: expected finite value in [{low}, {high}]")


@dataclass(frozen=True)
class PaperTrade:
    paper_trade_id: str
    timestamp: datetime
    category: str
    market_id: str
    outcome: str
    side: str
    execution_price: float
    token_id: str = ""
    size: float = 1.0
    fees: float | None = None
    slippage: float | None = None
    exit_fee_bps: float | None = None
    exit_slippage_bps: float | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "timestamp", ensure_utc(self.timestamp))
        if not self.paper_trade_id or not self.market_id or not self.outcome:
            raise ValueError("trade requires stable identity")
        if self.side not in {"BUY_YES", "SELL_YES"}:
            raise ValueError("unsupported trade side")
        finite_number(self.execution_price, "entry price")
        finite_number(self.size, "size", 0, float("inf"))
        if self.size <= 0:
            raise ValueError("trade size must be positive")
        for name in ("fees", "slippage", "exit_fee_bps", "exit_slippage_bps"):
            if (value := getattr(self, name)) is not None:
                finite_number(value, name, 0, float("inf"))


@dataclass(frozen=True)
class SnapshotSignal:
    timestamp: datetime
    category: str
    market_id: str
    outcome: str
    fair_probability: float | None
    mid: float | None
    bid: float | None = None
    ask: float | None = None
    token_id: str = ""
    quote_timestamp: datetime | None = None
    bid_size: float | None = None
    ask_size: float | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "timestamp", ensure_utc(self.timestamp))
        if self.quote_timestamp is not None:
            object.__setattr__(self, "quote_timestamp", ensure_utc(self.quote_timestamp))
            if self.quote_timestamp > self.timestamp:
                raise ValueError("quote source time exceeds observation time")
        for name in ("fair_probability", "mid", "bid", "ask"):
            if (value := getattr(self, name)) is not None:
                finite_number(value, name)
        for name in ("bid_size", "ask_size"):
            if (value := getattr(self, name)) is not None:
                finite_number(value, name, 0, float("inf"))
        if self.bid is not None and self.ask is not None and self.bid > self.ask:
            raise ValueError("crossed follow-up book")

    @property
    def clv_reference(self) -> tuple[float | None, str]:
        if self.mid is not None:
            return self.mid, "mid"
        return (
            (self.fair_probability, "fair_probability")
            if self.fair_probability is not None
            else (None, "")
        )

    def conservative_exit(self, side: str) -> tuple[float | None, str]:
        if side == "BUY_YES":
            return self.bid, "FUTURE_BID"
        if side == "SELL_YES":
            return self.ask, "FUTURE_ASK"
        return None, ""


class ObservationStore:
    """Append-only follow-up revisions; CSV projections can be rebuilt after a crash."""

    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(path, timeout=30)
        self.connection.execute(
            "CREATE TABLE IF NOT EXISTS observations (digest TEXT PRIMARY KEY, trade_id TEXT NOT NULL, "
            "horizon TEXT NOT NULL, recorded_at TEXT NOT NULL, payload TEXT NOT NULL)"
        )
        self.connection.commit()

    def append(self, rows: list[dict[str, str]]) -> None:
        with self.connection:
            for row in rows:
                if not row.get("paper_trade_id") or not row.get("horizon"):
                    raise ValueError("observation requires trade and horizon identity")
                payload = json.dumps(row, sort_keys=True, allow_nan=False)
                digest = hashlib.sha256(payload.encode()).hexdigest()
                self.connection.execute(
                    "INSERT OR IGNORE INTO observations VALUES (?,?,?,?,?)",
                    (
                        digest,
                        row["paper_trade_id"],
                        row["horizon"],
                        datetime.now(UTC).isoformat(),
                        payload,
                    ),
                )

    def close(self) -> None:
        self.connection.close()


@dataclass(frozen=True)
class CLVRecord:
    signal_id: str
    entry_price: float
    later_fair_value: float
    side: str

    @property
    def clv(self) -> float:
        if self.side == "BUY_YES":
            return self.later_fair_value - self.entry_price
        if self.side == "SELL_YES":
            return self.entry_price - self.later_fair_value
        return 0.0


def summarize_clv(records: list[CLVRecord]) -> dict[str, float]:
    values = [record.clv for record in records]
    if not values:
        raise ValueError("CLV summary requires actual observations")
    values_sorted = sorted(values)
    mid = len(values_sorted) // 2
    median = (
        values_sorted[mid]
        if len(values_sorted) % 2
        else (values_sorted[mid - 1] + values_sorted[mid]) / 2
    )
    return {"average_clv": sum(values) / len(values), "median_clv": median}


def approved_trade_count_from_ledger(path: Path) -> int:
    if not path.exists():
        return 0
    with path.open(newline="", encoding="utf-8") as handle:
        return sum(1 for row in csv.DictReader(handle) if row.get("approved") == "True")


def render_clv_report(records: list[CLVRecord], approved_trade_count: int) -> str:
    lines = [
        "# EdgeLab CLV Report",
        "",
        f"- Approved paper trades in ledger: {approved_trade_count}",
        f"- Trades with follow-up CLV observations: {len(records)}",
        "",
    ]
    if not records:
        lines.extend(
            [
                "No CLV is reported yet.",
                "",
                "Reason: CLV requires an approved paper trade and a later fair-value or "
                "prediction-market snapshot at 5m, 30m, 1h, or pre-close.",
                "",
                "This avoids implying profitability from mock data.",
            ]
        )
        return "\n".join(lines)
    summary = summarize_clv(records)
    lines.extend(
        [
            f"- Average CLV: {summary['average_clv']:.4f}",
            f"- Median CLV: {summary['median_clv']:.4f}",
            "- CLV by market type: requires real ledger metadata",
            "- CLV by fair-value source: requires real ledger metadata",
            "- False positives: requires resolved outcomes",
        ]
    )
    return "\n".join(lines)
