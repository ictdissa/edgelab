"""Canonical fixture identity for risk; venue event IDs remain audit identifiers."""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime
from typing import Sequence

from edgelab.data.schemas import ExternalAnchor, PredictionMarket, ensure_utc
from edgelab.matching.event_resolver import EventMatch


def bind_fixture_risk(
    markets: Sequence[PredictionMarket],
    matches: Sequence[EventMatch],
    anchors: Sequence[ExternalAnchor],
) -> list[PredictionMarket]:
    """Only unique accepted mappings from a named provider authorize fixture risk.

    One provider ID can group several venue wrappers. Conflicting mappings and
    missing provider provenance stay unbound, not an invented independent event.
    """
    keys: dict[str, set[str]] = defaultdict(set)
    receipts: dict[str, list[datetime]] = defaultdict(list)
    for match in matches:
        if match.decision != "MATCH":
            continue
        providers = {
            str(a.metadata.get("provider") or "").strip()
            for a in anchors
            if a.event_id == match.external_event_id and a.market_id in {None, match.market_id}
        }
        if len(providers) == 1 and "" not in providers:
            keys[match.market_id].add(f"fixture:{next(iter(providers))}:{match.external_event_id}")
            receipts[match.market_id].extend(
                a.available_at or a.timestamp
                for a in anchors
                if a.event_id == match.external_event_id and a.market_id in {None, match.market_id}
            )
    output = []
    for original in markets:
        market = original.model_copy(deep=True)
        if market.category in {"sports_1x2", "sports_moneyline"}:
            candidates = keys.get(market.market_id, set())
            market.risk_event_id = next(iter(candidates)) if len(candidates) == 1 else None
            if market.risk_event_id:
                bound_at = max(receipts[market.market_id])
                market.raw_metadata["risk_identity_available_at"] = bound_at.isoformat()
                stamp = market.raw_metadata.get("collected_at")
                if stamp:
                    try:
                        venue_at = ensure_utc(datetime.fromisoformat(str(stamp)))
                    except ValueError:
                        pass  # Never turn an invalid venue receipt into a valid one.
                    else:
                        market.raw_metadata.setdefault("venue_metadata_collected_at", str(stamp))
                        # Composite metadata cannot precede availability of its risk mapping.
                        market.raw_metadata["collected_at"] = max(venue_at, bound_at).isoformat()
        output.append(market)
    return output
