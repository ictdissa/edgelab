from __future__ import annotations

from datetime import UTC, datetime

from sharpedge.config.settings import Settings
from sharpedge.data.schemas import BestBidAsk, FairValue, Market
from sharpedge.strategies.cross_market_arbitrage import detect_cross_market_inconsistencies


def test_mma_method_cannot_exceed_moneyline() -> None:
    as_of = datetime(2026, 5, 16, tzinfo=UTC)
    markets = [
        Market(
            market_id="ml",
            event_id="fight",
            sport="MMA",
            league="UFC",
            title="Fighter A vs Fighter B moneyline",
            outcomes=["Fighter A", "Fighter B"],
            token_ids=["a", "b"],
            start_time=datetime(2026, 6, 1, tzinfo=UTC),
            end_time=datetime(2026, 6, 1, 3, tzinfo=UTC),
            resolution_source="official",
            status="active",
            raw_metadata={},
        ),
        Market(
            market_id="method",
            event_id="fight",
            sport="MMA",
            league="UFC",
            title="Fighter A vs Fighter B method of victory",
            outcomes=["Fighter A by KO/TKO", "Fighter B by Decision"],
            token_ids=["a_ko", "b_dec"],
            start_time=datetime(2026, 6, 1, tzinfo=UTC),
            end_time=datetime(2026, 6, 1, 3, tzinfo=UTC),
            resolution_source="official",
            status="active",
            raw_metadata={},
        ),
    ]
    fair_values = [
        FairValue(event_id="fight", market_id="ml", outcome="Fighter A", fair_probability=0.55, source="test", timestamp=as_of, uncertainty=0.01),
        FairValue(event_id="fight", market_id="ml", outcome="Fighter B", fair_probability=0.45, source="test", timestamp=as_of, uncertainty=0.01),
        FairValue(event_id="fight", market_id="method", outcome="Fighter A by KO/TKO", fair_probability=0.60, source="test", timestamp=as_of, uncertainty=0.01),
        FairValue(event_id="fight", market_id="method", outcome="Fighter B by Decision", fair_probability=0.40, source="test", timestamp=as_of, uncertainty=0.01),
    ]
    bbas = [
        BestBidAsk(
            market_id="method",
            token_id="a_ko",
            timestamp=as_of,
            best_bid=0.58,
            best_bid_size=50,
            best_ask=0.61,
            best_ask_size=50,
            mid=0.595,
            spread=0.03,
        )
    ]
    results = detect_cross_market_inconsistencies(markets, fair_values, bbas, Settings())
    assert any(item.inconsistency_type == "mma_method_exceeds_moneyline" for item in results)


def test_low_liquidity_inconsistency_is_theoretical_only() -> None:
    as_of = datetime(2026, 5, 16, tzinfo=UTC)
    market = Market(
        market_id="sum",
        event_id="event",
        sport="Soccer",
        league="EPL",
        title="Match winner",
        outcomes=["Home", "Draw", "Away"],
        token_ids=["h", "d", "a"],
        start_time=datetime(2026, 6, 1, tzinfo=UTC),
        end_time=datetime(2026, 6, 1, 3, tzinfo=UTC),
        resolution_source="official",
        status="active",
        raw_metadata={},
    )
    fair_values = [
        FairValue(event_id="event", market_id="sum", outcome="Home", fair_probability=0.50, source="test", timestamp=as_of, uncertainty=0.01),
        FairValue(event_id="event", market_id="sum", outcome="Draw", fair_probability=0.35, source="test", timestamp=as_of, uncertainty=0.01),
        FairValue(event_id="event", market_id="sum", outcome="Away", fair_probability=0.30, source="test", timestamp=as_of, uncertainty=0.01),
    ]
    results = detect_cross_market_inconsistencies([market], fair_values, [], Settings())
    assert results
    assert results[0].theoretical_only
    assert not results[0].executable_after_costs
