"""Signal lifecycle semantics shared by diagnostics and paper-candidate generation."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Literal

from edgelab.config.settings import Settings

BEST_SIDE_BUY = "BUY_YES"
BEST_SIDE_SELL = "SELL_YES"
BEST_SIDE_NONE = "NONE"

FINAL_PAPER_CANDIDATE = "PAPER_CANDIDATE"
FINAL_WATCHLIST_ONLY = "WATCHLIST_ONLY"
FINAL_REJECTED_BY_RISK = "REJECTED_BY_RISK"
FINAL_NO_EDGE = "NO_EDGE"
FINAL_RELATIVE_VALUE_MULTI_LEG = "RELATIVE_VALUE_MULTI_LEG"
FINAL_THEORETICAL_ONLY = "THEORETICAL_ONLY"
FINAL_DATA_INVALID = "DATA_INVALID"

RV_ACTION_PREFIX = "RELATIVE_VALUE"

RiskDecision = Literal["APPROVED", "REJECTED", "NOT_EVALUATED"]


@dataclass(frozen=True)
class SanityWarning:
    code: str
    message: str
    blocks_risk_approval: bool = True


@dataclass(frozen=True)
class EnrichedSignal:
    buy_edge_raw: float | None
    sell_edge_raw: float | None
    buy_edge_adjusted: float | None
    sell_edge_adjusted: float | None
    best_side: str
    best_adjusted_edge: float | None
    edge_passes_threshold: bool
    signal_action: str
    risk_decision: str
    risk_reasons: list[str]
    final_decision: str
    sanity_warnings: list[SanityWarning]

    def as_row_fields(self) -> dict[str, str]:
        return {
            "buy_edge_raw": format_float(self.buy_edge_raw),
            "sell_edge_raw": format_float(self.sell_edge_raw),
            "buy_edge_adjusted": format_float(self.buy_edge_adjusted),
            "sell_edge_adjusted": format_float(self.sell_edge_adjusted),
            "best_side": self.best_side,
            "best_adjusted_edge": format_float(self.best_adjusted_edge),
            "edge_passes_threshold": format_bool(self.edge_passes_threshold),
            "signal_action": self.signal_action,
            "risk_decision": self.risk_decision,
            "risk_reasons": json.dumps(self.risk_reasons),
            "final_decision": self.final_decision,
        }


def enrich_signal_row(
    row: dict[str, str],
    settings: Settings,
    *,
    allow_suspicious_edges: bool = False,
) -> dict[str, str]:
    enriched = compute_lifecycle(row, settings, allow_suspicious_edges=allow_suspicious_edges)
    output = dict(row)
    output.update(enriched.as_row_fields())
    raw = raw_edge(output)
    adjusted = parse_optional_float(output.get("adjusted_edge"))
    spread = row_spread(output)
    if "raw_edge" not in output or output.get("raw_edge") == "":
        output["raw_edge"] = format_float(raw)
    if "adjusted_edge" not in output or output.get("adjusted_edge") == "":
        output["adjusted_edge"] = format_float(adjusted)
    if "spread" not in output or output.get("spread") == "":
        output["spread"] = format_float(spread)
    output["sanity_warning_codes"] = "; ".join(warning.code for warning in enriched.sanity_warnings)
    output["sanity_warning_count"] = str(len(enriched.sanity_warnings))
    output["sanity_blocks_risk"] = format_bool(
        any(warning.blocks_risk_approval for warning in enriched.sanity_warnings)
    )
    return output


def compute_lifecycle(
    row: dict[str, str],
    settings: Settings,
    *,
    allow_suspicious_edges: bool = False,
) -> EnrichedSignal:
    fair_probability = parse_optional_float(row.get("fair_probability"))
    bid = parse_optional_float(row.get("bid") or row.get("prediction_bid"))
    ask = parse_optional_float(row.get("ask") or row.get("prediction_ask"))
    cost_drag = cost_drag_from_row(row)
    buy_raw = fair_probability - ask if fair_probability is not None and ask is not None else None
    sell_raw = bid - fair_probability if fair_probability is not None and bid is not None else None
    buy_adjusted = buy_raw - cost_drag if buy_raw is not None else None
    sell_adjusted = sell_raw - cost_drag if sell_raw is not None else None
    best_side, best_adjusted = best_side_from_edges(buy_adjusted, sell_adjusted)
    edge_passes = best_adjusted is not None and best_adjusted >= settings.min_adjusted_edge
    signal_action = row.get("signal_action") or row.get("action", "") or "HOLD"
    risk_reasons = parse_reasons(row)
    risk_decision = normalize_risk_decision(row, risk_reasons)
    warnings = sanity_warnings_for_row(
        row,
        settings,
        buy_raw=buy_raw,
        sell_raw=sell_raw,
        best_adjusted=best_adjusted,
        allow_suspicious_edges=allow_suspicious_edges,
    )
    if any(warning.blocks_risk_approval for warning in warnings) and not allow_suspicious_edges:
        risk_decision = "REJECTED"
        for warning in warnings:
            if warning.blocks_risk_approval and warning.message not in risk_reasons:
                risk_reasons.append(warning.message)
    final_decision = classify_final_decision(
        row=row,
        settings=settings,
        best_side=best_side,
        best_adjusted_edge=best_adjusted,
        edge_passes_threshold=edge_passes,
        risk_decision=risk_decision,
        warnings=warnings,
    )
    return EnrichedSignal(
        buy_edge_raw=buy_raw,
        sell_edge_raw=sell_raw,
        buy_edge_adjusted=buy_adjusted,
        sell_edge_adjusted=sell_adjusted,
        best_side=best_side,
        best_adjusted_edge=best_adjusted,
        edge_passes_threshold=edge_passes,
        signal_action=signal_action,
        risk_decision=risk_decision,
        risk_reasons=risk_reasons,
        final_decision=final_decision,
        sanity_warnings=warnings,
    )


def classify_final_decision(
    *,
    row: dict[str, str],
    settings: Settings,
    best_side: str,
    best_adjusted_edge: float | None,
    edge_passes_threshold: bool,
    risk_decision: str,
    warnings: list[SanityWarning],
) -> str:
    action = row.get("signal_action") or row.get("action", "")
    if action.startswith(RV_ACTION_PREFIX) or row.get("category") == "relative_value":
        if (
            action == "RELATIVE_VALUE_THEORETICAL_ONLY"
            or row.get("rejection_reason") == "THEORETICAL_ONLY"
        ):
            return FINAL_THEORETICAL_ONLY
        return FINAL_RELATIVE_VALUE_MULTI_LEG
    if any(warning.blocks_risk_approval for warning in warnings) and missing_core_data_warning(
        warnings
    ):
        return FINAL_DATA_INVALID
    if best_side == BEST_SIDE_NONE or not edge_passes_threshold or best_adjusted_edge is None:
        return FINAL_NO_EDGE
    if not has_side_quote(row, best_side) or not is_fresh(row, settings):
        return FINAL_DATA_INVALID
    if not liquidity_and_spread_ok(row, settings):
        return FINAL_REJECTED_BY_RISK
    if risk_decision == "APPROVED":
        return FINAL_PAPER_CANDIDATE
    if risk_decision == "REJECTED":
        return FINAL_REJECTED_BY_RISK
    return FINAL_WATCHLIST_ONLY


def sanity_warnings_for_row(
    row: dict[str, str],
    settings: Settings,
    *,
    buy_raw: float | None = None,
    sell_raw: float | None = None,
    best_adjusted: float | None = None,
    allow_suspicious_edges: bool = False,
) -> list[SanityWarning]:
    warnings: list[SanityWarning] = []
    fair = parse_optional_float(row.get("fair_probability"))
    bid = parse_optional_float(row.get("bid") or row.get("prediction_bid"))
    ask = parse_optional_float(row.get("ask") or row.get("prediction_ask"))
    spread = row_spread(row)
    raw = raw_edge(row)
    adjusted = best_adjusted
    if adjusted is None:
        adjusted = parse_optional_float(row.get("best_adjusted_edge") or row.get("adjusted_edge"))
    if raw is None and (buy_raw is not None or sell_raw is not None):
        raw = max(value for value in (buy_raw, sell_raw) if value is not None)
    if adjusted is not None and adjusted > 0.25:
        warnings.append(
            SanityWarning(
                "SUSPICIOUS_ADJUSTED_EDGE",
                "adjusted_edge > 0.25",
                blocks_risk_approval=False,
            )
        )
    if raw is not None and raw > 0.30:
        warnings.append(
            SanityWarning(
                "SUSPICIOUS_RAW_EDGE",
                "raw_edge > 0.30",
                blocks_risk_approval=False,
            )
        )
    if bid is None or ask is None:
        warnings.append(SanityWarning("MISSING_BID_ASK", "bid or ask missing"))
    if bid is not None and ask is not None and bid > ask:
        warnings.append(SanityWarning("CROSSED_BOOK", "bid > ask"))
    if bid is not None and not 0 <= bid <= 1:
        warnings.append(SanityWarning("BID_OUT_OF_RANGE", "bid outside [0, 1]"))
    if ask is not None and not 0 <= ask <= 1:
        warnings.append(SanityWarning("ASK_OUT_OF_RANGE", "ask outside [0, 1]"))
    if spread is not None and spread > 0.25:
        warnings.append(SanityWarning("WIDE_SPREAD", "spread > 0.25"))
    if fair is not None and not 0 <= fair <= 1:
        warnings.append(
            SanityWarning("FAIR_PROBABILITY_INVALID", "fair_probability outside [0, 1]")
        )
    if not is_fresh(row, settings):
        warnings.append(SanityWarning("STALE_FAIR_VALUE", "stale fair value"))
    if not row.get("outcome"):
        warnings.append(SanityWarning("MISSING_OUTCOME_MAPPING", "missing outcome mapping"))
    if row.get("relation_type") == "exhaustive_group" and row.get("partial_group") in {
        "true",
        "True",
        "1",
    }:
        warnings.append(
            SanityWarning(
                "PARTIAL_EXHAUSTIVE_GROUP",
                "partial exhaustive group treated as exhaustive",
            )
        )
    return warnings


def missing_core_data_warning(warnings: list[SanityWarning]) -> bool:
    data_codes = {
        "MISSING_BID_ASK",
        "CROSSED_BOOK",
        "BID_OUT_OF_RANGE",
        "ASK_OUT_OF_RANGE",
        "FAIR_PROBABILITY_INVALID",
        "MISSING_OUTCOME_MAPPING",
    }
    return any(warning.code in data_codes for warning in warnings)


def best_side_from_edges(
    buy_adjusted: float | None,
    sell_adjusted: float | None,
) -> tuple[str, float | None]:
    candidates = []
    if buy_adjusted is not None:
        candidates.append((BEST_SIDE_BUY, buy_adjusted))
    if sell_adjusted is not None:
        candidates.append((BEST_SIDE_SELL, sell_adjusted))
    if not candidates:
        return BEST_SIDE_NONE, None
    side, value = max(candidates, key=lambda item: item[1])
    if value <= 0:
        return BEST_SIDE_NONE, value
    return side, value


def cost_drag_from_row(row: dict[str, str]) -> float:
    explicit = parse_optional_float(row.get("cost_drag"))
    if explicit is not None:
        return explicit
    raw = raw_edge(row)
    adjusted = parse_optional_float(row.get("adjusted_edge"))
    if raw is not None and adjusted is not None:
        return raw - adjusted
    return 0.0


def raw_edge(row: dict[str, str]) -> float | None:
    explicit = parse_optional_float(row.get("raw_edge"))
    if explicit is not None:
        return explicit
    fair = parse_optional_float(row.get("fair_probability"))
    if fair is None:
        return None
    bid = parse_optional_float(row.get("bid") or row.get("prediction_bid"))
    ask = parse_optional_float(row.get("ask") or row.get("prediction_ask"))
    candidates = []
    if ask is not None:
        candidates.append(fair - ask)
    if bid is not None:
        candidates.append(bid - fair)
    return max(candidates) if candidates else None


def row_spread(row: dict[str, str]) -> float | None:
    explicit = parse_optional_float(row.get("spread"))
    if explicit is not None:
        return explicit
    bid = parse_optional_float(row.get("bid") or row.get("prediction_bid"))
    ask = parse_optional_float(row.get("ask") or row.get("prediction_ask"))
    if bid is None or ask is None:
        return None
    return max(0.0, ask - bid)


def has_side_quote(row: dict[str, str], side: str) -> bool:
    bid = parse_optional_float(row.get("bid") or row.get("prediction_bid"))
    ask = parse_optional_float(row.get("ask") or row.get("prediction_ask"))
    if side == BEST_SIDE_BUY:
        return ask is not None
    if side == BEST_SIDE_SELL:
        return bid is not None
    return False


def liquidity_and_spread_ok(row: dict[str, str], settings: Settings) -> bool:
    liquidity = parse_optional_float(row.get("liquidity") or row.get("available_size"))
    if liquidity is None or liquidity < settings.min_liquidity:
        return False
    spread = row_spread(row)
    return spread is not None and 0 < spread <= settings.max_spread


def is_fresh(row: dict[str, str], settings: Settings) -> bool:
    freshness_status = row.get("freshness_status")
    if freshness_status in {"stale", "future_data"}:
        return False
    age = parse_optional_float(row.get("data_age_seconds"))
    if age is None:
        return False
    return age <= settings.max_data_age_seconds


def normalize_risk_decision(row: dict[str, str], risk_reasons: list[str]) -> str:
    explicit = row.get("risk_decision", "")
    if explicit in {"APPROVED", "REJECTED", "NOT_EVALUATED"}:
        return explicit
    risk_approved = row.get("risk_approved", "")
    if risk_approved in {"True", "true", "1"}:
        return "APPROVED"
    if risk_approved in {"False", "false", "0"} or risk_reasons:
        return "REJECTED"
    return "NOT_EVALUATED"


def parse_reasons(row: dict[str, str]) -> list[str]:
    value = row.get("risk_reasons") or row.get("rejection_reason") or row.get("reason") or ""
    if value.startswith("["):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            parsed = None
        if isinstance(parsed, list):
            return [str(item) for item in parsed if str(item)]
    return [part.strip() for part in value.split(";") if part.strip()]


def parse_optional_float(value: str | float | int | None) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def format_float(value: float | int | None) -> str:
    if value is None:
        return ""
    if isinstance(value, int):
        return str(value)
    return f"{value:.10g}"


def format_bool(value: bool) -> str:
    return "true" if value else "false"
