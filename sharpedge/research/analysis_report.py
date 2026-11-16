"""Analysis-first research report generation.

This module is intentionally script-friendly: it turns a point-in-time snapshot
into a concise trading memo focused on edge quality, costs, liquidity, risk
gates, and what would need to be proven before capital is put at risk.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from datetime import datetime

from sharpedge.config.settings import Settings
from sharpedge.data.schemas import BestBidAsk, FairValue, Market, Signal
from sharpedge.execution.risk_manager import RiskManager
from sharpedge.research.backtest import BacktestResult
from sharpedge.research.pipeline import ResearchSnapshot
from sharpedge.strategies.taker_edge import costs_from_settings


@dataclass(frozen=True)
class AnalyzedSignal:
    sport: str
    event: str
    market: str
    market_id: str
    outcome: str
    candidate_action: str
    final_action: str
    fair_probability: float
    bid: float | None
    ask: float | None
    raw_edge: float
    slippage: float
    uncertainty: float
    resolution_risk: float
    safety_margin: float
    total_cost: float
    adjusted_edge: float
    available_size: float
    risk_size: float
    estimated_net_ev: float
    confidence: float
    reason: str
    risk_approved: bool
    risk_reasons: list[str]
    orderbook_age_seconds: float | None
    odds_age_seconds: float | None


def build_analyzed_signals(
    snapshot: ResearchSnapshot,
    settings: Settings,
) -> list[AnalyzedSignal]:
    markets = {market.market_id: market for market in snapshot.markets}
    fair_values = {
        (value.market_id, value.outcome): value for value in snapshot.fair_values
    }
    bbas = _bba_by_market_outcome(snapshot.markets, snapshot.best_bid_asks)
    latest_odds_ts_by_event = _latest_odds_timestamp_by_event(snapshot)
    risk_manager = RiskManager(settings)

    analyzed: list[AnalyzedSignal] = []
    for signal in snapshot.signals:
        market = markets.get(signal.market_id)
        fair_value = fair_values.get((signal.market_id, signal.outcome))
        bba = bbas.get((signal.market_id, signal.outcome))
        if not market or not fair_value or not bba:
            continue
        costs = costs_from_settings(settings, fair_value)
        candidate_action = _candidate_action(signal, fair_value, bba)
        proposed_size = min(settings.max_trade_size, signal.available_size)
        odds_ts = latest_odds_ts_by_event.get(market.event_id, signal.timestamp)
        if signal.action in {"BUY_YES", "SELL_YES"}:
            risk_decision = risk_manager.check_signal(
                signal=signal,
                market=market,
                proposed_size=proposed_size,
                orderbook_timestamp=bba.timestamp,
                odds_timestamp=odds_ts,
                live=False,
                resolution_rules_clear=bool(market.resolution_source),
                as_of=snapshot.as_of,
            )
            risk_approved = risk_decision.approved
            risk_reasons = risk_decision.reasons
            risk_size = risk_decision.max_approved_size if risk_decision.approved else 0.0
        else:
            risk_approved = False
            risk_reasons = []
            risk_size = 0.0
        analyzed.append(
            AnalyzedSignal(
                sport=market.sport,
                event=market.event_id,
                market=market.title,
                market_id=market.market_id,
                outcome=signal.outcome,
                candidate_action=candidate_action,
                final_action=signal.action,
                fair_probability=signal.fair_probability,
                bid=signal.polymarket_bid,
                ask=signal.polymarket_ask,
                raw_edge=signal.raw_edge,
                slippage=costs.slippage,
                uncertainty=costs.uncertainty,
                resolution_risk=costs.resolution_risk,
                safety_margin=costs.safety_margin,
                total_cost=costs.total,
                adjusted_edge=signal.adjusted_edge,
                available_size=signal.available_size,
                risk_size=risk_size,
                estimated_net_ev=max(0.0, signal.adjusted_edge) * risk_size,
                confidence=signal.confidence,
                reason=signal.reason,
                risk_approved=risk_approved,
                risk_reasons=risk_reasons,
                orderbook_age_seconds=(snapshot.as_of - bba.timestamp).total_seconds(),
                odds_age_seconds=(snapshot.as_of - odds_ts).total_seconds(),
            )
        )
    return sorted(analyzed, key=lambda row: row.adjusted_edge, reverse=True)


def render_research_memo(
    snapshot: ResearchSnapshot,
    settings: Settings,
    backtest_result: BacktestResult | None = None,
    top_n: int = 10,
) -> str:
    rows = build_analyzed_signals(snapshot, settings)
    executable = [row for row in rows if row.risk_approved and row.final_action in {"BUY_YES", "SELL_YES"}]
    risk_rejected = [
        row for row in rows if row.final_action in {"BUY_YES", "SELL_YES"} and not row.risk_approved
    ]
    positive = [row for row in rows if row.adjusted_edge > 0]
    model_blocks = Counter(row.reason for row in rows if row.final_action not in {"BUY_YES", "SELL_YES"})
    risk_blocks = Counter(reason for row in risk_rejected for reason in row.risk_reasons)
    missing_fair = _missing_fair_count(snapshot)
    total_net_ev = sum(row.estimated_net_ev for row in executable)
    max_edge = max((row.adjusted_edge for row in rows), default=0.0)

    lines: list[str] = []
    lines.append("# SharpEdge Sports Research Memo")
    lines.append("")
    lines.append(f"as_of: `{snapshot.as_of.isoformat()}`")
    lines.append("")
    lines.append("## Executive Summary")
    lines.append("")
    lines.append(
        f"- Markets scanned: {len(snapshot.markets)}; signals evaluated: {len(rows)}; "
        f"risk-approved executable signals: {len(executable)}."
    )
    lines.append(
        f"- Positive adjusted-edge candidates: {len(positive)}; max adjusted edge: "
        f"{_pct(max_edge)}; risk-capped estimated net EV: ${total_net_ev:.2f}."
    )
    lines.append(
        f"- Fair-value coverage gaps: {missing_fair} market outcomes lacked a matched external fair value."
    )
    lines.append(
        "- Profitability claim status: research-only. This output is a candidate-selection and "
        "measurement framework; real profitability needs live-quality data, fill tracking, fees, "
        "and persistent positive CLV."
    )
    lines.append("")

    lines.append("## Top Risk-Approved Edges")
    lines.append("")
    if executable:
        lines.extend(
            _markdown_table(
                [
                    "sport",
                    "event",
                    "market",
                    "outcome",
                    "side",
                    "fair",
                    "bid",
                    "ask",
                    "raw",
                    "cost",
                    "adj",
                    "size",
                    "net_ev",
                ],
                [
                    [
                        row.sport,
                        row.event,
                        _short(row.market, 42),
                        _short(row.outcome, 28),
                        row.final_action,
                        _pct(row.fair_probability),
                        _price(row.bid),
                        _price(row.ask),
                        _pct(row.raw_edge),
                        _pct(row.total_cost),
                        _pct(row.adjusted_edge),
                        f"{row.risk_size:.1f}",
                        f"${row.estimated_net_ev:.2f}",
                    ]
                    for row in executable[:top_n]
                ],
            )
        )
    else:
        lines.append("No signal passed all execution and risk gates.")
    lines.append("")

    lines.append("## Cost Decomposition")
    lines.append("")
    cost_rows = positive[:top_n]
    if cost_rows:
        lines.extend(
            _markdown_table(
                [
                    "market",
                    "outcome",
                    "side",
                    "raw_edge",
                    "slip",
                    "uncert",
                    "res",
                    "safety",
                    "adjusted",
                    "decision",
                ],
                [
                    [
                        _short(row.market, 36),
                        _short(row.outcome, 24),
                        row.candidate_action,
                        _pct(row.raw_edge),
                        _pct(row.slippage),
                        _pct(row.uncertainty),
                        _pct(row.resolution_risk),
                        _pct(row.safety_margin),
                        _pct(row.adjusted_edge),
                        row.final_action,
                    ]
                    for row in cost_rows
                ],
            )
        )
    else:
        lines.append("No positive adjusted-edge candidates in this snapshot.")
    lines.append("")

    lines.append("## Model Gate Diagnostics")
    lines.append("")
    if model_blocks:
        lines.extend(
            _markdown_table(
                ["hold_or_avoid_reason", "count"],
                [[reason, str(count)] for reason, count in model_blocks.most_common()],
            )
        )
    else:
        lines.append("No model-gated HOLD/AVOID signals.")
    lines.append("")

    lines.append("## Risk Gate Diagnostics")
    lines.append("")
    if risk_blocks:
        lines.extend(
            _markdown_table(
                ["risk_rejection_reason", "count"],
                [[reason, str(count)] for reason, count in risk_blocks.most_common()],
            )
        )
    else:
        lines.append("No risk-manager rejections among model-approved executable signals.")
    lines.append("")

    lines.append("## Cross-Market Consistency")
    lines.append("")
    if snapshot.inconsistencies:
        lines.extend(
            _markdown_table(
                [
                    "type",
                    "markets",
                    "implied_edge",
                    "liquidity",
                    "executable",
                    "reason",
                ],
                [
                    [
                        item.inconsistency_type,
                        ", ".join(item.involved_markets),
                        _pct(item.implied_edge),
                        f"{item.liquidity_available:.1f}",
                        "yes" if item.executable_after_costs else "theoretical",
                        _short(item.reason, 80),
                    ]
                    for item in snapshot.inconsistencies[:top_n]
                ],
            )
        )
    else:
        lines.append("No cross-market consistency violations found in this snapshot.")
    lines.append("")

    lines.append("## Data Quality")
    lines.append("")
    freshness = _freshness_summary(rows)
    lines.extend(
        _markdown_table(
            ["metric", "value"],
            [
                ["order_books", str(len(snapshot.orderbooks))],
                ["external_odds_rows", str(len(snapshot.odds))],
                ["fair_values", str(len(snapshot.fair_values))],
                ["missing_fair_values", str(missing_fair)],
                ["max_orderbook_age_seconds", freshness["max_orderbook_age_seconds"]],
                ["max_odds_age_seconds", freshness["max_odds_age_seconds"]],
            ],
        )
    )
    lines.append("")

    if backtest_result:
        lines.append("## Point-In-Time Backtest Smoke Test")
        lines.append("")
        lines.extend(
            _markdown_table(
                ["metric", "value"],
                [
                    ["signals_evaluated", str(backtest_result.signals_evaluated)],
                    ["decisions", str(len(backtest_result.decisions))],
                    [
                        "executed_trades",
                        str(sum(1 for decision in backtest_result.decisions if decision.executed)),
                    ],
                    ["final_cash_pnl", f"{backtest_result.final_cash_pnl:.4f}"],
                    [
                        "final_mark_to_market_pnl",
                        f"{backtest_result.final_mark_to_market_pnl:.4f}",
                    ],
                ],
            )
        )
        lines.append(
            "The backtest uses only order-book and external-odds snapshots available at or before "
            "each decision timestamp."
        )
        lines.append("")

    lines.append("## What Would Make This Tradeable")
    lines.append("")
    lines.append("- Persistent positive CLV by sport, market type, and liquidity bucket.")
    lines.append("- Real fill-rate evidence at displayed bid/ask, including missed fills and queue position.")
    lines.append("- Fee and funding-cost model by venue, order type, and settlement path.")
    lines.append("- Entity/event matching against real provider IDs rather than names alone.")
    lines.append("- Capacity analysis: edge decay as size increases and stale quotes are consumed.")
    lines.append("- Kill switches proven in paper mode before any live credentials are enabled.")
    lines.append("")
    lines.append("Research/education only. Live trading remains disabled unless explicitly configured.")
    lines.append("")
    return "\n".join(lines)


def _candidate_action(signal: Signal, fair_value: FairValue, bba: BestBidAsk) -> str:
    buy_edge = (
        fair_value.fair_probability - bba.best_ask
        if bba.best_ask is not None
        else float("-inf")
    )
    sell_edge = (
        bba.best_bid - fair_value.fair_probability
        if bba.best_bid is not None
        else float("-inf")
    )
    if signal.action in {"BUY_YES", "SELL_YES"}:
        return signal.action
    return "BUY_YES" if buy_edge >= sell_edge else "SELL_YES"


def _bba_by_market_outcome(
    markets: list[Market], bbas: list[BestBidAsk]
) -> dict[tuple[str, str], BestBidAsk]:
    by_token = {(bba.market_id, bba.token_id): bba for bba in bbas}
    output: dict[tuple[str, str], BestBidAsk] = {}
    for market in markets:
        for outcome, token_id in zip(market.outcomes, market.token_ids, strict=False):
            bba = by_token.get((market.market_id, token_id))
            if bba:
                output[(market.market_id, outcome)] = bba
    return output


def _latest_odds_timestamp_by_event(snapshot: ResearchSnapshot) -> dict[str, datetime]:
    output: dict[str, datetime] = {}
    for row in snapshot.odds:
        current = output.get(row.event_id)
        if current is None or row.timestamp > current:
            output[row.event_id] = row.timestamp
    return output


def _missing_fair_count(snapshot: ResearchSnapshot) -> int:
    covered = {(value.market_id, value.outcome) for value in snapshot.fair_values}
    required = {
        (market.market_id, outcome) for market in snapshot.markets for outcome in market.outcomes
    }
    return len(required - covered)


def _freshness_summary(rows: list[AnalyzedSignal]) -> dict[str, str]:
    orderbook_ages = [
        row.orderbook_age_seconds for row in rows if row.orderbook_age_seconds is not None
    ]
    odds_ages = [row.odds_age_seconds for row in rows if row.odds_age_seconds is not None]
    return {
        "max_orderbook_age_seconds": f"{max(orderbook_ages, default=0.0):.0f}",
        "max_odds_age_seconds": f"{max(odds_ages, default=0.0):.0f}",
    }


def _markdown_table(headers: list[str], rows: list[list[str]]) -> list[str]:
    output = ["| " + " | ".join(headers) + " |"]
    output.append("| " + " | ".join("---" for _ in headers) + " |")
    for row in rows:
        output.append("| " + " | ".join(_escape_cell(str(cell)) for cell in row) + " |")
    return output


def _escape_cell(value: str) -> str:
    return value.replace("|", "\\|").replace("\n", " ")


def _pct(value: float) -> str:
    return f"{100 * value:.2f}%"


def _price(value: float | None) -> str:
    return "" if value is None else f"{value:.3f}"


def _short(value: str, max_length: int) -> str:
    return value if len(value) <= max_length else value[: max_length - 1] + "…"
