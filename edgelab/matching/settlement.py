"""Explicit human-reviewed fixture settlement equivalence, not title inference."""

from datetime import datetime
from pathlib import Path
import json
from typing import Any, Mapping, Literal

from pydantic import Field, field_validator

from edgelab.data.schemas import StrictModel, ensure_utc, ExternalAnchor, PredictionMarket


class FixtureSettlement(StrictModel):
    review_status: Literal["reviewed"]
    period: Literal["regulation_time", "full_fight", "qualification"]
    includes_overtime: bool
    draw_is_outcome: bool
    cancellation_rule: str = Field(min_length=1)
    evidence_reference: str = Field(min_length=1)
    reviewed_at: datetime

    @field_validator("reviewed_at")
    @classmethod
    def utc(cls, value: datetime) -> datetime:
        return ensure_utc(value)


def settlement_equivalent(
    prediction: Mapping[str, Any] | None,
    external: Mapping[str, Any] | None,
    as_of: datetime,
) -> tuple[bool, str]:
    """Certify only the initial soccer regulation-time 1X2 research scope.

    An evidence reference is an auditable operator attestation, not automatic legal
    verification. Providers without reviewed specs remain watchlist-only.
    """
    try:
        a, b = (
            FixtureSettlement.model_validate(prediction),
            FixtureSettlement.model_validate(external),
        )
    except ValueError:
        return False, "missing_or_invalid_settlement_spec"
    if max(a.reviewed_at, b.reviewed_at) > ensure_utc(as_of):
        return False, "settlement_review_not_available_as_of_decision"
    fields = ("period", "includes_overtime", "draw_is_outcome", "cancellation_rule")
    if any(getattr(a, k) != getattr(b, k) for k in fields):
        return False, "settlement_terms_differ"
    if a.period != "regulation_time" or a.includes_overtime or not a.draw_is_outcome:
        return False, "outside_certified_regulation_time_1x2_scope"
    return True, "reviewed_settlement_equivalence"


def apply_settlement_registry(
    markets: list[PredictionMarket],
    anchors: list[ExternalAnchor],
    path: Path | None,
) -> tuple[list[PredictionMarket], list[ExternalAnchor]]:
    if path is None:
        return markets, anchors
    payload = json.loads(path.read_text())
    if not isinstance(payload, list):
        raise ValueError("settlement registry must contain a list of reviewed contract pairs")
    market_by_id = {m.market_id: m.model_copy(deep=True) for m in markets}
    output: list[ExternalAnchor] = []
    for original in anchors:
        anchor = original.model_copy(deep=True)
        for row in payload:
            if row.get("external_event_id") != anchor.event_id or row.get("book") != anchor.source:
                continue
            market = market_by_id.get(row.get("market_id"))
            if market is None:
                continue
            prediction = FixtureSettlement.model_validate(row.get("prediction"))
            external = FixtureSettlement.model_validate(row.get("external"))
            prior = market.raw_metadata.get("settlement_spec")
            if prior is not None and prior != prediction.model_dump(mode="json"):
                raise ValueError("conflicting prediction settlement reviews")
            market.raw_metadata["settlement_spec"] = prediction.model_dump(mode="json")
            if anchor.market_id not in {None, market.market_id}:
                continue
            scoped = anchor.model_copy(deep=True)
            scoped.market_id = market.market_id
            scoped.metadata["settlement_spec"] = external.model_dump(mode="json")
            output.append(scoped)
        # Unreviewed books remain in the universe, not silently discarded to reduce disagreement.
        if not any(a.source_id == anchor.source_id for a in output):
            output.append(anchor)
    return list(market_by_id.values()), output
