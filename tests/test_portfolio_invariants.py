"""Accounting state transitions, recovery and terminal settlement invariants."""

import random
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta

import pytest

from edgelab.config.settings import Settings
from edgelab.data.schemas import PredictionMarket, Signal, TradeDecision
from edgelab.execution.paper_trader import PaperTrader
from edgelab.execution.portfolio import PaperPortfolio
from scripts.run_paper_trader import sync_portfolio_ledger, read_csv_rows

NOW = datetime(2026, 10, 13, 12, tzinfo=UTC)


def trade(key="buy", side="BUY_YES", size=100, price=0.5, timestamp=NOW):
    return TradeDecision(
        timestamp=timestamp,
        signal_id=key,
        market_id="m",
        event_id="e",
        token_id="yes",
        outcome="Yes",
        category="crypto_threshold",
        side=side,
        price=price,
        size=size,
        approved=True,
        reason="test",
        orderbook_timestamp=timestamp,
    )


def signal(key="s", action="BUY_YES", timestamp=NOW):
    return Signal(
        signal_id=key,
        timestamp=timestamp,
        market_id="m",
        event_id="e",
        outcome="Yes",
        category="crypto_threshold",
        token_id="yes",
        match_decision="MATCH",
        fair_probability=0.9 if action == "BUY_YES" else 0.1,
        prediction_bid=0.4,
        prediction_ask=0.42,
        raw_edge=0.48,
        adjusted_edge=0.4,
        cost_drag=0.08,
        available_size=10,
        confidence=0.5,
        action=action,
        reason="test",
        fair_value_source="test",
        fair_value_timestamp=timestamp,
        orderbook_timestamp=timestamp,
    )


def market():
    return PredictionMarket(
        market_id="m",
        event_id="e",
        category="crypto_threshold",
        title="BTC above $100 at expiry",
        outcomes=["Yes", "No"],
        token_ids=["yes", "no"],
        end_time=NOW + timedelta(days=3),
        resolution_source="index",
    )


def test_overnight_equity_loss_is_daily_loss_and_survives_restart(tmp_path):
    path = tmp_path / "portfolio.sqlite"
    p = PaperPortfolio(100, path)
    p.fill(trade(timestamp=NOW - timedelta(days=1)), 0.7)
    p.mark("yes", 0.5, NOW)
    assert p.daily_pnl(NOW) == pytest.approx(-20)
    assert p.loss_stopped(NOW, 10)
    p.connection.close()
    recovered = PaperPortfolio(100, path)
    recovered.mark("yes", 0.8, NOW + timedelta(seconds=1))
    assert recovered.daily_pnl(NOW) > 0
    assert recovered.loss_stopped(NOW, 10), "loss stop stays latched despite recovery"
    tomorrow = NOW + timedelta(days=1)
    recovered.begin_day(tomorrow)
    assert recovered.daily_pnl(tomorrow) == 0
    assert not recovered.loss_stopped(tomorrow, 10)


def test_settlement_is_terminal_even_without_inventory(tmp_path):
    p = PaperPortfolio(100, tmp_path / "p.sqlite")
    p.settle("yes", 1, NOW, NOW)
    with pytest.raises(ValueError, match="settled"):
        p.fill(trade(), 0.5)
    with pytest.raises(ValueError, match="settled"):
        p.mark("yes", 0.5, NOW)
    p.settle("yes", 1, NOW, NOW)
    with pytest.raises(ValueError, match="conflicting"):
        p.settle("yes", 0, NOW, NOW)
    assert p.cash == 100 and p.inventory("yes") == 0


def test_paper_service_cannot_reopen_settled_contract():
    t = PaperTrader(Settings(_env_file=None, MIN_LIQUIDITY=1, MAX_TRADE_SIZE=10))
    assert t.execute([signal()], {"m": market()}, NOW)[0].approved
    t.portfolio.settle("yes", 1, NOW, NOW)
    result = t.execute([signal("later")], {"m": market()}, NOW)[0]
    assert not result.approved and "settled" in result.reason
    assert t.portfolio.inventory("yes") == 0


def test_invalid_direct_fill_rolls_back_all_changes():
    p = PaperPortfolio(100)
    bad = trade()
    bad.orderbook_timestamp = NOW + timedelta(seconds=1)
    with pytest.raises(ValueError, match="future"):
        p.fill(bad, 0.5)
    assert p.cash == 100 and p.inventory("yes") == 0 and not p.decisions()
    p.assert_invariants()


def test_nested_failed_fill_rolls_back_even_when_caller_handles_error():
    p = PaperPortfolio(100)
    bad = trade()
    bad.orderbook_timestamp = NOW + timedelta(seconds=1)
    with p.transaction():
        with pytest.raises(ValueError, match="future"):
            p.fill(bad, 0.5)
    assert p.cash == 100 and p.inventory("yes") == 0
    p.assert_invariants()


def test_parallel_account_initialization_and_fills_reconcile(tmp_path):
    path = tmp_path / "shared.sqlite"

    def worker(i):
        p = PaperPortfolio(100, path)
        try:
            p.fill(trade(str(i), size=1), 0.49)
        finally:
            p.connection.close()

    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(worker, range(12)))
    p = PaperPortfolio(100, path)
    assert p.cash == 94 and p.inventory("yes") == 12
    p.assert_invariants()


def test_committed_fill_is_recovered_if_csv_export_was_interrupted(tmp_path):
    path = tmp_path / "ledger.csv"
    t = PaperTrader(
        Settings(_env_file=None, MIN_LIQUIDITY=1), portfolio_path=path.with_suffix(".sqlite")
    )
    first = t.execute([signal()], {"m": market()}, NOW)[0]
    assert first.approved and not path.exists()
    rejected = t.execute([signal()], {"m": market()}, NOW)[0]
    assert not rejected.approved
    sync_portfolio_ledger(path, t)
    saved = read_csv_rows(path)
    assert len(saved) == 1 and saved[0]["side"] == "BUY_YES"
    assert float(saved[0]["execution_price"]) == 0.42 and saved[0]["clv_status"] == "pending"
    sync_portfolio_ledger(path, t)
    assert read_csv_rows(path) == saved


def test_funded_sell_can_reduce_inventory_after_loss_stop():
    settings = Settings(_env_file=None, MIN_LIQUIDITY=1, MAX_TRADE_SIZE=10, MAX_DAILY_LOSS=0.1)
    t = PaperTrader(settings)
    assert t.execute([signal()], {"m": market()}, NOW)[0].approved
    assert t.portfolio.loss_stopped(NOW, 0.1)
    assert not t.execute([signal("blocked")], {"m": market()}, NOW)[0].approved
    assert t.execute([signal("reduce", "SELL_YES")], {"m": market()}, NOW)[0].approved
    assert t.portfolio.inventory("yes") == 0


@pytest.mark.parametrize("seed", range(8))
def test_randomized_funded_state_machine_reconciles(seed):
    rng = random.Random(seed)
    p = PaperPortfolio(1000)
    for i in range(80):
        now = NOW + timedelta(seconds=i)
        inventory = p.inventory("yes")
        sale = inventory > 0 and rng.random() < 0.5
        size = min(inventory, rng.randint(1, 8)) if sale else rng.randint(1, 8)
        price = rng.uniform(0.05, 0.95)
        d = trade(str(i), "SELL_YES" if sale else "BUY_YES", size, price, now)
        d.fees, d.slippage = 0.01, 0.005
        p.fill(d, max(0, price - 0.01))
        p.assert_invariants()
        assert p.cash >= 0 and p.inventory("yes") >= 0
    p.settle("yes", seed % 2, now, now)
    p.assert_invariants()
    assert p.inventory("yes") == 0
