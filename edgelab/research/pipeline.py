"""End-to-end mock research pipeline."""

from __future__ import annotations

import csv
import logging
from datetime import UTC, datetime
from pathlib import Path

from edgelab.config.settings import Settings
from edgelab.data.external_sources.mock_provider import MockProvider
from edgelab.data.schemas import BestBidAsk, FairValue, PredictionMarket, Signal, TradeDecision
from edgelab.execution.paper_trader import PaperTrader, write_ledger
from edgelab.fair_value.crypto_threshold_model import CryptoThresholdModel
from edgelab.fair_value.sports_odds_model import SportsOddsModel
from edgelab.fair_value.weather_model import WeatherThresholdModel
from edgelab.matching.event_resolver import EventMatch, resolve_events, write_event_match_audit
from edgelab.research.analysis_report import render_research_report
from edgelab.strategies.taker_edge import generate_signals

LOGGER = logging.getLogger(__name__)


class MockResearchRun:
    def __init__(
        self, settings: Settings | None = None, provider: MockProvider | None = None
    ) -> None:
        self.settings = settings or Settings()
        provider = provider or MockProvider()
        self.markets = provider.markets()
        self.orderbooks = provider.orderbooks()
        self.anchors = provider.anchors()
        self.as_of = self._sample_as_of()
        LOGGER.info(
            "mock_research_loaded markets=%d orderbooks=%d anchors=%d as_of=%s",
            len(self.markets),
            len(self.orderbooks),
            len(self.anchors),
            self.as_of.isoformat(),
        )

    def _sample_as_of(self) -> datetime:
        timestamps = [book.timestamp for book in self.orderbooks]
        timestamps.extend(anchor.timestamp for anchor in self.anchors)
        return max(timestamps) if timestamps else datetime.now(UTC)

    def fair_values(self) -> list[FairValue]:
        values: list[FairValue] = []
        for model in (
            SportsOddsModel(self.settings, as_of=self.as_of),
            CryptoThresholdModel(self.settings, as_of=self.as_of),
            WeatherThresholdModel(self.settings, as_of=self.as_of),
        ):
            values.extend(model.compute(self.markets, self.anchors, self.orderbooks))
        return values

    def best_bid_asks(self) -> list[BestBidAsk]:
        return [book.best_bid_ask() for book in self.orderbooks]

    def matches(self) -> list[EventMatch]:
        return resolve_events(self.markets, self.anchors, as_of=self.as_of)

    def signals(self) -> list[Signal]:
        return generate_signals(
            self.markets,
            self.fair_values(),
            self.best_bid_asks(),
            self.settings,
            as_of=self.as_of,
        )

    def paper_trades(self, signals: list[Signal] | None = None) -> list[TradeDecision]:
        market_by_id: dict[str, PredictionMarket] = {
            market.market_id: market for market in self.markets
        }
        return PaperTrader(self.settings).execute(
            signals or self.signals(), market_by_id, as_of=self.as_of
        )

    def report(self) -> str:
        fair_values = self.fair_values()
        signals = generate_signals(
            self.markets,
            fair_values,
            self.best_bid_asks(),
            self.settings,
            as_of=self.as_of,
        )
        trades = self.paper_trades(signals)
        return render_research_report(self.markets, self.matches(), fair_values, signals, trades)


def write_signals_csv(path: Path, signals: list[Signal]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    columns = list(Signal.model_fields)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns)
        writer.writeheader()
        for signal in signals:
            writer.writerow(signal.model_dump(mode="json"))


def write_mock_report(output: Path, settings: Settings | None = None) -> None:
    run = MockResearchRun(settings)
    output.parent.mkdir(parents=True, exist_ok=True)
    audit = output.with_name("mock_event_match_audit.csv")
    output.write_text(
        run.report().replace("reports/event_match_audit.csv", str(audit)), encoding="utf-8"
    )
    write_event_match_audit(audit, run.matches())


def run_mock_paper_trader(ledger: Path, settings: Settings | None = None) -> list[TradeDecision]:
    run = MockResearchRun(settings)
    trades = run.paper_trades()
    write_ledger(ledger, trades, append=False)
    return trades
