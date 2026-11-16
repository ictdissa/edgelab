from __future__ import annotations

from edgelab.config.settings import Settings
from scripts.audit_execution_realism import audit_row


def _row(**overrides: str) -> dict[str, str]:
    row = {
        "paper_trade_id": "t1",
        "market_id": "m1",
        "outcome": "Yes",
        "side": "BUY_YES",
        "execution_price": "0.42",
        "execution_price_source": "ASK",
        "size": "10",
        "bid_at_entry": "0.40",
        "ask_at_entry": "0.42",
        "spread_at_entry": "0.02",
        "liquidity_at_entry": "50",
        "clv_status": "pending",
    }
    row.update(overrides)
    return row


def test_buy_yes_at_ask_is_realistic() -> None:
    audited = audit_row(_row(), "ledger", Settings(MIN_LIQUIDITY=1, MAX_SPREAD=0.05))

    assert audited["fill_realistic"] == "true"
    assert audited["live_order_called"] == "false"
    assert audited["vwap_price"] == "0.42"
    assert audited["depth_sufficient"] == "true"


def test_buy_yes_at_bid_is_rejected() -> None:
    audited = audit_row(
        _row(execution_price="0.40", execution_price_source="BID"),
        "ledger",
        Settings(MIN_LIQUIDITY=1, MAX_SPREAD=0.05),
    )

    assert audited["fill_realistic"] == "false"
    assert audited["blocker"] == "BUY_YES not filled at ask"


def test_size_above_liquidity_is_rejected() -> None:
    audited = audit_row(
        _row(size="60"),
        "ledger",
        Settings(MIN_LIQUIDITY=1, MAX_SPREAD=0.05, MAX_TRADE_SIZE=100),
    )

    assert audited["fill_realistic"] == "false"
    assert audited["blocker"] == "size exceeds available conservative liquidity"
    assert audited["partial_fill_size"] == "50"
    assert audited["depth_sufficient"] == "false"
