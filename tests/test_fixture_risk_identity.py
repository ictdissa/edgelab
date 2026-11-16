"""Risk joins must be durable, provider-scoped and independent of venue wrappers."""

from datetime import UTC, datetime, timedelta

import pytest

from edgelab.config.settings import Settings
from edgelab.data.schemas import ExternalAnchor, PredictionMarket, Signal, TradeDecision
from edgelab.execution.paper_trader import PaperTrader
from edgelab.execution.portfolio import PaperPortfolio
from edgelab.matching.event_resolver import EventMatch
from edgelab.matching.risk_identity import bind_fixture_risk

NOW = datetime(2026, 10, 13, 12, tzinfo=UTC)
KEY = "fixture:odds_api_io:external-1"


def market(name="a", risk=KEY):
    return PredictionMarket(
        market_id=name,
        event_id=f"venue-{name}",
        risk_event_id=risk,
        category="sports_1x2",
        title="Arsenal vs Chelsea match winner",
        outcomes=["Yes", "No"],
        token_ids=[name, name + "-no"],
        resolution_source="SYNTHETIC",
        raw_metadata={"synthetic_data": True},
    )


def signal(m, risk=KEY):
    return Signal(
        signal_id=f"entry-{m.market_id}:{risk}",
        timestamp=NOW,
        market_id=m.market_id,
        event_id=m.event_id,
        risk_event_id=risk,
        token_id=m.token_ids[0],
        outcome="Yes",
        category=m.category,
        fair_probability=0.7,
        prediction_bid=0.29,
        prediction_ask=0.31,
        raw_edge=0.39,
        cost_drag=0.03,
        adjusted_edge=0.36,
        available_size=100,
        confidence=0.7,
        action="BUY_YES",
        reason="SYNTHETIC",
        fair_value_source="synthetic",
        fair_value_timestamp=NOW,
        orderbook_timestamp=NOW,
        match_decision="MATCH",
    )


def anchor(event="external-1", provider="odds_api_io"):
    return ExternalAnchor(
        source_id=event,
        event_id=event,
        source="Book",
        category="sports_1x2",
        outcome="Arsenal",
        decimal_odds=2,
        timestamp=NOW,
        metadata={"provider": provider},
    )


def match(m, event="external-1", decision="MATCH"):
    return EventMatch(
        external_event_id=event,
        prediction_market_event_id=m.event_id,
        market_id=m.market_id,
        title=m.title,
        matched_outcomes={"Yes": "Arsenal"},
        score=1,
        decision=decision,
        reasons=["SYNTHETIC"],
    )


def test_resolver_binds_wrappers_without_mutating_inputs():
    a, b = market("a", None), market("b", None)
    output = bind_fixture_risk([a, b], [match(a), match(b)], [anchor()])
    assert {m.risk_event_id for m in output} == {KEY}
    assert {m.event_id for m in output} == {"venue-a", "venue-b"}
    assert a.risk_event_id is None and b.risk_event_id is None


@pytest.mark.parametrize(
    "bad", ["review", "conflicting_events", "missing_provider", "multiple_providers"]
)
def test_ambiguous_risk_joins_stay_unbound(bad):
    m = market()
    matches, anchors = [match(m)], [anchor()]
    if bad == "review":
        matches = [match(m, decision="REVIEW")]
    elif bad == "conflicting_events":
        matches.append(match(m, "external-2"))
        anchors.append(anchor("external-2"))
    elif bad == "missing_provider":
        anchors = [anchor(provider="")]
    else:
        anchors.append(anchor(provider="another_provider"))
    assert bind_fixture_risk([m], matches, anchors)[0].risk_event_id is None


def test_legacy_holding_is_regrouped_before_new_wrapper_risk(tmp_path):
    settings = Settings(_env_file=None, **{k: f.default for k, f in Settings.model_fields.items()})
    settings.max_position_per_event = 100
    trader = PaperTrader(settings, portfolio_path=tmp_path / "portfolio.sqlite")
    legacy = TradeDecision(
        signal_id="legacy",
        timestamp=NOW,
        market_id="b",
        event_id="venue-b",
        token_id="b",
        outcome="Yes",
        category="sports_1x2",
        side="BUY_YES",
        price=0.31,
        size=100,
        approved=True,
        reason="SYNTHETIC legacy accounting",
    )
    trader.portfolio.fill(legacy, 0.29)
    trader.portfolio.mark("b", 0.29, NOW, NOW)
    cash = trader.portfolio.cash
    a, b = market("a"), market("b")
    decision = trader.execute([signal(a)], {"a": a, "b": b}, NOW)[0]
    assert not decision.approved and "max position per event" in decision.reason
    assert trader.portfolio.exposures()[1] == {KEY: 100}
    assert trader.portfolio.cash == cash
    assert trader.portfolio.decisions()[0].risk_event_id is None
    trader.portfolio.connection.close()
    reopened = PaperPortfolio(settings.paper_starting_cash, tmp_path / "portfolio.sqlite")
    assert reopened.exposures()[1] == {KEY: 100}
    with pytest.raises(ValueError, match="identity changed"):
        reopened.bind_risk_event("venue-b", "different-fixture")
    reopened.connection.close()


def test_missing_or_conflicting_signal_risk_identity_is_rejected():
    trader = PaperTrader()
    m = market()
    for risk in (None, "another-fixture"):
        decision = trader.execute([signal(m, risk=risk)], {m.market_id: m}, NOW)[0]
        assert not decision.approved and "identity mismatch" in decision.reason
    assert trader.portfolio.cash == trader.settings.paper_starting_cash
    trader.portfolio.connection.close()


def test_persisted_risk_binding_cannot_be_deleted_to_reset_exposure(tmp_path):
    path = tmp_path / "portfolio.sqlite"
    trader = PaperTrader(portfolio_path=path)
    m = market()
    assert trader.execute([signal(m)], {m.market_id: m}, NOW)[0].approved
    trader.portfolio.connection.execute("DELETE FROM event_risk_keys")
    with pytest.raises(ValueError, match="risk identity/accounting"):
        trader.portfolio.assert_invariants()
    trader.portfolio.connection.close()
    with pytest.raises(ValueError, match="risk identity/accounting"):
        PaperPortfolio(trader.settings.paper_starting_cash, path)


def test_funded_sale_releases_shared_fixture_capacity():
    settings = Settings(_env_file=None, **{k: f.default for k, f in Settings.model_fields.items()})
    settings.max_position_per_event = 100
    trader = PaperTrader(settings)
    a, b = market("a"), market("b")
    markets = {"a": a, "b": b}
    assert trader.execute([signal(a)], markets, NOW)[0].approved
    sale = signal(a).model_copy(
        update={
            "signal_id": "sell-a",
            "action": "SELL_YES",
            "fair_probability": 0.1,
            "adjusted_edge": 0.16,
        }
    )
    assert trader.execute([sale], markets, NOW)[0].approved
    assert trader.portfolio.exposures()[1] == {}
    assert trader.execute([signal(b)], markets, NOW)[0].approved
    assert trader.portfolio.exposures()[1] == {KEY: 100}
    trader.portfolio.connection.close()


def test_standalone_clv_uses_same_canonical_blocks_as_risk():
    from scripts.analyze_clv_robustness import attach_ledger, summarize_group

    observations = []
    for key, venue, risk, value in [
        ("a", "venue-a", KEY, 0.1),
        ("b", "venue-b", KEY, 0.3),
        ("c", "venue-c", "fixture:other", -0.2),
    ]:
        observations.append(
            attach_ledger(
                {"paper_trade_id": key, "clv": str(value)},
                {"event_id": venue, "risk_event_id": risk},
            )
        )
    result = summarize_group("overall", "all", observations)
    assert result["independent_blocks"] == "2"
    assert float(result["average_clv"]) == pytest.approx(0)


def test_risk_mapping_cannot_backdate_the_composite_market_view():
    from edgelab.research.backtest import market_universe_as_of

    m = market(risk=None)
    m.raw_metadata["collected_at"] = NOW.isoformat()
    later = NOW + timedelta(seconds=5)
    a = anchor().model_copy(update={"available_at": later})
    bound = bind_fixture_risk([m], [match(m)], [a])[0]
    assert bound.raw_metadata["venue_metadata_collected_at"] == NOW.isoformat()
    assert bound.raw_metadata["collected_at"] == later.isoformat()
    assert market_universe_as_of([bound], [], NOW) == {}
    assert market_universe_as_of([bound], [], later)[m.market_id].risk_event_id == KEY
