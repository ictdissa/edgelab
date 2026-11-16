"""Reusable research pipeline for scripts and dashboard."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from sharpedge.config.settings import Settings
from sharpedge.data.odds_collector import MockOddsProvider, OddsAPIProvider
from sharpedge.data.polymarket_collector import PolymarketCollector
from sharpedge.data.sample_loader import (
    latest_mock_timestamp,
    load_mock_markets,
    load_mock_odds,
    load_mock_orderbooks,
)
from sharpedge.data.schemas import BestBidAsk, CrossMarketInconsistency, ExternalOdds, FairValue, Market, OrderBook, Signal
from sharpedge.data.storage import Storage
from sharpedge.research.fair_value_model import FairValueModel
from sharpedge.strategies.cross_market_arbitrage import detect_cross_market_inconsistencies
from sharpedge.strategies.taker_edge import generate_edge_signals


@dataclass
class ResearchSnapshot:
    markets: list[Market]
    orderbooks: list[OrderBook]
    odds: list[ExternalOdds]
    fair_values: list[FairValue]
    best_bid_asks: list[BestBidAsk]
    signals: list[Signal]
    inconsistencies: list[CrossMarketInconsistency]
    as_of: datetime


def build_research_snapshot(
    settings: Settings,
    mock: bool = True,
    as_of: datetime | None = None,
    persist: bool = False,
    odds_file: Path | None = None,
) -> ResearchSnapshot:
    storage = Storage(settings.database_url) if persist else None
    try:
        if mock:
            markets = load_mock_markets()
            orderbooks = load_mock_orderbooks()
            odds = load_mock_odds()
            as_of = as_of or latest_mock_timestamp(orderbooks, odds)
        else:
            collector = PolymarketCollector(settings=settings, storage=storage, mock=False)
            markets, orderbooks = collector.collect_snapshot()
            provider = (
                MockOddsProvider(odds_file)
                if odds_file
                else OddsAPIProvider(settings.odds_api_key, settings.odds_api_base)
            )
            odds = provider.fetch_odds(as_of=as_of)
            if as_of is None:
                as_of = latest_mock_timestamp(orderbooks, odds)
        model = FairValueModel(
            base_uncertainty_bps=settings.model_uncertainty_bps,
            now=as_of,
        )
        fair_values = model.compute_fair_values(markets, odds, orderbooks, as_of=as_of)
        best_bid_asks = [book.best_bid_ask() for book in orderbooks if book.timestamp <= as_of]
        signals = generate_edge_signals(markets, fair_values, best_bid_asks, settings, as_of=as_of)
        inconsistencies = detect_cross_market_inconsistencies(
            markets, fair_values, best_bid_asks, settings
        )
        if storage:
            storage.insert_markets(markets)
            storage.insert_orderbooks(orderbooks)
            storage.insert_external_odds(odds)
            storage.insert_fair_values(fair_values)
            for signal in signals:
                storage.insert_signal(signal)
        return ResearchSnapshot(
            markets=markets,
            orderbooks=orderbooks,
            odds=odds,
            fair_values=fair_values,
            best_bid_asks=best_bid_asks,
            signals=signals,
            inconsistencies=inconsistencies,
            as_of=as_of,
        )
    finally:
        if storage:
            storage.close()
