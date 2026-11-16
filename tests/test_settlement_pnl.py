from __future__ import annotations

from edgelab.config.settings import Settings
from scripts.analyze_settlement_pnl import load_settlements, pnl_row


def test_settlement_pnl_computes_buy_yes_realized_pnl(tmp_path) -> None:
    settlements_path = tmp_path / "settlements.csv"
    settlements_path.write_text("market_id,outcome,final_payout\nm1,Yes,1\n", encoding="utf-8")
    settlements = load_settlements(settlements_path)
    row = {
        "paper_trade_id": "t1",
        "market_id": "m1",
        "outcome": "Yes",
        "side": "BUY_YES",
        "execution_price": "0.40",
        "size": "10",
        "clv_5m": "0.05",
    }

    pnl = pnl_row(row, settlements, Settings(FEE_BPS=0))

    assert pnl["status"] == "settled"
    assert pnl["realized_pnl"] == "6"
    assert pnl["clv_vs_pnl_same_sign"] == "true"
