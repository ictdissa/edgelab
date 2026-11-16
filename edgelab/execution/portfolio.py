"""Transactional paper portfolio. Inventory-only sales; no simulated naked shorts."""

from __future__ import annotations

import json
import math
import sqlite3
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Iterator

from edgelab.data.schemas import TradeDecision, ensure_utc


class PaperPortfolio:
    def __init__(self, starting_cash: float, path: Path | None = None) -> None:
        if not math.isfinite(starting_cash) or starting_cash <= 0:
            raise ValueError("starting cash must be finite and positive")
        if path:
            path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(
            str(path) if path else ":memory:", timeout=30, isolation_level=None
        )
        self.connection.row_factory = sqlite3.Row
        self._transaction_counter = 0
        self.connection.executescript("""
            CREATE TABLE IF NOT EXISTS cash (id INTEGER PRIMARY KEY CHECK(id=1), balance REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS holdings (token TEXT PRIMARY KEY, market TEXT, event TEXT,
                category TEXT, quantity REAL NOT NULL, cost REAL NOT NULL, bid REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS decisions (signal_id TEXT PRIMARY KEY, payload TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS settlements (token TEXT PRIMARY KEY, observed_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS quote_marks (token TEXT PRIMARY KEY, observed_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS quote_usage (token TEXT, observed_at TEXT, side TEXT, quantity REAL,
                PRIMARY KEY(token,observed_at,side));
            CREATE TABLE IF NOT EXISTS attempts (id INTEGER PRIMARY KEY AUTOINCREMENT, payload TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS risk_days (day TEXT PRIMARY KEY, opening_equity REAL NOT NULL,
                opened_at TEXT NOT NULL, loss_stop INTEGER NOT NULL DEFAULT 0);
            CREATE TABLE IF NOT EXISTS portfolio_metadata (key TEXT PRIMARY KEY, value REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS event_risk_keys (event TEXT PRIMARY KEY, risk_event TEXT NOT NULL);
        """)
        with self.transaction():
            self.connection.execute("INSERT OR IGNORE INTO cash VALUES (1, ?)", (starting_cash,))
            if "payout" not in {
                r[1] for r in self.connection.execute("PRAGMA table_info(settlements)")
            }:
                self.connection.execute("ALTER TABLE settlements ADD COLUMN payout REAL")
            initial = self.cash - sum(d.cash_change for d in self.decisions())
            self.connection.execute(
                "INSERT OR IGNORE INTO portfolio_metadata VALUES ('initial_cash', ?)", (initial,)
            )
            self.assert_invariants()

    @contextmanager
    def transaction(self) -> Iterator[None]:
        """Reuse an execution transaction, or make direct accounting calls atomic."""
        owner = not self.connection.in_transaction
        self._transaction_counter += 1
        savepoint = f"accounting_{self._transaction_counter}"
        if owner:
            self.connection.execute("BEGIN IMMEDIATE")
        else:
            self.connection.execute(f"SAVEPOINT {savepoint}")
        try:
            yield
            if owner:
                self.connection.execute("COMMIT")
            else:
                self.connection.execute(f"RELEASE SAVEPOINT {savepoint}")
        except Exception:
            if owner:
                self.connection.execute("ROLLBACK")
            else:
                self.connection.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
                self.connection.execute(f"RELEASE SAVEPOINT {savepoint}")
            raise

    @property
    def equity(self) -> float:
        value = self.connection.execute(
            "SELECT COALESCE(SUM(quantity*bid),0) FROM holdings"
        ).fetchone()[0]
        return self.cash + float(value)

    def begin_day(self, now: datetime) -> None:
        """UTC first-observation baseline, captured BEFORE the day's first mutation.

        Opening marks are the previous known bids, not an invented midnight quote.
        New risk is separately blocked when open holdings lack fresh marks.
        """
        now = ensure_utc(now)
        day = now.date().isoformat()
        latest = self.connection.execute("SELECT MAX(day) FROM risk_days").fetchone()[0]
        if latest and day < latest:
            raise ValueError("portfolio risk clock cannot move to an earlier UTC day")
        self.connection.execute(
            "INSERT OR IGNORE INTO risk_days(day,opening_equity,opened_at) VALUES (?,?,?)",
            (day, self.equity, now.isoformat()),
        )

    def is_settled(self, token: str) -> bool:
        return (
            self.connection.execute("SELECT 1 FROM settlements WHERE token=?", (token,)).fetchone()
            is not None
        )

    def stale_positions(self, now: datetime, max_age_seconds: int) -> list[str]:
        now = ensure_utc(now)
        return [
            row[0]
            for row in self.connection.execute("SELECT token FROM holdings WHERE quantity>0")
            if (stamp := self.quote_time(row[0])) is None
            or not 0 <= (now - stamp).total_seconds() <= max_age_seconds
        ]

    def assert_invariants(self) -> None:
        """Reconcile equity to funded cash flows and realized/cost-basis accounting."""
        if not math.isfinite(self.cash) or self.cash < -1e-7:
            raise ValueError("portfolio cash invariant failed")
        for row in self.connection.execute("SELECT * FROM holdings"):
            if any(not math.isfinite(row[k]) for k in ("quantity", "cost", "bid")):
                raise ValueError("nonfinite holding")
            if row["quantity"] < -1e-7 or row["cost"] < -1e-7 or not 0 <= row["bid"] <= 1:
                raise ValueError("invalid holding")
            if self.is_settled(row["token"]) and (row["quantity"] > 1e-7 or row["cost"] > 1e-7):
                raise ValueError("settled token has unresolved exposure")
        initial = float(
            self.connection.execute(
                "SELECT value FROM portfolio_metadata WHERE key='initial_cash'"
            ).fetchone()[0]
        )
        decisions = self.decisions()
        for decision in decisions:
            if (
                decision.approved
                and decision.risk_event_id
                and self.risk_key(decision.event_id) != decision.risk_event_id
            ):
                raise ValueError("canonical risk identity/accounting reconciliation failed")
        if not math.isclose(
            self.cash, initial + sum(d.cash_change for d in decisions), abs_tol=1e-7
        ):
            raise ValueError("cash/decision reconciliation failed")
        expected = initial + sum(d.realized_pnl for d in decisions) + self.unrealized_pnl()
        if not math.isclose(self.equity, expected, abs_tol=1e-7):
            raise ValueError("equity/PnL reconciliation failed")

    @property
    def cash(self) -> float:
        return float(self.connection.execute("SELECT balance FROM cash WHERE id=1").fetchone()[0])

    def inventory(self, token: str) -> float:
        row = self.connection.execute(
            "SELECT quantity FROM holdings WHERE token=?", (token,)
        ).fetchone()
        return float(row[0]) if row else 0.0

    def quote_time(self, token: str) -> datetime | None:
        row = self.connection.execute(
            "SELECT observed_at FROM quote_marks WHERE token=?", (token,)
        ).fetchone()
        return datetime.fromisoformat(row[0]) if row else None

    def mark(
        self,
        token: str,
        bid: float,
        observed_at: datetime | None = None,
        as_of: datetime | None = None,
    ) -> None:
        observed_at = ensure_utc(observed_at or datetime.now(UTC))
        as_of = ensure_utc(as_of or observed_at)
        if not math.isfinite(bid) or not 0 <= bid <= 1 or observed_at > as_of:
            raise ValueError("mark must be a finite binary price available as of now")
        with self.transaction():
            if self.is_settled(token):
                raise ValueError("cannot mark a settled token")
            previous = self.quote_time(token)
            if previous and observed_at < previous:
                return
            self.begin_day(as_of)
            self.connection.execute(
                "INSERT OR REPLACE INTO quote_marks VALUES (?,?)", (token, observed_at.isoformat())
            )
            self.connection.execute("UPDATE holdings SET bid=? WHERE token=?", (bid, token))

    def consumed(self, token: str, observed_at: datetime, side: str) -> float:
        row = self.connection.execute(
            "SELECT quantity FROM quote_usage WHERE token=? AND observed_at=? AND side=?",
            (token, observed_at.isoformat(), side),
        ).fetchone()
        return float(row[0]) if row else 0.0

    def exposures(self) -> tuple[dict[str, float], dict[str, float], float]:
        markets: dict[str, float] = {}
        events: dict[str, float] = {}
        total = 0.0
        for row in self.connection.execute(
            "SELECT h.*, COALESCE(k.risk_event,h.event) AS risk_event FROM holdings h "
            "LEFT JOIN event_risk_keys k ON k.event=h.event WHERE h.quantity>0"
        ):
            markets[row["market"]] = markets.get(row["market"], 0.0) + row["quantity"]
            events[row["risk_event"]] = events.get(row["risk_event"], 0.0) + row["quantity"]
            total += row["quantity"]
        return markets, events, total

    def risk_key(self, event: str) -> str | None:
        row = self.connection.execute(
            "SELECT risk_event FROM event_risk_keys WHERE event=?", (event,)
        ).fetchone()
        return str(row[0]) if row else None

    def bind_risk_event(self, event: str, risk_event: str) -> None:
        """Durably regroup existing holdings without rewriting accounting history."""
        if not event.strip() or not risk_event.strip():
            raise ValueError("risk identity requires nonempty identifiers")
        with self.transaction():
            previous = self.risk_key(event)
            if previous is not None and previous != risk_event:
                raise ValueError(
                    "canonical fixture identity changed; manual reconciliation required"
                )
            self.connection.execute(
                "INSERT OR IGNORE INTO event_risk_keys VALUES (?,?)", (event, risk_event)
            )

    def unrealized_pnl(self) -> float:
        return float(
            self.connection.execute(
                "SELECT COALESCE(SUM(quantity*bid-cost),0) FROM holdings"
            ).fetchone()[0]
        )

    def daily_pnl(self, now: datetime) -> float:
        now = ensure_utc(now)
        self.begin_day(now)
        opening = self.connection.execute(
            "SELECT opening_equity FROM risk_days WHERE day=?", (now.date().isoformat(),)
        ).fetchone()[0]
        return self.equity - float(opening)

    def loss_stopped(self, now: datetime, limit: float) -> bool:
        if not math.isfinite(limit) or limit <= 0:
            raise ValueError("daily loss limit must be finite and positive")
        with self.transaction():
            if self.daily_pnl(now) <= -limit:
                self.connection.execute(
                    "UPDATE risk_days SET loss_stop=1 WHERE day=?",
                    (ensure_utc(now).date().isoformat(),),
                )
            return bool(
                self.connection.execute(
                    "SELECT loss_stop FROM risk_days WHERE day=?",
                    (ensure_utc(now).date().isoformat(),),
                ).fetchone()[0]
            )

    def seen(self, signal_id: str) -> bool:
        return (
            self.connection.execute(
                "SELECT 1 FROM decisions WHERE signal_id=?", (signal_id,)
            ).fetchone()
            is not None
        )

    def record(self, decision: TradeDecision) -> None:
        self.connection.execute(
            "INSERT INTO decisions VALUES (?,?)", (decision.signal_id, decision.model_dump_json())
        )

    def fill(self, decision: TradeDecision, bid: float) -> None:
        with self.transaction():
            self.begin_day(decision.timestamp)
            self._fill(decision, bid)
            self.assert_invariants()

    def _fill(self, decision: TradeDecision, bid: float) -> None:
        token = decision.token_id
        if (
            not token
            or decision.price is None
            or decision.size <= 0
            or not decision.approved
            or decision.paper_or_live != "paper"
            or decision.side not in {"BUY_YES", "SELL_YES"}
            or not math.isfinite(bid)
            or not 0 <= bid <= 1
        ):
            raise ValueError("fill requires an approved funded paper trade and valid quote")
        if self.is_settled(token):
            raise ValueError("cannot fill a settled token")
        if self.seen(decision.signal_id):
            raise ValueError("duplicate accounting fill")
        if decision.risk_event_id:
            self.bind_risk_event(decision.event_id, decision.risk_event_id)
        elif self.risk_key(decision.event_id) is not None:
            raise ValueError("cannot drop an established canonical risk identity")
        row = self.connection.execute("SELECT * FROM holdings WHERE token=?", (token,)).fetchone()
        if row and (row["market"], row["event"], row["category"]) != (
            decision.market_id,
            decision.event_id,
            decision.category,
        ):
            raise ValueError("token identity changed")
        quantity, cost = (float(row["quantity"]), float(row["cost"])) if row else (0.0, 0.0)
        drag = decision.fees + decision.slippage
        if decision.side == "BUY_YES":
            delta = -decision.price * decision.size - drag
            quantity += decision.size
            cost -= delta
        else:
            if decision.size > quantity + 1e-9:
                raise ValueError("naked sales are not funded")
            released = cost * decision.size / quantity
            delta = decision.price * decision.size - drag
            quantity -= decision.size
            cost -= released
            decision.realized_pnl = delta - released
        if self.cash + delta < -1e-9:
            raise ValueError("insufficient paper cash")
        decision.cash_change = delta
        decision.net_entry_price = (
            -delta / decision.size if decision.side == "BUY_YES" else delta / decision.size
        )
        self.connection.execute("UPDATE cash SET balance=balance+? WHERE id=1", (delta,))
        self.connection.execute(
            "INSERT OR REPLACE INTO holdings VALUES (?,?,?,?,?,?,?)",
            (
                token,
                decision.market_id,
                decision.event_id,
                decision.category,
                max(0, quantity),
                max(0, cost),
                bid,
            ),
        )
        if decision.orderbook_timestamp:
            if decision.orderbook_timestamp > decision.timestamp:
                raise ValueError("fill quote is from the future")
            consumed = self.consumed(token, decision.orderbook_timestamp, decision.side)
            self.connection.execute(
                "INSERT OR REPLACE INTO quote_usage VALUES (?,?,?,?)",
                (
                    token,
                    decision.orderbook_timestamp.isoformat(),
                    decision.side,
                    consumed + decision.size,
                ),
            )
        self.record(decision)

    def settle(self, token: str, payout: float, observed_at: datetime, as_of: datetime) -> None:
        observed_at, as_of = ensure_utc(observed_at), ensure_utc(as_of)
        if observed_at > as_of or payout not in {0.0, 1.0}:
            raise ValueError("settlement must be binary and available at decision time")
        if not token:
            raise ValueError("settlement requires token identity")
        with self.transaction():
            self.begin_day(as_of)
            previous = self.connection.execute(
                "SELECT payout FROM settlements WHERE token=?", (token,)
            ).fetchone()
            if previous:
                if previous[0] is not None and previous[0] != payout:
                    raise ValueError("conflicting settlement payout")
                return
            row = self.connection.execute(
                "SELECT * FROM holdings WHERE token=?", (token,)
            ).fetchone()
            self.connection.execute(
                "INSERT INTO settlements(token,observed_at,payout) VALUES (?,?,?)",
                (token, observed_at.isoformat(), payout),
            )
            if not row:
                return
            self.connection.execute(
                "UPDATE cash SET balance=balance+? WHERE id=1", (row["quantity"] * payout,)
            )
            self.connection.execute(
                "UPDATE holdings SET quantity=0,cost=0,bid=? WHERE token=?", (payout, token)
            )
            self.record(
                TradeDecision(
                    timestamp=as_of,
                    signal_id=f"settlement:{token}",
                    market_id=row["market"],
                    event_id=row["event"],
                    risk_event_id=self.risk_key(row["event"]),
                    outcome="settled contract",
                    token_id=token,
                    category=row["category"],
                    side="SKIP",
                    price=payout,
                    size=row["quantity"],
                    cash_change=row["quantity"] * payout,
                    realized_pnl=row["quantity"] * payout - row["cost"],
                    approved=True,
                    reason="observed binary settlement",
                )
            )
            self.assert_invariants()

    def decisions(self) -> list[TradeDecision]:
        return [
            TradeDecision.model_validate(json.loads(row[0]))
            for row in self.connection.execute("SELECT payload FROM decisions ORDER BY rowid")
        ]
