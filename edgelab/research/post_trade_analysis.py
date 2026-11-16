"""Post-trade analysis helpers."""

from __future__ import annotations

from edgelab.data.schemas import TradeDecision


def rejected_reasons(trades: list[TradeDecision]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for trade in trades:
        if trade.approved:
            continue
        counts[trade.reason] = counts.get(trade.reason, 0) + 1
    return counts
