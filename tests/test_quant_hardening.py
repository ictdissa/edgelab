"""Regressions for contract identity, observation timing, accounting and risk."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from edgelab.config.settings import Settings
from edgelab.data.cache import CachedHttpClient, RollingRateLimiter
from edgelab.data.schemas import ExternalAnchor, FairValue, OrderBook, PredictionMarket, PriceLevel
from edgelab.execution.paper_trader import PaperTrader
from edgelab.fair_value.crypto_threshold_model import CryptoThresholdModel
from edgelab.fair_value.sports_odds_model import SportsOddsModel
from edgelab.matching.event_resolver import resolve_events
from edgelab.research.backtest import replay, Settlement, point_in_time_filter
from edgelab.strategies.relative_value_graph import build_graph
from edgelab.strategies.taker_edge import generate_signals
from scripts.run_paper_trader import validate_candidate
from scripts.update_clv import PaperTrade, SnapshotSignal, observe_horizon

NOW = datetime(2026, 10, 13, 12, tzinfo=UTC)


@pytest.fixture
def settings() -> Settings:
    return Settings(
        _env_file=None,
        MIN_LIQUIDITY=1,
        MIN_ADJUSTED_EDGE=0.001,
        MAX_TRADE_SIZE=10,
        MAX_POSITION_PER_MARKET=15,
        MAX_POSITION_PER_EVENT=30,
        MAX_TOTAL_UNRESOLVED_EXPOSURE=50,
        PAPER_STARTING_CASH=100,
        DEFAULT_SLIPPAGE_BPS=25,
        FEE_BPS=20,
        MODEL_UNCERTAINTY_BPS=10,
        SAFETY_MARGIN_BPS=10,
        RESOLUTION_RISK_BPS=10,
        MAX_DATA_AGE_SECONDS=900,
    )


def market(title: str = "Will BTC be above $110 on expiry?", mid: str = "m") -> PredictionMarket:
    return PredictionMarket(
        market_id=mid,
        event_id="event",
        category="crypto_threshold",
        title=title,
        outcomes=["Yes", "No"],
        token_ids=[f"{mid}-yes", f"{mid}-no"],
        end_time=NOW + timedelta(days=30),
        resolution_source="index",
        raw_metadata={"collected_at": NOW.isoformat()},
    )


def anchor(mid: str = "m", threshold: float = 110, timestamp: datetime = NOW) -> ExternalAnchor:
    return ExternalAnchor(
        source_id=mid,
        market_id=mid,
        event_id="event",
        category="crypto_threshold",
        source="mock_spot",
        outcome="Yes",
        timestamp=timestamp,
        available_at=timestamp,
        metadata={"spot": 100, "threshold": threshold, "volatility": 0.5},
    )


def book(
    m: PredictionMarket,
    bid: float = 0.10,
    ask: float = 0.12,
    token: str | None = None,
    timestamp: datetime = NOW,
) -> OrderBook:
    return OrderBook(
        market_id=m.market_id,
        token_id=token or m.token_ids[0],
        timestamp=timestamp,
        available_at=timestamp,
        bids=[PriceLevel(price=bid, size=100)],
        asks=[PriceLevel(price=ask, size=100)],
    )


def signals(m: PredictionMarket, settings: Settings, bid: float = 0.10, ask: float = 0.12):
    fv = FairValue(
        event_id=m.event_id,
        market_id=m.market_id,
        outcome="Yes",
        fair_probability=0.6,
        source="test",
        timestamp=NOW,
        uncertainty=0.001,
        metadata={"match_decision": "MATCH"},
    )
    return generate_signals([m], [fv], [book(m, bid, ask).best_bid_ask()], settings, NOW)


def test_terminal_below_is_complement_and_barrier_is_unsupported(settings):
    above, below, hit = (
        market(),
        market("Will BTC be below $110 on expiry?"),
        market("Will BTC hit $110 before expiry?"),
    )
    model = CryptoThresholdModel(settings, NOW)
    a = model.compute([above], [anchor()], [])[0]
    b = model.compute([below], [anchor()], [])[0]
    assert a.fair_probability + b.fair_probability == pytest.approx(1)
    assert model.compute([hit], [anchor()], []) == []
    expired = above.model_copy(update={"end_time": NOW - timedelta(seconds=1)})
    assert model.compute([expired], [anchor()], []) == []


def test_short_expiry_not_floored_to_one_day(settings):
    m = market().model_copy(update={"end_time": NOW + timedelta(minutes=30)})
    value = CryptoThresholdModel(settings, NOW).compute([m], [anchor()], [])[0]
    assert value.metadata["years_to_expiry"] == pytest.approx(1800 / (365 * 86400))


def test_contract_anchor_order_cannot_change_fair_value(settings):
    m = market()
    rows = [anchor(), anchor("other", 200)]
    model = CryptoThresholdModel(settings, NOW)
    assert model.compute([m], rows, []) == model.compute([m], list(reversed(rows)), [])


@pytest.mark.parametrize("seconds", [-864000, 60])
def test_source_timestamps_never_laundered(settings, seconds):
    assert (
        CryptoThresholdModel(settings, NOW).compute(
            [market()], [anchor(timestamp=NOW + timedelta(seconds=seconds))], []
        )
        == []
    )


def test_delayed_receipt_excluded_from_replay(settings):
    a = anchor().model_copy(update={"available_at": NOW + timedelta(seconds=10)})
    assert point_in_time_filter(NOW, [a], [])[0] == []
    assert replay([market()], [a], [book(market())], settings, [NOW]).signals == []


def test_token_join_is_exact_and_order_invariant(settings):
    m = market()
    fv = CryptoThresholdModel(settings, NOW).compute([m], [anchor()], [])[0]
    books = [book(m).best_bid_ask(), book(m, 0.01, 0.02, m.token_ids[1]).best_bid_ask()]
    first = generate_signals([m], [fv], books, settings, NOW)
    assert first == generate_signals([m], [fv], list(reversed(books)), settings, NOW)
    assert first[0].prediction_ask == 0.12
    assert first[0].token_id == "m-yes"


def sports_rows(event="e", teams=("Arsenal", "Draw", "Chelsea"), timestamp=NOW):
    return [
        ExternalAnchor(
            source_id=o,
            event_id=event,
            source="book",
            category="sports_1x2",
            outcome=o,
            decimal_odds=2 + i,
            timestamp=timestamp,
        )
        for i, o in enumerate(teams)
    ]


def fixture(title="Arsenal vs Chelsea match winner", event="e"):
    return PredictionMarket(
        market_id="football",
        event_id=event,
        category="sports_1x2",
        title=title,
        outcomes=["Arsenal", "Draw", "Chelsea"],
        token_ids=["ars", "draw", "che"],
        resolution_source="league",
    )


def test_incomplete_and_mixed_vintage_sports_fields_rejected(settings):
    model = SportsOddsModel(settings, NOW)
    rows = sports_rows()
    assert model.compute([fixture()], [rows[0], rows[2]], []) == []
    assert (
        model.compute(
            [fixture()],
            [*rows[:2], rows[2].model_copy(update={"timestamp": NOW - timedelta(seconds=1)})],
            [],
        )
        == []
    )
    assert {v.outcome for v in model.compute([fixture()], rows, [])} == {
        "Arsenal",
        "Draw",
        "Chelsea",
    }


def test_wrong_opponent_and_tied_candidates_rejected():
    wrong = fixture("Arsenal vs Tottenham match winner", "other")
    assert resolve_events([wrong], sports_rows())[0].decision != "MATCH"
    two = [*sports_rows("one"), *sports_rows("two")]
    result = resolve_events([fixture(event="other")], two)[0]
    assert result.decision == "REVIEW"
    assert result.rejection_reason == "ambiguous_event_candidates"


def test_year_overlap_is_not_same_subject_and_scores_not_exclusive():
    a = market("Will Arsenal win the league in 2026?", "a").model_copy(
        update={"category": "sports_outright"}
    )
    b = market("Will Chelsea finish top 4 in 2026?", "b").model_copy(
        update={"category": "sports_outright"}
    )
    assert not build_graph([a, b]).edges
    score = fixture("Arsenal exact score 1-0").model_copy(
        update={
            "market_id": "score",
            "category": "sports_exact_score",
            "outcomes": ["Yes", "No"],
            "token_ids": ["score-yes", "score-no"],
        }
    )
    assert not any(
        e.child_market == "score" or e.parent_market == "score"
        for e in build_graph([fixture(), score]).edges
    )


def test_cash_inventory_and_persistent_limits(settings, tmp_path):
    m = market()
    path = tmp_path / "portfolio.sqlite"
    trader = PaperTrader(settings, portfolio_path=path)
    buy = signals(m, settings)[0]
    decision = trader.execute([buy], {m.market_id: m}, NOW)[0]
    assert (
        decision.approved and decision.price == 0.12 and decision.slippage > 0 and decision.fees > 0
    )
    assert trader.portfolio.cash == pytest.approx(100 + decision.cash_change)
    restarted = PaperTrader(settings, portfolio_path=path)
    assert not restarted.execute([buy], {m.market_id: m}, NOW)[0].approved
    another = buy.model_copy(update={"signal_id": "next"})
    assert "max position" in restarted.execute([another], {m.market_id: m}, NOW)[0].reason
    sell = signals(m, settings, 0.8, 0.82)[0].model_copy(update={"signal_id": "sell"})
    assert sell.action == "SELL_YES"
    assert restarted.execute([sell], {m.market_id: m}, NOW)[0].approved
    assert restarted.portfolio.inventory("m-yes") == 0
    assert not restarted.execute(
        [sell.model_copy(update={"signal_id": "naked"})], {m.market_id: m}, NOW
    )[0].approved


def test_risk_independently_checks_depth_spread_and_size(settings):
    m = market()
    s = signals(m, settings)[0]
    trader = PaperTrader(settings)
    for bad in (
        s.model_copy(update={"prediction_ask": 0.99}),
        s.model_copy(update={"token_id": "m-no"}),
        s.model_copy(update={"match_decision": "REVIEW"}),
    ):
        assert not trader.execute([bad], {m.market_id: m}, NOW)[0].approved
    decision = trader.risk.check(s, m, -1, NOW, as_of=NOW)
    assert not decision.approved


def test_csv_cannot_bypass_risk(settings):
    forged = {
        "side": "BUY_YES",
        "execution_price": ".4",
        "execution_price_source": "ASK",
        "size": "999999",
        "adjusted_edge_at_entry": ".5",
        "liquidity_at_entry": "100",
        "spread_at_entry": ".01",
    }
    assert not validate_candidate(forged, settings)[0]


def test_same_quote_depth_cannot_fill_repeatedly(settings):
    settings.max_position_per_market = 200
    settings.max_position_per_event = 300
    settings.max_total_unresolved_exposure = 500
    m = market()
    s = signals(m, settings)[0]
    trader = PaperTrader(settings)
    fills = trader.execute(
        [s.model_copy(update={"signal_id": str(i)}) for i in range(11)], {m.market_id: m}, NOW
    )
    assert sum(d.size for d in fills if d.approved) == 100
    assert not fills[-1].approved
    assert "already consumed" in fills[-1].reason
    older = s.model_copy(
        update={"signal_id": "old", "orderbook_timestamp": NOW - timedelta(seconds=1)}
    )
    assert "out-of-order" in trader.execute([older], {m.market_id: m}, NOW)[0].reason


def test_paper_cash_and_daily_loss_guards(settings):
    m = market()
    s = signals(m, settings)[0]
    settings.paper_starting_cash = 0.01
    cash_rejection = PaperTrader(settings).execute([s], {m.market_id: m}, NOW)[0]
    assert not cash_rejection.approved and "insufficient paper cash" in cash_rejection.reason
    settings.paper_starting_cash = 100
    settings.max_position_per_market = 100
    settings.max_daily_loss = 0.1
    trader = PaperTrader(settings)
    assert trader.execute([s], {m.market_id: m}, NOW)[0].approved
    assert trader.portfolio.daily_pnl(NOW) < -0.1
    next_signal = s.model_copy(update={"signal_id": "after-loss"})
    rejection = trader.execute([next_signal], {m.market_id: m}, NOW)[0]
    assert not rejection.approved and "max daily loss reached" in rejection.reason


@pytest.mark.parametrize("limit", ["max_position_per_event", "max_total_unresolved_exposure"])
def test_portfolio_caps_are_independently_enforced(settings, limit):
    setattr(settings, limit, 5)
    m = market()
    decision = PaperTrader(settings).execute(signals(m, settings), {m.market_id: m}, NOW)[0]
    assert not decision.approved
    expected = (
        "max position per event exceeded"
        if limit == "max_position_per_event"
        else "max total unresolved exposure exceeded"
    )
    assert expected in decision.reason


def test_live_order_route_cannot_be_unlocked(settings):
    from edgelab.execution.live_trader import LiveTrader

    with pytest.raises(ValueError, match="Live trading is intentionally disabled"):
        Settings(_env_file=None, ENABLE_LIVE_TRADING=True)
    with pytest.raises(RuntimeError):
        LiveTrader(settings)
    with pytest.raises(RuntimeError, match="not implemented"):
        LiveTrader(settings.model_copy(update={"enable_live_trading": True}))


def test_clv_rejects_wrong_token_late_quote_and_missing_depth():
    trade = PaperTrade("t", NOW, "crypto", "m", "Yes", "BUY_YES", 0.4, "yes", 10)
    good = SnapshotSignal(
        NOW + timedelta(minutes=5),
        "crypto",
        "m",
        "Yes",
        0.6,
        0.6,
        0.59,
        0.61,
        "yes",
        NOW + timedelta(minutes=5),
        20,
        20,
    )
    assert observe_horizon(trade, [good], "5m", timedelta(minutes=5))["status"] == "observed"
    from dataclasses import replace

    for bad in (
        replace(good, outcome="No"),
        replace(good, token_id="no"),
        replace(good, timestamp=NOW + timedelta(days=7)),
        replace(good, quote_timestamp=NOW),
    ):
        assert (
            observe_horizon(trade, [bad], "5m", timedelta(minutes=5))["status"]
            == "insufficient_followup_data"
        )
    assert (
        observe_horizon(trade, [replace(good, bid_size=None)], "5m", timedelta(minutes=5))["status"]
        == "quote_observed"
    )


def test_cache_keeps_receipt_and_archives_versions(tmp_path):
    calls = []

    def fake(*args, **kwargs):
        calls.append(1)
        return SimpleNamespace(status_code=200, json=lambda: {"price": 100})

    client = CachedHttpClient(
        tmp_path, "test", RollingRateLimiter(tmp_path / "limit.json", 3), fake
    )
    first = client.get_json("https://example.test/data")
    cached = client.get_json("https://example.test/data")
    assert len(calls) == 1 and cached.fetched_at == first.fetched_at
    assert len(list((tmp_path / "test/archive").glob("*.json"))) == 1


def test_corrupt_limiter_fails_closed_and_hourly_cap(tmp_path):
    path = tmp_path / "budget.json"
    limiter = RollingRateLimiter(path, 90, 3600)
    for _ in range(90):
        assert limiter.reserve(now=1000).allowed
    assert not limiter.reserve(now=1001).allowed
    path.write_text("corrupt")
    with pytest.raises(RuntimeError, match="blocked"):
        limiter.reserve(now=5000)


def test_replay_future_data_cannot_change_entry_or_settlement(settings):
    m = market()
    base = replay([m], [anchor()], [book(m)], settings, [NOW])
    future = anchor(timestamp=NOW + timedelta(days=1)).model_copy(
        update={"metadata": {"spot": 1000, "volatility": 0.5}}
    )
    changed = replay(
        [m],
        [anchor(), future],
        [book(m), book(m, 0.9, 0.95, timestamp=NOW + timedelta(days=1))],
        settings,
        [NOW],
        [Settlement("m-yes", 1, NOW + timedelta(days=1))],
    )
    assert base.signals == changed.signals and base.equity == changed.equity
