from __future__ import annotations

from datetime import UTC, datetime, timedelta

from edgelab.config.settings import Settings
from edgelab.data.schemas import PredictionMarket, Signal
from edgelab.execution.paper_trader import PaperTrader


def _market() -> PredictionMarket:
    return PredictionMarket(
        market_id="m1",
        event_id="e1",
        risk_event_id="fixture:odds_api_io:test-event",
        category="sports_1x2",
        title="Will Arsenal beat Chelsea?",
        outcomes=["Yes", "No"],
        token_ids=["yes", "no"],
        resolution_source="official",
        raw_metadata={
            "settlement_spec": dict(
                review_status="reviewed",
                period="regulation_time",
                includes_overtime=False,
                draw_is_outcome=True,
                cancellation_rule="synthetic-identical-v1",
                evidence_reference="unit-test reviewed rules",
                reviewed_at=(datetime.now(UTC) - timedelta(days=1)).isoformat(),
            )
        },
    )


def _signal(action: str, risk_approved: bool = True) -> Signal:
    now = datetime.now(UTC)
    return Signal(
        signal_id=f"sig-{action}",
        timestamp=now,
        market_id="m1",
        event_id="e1",
        risk_event_id="fixture:odds_api_io:test-event",
        outcome="Yes",
        category="sports_1x2",
        settlement_verified=True,
        fair_probability=0.60 if action == "BUY_YES" else 0.35,
        prediction_bid=0.45,
        prediction_ask=0.50,
        raw_edge=0.10,
        cost_drag=0.01,
        adjusted_edge=0.09,
        available_size=20,
        confidence=0.8,
        action=action,
        reason="test",
        fair_value_source="sports_no_vig:1books",
        fair_value_provider="odds_api_io",
        fair_value_timestamp=now,
        orderbook_timestamp=now,
        freshness_status="fresh",
        match_score=0.95,
        risk_approved=risk_approved,
        token_id="yes",
        match_decision="MATCH",
    )


def test_paper_trader_uses_ask_and_bid_never_midpoint() -> None:
    settings = Settings(MIN_LIQUIDITY=1, MIN_ADJUSTED_EDGE=0.01)
    market = _market()
    trades = PaperTrader(settings).execute(
        [_signal("BUY_YES"), _signal("SELL_YES")],
        {"m1": market},
        as_of=datetime.now(UTC),
    )

    assert trades[0].approved
    assert trades[0].price == 0.50
    assert trades[0].execution_price_source == "ask"
    assert trades[1].approved
    assert trades[1].price == 0.45
    assert trades[1].execution_price_source == "bid"
    assert trades[0].fair_value_provider == "odds_api_io"
    assert trades[0].match_score == 0.95


def test_paper_trader_skips_live_signal_without_risk_approval() -> None:
    settings = Settings(MIN_LIQUIDITY=1, MIN_ADJUSTED_EDGE=0.01)
    trade = PaperTrader(settings).execute(
        [_signal("BUY_YES", risk_approved=False)],
        {"m1": _market()},
        as_of=datetime.now(UTC),
    )[0]

    assert not trade.approved
    assert trade.side == "SKIP"
    assert "risk_approved=false" in trade.reason


def test_reviewed_sports_rules_do_not_replace_canonical_risk_identity() -> None:
    market = _market().model_copy(update={"risk_event_id": None})
    signal = _signal("BUY_YES").model_copy(update={"risk_event_id": None})
    trader = PaperTrader()
    trade = trader.execute([signal], {market.market_id: market}, datetime.now(UTC))[0]
    assert not trade.approved
    assert "canonical fixture risk identity unverified" in trade.reason
    trader.portfolio.connection.close()
