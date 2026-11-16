from __future__ import annotations

from datetime import UTC, datetime

from edgelab.config.settings import Settings
from edgelab.data.polymarket_collector import parse_gamma_market
from edgelab.data.schemas import ExternalAnchor, OrderBook, PredictionMarket, PriceLevel
from scripts.collect_research_snapshots import build_current_category_signal_rows


NOW = datetime(2026, 5, 20, tzinfo=UTC)


def test_polymarket_description_resolution_source_is_normalized() -> None:
    market = parse_gamma_market(
        {
            "conditionId": "m1",
            "eventId": "e1",
            "question": "Will Bitcoin be above $100,000 on June 30, 2026?",
            "description": (
                "This market resolves to Yes if BTC is above the threshold. "
                "The resolution source for Bitcoin is Binance BTCUSDT 1 minute candles."
            ),
            "outcomes": '["Yes","No"]',
            "clobTokenIds": '["yes","no"]',
            "active": True,
            "closed": False,
            "endDate": "2026-06-30T23:59:00Z",
            "resolutionSource": "",
        }
    )

    assert market is not None
    assert "Binance" in (market.resolution_source or "")


def test_current_local_crypto_snapshot_generates_fresh_signal_rows() -> None:
    market = PredictionMarket(
        market_id="m1",
        event_id="e1",
        category="crypto_threshold",
        title="Will Bitcoin be above $90,000 on June 30, 2026?",
        description="The resolution source for Bitcoin is Binance BTCUSDT candles.",
        outcomes=["Yes", "No"],
        token_ids=["yes", "no"],
        end_time=datetime(2026, 6, 30, tzinfo=UTC),
        resolution_source="Binance BTCUSDT",
    )
    anchor = ExternalAnchor(
        source_id="btc",
        event_id="e1",
        source="crypto_spot",
        category="crypto_threshold",
        outcome="Yes",
        timestamp=NOW,
        metadata={"spot": 100_000, "threshold": 90_000, "volatility": 0.50},
    )
    book = OrderBook(
        market_id="m1",
        token_id="yes",
        timestamp=NOW,
        bids=[PriceLevel(price=0.18, size=25)],
        asks=[PriceLevel(price=0.20, size=25)],
    )

    rows = build_current_category_signal_rows(
        "crypto",
        [market],
        [book],
        [anchor],
        NOW,
        Settings(MIN_LIQUIDITY=1, MIN_ADJUSTED_EDGE=0.01, MAX_SPREAD=0.10),
    )

    assert rows
    assert rows[0]["source"] == "current_local_snapshot"
    assert rows[0]["freshness_status"] == "fresh"
    assert "unclear resolution" not in rows[0]["risk_reasons"]
