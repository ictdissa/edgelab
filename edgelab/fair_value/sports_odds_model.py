"""Sports fair values from complete, point-in-time no-vig bookmaker snapshots."""

from __future__ import annotations

import statistics
from collections import defaultdict
from datetime import UTC, datetime
from typing import Mapping, Sequence

from edgelab.config.settings import Settings
from edgelab.data.external_sources.odds_provider import (
    american_to_decimal,
    fractional_to_decimal,
    validate_decimal_odds,
)
from edgelab.data.schemas import ExternalAnchor, FairValue, OrderBook, PredictionMarket
from edgelab.fair_value.base import FairValueModel
from edgelab.fair_value.inputs import usable
from edgelab.fair_value.uncertainty import estimate_probability_uncertainty
from edgelab.matching.market_classifier import is_fixture_winner_contract
from edgelab.matching.outcome_mapper import binary_yes_target_from_title, normalize_name, similarity
from edgelab.matching.settlement import settlement_equivalent


def generic_no_vig(decimal_odds_by_outcome: Mapping[str, float]) -> dict[str, float]:
    if len(decimal_odds_by_outcome) < 2:
        raise ValueError("No-vig conversion requires at least two outcomes")
    implied = {
        name: 1 / validate_decimal_odds(odds) for name, odds in decimal_odds_by_outcome.items()
    }
    total = sum(implied.values())
    return {name: value / total for name, value in implied.items()}


def implied_probability(decimal_odds: float) -> float:
    return 1 / validate_decimal_odds(decimal_odds)


def no_vig_two_way(decimal_a: float, decimal_b: float) -> tuple[float, float]:
    p = generic_no_vig({"a": decimal_a, "b": decimal_b})
    return p["a"], p["b"]


def no_vig_three_way(
    decimal_home: float, decimal_draw: float, decimal_away: float
) -> tuple[float, float, float]:
    p = generic_no_vig({"home": decimal_home, "draw": decimal_draw, "away": decimal_away})
    return p["home"], p["draw"], p["away"]


class SportsOddsModel(FairValueModel):
    def __init__(self, settings: Settings | None = None, as_of: datetime | None = None) -> None:
        self.settings = settings or Settings()
        self.as_of = as_of or datetime.now(UTC)

    def compute(
        self,
        markets: Sequence[PredictionMarket],
        anchors: Sequence[ExternalAnchor],
        orderbooks: Sequence[OrderBook],
    ) -> list[FairValue]:
        values: list[FairValue] = []
        for market in markets:
            if market.category not in {"sports_1x2", "sports_moneyline"}:
                continue
            if not is_fixture_winner_contract(
                market.title, market.description, market.outcomes
            ):
                continue
            # A vintage is an atomic bookmaker market update, not latest odds per team.
            vintages: dict[tuple[str, datetime], list[ExternalAnchor]] = defaultdict(list)
            for a in anchors:
                if (
                    a.event_id == market.event_id
                    and a.category == market.category
                    and a.decimal_odds
                    and (a.market_id is None or a.market_id == market.market_id)
                    and usable(a, self.as_of, self.settings.max_data_age_seconds)
                    and not a.outcome.lower().startswith(("lay", "depth"))
                ):
                    vintages[(a.source, a.timestamp)].append(a)
            latest: dict[str, list[ExternalAnchor]] = {}
            for (source, _), rows in sorted(vintages.items(), key=lambda item: item[0][1]):
                latest[source] = rows
            complete = [rows for rows in latest.values() if self._complete(rows, market.category)]
            if not complete:
                continue
            external = [a.outcome for a in complete[0]]
            targets: dict[str, str] = {}
            if {x.casefold() for x in market.outcomes} == {"yes", "no"}:
                teams = sorted(x for x in external if x.casefold() != "draw")
                target = binary_yes_target_from_title(market.title, teams[0], teams[-1]) or ""
                declared = str(market.raw_metadata.get("target_outcome") or "")
                if declared and normalize_name(declared) != normalize_name(target):
                    continue
                if target:
                    targets["Yes"] = target
            else:
                targets = {outcome: outcome for outcome in market.outcomes}
            for outcome, target in targets.items():
                probabilities: list[float] = []
                used: list[ExternalAnchor] = []
                for rows in complete:
                    scores = sorted(
                        ((similarity(a.outcome, target), a.outcome) for a in rows), reverse=True
                    )
                    if scores[0][0] < 0.90 or (
                        len(scores) > 1 and scores[0][0] - scores[1][0] < 0.10
                    ):
                        continue
                    p = generic_no_vig(
                        {a.outcome: a.decimal_odds for a in rows if a.decimal_odds is not None}
                    )
                    probabilities.append(p[scores[0][1]])
                    used.extend(rows)
                if not probabilities:
                    continue
                settlement_verified = market.category == "sports_1x2" and all(
                    settlement_equivalent(
                        market.raw_metadata.get("settlement_spec"),
                        a.metadata.get("settlement_spec"),
                        self.as_of,
                    )[0]
                    for a in used
                )
                oldest = min(a.timestamp for a in used)
                liquidity = min(
                    (
                        max((level.size for level in b.asks), default=0)
                        for b in orderbooks
                        if b.token_id == market.token_for(outcome)
                    ),
                    default=0,
                )
                values.append(
                    FairValue(
                        event_id=market.event_id,
                        market_id=market.market_id,
                        outcome=outcome,
                        fair_probability=statistics.fmean(probabilities),
                        source=f"sports_no_vig:{len(probabilities)}books",
                        timestamp=oldest,
                        computed_at=self.as_of,
                        available_at=max(a.available_at or a.timestamp for a in used),
                        uncertainty=estimate_probability_uncertainty(
                            probabilities,
                            oldest,
                            self.settings.model_uncertainty_bps,
                            liquidity,
                            self.as_of,
                        ),
                        metadata={
                            "target_outcome": target,
                            "match_decision": "MATCH",
                            "settlement_verified": settlement_verified
                            or market.raw_metadata.get("synthetic_data") is True,
                            "provider": ",".join(
                                sorted({str(a.metadata.get("provider") or a.source) for a in used})
                            ),
                        },
                    )
                )
        return values

    @staticmethod
    def _complete(rows: list[ExternalAnchor], category: str) -> bool:
        outcomes = [a.outcome.casefold() for a in rows]
        expected = 3 if category == "sports_1x2" else 2
        return len(outcomes) == len(set(outcomes)) == expected and (
            category != "sports_1x2" or "draw" in outcomes
        )


__all__ = [
    "american_to_decimal",
    "fractional_to_decimal",
    "validate_decimal_odds",
    "generic_no_vig",
    "no_vig_two_way",
    "no_vig_three_way",
    "SportsOddsModel",
]
