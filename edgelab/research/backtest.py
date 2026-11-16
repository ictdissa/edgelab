"""Chronological, funded paper replay with observation/availability guards."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Sequence
from dataclasses import dataclass, field

from edgelab.data.schemas import ExternalAnchor, OrderBook, PredictionMarket, Signal, TradeDecision
from edgelab.config.settings import Settings
from edgelab.execution.paper_trader import PaperTrader
from edgelab.fair_value.crypto_threshold_model import CryptoThresholdModel
from edgelab.fair_value.sports_odds_model import SportsOddsModel
from edgelab.fair_value.weather_model import WeatherThresholdModel
from edgelab.strategies.taker_edge import generate_signals


def ensure_aware_utc(value: datetime) -> datetime:
    return value.astimezone(UTC) if value.tzinfo else value.replace(tzinfo=UTC)


def point_in_time_filter(
    as_of: datetime,
    anchors: Sequence[ExternalAnchor],
    orderbooks: Sequence[OrderBook],
    require_receipt: bool = True,
) -> tuple[list[ExternalAnchor], list[OrderBook]]:
    as_of = ensure_aware_utc(as_of)
    return (
        [
            anchor
            for anchor in anchors
            if anchor.timestamp <= as_of
            and (anchor.available_at or anchor.timestamp) <= as_of
            and (not require_receipt or anchor.available_at is not None)
        ],
        [
            book
            for book in orderbooks
            if book.timestamp <= as_of
            and (book.available_at or book.timestamp) <= as_of
            and (not require_receipt or book.available_at is not None)
        ],
    )


def assert_no_lookahead(
    as_of: datetime,
    anchors: Sequence[ExternalAnchor],
    orderbooks: Sequence[OrderBook],
) -> None:
    as_of = ensure_aware_utc(as_of)
    if any(a.available_at is None for a in anchors) or any(
        b.available_at is None for b in orderbooks
    ):
        raise ValueError("Missing receipt timestamps; no-lookahead cannot be established")
    future_anchors = [
        anchor.source_id
        for anchor in anchors
        if max(anchor.timestamp, anchor.available_at or anchor.timestamp) > as_of
    ]
    future_books = [
        book.market_id
        for book in orderbooks
        if max(book.timestamp, book.available_at or book.timestamp) > as_of
    ]
    if future_anchors or future_books:
        raise ValueError(
            "Lookahead data detected: "
            f"future_anchors={future_anchors}, future_orderbooks={future_books}"
        )


@dataclass(frozen=True)
class Settlement:
    token_id: str
    payout: float
    available_at: datetime

    def __post_init__(self) -> None:
        object.__setattr__(self, "available_at", ensure_aware_utc(self.available_at))
        if not self.token_id or self.payout not in {0.0, 1.0}:
            raise ValueError("settlement requires exact token and binary payout")


@dataclass
class ReplayResult:
    signals: list[Signal] = field(default_factory=list)
    decisions: list[TradeDecision] = field(default_factory=list)
    equity: list[tuple[datetime, float]] = field(default_factory=list)
    equity_marks_fresh: list[bool] = field(default_factory=list)


@dataclass(frozen=True)
class MarketObservation:
    market: PredictionMarket
    observed_at: datetime
    available_at: datetime

    def __post_init__(self) -> None:
        object.__setattr__(self, "market", self.market.model_copy(deep=True))
        object.__setattr__(self, "observed_at", ensure_aware_utc(self.observed_at))
        object.__setattr__(self, "available_at", ensure_aware_utc(self.available_at))
        if self.observed_at > self.available_at:
            raise ValueError("market receipt cannot precede its source time")


def market_universe_as_of(
    markets: Sequence[PredictionMarket],
    history: Sequence[MarketObservation],
    now: datetime,
) -> dict[str, PredictionMarket]:
    versions = list(history)
    covered = {
        v.market.market_id for v in history if v.available_at <= now and v.observed_at <= now
    }
    for market in markets:
        if market.market_id in covered:
            continue
        stamp = market.raw_metadata.get("collected_at")
        if stamp:
            observed = ensure_aware_utc(datetime.fromisoformat(str(stamp)))
            versions.append(MarketObservation(market, observed, observed))
        elif market.raw_metadata.get("synthetic_data") is True:
            versions.append(MarketObservation(market, now, now))
    result: dict[str, PredictionMarket] = {}
    seen: dict[tuple[str, datetime, datetime], PredictionMarket] = {}
    for version in sorted(versions, key=lambda v: (v.observed_at, v.available_at)):
        if max(version.available_at, version.observed_at) > now:
            continue
        key = (version.market.market_id, version.available_at, version.observed_at)
        if key in seen and seen[key] != version.market:
            raise ValueError("conflicting same-vintage market metadata")
        seen[key] = version.market
        result[version.market.market_id] = version.market
    return result


def replay(
    markets: Sequence[PredictionMarket],
    anchors: Sequence[ExternalAnchor],
    orderbooks: Sequence[OrderBook],
    settings: Settings,
    ticks: Sequence[datetime],
    settlements: Sequence[Settlement] = (),
    market_history: Sequence[MarketObservation] = (),
) -> ReplayResult:
    trader = PaperTrader(settings)
    result = ReplayResult()
    settled: set[str] = set()
    for now in sorted(set(ensure_aware_utc(t) for t in ticks)):
        trader.portfolio.begin_day(now)
        market_by_id = market_universe_as_of(markets, market_history, now)
        available_anchors, available_books = point_in_time_filter(
            now, anchors, orderbooks, require_receipt=True
        )
        assert_no_lookahead(now, available_anchors, available_books)
        latest: dict[tuple[str, str], OrderBook] = {}
        for book in sorted(
            available_books, key=lambda b: (b.timestamp, b.available_at or b.timestamp)
        ):
            previous = latest.get((book.market_id, book.token_id))
            if (
                previous
                and (previous.timestamp, previous.available_at)
                == (book.timestamp, book.available_at)
                and previous != book
            ):
                raise ValueError("conflicting same-vintage order books")
            latest[(book.market_id, book.token_id)] = book
        for resolution in settlements:
            if resolution.available_at <= now:
                trader.portfolio.settle(
                    resolution.token_id, resolution.payout, resolution.available_at, now
                )
                settled.add(resolution.token_id)
        books = [b for b in latest.values() if b.token_id not in settled]
        for book in books:
            quote = book.best_bid_ask()
            if quote.best_bid is not None and (
                quote.best_ask is None or quote.best_bid <= quote.best_ask
            ):
                trader.portfolio.mark(book.token_id, quote.best_bid, book.timestamp, now)
        active = [m for m in market_by_id.values() if m.status == "active"]
        values = []
        for model in (
            SportsOddsModel(settings, now),
            CryptoThresholdModel(settings, now),
            WeatherThresholdModel(settings, now),
        ):
            values.extend(model.compute(active, available_anchors, books))
        values = [
            v for v in values if market_by_id[v.market_id].token_for(v.outcome) not in settled
        ]
        signals = generate_signals(active, values, [b.best_bid_ask() for b in books], settings, now)
        result.signals.extend(signals)
        result.decisions.extend(trader.execute(signals, market_by_id, now))
        holdings_value = float(
            trader.portfolio.connection.execute(
                "SELECT COALESCE(SUM(quantity*bid),0) FROM holdings"
            ).fetchone()[0]
        )
        result.equity.append((now, trader.portfolio.cash + holdings_value))
        result.equity_marks_fresh.append(
            not trader.portfolio.stale_positions(
                now, min(settings.max_orderbook_age_seconds, settings.max_data_age_seconds)
            )
        )
    return result
