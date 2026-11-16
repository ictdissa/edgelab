"""Internal relative-value checks."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

from edgelab.data.schemas import BestBidAsk, PredictionMarket


@dataclass(frozen=True)
class RelativeValueFinding:
    constraint_type: str
    involved_markets: tuple[str, ...]
    implied_edge: float
    executable: bool
    reason: str


def exhaustive_sum_check(
    bbas: Sequence[BestBidAsk], tolerance: float = 0.05
) -> RelativeValueFinding | None:
    ask_sum = sum(bba.best_ask or 0.0 for bba in bbas)
    if abs(ask_sum - 1.0) <= tolerance:
        return None
    return RelativeValueFinding(
        constraint_type="exhaustive_sum",
        involved_markets=tuple(bba.market_id for bba in bbas),
        implied_edge=abs(ask_sum - 1.0),
        executable=all((bba.best_ask_size or 0.0) > 0 for bba in bbas),
        reason=f"ask_sum={ask_sum:.3f}",
    )


class RelativeValueModel:
    def implication_check(
        self,
        child: PredictionMarket,
        parent: PredictionMarket,
        child_probability: float,
        parent_probability: float,
    ) -> RelativeValueFinding | None:
        if child_probability <= parent_probability:
            return None
        return RelativeValueFinding(
            constraint_type="implication",
            involved_markets=(child.market_id, parent.market_id),
            implied_edge=child_probability - parent_probability,
            executable=False,
            reason="logical implication violation; needs executable hedge before trading",
        )
