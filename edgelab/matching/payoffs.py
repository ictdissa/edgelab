"""Conservative payoff parsing; path-dependent contracts require a separate model."""

from __future__ import annotations

import re
from typing import Literal

from edgelab.data.schemas import PredictionMarket, ThresholdPayoff


def crypto_payoff(market: PredictionMarket) -> ThresholdPayoff | None:
    if market.payoff is not None:
        return market.payoff
    if market.end_time is None:
        return None
    text = market.title.casefold().replace(",", "")
    asset = re.search(r"\b(btc|bitcoin|eth|ethereum)\b", text)
    level = re.search(
        r"\b(above|below|over|under|hit|reach)\s+\$?([0-9]+(?:\.[0-9]+)?)(k|m)?\b", text
    )
    if not asset or not level:
        return None
    # A range or multiple barriers cannot be represented by this payoff.
    if len(re.findall(r"\b(?:above|below|over|under|hit|reach)\s+\$?\d", text)) != 1:
        return None
    direction: Literal["above", "below"] = "below" if level[1] in {"below", "under"} else "above"
    kind: Literal["terminal", "barrier"] = (
        "barrier"
        if level[1] in {"hit", "reach"}
        or re.search(r"\b(before|any time|anytime)\b", text)
        or re.search(
            r"\b(at any time|highest price|lowest price|touches)\b",
            (market.description or "").casefold(),
        )
        else "terminal"
    )
    symbol = "BTC" if asset[1] in {"btc", "bitcoin"} else "ETH"
    multiplier = {"k": 1000, "m": 1_000_000}.get(level[3], 1)
    return ThresholdPayoff(
        asset=symbol,
        threshold=float(level[2]) * multiplier,
        direction=direction,
        expiry=market.end_time,
        kind=kind,
    )
