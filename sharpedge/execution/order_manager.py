"""Order management abstractions for paper/live execution."""

from __future__ import annotations

from sharpedge.data.schemas import Signal


class OrderManager:
    def route_signal(self, signal: Signal) -> str:
        if signal.action in {"BUY_YES", "SELL_YES"}:
            return "paper_execution"
        if signal.action in {"QUOTE_BID", "QUOTE_ASK"}:
            return "market_making_simulation"
        return "skip"
