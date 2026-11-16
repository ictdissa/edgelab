#!/usr/bin/env python
from __future__ import annotations

import argparse
import csv
import json
import re
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import _bootstrap  # noqa: F401

from sharpedge.config.settings import Settings
from sharpedge.data.odds_api_io_provider import (
    OddsAPIIOProvider,
    read_external_odds_csv,
    write_external_odds_csv,
)
from sharpedge.data.odds_event_matcher import OddsEventCandidate, match_polymarket_text_to_odds_events
from sharpedge.data.schemas import ExternalOdds


EVENTS_PATH = Path("data/raw/odds_api_io_events.json")
ODDS_PATH = Path("data/raw/odds_api_io_normalized.csv")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Find the closest Odds-API.io event for a typed Polymarket event title"
    )
    parser.add_argument(
        "--event",
        required=True,
        help='Polymarket event/title, e.g. "Arsenal vs Chelsea"',
    )
    parser.add_argument("--sport", default="football")
    parser.add_argument(
        "--league",
        help='Optional Odds-API.io league slug, e.g. "england-premier-league"',
    )
    parser.add_argument("--bookmakers", default="Bet365,Betfair Exchange")
    parser.add_argument("--max-events", type=int, default=100)
    parser.add_argument("--refresh", action="store_true", help="Refresh Odds-API.io cache first")
    parser.add_argument(
        "--use-cache",
        action="store_true",
        help="Require cached Odds-API.io data and make zero API calls",
    )
    parser.add_argument("--min-match-score", type=float, default=0.82)
    parser.add_argument(
        "--audit-output",
        type=Path,
        default=Path("reports/polymarket_to_odds_api_matches.csv"),
    )
    parser.add_argument(
        "--odds-output",
        type=Path,
        default=Path("data/raw/polymarket_event_odds_match.csv"),
    )
    parser.add_argument(
        "--report-output",
        type=Path,
        default=Path("reports/polymarket_to_odds_api_lookup.md"),
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    settings = Settings()
    resolved_event, resolution_meta = resolve_polymarket_event_input(args.event, settings)
    events, odds, meta = load_or_refresh(args, settings)
    meta.update(resolution_meta)
    candidates = match_polymarket_text_to_odds_events(
        polymarket_event=resolved_event,
        odds_events=events,
        min_match_score=args.min_match_score,
    )
    write_audit(args.audit_output, candidates)
    best = candidates[0] if candidates else None
    matched_odds = [
        row
        for row in odds
        if best and best.decision in {"MATCH", "REVIEW"} and row.event_id == best.odds_event_id
    ]
    write_external_odds_csv(args.odds_output, matched_odds)
    report = render_report(args.event, resolved_event, candidates, matched_odds, meta)
    args.report_output.parent.mkdir(parents=True, exist_ok=True)
    args.report_output.write_text(report, encoding="utf-8")

    print(f"Best decision: {best.decision if best else 'NO_DATA'}")
    if best:
        print(f"Resolved event: {resolved_event}")
        print(f"Best score: {best.score:.3f}")
        print(f"Odds-API.io event: {best.odds_event_id} | {best.odds_title}")
        print(f"Odds rows written: {len(matched_odds)}")
    print(f"Wrote {args.audit_output}")
    print(f"Wrote {args.odds_output}")
    print(f"Wrote {args.report_output}")


def load_or_refresh(
    args: argparse.Namespace,
    settings: Settings,
) -> tuple[list[dict[str, Any]], list[ExternalOdds], dict[str, Any]]:
    if EVENTS_PATH.exists() and ODDS_PATH.exists() and not args.refresh:
        events_payload = json.loads(EVENTS_PATH.read_text(encoding="utf-8"))
        events = [
            event
            for event in events_payload
            if isinstance(event, dict) and _event_matches_league(event, args.league)
        ]
        odds = read_external_odds_csv(ODDS_PATH)
        return events[: args.max_events], odds, {
            "source": "cache",
            "requests_made": 0,
            "league": args.league or "",
        }
    if args.use_cache:
        raise FileNotFoundError("Cached Odds-API.io files are missing; remove --use-cache or run with --refresh")
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
        "remaining_budget": bundle.remaining_budget,
        "league": args.league or "",
    }


def resolve_polymarket_event_input(raw: str, settings: Settings) -> tuple[str, dict[str, Any]]:
    slug = extract_polymarket_slug(raw)
    if not slug:
        return raw, {"polymarket_resolution": "plain_text"}
    try:
        payload = fetch_polymarket_event_by_slug(slug, settings)
    except Exception as exc:
        fallback = title_from_polymarket_slug(slug)
        return fallback, {
            "polymarket_resolution": "slug_fallback",
            "polymarket_slug": slug,
            "polymarket_resolution_error": str(exc),
        }
    title = str(payload.get("title") or "").strip()
    if title:
        return title, {
            "polymarket_resolution": "gamma_slug",
            "polymarket_slug": slug,
            "polymarket_event_id": str(payload.get("id") or ""),
            "polymarket_event_closed": str(bool(payload.get("closed"))),
        }
    fallback = title_from_polymarket_slug(slug)
    return fallback, {"polymarket_resolution": "slug_fallback", "polymarket_slug": slug}


def _event_matches_league(event: dict[str, Any], league: str | None) -> bool:
    if not league:
        return True
    value = event.get("league")
    if isinstance(value, dict):
        candidate = str(value.get("slug") or value.get("name") or "").strip().lower()
    else:
        candidate = str(value or "").strip().lower()
    wanted = league.strip().lower()
    return candidate == wanted or wanted in candidate


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
    "ars": "Arsenal FC",
    "avl": "Aston Villa FC",
    "bou": "AFC Bournemouth",
    "bre": "Brentford FC",
    "bri": "Brighton & Hove Albion FC",
    "bur": "Burnley FC",
    "che": "Chelsea FC",
    "cry": "Crystal Palace FC",
    "eve": "Everton FC",
    "ful": "Fulham FC",
    "lee": "Leeds United FC",
    "liv": "Liverpool FC",
    "mci": "Manchester City FC",
    "mun": "Manchester United FC",
    "new": "Newcastle United FC",
    "nfo": "Nottingham Forest FC",
    "sun": "Sunderland AFC",
    "tot": "Tottenham Hotspur FC",
    "whu": "West Ham United FC",
    "wol": "Wolverhampton Wanderers FC",
}


def title_from_polymarket_slug(slug: str) -> str:
    parts = slug.split("-")
    if len(parts) >= 6 and re.fullmatch(r"\d{4}", parts[-3] or ""):
        home_code = parts[-5]
        away_code = parts[-4]
    elif len(parts) >= 5:
        home_code = parts[1]
        away_code = parts[2]
    else:
        return slug.replace("-", " ")
    home = TEAM_ABBREVIATIONS.get(home_code, home_code.upper())
    away = TEAM_ABBREVIATIONS.get(away_code, away_code.upper())
    return f"{home} vs {away}"


def write_audit(path: Path, candidates: list[OddsEventCandidate]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    columns = [
        "decision",
        "score",
        "query",
        "odds_event_id",
        "odds_title",
        "league",
        "sport",
        "start_time",
        "reasons",
        "rejection_reason",
    ]
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns)
        writer.writeheader()
        for item in candidates:
            writer.writerow(
                {
                    "decision": item.decision,
                    "score": f"{item.score:.4f}",
                    "query": item.query,
                    "odds_event_id": item.odds_event_id,
                    "odds_title": item.odds_title,
                    "league": item.league,
                    "sport": item.sport,
                    "start_time": item.start_time.isoformat() if item.start_time else "",
                    "reasons": "; ".join(item.reasons),
                    "rejection_reason": item.rejection_reason or "",
                }
            )


def render_report(
    raw_query: str,
    resolved_query: str,
    candidates: list[OddsEventCandidate],
    odds: list[ExternalOdds],
    meta: dict[str, Any],
) -> str:
    best = candidates[0] if candidates else None
    lines = [
        "# Polymarket To Odds-API.io Lookup",
        "",
        f"- Raw Polymarket input: `{raw_query}`",
        f"- Resolved event query: `{resolved_query}`",
        f"- Polymarket resolution: {meta.get('polymarket_resolution')}",
        f"- Odds data source: {meta.get('source')}",
        f"- Odds-API.io league filter: {meta.get('league') or 'none'}",
        f"- Odds-API.io requests made: {meta.get('requests_made', 0)}",
        f"- Candidates scored: {len(candidates)}",
        "",
        "## Best Match",
        "",
    ]
    if not best:
        lines.append("No Odds-API.io events were available to score.")
    else:
        lines.extend(
            [
                f"- Decision: {best.decision}",
                f"- Score: {best.score:.3f}",
                f"- Odds-API.io event id: `{best.odds_event_id}`",
                f"- Odds-API.io event: {best.odds_title}",
                f"- League: {best.league}",
                f"- Start time: {best.start_time.isoformat() if best.start_time else ''}",
                f"- Rejection reason: {best.rejection_reason or ''}",
                f"- Reasons: {'; '.join(best.reasons)}",
            ]
        )
    lines.extend(["", "## Top Candidates", ""])
    lines.extend(
        markdown_table(
            ["decision", "score", "event", "league", "reason"],
            [
                [
                    item.decision,
                    f"{item.score:.3f}",
                    item.odds_title,
                    item.league,
                    item.rejection_reason or "",
                ]
                for item in candidates[:10]
            ],
        )
    )
    lines.extend(["", "## Matched Odds Rows", ""])
    if odds:
        lines.extend(
            markdown_table(
                ["book", "market_type", "outcome", "decimal_odds", "timestamp"],
                [
                    [
                        row.book,
                        row.market_type,
                        row.outcome,
                        f"{row.decimal_odds:.3f}",
                        row.timestamp.isoformat(),
                    ]
                    for row in odds[:30]
                ],
            )
        )
    else:
        lines.append("No odds rows were found for the best candidate.")
    return "\n".join(lines)


def markdown_table(headers: list[str], rows: list[list[str]]) -> list[str]:
    output = ["| " + " | ".join(headers) + " |", "| " + " | ".join("---" for _ in headers) + " |"]
    for row in rows:
        output.append("| " + " | ".join(str(cell).replace("|", "\\|") for cell in row) + " |")
    return output


if __name__ == "__main__":
    main()
