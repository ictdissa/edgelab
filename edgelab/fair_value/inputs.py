"""Point-in-time input selection shared by valuation models."""

from __future__ import annotations

from datetime import datetime

from edgelab.data.schemas import ExternalAnchor


def usable(anchor: ExternalAnchor, as_of: datetime, max_age_seconds: int) -> bool:
    if anchor.metadata.get("cache_verified") is False:
        return False
    available = anchor.available_at or anchor.timestamp
    return (
        anchor.timestamp <= as_of
        and available <= as_of
        and (as_of - anchor.timestamp).total_seconds() <= max_age_seconds
    )


def contract_anchor(anchors: list[ExternalAnchor], market_id: str) -> ExternalAnchor | None:
    specific = [a for a in anchors if (a.market_id or a.metadata.get("market_id")) == market_id]
    candidates = specific or [
        a for a in anchors if not a.market_id and not a.metadata.get("market_id")
    ]
    if not candidates:
        return None
    latest = max(a.timestamp for a in candidates)
    choices = [a for a in candidates if a.timestamp == latest]
    # Conflicting simultaneous observations require explicit aggregation, not list-order selection.
    signatures = {str(sorted(a.metadata.items())) for a in choices}
    if len(signatures) > 1:
        return None
    return min(choices, key=lambda a: a.source_id)
