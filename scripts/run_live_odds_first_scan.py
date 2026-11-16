#!/usr/bin/env python
from __future__ import annotations

import argparse
import json
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Mapping

import _bootstrap  # noqa: F401

from sharpedge.config.settings import Settings
from sharpedge.data.event_resolver import (
    EventResolver,
    PolymarketCandidate,
    ResolvedEventMatch,
    candidates_from_public_search_payload,
    write_event_match_audit,
)
from sharpedge.data.market_type import market_types_compatible
from sharpedge.data.odds_api_io_provider import (
    OddsAPIIOProvider,
    read_external_odds_csv,
    write_external_odds_csv,
    write_json,
)
from sharpedge.data.polymarket_collector import PolymarketCollector
from sharpedge.data.schemas import ExternalOdds, FairValue, Market, OrderBook, Signal
from sharpedge.execution.risk_manager import RiskManager
from sharpedge.research.fair_value_model import FairValueModel
from sharpedge.research.no_vig_odds import generic_no_vig
from sharpedge.strategies.taker_edge import costs_from_settings, generate_edge_signals


RAW_DIR = Path("data/raw")
REPORTS_DIR = Path("reports")
ODDS_EVENTS_PATH = RAW_DIR / "odds_api_io_events.json"
ODDS_PAYLOADS_PATH = RAW_DIR / "odds_api_io_odds.json"
ODDS_NORMALIZED_PATH = RAW_DIR / "odds_api_io_normalized.csv"
MATCHED_ODDS_PATH = RAW_DIR / "odds_api_io_matched_polymarket.csv"
MATCHED_MARKETS_PATH = RAW_DIR / "odds_first_matched_markets.json"
CANDIDATES_PATH = RAW_DIR / "polymarket_candidates_for_odds_events.json"
MATCH_AUDIT_PATH = REPORTS_DIR / "event_match_audit.csv"
POLY_SEARCH_CACHE = RAW_DIR / "cache" / "polymarket_search"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run odds-first live research scan")
    parser.add_argument("--sport", default="football")
    parser.add_argument(
        "--league",
        help='Optional Odds-API.io league slug, e.g. "england-premier-league"',
    )
    parser.add_argument("--bookmakers", default="Bet365,Betfair Exchange")
    parser.add_argument("--max-events", type=int, default=30)
    parser.add_argument("--output", type=Path, default=REPORTS_DIR / "live_odds_first_scan.md")
    parser.add_argument("--use-cache", action="store_true")
    parser.add_argument("--refresh", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--min-match-score", type=float, default=0.82)
    parser.add_argument("--min-adjusted-edge", type=float)
    parser.add_argument("--paper-trade", action="store_true")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    settings = Settings()
    if args.min_adjusted_edge is not None:
        settings = Settings(MIN_ADJUSTED_EDGE=args.min_adjusted_edge)

    odds_events, odds_payloads, normalized_odds, odds_meta = load_or_fetch_odds(args, settings)
    if args.dry_run:
        report = render_report(
            as_of=datetime.now(UTC),
            args=args,
            odds_events=odds_events,
            candidates=[],
            matches=[],
            review=[],
            rejected=[],
            markets=[],
            orderbooks=[],
            fair_values=[],
            signals=[],
            risk_rows=[],
            warnings=[
                "dry run: no Polymarket search, CLOB book fetch, signal generation, or paper trading was executed"
            ],
            odds_meta=odds_meta,
        )
        write_text(args.output, report)
        print(f"Dry run complete. Odds events available: {len(odds_events)}")
        print(f"Wrote {args.output}")
        return

    candidates = search_polymarket_candidates(
        odds_events=odds_events,
        settings=settings,
        use_cache=args.use_cache,
        refresh=args.refresh,
    )
    resolver = EventResolver(min_match_score=args.min_match_score)
    resolution = resolver.match_odds_events_to_polymarket(odds_events, candidates)
    write_event_match_audit(MATCH_AUDIT_PATH, resolution)
    write_candidates_json(CANDIDATES_PATH, candidates)

    matched_markets = markets_from_matches(resolution.matched, candidates)
    matched_odds = rewrite_matched_odds(normalized_odds, resolution.matched)
    write_external_odds_csv(MATCHED_ODDS_PATH, matched_odds)
    write_markets_json(MATCHED_MARKETS_PATH, matched_markets)

    orderbooks: list[OrderBook] = []
    fair_values: list[FairValue] = []
    signals: list[Signal] = []
    risk_rows: list[dict[str, Any]] = []
    warnings: list[str] = []
    if args.paper_trade:
        warnings.append(
            "paper-trade flag noted: use scripts/run_paper_trader.py for ledger execution; no live orders placed"
        )
    as_of = datetime.now(UTC)
    if not resolution.matched:
        warnings.append("no matched Polymarket markets for the cached Odds-API.io fixture slate")
    else:
        collector = PolymarketCollector(settings=settings, mock=False)
        for market in matched_markets:
            for token_id in market.token_ids:
                book = collector.fetch_order_book(market.market_id, token_id)
                if book:
                    orderbooks.append(book)
        fair_values = FairValueModel(
            base_uncertainty_bps=settings.model_uncertainty_bps,
            now=as_of,
        ).compute_fair_values(matched_markets, matched_odds, orderbooks, as_of=as_of)
        bbas = [book.best_bid_ask() for book in orderbooks]
        signals = generate_edge_signals(matched_markets, fair_values, bbas, settings, as_of=as_of)
        risk_rows = risk_diagnostics(signals, matched_markets, fair_values, orderbooks, settings, as_of)

    report = render_report(
        as_of=as_of,
        args=args,
        odds_events=odds_events,
        candidates=candidates,
        matches=resolution.matched,
        review=resolution.review,
        rejected=resolution.rejected,
        markets=matched_markets,
        orderbooks=orderbooks,
        fair_values=fair_values,
        signals=signals,
        risk_rows=risk_rows,
        warnings=warnings,
        odds_meta=odds_meta,
    )
    write_text(args.output, report)
    print(
        "Live odds-first scan complete: "
        f"odds_events={len(odds_events)}, candidates={len(candidates)}, "
        f"matched={len(resolution.matched)}, review={len(resolution.review)}, "
        f"rejected={len(resolution.rejected)}, signals={len(signals)}"
    )
    print(f"Wrote {args.output}")
    print(f"Wrote {MATCH_AUDIT_PATH}")
    print(f"Wrote {MATCHED_ODDS_PATH}")


def load_or_fetch_odds(
    args: argparse.Namespace,
    settings: Settings,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[ExternalOdds], dict[str, Any]]:
    canonical_cache_ready = (
        ODDS_EVENTS_PATH.exists() and ODDS_PAYLOADS_PATH.exists() and ODDS_NORMALIZED_PATH.exists()
    )
    if canonical_cache_ready and not args.refresh:
        events = json.loads(ODDS_EVENTS_PATH.read_text(encoding="utf-8"))
        payloads = json.loads(ODDS_PAYLOADS_PATH.read_text(encoding="utf-8"))
        odds = read_external_odds_csv(ODDS_NORMALIZED_PATH)
        filtered_events = [
            event
            for event in events
            if isinstance(event, dict) and event_matches_league(event, args.league)
        ]
        return (
            filtered_events[: args.max_events],
            [payload for payload in payloads if isinstance(payload, dict)],
            odds,
            {
                "source": "canonical_cache",
                "requests_made": 0,
                "cache_hits": 3,
                "remaining_budget": None,
                "league": args.league or "",
            },
        )
    if args.use_cache and not canonical_cache_ready:
        return [], [], [], {"source": "missing_cache", "requests_made": 0, "cache_hits": 0}
    provider = OddsAPIIOProvider(settings)
    bundle = provider.fetch_snapshot(
        sport=args.sport,
        league=args.league,
        bookmakers=args.bookmakers,
        max_events=args.max_events,
        use_cache=args.use_cache,
        refresh=args.refresh,
    )
    return (
        bundle.events,
        bundle.odds_payloads,
        bundle.normalized_odds,
        {
            "source": "odds_api_io_cache_or_api",
            "requests_made": bundle.requests_made,
            "cache_hits": bundle.cache_hits,
            "remaining_budget": bundle.remaining_budget,
            "league": args.league or "",
        },
    )


def search_polymarket_candidates(
    odds_events: list[dict[str, Any]],
    settings: Settings,
    use_cache: bool,
    refresh: bool,
) -> list[PolymarketCandidate]:
    candidates: list[PolymarketCandidate] = []
    seen: set[tuple[str, str]] = set()
    for event in odds_events:
        for query in search_queries_for_event(event):
            payload = polymarket_public_search(
                settings=settings,
                query=query,
                use_cache=use_cache,
                refresh=refresh,
            )
            for candidate in candidates_from_public_search_payload(event, payload):
                key = (candidate.odds_event_id, candidate.market_id)
                if key in seen:
                    continue
                seen.add(key)
                candidates.append(candidate)
    return candidates


def search_queries_for_event(event: Mapping[str, Any]) -> list[str]:
    home = string(event.get("home") or event.get("homeTeam") or event.get("home_team"))
    away = string(event.get("away") or event.get("awayTeam") or event.get("away_team"))
    league = league_name(event)
    sport = sport_name(event)
    raw_queries = [
        home,
        away,
        f"{home} vs {away}",
        f"{away} vs {home}",
        league,
        sport,
    ]
    output: list[str] = []
    for query in raw_queries:
        query = query.strip()
        if query and query not in output:
            output.append(query)
    return output


def polymarket_public_search(
    settings: Settings,
    query: str,
    use_cache: bool,
    refresh: bool,
) -> Mapping[str, Any]:
    cache_path = POLY_SEARCH_CACHE / f"{safe_cache_key(query)}.json"
    if cache_path.exists() and (use_cache or not refresh):
        payload = json.loads(cache_path.read_text(encoding="utf-8"))
        if not use_cache and polymarket_search_needs_hydration(payload):
            payload = hydrate_polymarket_search_payload(settings, payload)
            write_json(cache_path, payload)
        return payload
    if use_cache:
        return {"events": []}
    import httpx

    response = httpx.get(
        f"{settings.polymarket_gamma_base.rstrip('/')}/public-search",
        params={
            "q": query,
            "limit_per_type": 25,
            "keep_closed_markets": 1,
            "search_profiles": "false",
            "optimized": "true",
        },
        timeout=20,
    )
    response.raise_for_status()
    payload = response.json()
    if isinstance(payload, Mapping):
        payload = hydrate_polymarket_search_payload(settings, payload)
    write_json(cache_path, payload)
    return payload if isinstance(payload, Mapping) else {"events": []}


def polymarket_search_needs_hydration(payload: Mapping[str, Any]) -> bool:
    for event in payload.get("events", []) or []:
        if not isinstance(event, Mapping):
            continue
        for market in event.get("markets", []) or []:
            if not isinstance(market, Mapping):
                continue
            if market.get("conditionId") and market.get("clobTokenIds"):
                return False
            if market.get("slug"):
                return True
    return False


def hydrate_polymarket_search_payload(
    settings: Settings,
    payload: Mapping[str, Any],
) -> Mapping[str, Any]:
    import httpx

    hydrated_events: list[Any] = []
    for event in payload.get("events", []) or []:
        if not isinstance(event, Mapping):
            hydrated_events.append(event)
            continue
        slug = string(event.get("slug"))
        if not slug:
            hydrated_events.append(event)
            continue
        try:
            response = httpx.get(
                f"{settings.polymarket_gamma_base.rstrip('/')}/events/slug/{slug}",
                timeout=20,
            )
            response.raise_for_status()
            hydrated = response.json()
        except Exception:
            hydrated_events.append(event)
            continue
        hydrated_events.append(hydrated if isinstance(hydrated, Mapping) else event)
    output = dict(payload)
    output["events"] = hydrated_events
    return output


def markets_from_matches(
    matches: list[ResolvedEventMatch],
    candidates: list[PolymarketCandidate],
) -> list[Market]:
    candidates_by_id = {candidate.market_id: candidate for candidate in candidates}
    markets: list[Market] = []
    for match in matches:
        if not match.polymarket_market_id:
            continue
        candidate = candidates_by_id.get(match.polymarket_market_id)
        if not candidate:
            continue
        markets.append(
            Market(
                market_id=candidate.market_id,
                event_id=candidate.event_id,
                sport=candidate.sport,
                league=candidate.league,
                title=candidate.title,
                description=str(candidate.raw.get("description") or ""),
                outcomes=list(candidate.outcomes),
                token_ids=list(candidate.token_ids),
                start_time=candidate.start_time,
                end_time=None,
                resolution_source=str(candidate.raw.get("resolutionSource") or ""),
                status="active" if not candidate.closed and candidate.active else "closed",
                raw_metadata=dict(candidate.raw),
            )
        )
    return markets


def rewrite_matched_odds(
    normalized_odds: list[ExternalOdds],
    matches: list[ResolvedEventMatch],
) -> list[ExternalOdds]:
    match_by_odds_id = {match.odds_event_id: match for match in matches}
    output: list[ExternalOdds] = []
    seen: set[tuple[str, str, str, str, str]] = set()
    output.extend(rewrite_binary_yes_no_odds(normalized_odds, matches, seen))
    for row in normalized_odds:
        match = match_by_odds_id.get(row.event_id)
        if not match or not match.polymarket_event_id:
            continue
        if binary_yes_no_target(match):
            continue
        if not market_types_compatible(row.market_type, match.market_type, odds_is_fixture=True):
            continue
        mapped = map_external_outcome(row.outcome, match)
        if not mapped:
            continue
        key = (
            match.polymarket_event_id,
            row.book,
            match.market_type,
            mapped,
            row.timestamp.isoformat(),
        )
        if key in seen:
            continue
        seen.add(key)
        output.append(
            ExternalOdds(
                event_id=match.polymarket_event_id,
                sport=match.sport,
                book=row.book,
                market_type=match.market_type,
                outcome=mapped,
                decimal_odds=row.decimal_odds,
                timestamp=row.timestamp,
            )
        )
    return output


def rewrite_binary_yes_no_odds(
    normalized_odds: list[ExternalOdds],
    matches: list[ResolvedEventMatch],
    seen: set[tuple[str, str, str, str, str]],
) -> list[ExternalOdds]:
    output: list[ExternalOdds] = []
    matches_by_odds_id = {
        match.odds_event_id: match
        for match in matches
        if match.polymarket_event_id and binary_yes_no_target(match)
    }
    rows_by_group: dict[tuple[str, str, str, str], list[ExternalOdds]] = {}
    for row in normalized_odds:
        match = matches_by_odds_id.get(row.event_id)
        if not match:
            continue
        if not market_types_compatible(row.market_type, match.market_type, odds_is_fixture=True):
            continue
        key = (row.event_id, row.book, row.market_type, row.timestamp.isoformat())
        rows_by_group.setdefault(key, []).append(row)

    for (odds_event_id, book, _market_type, timestamp_key), rows in rows_by_group.items():
        match = matches_by_odds_id[odds_event_id]
        target = binary_yes_no_target(match)
        if not target:
            continue
        allowed_outcomes = (match.odds_home, "Draw", match.odds_away)
        odds_by_outcome = {
            row.outcome: row.decimal_odds
            for row in rows
            if any(outcome_matches_target(row.outcome, allowed) for allowed in allowed_outcomes)
        }
        matched_target = next(
            (outcome for outcome in odds_by_outcome if outcome_matches_target(outcome, target)),
            None,
        )
        if not matched_target or len(odds_by_outcome) < 2:
            continue
        try:
            probabilities = generic_no_vig(odds_by_outcome)
        except ValueError:
            continue
        fair_probability = probabilities.get(matched_target)
        if fair_probability is None or fair_probability <= 0.0 or fair_probability >= 1.0:
            continue
        timestamp = rows[0].timestamp
        synthetic_rows = [
            ("Yes", 1.0 / fair_probability),
            ("No", 1.0 / (1.0 - fair_probability)),
        ]
        for outcome, decimal_odds in synthetic_rows:
            key = (match.polymarket_event_id, book, match.market_type, outcome, timestamp_key)
            if key in seen:
                continue
            seen.add(key)
            output.append(
                ExternalOdds(
                    event_id=match.polymarket_event_id,
                    sport=match.sport,
                    book=book,
                    market_type=match.market_type,
                    outcome=outcome,
                    decimal_odds=decimal_odds,
                    timestamp=timestamp,
                )
            )
    return output


def binary_yes_no_target(match: ResolvedEventMatch) -> str | None:
    title = match.polymarket_title
    normalized = title.lower()
    if "end in a draw" in normalized or "end in draw" in normalized:
        return "Draw"
    if " win on " in normalized:
        home_score = name_similarity_for_title(match.odds_home, title)
        away_score = name_similarity_for_title(match.odds_away, title)
        if max(home_score, away_score) < 0.55:
            return None
        return match.odds_home if home_score >= away_score else match.odds_away
    return None


def outcome_matches_target(outcome: str, target: str) -> bool:
    return name_similarity_for_title(outcome, target) >= 0.72


def name_similarity_for_title(left: str, right: str) -> float:
    from sharpedge.data.event_resolver import name_similarity

    return name_similarity(left, right)


def map_external_outcome(outcome: str, match: ResolvedEventMatch) -> str | None:
    if outcome in match.outcome_mapping:
        return match.outcome_mapping[outcome]
    if outcome.lower() == "draw" and "Draw" in match.outcome_mapping:
        return match.outcome_mapping["Draw"]
    for external, polymarket in match.outcome_mapping.items():
        if outcome.lower() == external.lower():
            return polymarket
    return None


def risk_diagnostics(
    signals: list[Signal],
    markets: list[Market],
    fair_values: list[FairValue],
    orderbooks: list[OrderBook],
    settings: Settings,
    as_of: datetime,
) -> list[dict[str, Any]]:
    markets_by_id = {market.market_id: market for market in markets}
    fair_by_key = {(value.market_id, value.outcome): value for value in fair_values}
    bbas = {
        (book.market_id, book.token_id): book.best_bid_ask()
        for book in orderbooks
    }
    outcome_to_bba = {}
    for market in markets:
        for outcome, token_id in zip(market.outcomes, market.token_ids, strict=False):
            bba = bbas.get((market.market_id, token_id))
            if bba:
                outcome_to_bba[(market.market_id, outcome)] = bba
    risk = RiskManager(settings)
    rows: list[dict[str, Any]] = []
    for signal in signals:
        market = markets_by_id[signal.market_id]
        bba = outcome_to_bba.get((signal.market_id, signal.outcome))
        fair = fair_by_key.get((signal.market_id, signal.outcome))
        if not bba or not fair:
            continue
        decision = risk.check_signal(
            signal=signal,
            market=market,
            proposed_size=min(settings.max_trade_size, signal.available_size),
            orderbook_timestamp=bba.timestamp,
            odds_timestamp=fair.timestamp,
            live=False,
            resolution_rules_clear=bool(market.resolution_source),
            as_of=as_of,
        )
        rows.append({"signal": signal, "approved": decision.approved, "reasons": decision.reasons})
    return rows


def render_report(
    as_of: datetime,
    args: argparse.Namespace,
    odds_events: list[dict[str, Any]],
    candidates: list[PolymarketCandidate],
    matches: list[ResolvedEventMatch],
    review: list[ResolvedEventMatch],
    rejected: list[ResolvedEventMatch],
    markets: list[Market],
    orderbooks: list[OrderBook],
    fair_values: list[FairValue],
    signals: list[Signal],
    risk_rows: list[dict[str, Any]],
    warnings: list[str],
    odds_meta: dict[str, Any],
) -> str:
    rejection_counts = Counter(row.rejection_reason or "below_threshold" for row in rejected)
    risk_by_signal = {row["signal"].market_id + row["signal"].outcome: row for row in risk_rows}
    lines = [
        "# SharpEdge Sports Live Odds-First Scan",
        "",
        f"as_of: `{as_of.isoformat()}`",
        "",
        "## Data Coverage",
        "",
        f"- Odds events: {len(odds_events)}",
        f"- Polymarket candidates searched: {len(candidates)}",
        f"- Matched: {len(matches)}; review: {len(review)}; rejected: {len(rejected)}",
        f"- Matched markets with CLOB books requested: {len(markets)}",
        f"- Order books collected: {len(orderbooks)}",
        f"- Fair values generated: {len(fair_values)}",
        f"- Odds source: {odds_meta.get('source')}; Odds-API.io requests made: {odds_meta.get('requests_made')}",
        f"- Odds-API.io league filter: {odds_meta.get('league') or 'none'}",
        "",
        "## Rejection Reasons",
        "",
    ]
    if rejection_counts:
        lines.extend(markdown_table(["reason", "count"], [[k, str(v)] for k, v in rejection_counts.items()]))
    else:
        lines.append("No rejected matches.")
    lines.extend(["", "## Edge Table", ""])
    if signals:
        rows = []
        for signal in signals:
            fair = next(
                (fv for fv in fair_values if fv.market_id == signal.market_id and fv.outcome == signal.outcome),
                None,
            )
            costs = costs_from_settings(args_settings(args), fair) if fair else None
            risk_row = risk_by_signal.get(signal.market_id + signal.outcome)
            rows.append(
                [
                    market_title(signal.market_id, markets),
                    signal.outcome,
                    pct(signal.fair_probability),
                    price(signal.polymarket_bid),
                    price(signal.polymarket_ask),
                    pct(signal.raw_edge),
                    pct(costs.total if costs else 0.0),
                    pct(signal.adjusted_edge),
                    f"{signal.available_size:.1f}",
                    signal.action,
                    "yes" if risk_row and risk_row["approved"] else "no",
                    "; ".join(risk_row["reasons"]) if risk_row else signal.reason,
                ]
            )
        lines.extend(
            markdown_table(
                [
                    "event/market",
                    "outcome",
                    "fair",
                    "bid",
                    "ask",
                    "raw_edge",
                    "cost_drag",
                    "adjusted",
                    "size",
                    "action",
                    "risk_ok",
                    "risk/reason",
                ],
                rows,
            )
        )
    else:
        lines.append("No edge signals generated.")
    lines.extend(["", "## Warnings", ""])
    warning_lines = list(warnings)
    if not matches:
        warning_lines.append("no matched events: external fixture slate did not map to active Polymarket match markets")
    if review:
        warning_lines.append("some event matches require manual review")
    if not warning_lines:
        warning_lines.append("no major data-quality warnings in this run")
    for warning in warning_lines:
        lines.append(f"- {warning}")
    lines.extend(["", "## Research Interpretation", ""])
    if not matches:
        lines.append(
            "There is no evidence of tradable edge in this run because no comparable "
            "Polymarket match markets were found for the external odds events."
        )
    elif not signals:
        lines.append(
            "Comparable markets were found, but no fair-value signal survived matching, "
            "order-book, and model requirements."
        )
    else:
        approved = sum(1 for row in risk_rows if row["approved"])
        lines.append(
            f"Signals generated: {len(signals)}; risk-approved paper candidates: {approved}. "
            "Do not enable live trading until CLV and paper-fill evidence are positive over time."
        )
    lines.append("")
    lines.append("Live trading remains disabled. This report is research/paper only.")
    return "\n".join(lines)


def args_settings(args: argparse.Namespace) -> Settings:
    if args.min_adjusted_edge is not None:
        return Settings(MIN_ADJUSTED_EDGE=args.min_adjusted_edge)
    return Settings()


def write_candidates_json(path: Path, candidates: list[PolymarketCandidate]) -> None:
    payload = [
        {
            "odds_event_id": c.odds_event_id,
            "event_id": c.event_id,
            "market_id": c.market_id,
            "title": c.title,
            "sport": c.sport,
            "league": c.league,
            "start_time": c.start_time.isoformat() if c.start_time else "",
            "outcomes": list(c.outcomes),
            "token_ids": list(c.token_ids),
            "status": c.status,
            "closed": c.closed,
            "active": c.active,
            "accepting_orders": c.accepting_orders,
            "market_type": c.market_type,
        }
        for c in candidates
    ]
    write_json(path, payload)


def write_markets_json(path: Path, markets: list[Market]) -> None:
    write_json(path, [market.model_dump(mode="json") for market in markets])


def write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def safe_cache_key(value: str) -> str:
    import hashlib

    return hashlib.sha256(value.lower().strip().encode()).hexdigest()[:24]


def league_name(event: Mapping[str, Any]) -> str:
    league = event.get("league")
    if isinstance(league, Mapping):
        return string(league.get("name") or league.get("slug"))
    return string(league)


def event_matches_league(event: Mapping[str, Any], league: str | None) -> bool:
    if not league:
        return True
    value = event.get("league")
    if isinstance(value, Mapping):
        candidate = string(value.get("slug") or value.get("name")).lower()
    else:
        candidate = string(value).lower()
    wanted = league.strip().lower()
    return candidate == wanted or wanted in candidate


def sport_name(event: Mapping[str, Any]) -> str:
    sport = event.get("sport")
    if isinstance(sport, Mapping):
        return string(sport.get("name") or sport.get("slug"))
    return string(sport)


def string(value: Any) -> str:
    return "" if value is None else str(value).strip()


def market_title(market_id: str, markets: list[Market]) -> str:
    for market in markets:
        if market.market_id == market_id:
            return market.title
    return market_id


def markdown_table(headers: list[str], rows: list[list[str]]) -> list[str]:
    output = ["| " + " | ".join(headers) + " |", "| " + " | ".join("---" for _ in headers) + " |"]
    for row in rows:
        output.append("| " + " | ".join(str(cell).replace("|", "\\|") for cell in row) + " |")
    return output


def pct(value: float) -> str:
    return f"{100 * value:.2f}%"


def price(value: float | None) -> str:
    return "" if value is None else f"{value:.3f}"


if __name__ == "__main__":
    main()
