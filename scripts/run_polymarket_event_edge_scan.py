#!/usr/bin/env python
from __future__ import annotations

import argparse
import csv
import json
import re
import statistics
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import _bootstrap  # noqa: F401

from sharpedge.config.settings import Settings
from sharpedge.data.odds_api_io_provider import (
    OddsAPIIOProvider,
    read_external_odds_csv,
)
from sharpedge.data.odds_event_matcher import OddsEventCandidate, match_polymarket_text_to_odds_events
from sharpedge.data.polymarket_collector import parse_clob_order_book, parse_gamma_market
from sharpedge.data.schemas import BestBidAsk, ExternalOdds, FairValue, Market
from sharpedge.research.fair_value_model import FairValueModel
from sharpedge.research.no_vig_odds import generic_no_vig
from sharpedge.strategies.taker_edge import compute_edge_signal, costs_from_settings


EVENTS_PATH = Path("data/raw/odds_api_io_events.json")
ODDS_PATH = Path("data/raw/odds_api_io_normalized.csv")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Scan one Polymarket sports event against matched Odds-API.io fair values"
    )
    parser.add_argument("--event", required=True, help="Polymarket event URL or event slug")
    parser.add_argument("--sport", default="football")
    parser.add_argument("--league", help='Odds-API.io league slug, e.g. "england-premier-league"')
    parser.add_argument("--bookmakers", default="Bet365,Betfair Exchange")
    parser.add_argument("--max-events", type=int, default=100)
    parser.add_argument("--refresh-odds", action="store_true")
    parser.add_argument("--use-cache", action="store_true", help="Make zero Odds-API.io calls")
    parser.add_argument("--skip-clob", action="store_true", help="Use Gamma best bid/ask only")
    parser.add_argument("--min-match-score", type=float, default=0.82)
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("reports/polymarket_event_edge_scan.md"),
    )
    parser.add_argument(
        "--csv-output",
        type=Path,
        default=Path("reports/polymarket_event_edge_scan.csv"),
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    settings = Settings()
    slug = extract_polymarket_slug(args.event)
    if not slug:
        raise SystemExit("Provide a Polymarket event URL or slug so the script can fetch market prices.")

    polymarket_event = fetch_polymarket_event_by_slug(slug, settings)
    event_title = string(polymarket_event.get("title")) or title_from_polymarket_slug(slug)
    markets = parse_event_markets(polymarket_event)
    odds_events, odds_rows, odds_meta = load_or_refresh_odds(args, settings)
    candidates = match_polymarket_text_to_odds_events(
        polymarket_event=event_title,
        odds_events=odds_events,
        min_match_score=args.min_match_score,
    )
    best = candidates[0] if candidates else None
    if not best or best.decision not in {"MATCH", "REVIEW"}:
        write_outputs(args, event_title, best, odds_meta, [], [])
        print(f"No matched Odds-API.io event for {event_title}")
        print(f"Wrote {args.output}")
        return

    rows_for_event = [row for row in odds_rows if row.event_id == best.odds_event_id]
    as_of = datetime.now(UTC)
    scan_rows: list[dict[str, Any]] = []
    signals = []
    for market in markets:
        if not is_binary_yes_no(market):
            continue
        target = binary_target_for_market(market, best)
        if not target:
            continue
        fair_probability, fair_meta = fair_probability_from_1x2(
            target=target,
            odds_rows=rows_for_event,
            settings=settings,
            market=market,
            best=best,
            as_of=as_of,
        )
        if fair_probability is None:
            continue
        bba = (
            gamma_best_bid_ask(market, as_of)
            if args.skip_clob
            else fetch_yes_best_bid_ask(settings, market, as_of)
        )
        fair_value = FairValue(
            event_id=market.event_id,
            market_id=market.market_id,
            outcome="Yes",
            fair_probability=fair_probability,
            source=f"odds_api_io_no_vig_1x2:{fair_meta['books']}books",
            timestamp=as_of,
            uncertainty=float(fair_meta["uncertainty"]),
        )
        signal = compute_edge_signal(market, "Yes", fair_value, bba, settings, as_of=as_of)
        signals.append(signal)
        costs = costs_from_settings(settings, fair_value)
        scan_rows.append(
            {
                "event": event_title,
                "market": market.title,
                "target": target,
                "fair_probability": f"{fair_probability:.6f}",
                "polymarket_bid": "" if bba.best_bid is None else f"{bba.best_bid:.4f}",
                "polymarket_ask": "" if bba.best_ask is None else f"{bba.best_ask:.4f}",
                "raw_edge": f"{signal.raw_edge:.6f}",
                "cost_drag": f"{costs.total:.6f}",
                "adjusted_edge": f"{signal.adjusted_edge:.6f}",
                "available_size": f"{signal.available_size:.2f}",
                "action": signal.action,
                "reason": signal.reason,
                "books": str(fair_meta["books"]),
                "odds_timestamp": fair_meta["latest_timestamp"],
            }
        )

    write_outputs(args, event_title, best, odds_meta, scan_rows, signals)
    print(
        "Single-event scan complete: "
        f"event={event_title}, odds_match={best.odds_title}, markets_scanned={len(scan_rows)}, "
        f"signals={len(signals)}"
    )
    print(f"Wrote {args.output}")
    print(f"Wrote {args.csv_output}")


def load_or_refresh_odds(
    args: argparse.Namespace,
    settings: Settings,
) -> tuple[list[dict[str, Any]], list[ExternalOdds], dict[str, Any]]:
    if EVENTS_PATH.exists() and ODDS_PATH.exists() and not args.refresh_odds:
        events_payload = json.loads(EVENTS_PATH.read_text(encoding="utf-8"))
        events = [
            event
            for event in events_payload
            if isinstance(event, dict) and event_matches_league(event, args.league)
        ][: args.max_events]
        return events, read_external_odds_csv(ODDS_PATH), {
            "source": "cache",
            "requests_made": 0,
            "league": args.league or "",
        }
    if args.use_cache:
        raise FileNotFoundError("Cached Odds-API.io files are missing; rerun without --use-cache.")
    provider = OddsAPIIOProvider(settings)
    bundle = provider.fetch_snapshot(
        sport=args.sport,
        league=args.league,
        bookmakers=args.bookmakers,
        max_events=args.max_events,
        refresh=True,
    )
    return bundle.events, bundle.normalized_odds, {
        "source": "odds_api_io_refresh",
        "requests_made": bundle.requests_made,
        "league": args.league or "",
    }


def parse_event_markets(event_payload: dict[str, Any]) -> list[Market]:
    event = dict(event_payload)
    output: list[Market] = []
    for market_payload in event_payload.get("markets", []) or []:
        if not isinstance(market_payload, dict):
            continue
        enriched = dict(market_payload)
        enriched["event"] = event
        enriched["eventId"] = string(event_payload.get("id"))
        market = parse_gamma_market(enriched)
        if market:
            output.append(market)
    return output


def fair_probability_from_1x2(
    target: str,
    odds_rows: list[ExternalOdds],
    settings: Settings,
    market: Market,
    best: OddsEventCandidate,
    as_of: datetime,
) -> tuple[float | None, dict[str, Any]]:
    allowed_outcomes = (best.odds_home, "Draw", best.odds_away)
    grouped: dict[tuple[str, str], list[ExternalOdds]] = defaultdict(list)
    for row in odds_rows:
        if row.market_type != "1x2" or row.timestamp > as_of:
            continue
        if not any(outcomes_match(row.outcome, allowed) for allowed in allowed_outcomes):
            continue
        grouped[(row.book, row.timestamp.isoformat())].append(row)

    book_probs: list[float] = []
    latest_timestamp: datetime | None = None
    for rows in grouped.values():
        odds_by_outcome = {row.outcome: row.decimal_odds for row in rows}
        matched = next((outcome for outcome in odds_by_outcome if outcomes_match(outcome, target)), None)
        if not matched or len(odds_by_outcome) < 2:
            continue
        try:
            probabilities = generic_no_vig(odds_by_outcome)
        except ValueError:
            continue
        book_probs.append(probabilities[matched])
        latest_timestamp = max(latest_timestamp or rows[0].timestamp, *(row.timestamp for row in rows))

    if not book_probs or latest_timestamp is None:
        return None, {"books": 0, "uncertainty": 1.0, "latest_timestamp": ""}
    fair_probability = statistics.fmean(book_probs)
    uncertainty = FairValueModel(
        base_uncertainty_bps=settings.model_uncertainty_bps,
        now=as_of,
    ).estimate_uncertainty(
        book_probs=book_probs,
        books_count=len(book_probs),
        latest_odds_timestamp=latest_timestamp,
        market_liquidity=float(market.raw_metadata.get("liquidityNum") or 0.0),
        event_start=market.start_time,
        as_of=as_of,
    )
    return fair_probability, {
        "books": len(book_probs),
        "uncertainty": uncertainty,
        "latest_timestamp": latest_timestamp.isoformat(),
    }


def fetch_yes_best_bid_ask(settings: Settings, market: Market, as_of: datetime) -> BestBidAsk:
    token_id = yes_token_id(market)
    if not token_id:
        return gamma_best_bid_ask(market, as_of)
    try:
        import httpx

        response = httpx.get(
            f"{settings.polymarket_clob_base.rstrip('/')}/book",
            params={"token_id": token_id},
            timeout=6,
        )
        response.raise_for_status()
        orderbook = parse_clob_order_book(market.market_id, token_id, response.json())
    except Exception:
        return gamma_best_bid_ask(market, as_of)
    return orderbook.best_bid_ask() if orderbook else gamma_best_bid_ask(market, as_of)


def gamma_best_bid_ask(market: Market, as_of: datetime) -> BestBidAsk:
    best_bid = parse_optional_float(market.raw_metadata.get("bestBid"))
    best_ask = parse_optional_float(market.raw_metadata.get("bestAsk"))
    yes_token = yes_token_id(market) or "yes"
    spread = None if best_bid is None or best_ask is None else max(0.0, best_ask - best_bid)
    mid = None if best_bid is None or best_ask is None else (best_bid + best_ask) / 2
    return BestBidAsk(
        market_id=market.market_id,
        token_id=yes_token,
        timestamp=as_of,
        best_bid=best_bid,
        best_bid_size=None,
        best_ask=best_ask,
        best_ask_size=None,
        mid=mid,
        spread=spread,
    )


def yes_token_id(market: Market) -> str | None:
    for outcome, token_id in zip(market.outcomes, market.token_ids, strict=False):
        if outcome.lower() == "yes":
            return token_id
    return market.token_ids[0] if market.token_ids else None


def binary_target_for_market(market: Market, best: OddsEventCandidate) -> str | None:
    title = market.title.lower()
    if "end in a draw" in title or "end in draw" in title:
        return "Draw"
    if " win on " not in title:
        return None
    home_score = similarity(best.odds_home, market.title)
    away_score = similarity(best.odds_away, market.title)
    if max(home_score, away_score) < 0.55:
        return None
    return best.odds_home if home_score >= away_score else best.odds_away


def is_binary_yes_no(market: Market) -> bool:
    return {outcome.lower() for outcome in market.outcomes} == {"yes", "no"}


def outcomes_match(left: str, right: str) -> bool:
    return similarity(left, right) >= 0.72


def similarity(left: str, right: str) -> float:
    from sharpedge.data.event_resolver import name_similarity

    return name_similarity(left, right)


def write_outputs(
    args: argparse.Namespace,
    event_title: str,
    best: OddsEventCandidate | None,
    odds_meta: dict[str, Any],
    rows: list[dict[str, Any]],
    signals: list[Any],
) -> None:
    args.csv_output.parent.mkdir(parents=True, exist_ok=True)
    columns = [
        "event",
        "market",
        "target",
        "fair_probability",
        "polymarket_bid",
        "polymarket_ask",
        "raw_edge",
        "cost_drag",
        "adjusted_edge",
        "available_size",
        "action",
        "reason",
        "books",
        "odds_timestamp",
    ]
    with args.csv_output.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns)
        writer.writeheader()
        writer.writerows(rows)
    report = render_report(event_title, best, odds_meta, rows, signals)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(report, encoding="utf-8")


def render_report(
    event_title: str,
    best: OddsEventCandidate | None,
    odds_meta: dict[str, Any],
    rows: list[dict[str, Any]],
    signals: list[Any],
) -> str:
    lines = [
        "# Polymarket Event Edge Scan",
        "",
        f"- Polymarket event: {event_title}",
        f"- Odds source: {odds_meta.get('source')}",
        f"- Odds-API.io requests made: {odds_meta.get('requests_made', 0)}",
        f"- League filter: {odds_meta.get('league') or 'none'}",
        "",
        "## Matched External Event",
        "",
    ]
    if best:
        lines.extend(
            [
                f"- Decision: {best.decision}",
                f"- Score: {best.score:.3f}",
                f"- Odds-API.io event id: `{best.odds_event_id}`",
                f"- Odds-API.io event: {best.odds_title}",
                f"- League: {best.league}",
                f"- Start time: {best.start_time.isoformat() if best.start_time else ''}",
            ]
        )
    else:
        lines.append("No external event match.")
    lines.extend(["", "## Edge Table", ""])
    if rows:
        lines.extend(markdown_table(list(rows[0]), [[str(row[key]) for key in rows[0]] for row in rows]))
    else:
        lines.append("No comparable binary 1X2 markets were found.")
    lines.extend(["", "## Interpretation", ""])
    if not rows:
        lines.append("No tradable edge conclusion can be made from this event.")
    else:
        live_actions = [signal for signal in signals if signal.action in {"BUY_YES", "SELL_YES"}]
        lines.append(
            f"Signals scanned: {len(signals)}. Risk/edge-approved actions: {len(live_actions)}. "
            "Live trading remains disabled; use paper execution only."
        )
    return "\n".join(lines)


def markdown_table(headers: list[str], rows: list[list[str]]) -> list[str]:
    output = ["| " + " | ".join(headers) + " |", "| " + " | ".join("---" for _ in headers) + " |"]
    for row in rows:
        output.append("| " + " | ".join(cell.replace("|", "\\|") for cell in row) + " |")
    return output


def fetch_polymarket_event_by_slug(slug: str, settings: Settings) -> dict[str, Any]:
    import httpx

    response = httpx.get(
        f"{settings.polymarket_gamma_base.rstrip('/')}/events/slug/{slug}",
        timeout=20,
    )
    response.raise_for_status()
    payload = response.json()
    return payload if isinstance(payload, dict) else {}


def extract_polymarket_slug(value: str) -> str | None:
    text = value.strip()
    parsed = urlparse(text)
    if parsed.scheme and "polymarket.com" in parsed.netloc:
        parts = [part for part in parsed.path.split("/") if part]
        return parts[-1] if parts else None
    if re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)+-\d{4}-\d{2}-\d{2}", text):
        return text
    return None


TEAM_ABBREVIATIONS = {
    "lee": "Leeds United FC",
    "bri": "Brighton & Hove Albion FC",
}


def title_from_polymarket_slug(slug: str) -> str:
    parts = slug.split("-")
    if len(parts) >= 6 and re.fullmatch(r"\d{4}", parts[-3] or ""):
        home_code = parts[-5]
        away_code = parts[-4]
        return (
            f"{TEAM_ABBREVIATIONS.get(home_code, home_code.upper())} vs "
            f"{TEAM_ABBREVIATIONS.get(away_code, away_code.upper())}"
        )
    return slug.replace("-", " ")


def event_matches_league(event: dict[str, Any], league: str | None) -> bool:
    if not league:
        return True
    value = event.get("league")
    if isinstance(value, dict):
        candidate = string(value.get("slug") or value.get("name")).lower()
    else:
        candidate = string(value).lower()
    wanted = league.strip().lower()
    return candidate == wanted or wanted in candidate


def parse_optional_float(value: Any) -> float | None:
    if value in {None, ""}:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def string(value: Any) -> str:
    return "" if value is None else str(value).strip()


if __name__ == "__main__":
    main()
