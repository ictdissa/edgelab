"""Market-type classification shared by discovery, matching, and research."""

from __future__ import annotations

import re
from typing import Sequence


MARKET_TYPES = {
    "moneyline",
    "1x2",
    "total",
    "spread",
    "exact_score",
    "method_of_victory",
    "goes_distance",
    "outright",
    "top_4",
    "unknown",
}


def classify_market_type(
    title: str,
    sport: str | None = None,
    outcomes: Sequence[str] | None = None,
) -> str:
    """Classify a market without using provider-specific IDs."""

    text = _normalize(f"{title} {' '.join(outcomes or [])}")
    sport_norm = _normalize(sport or "")

    if re.search(r"\b(top\s*4|top\s*four|finish(?:es)?\s+top)\b", text):
        return "top_4"
    if _looks_like_outright(text):
        return "outright"
    if "exact score" in text or "correct score" in text or "scoreline" in text:
        return "exact_score"
    if "goes the distance" in text or "go distance" in text or "fight goes distance" in text:
        return "goes_distance"
    if "method of victory" in text or (
        sport_norm in {"mma", "ufc"} and re.search(r"\b(ko|tko|submission|decision)\b", text)
    ):
        return "method_of_victory"
    if re.search(r"\b(spread|handicap|asian handicap)\b", text):
        return "spread"
    if re.search(r"\b(o/u|over/under|over under|total goals?|totals?)\b", text):
        return "total"
    if "1x2" in text or "draw" in text:
        return "1x2"
    if re.search(r"\b(match winner|moneyline|ml)\b", text):
        return "1x2" if sport_norm in {"football", "soccer"} else "moneyline"
    if sport_norm in {"football", "soccer"} and re.search(
        r"\bwill\s+.+\s+win\s+on\s+\d{4}\s+\d{2}\s+\d{2}\b", text
    ):
        return "1x2"
    if sport_norm in {"football", "soccer"} and _has_three_way_outcomes(outcomes or []):
        return "1x2"
    if sport_norm in {"mma", "ufc"}:
        return "moneyline"
    return "unknown"


def normalize_external_market_type(value: str, sport: str | None = None) -> str:
    text = _normalize(value).replace(" ", "_")
    aliases = {
        "ml": "1x2" if _normalize(sport or "") in {"football", "soccer"} else "moneyline",
        "moneyline": "1x2" if _normalize(sport or "") in {"football", "soccer"} else "moneyline",
        "match_result": "1x2",
        "match_winner": "1x2",
        "h2h": "1x2",
        "head_to_head": "1x2",
        "over_under": "total",
        "totals": "total",
        "total_goals": "total",
        "asian_handicap": "spread",
        "correct_score": "exact_score",
        "top_four": "top_4",
        "finish_top_four": "top_4",
        "league_winner": "outright",
        "title_winner": "outright",
        "winner": "outright",
        "futures": "outright",
    }
    return aliases.get(text, text if text in MARKET_TYPES else "unknown")


def market_types_compatible(
    odds_market_type: str,
    polymarket_market_type: str,
    odds_is_fixture: bool = True,
) -> bool:
    odds_type = normalize_external_market_type(odds_market_type, "soccer" if odds_is_fixture else None)
    pm_type = normalize_external_market_type(polymarket_market_type)
    if odds_is_fixture and pm_type in {"outright", "top_4"}:
        return False
    if odds_type == "1x2":
        return pm_type in {"1x2", "moneyline"}
    return odds_type == pm_type


def _looks_like_outright(text: str) -> bool:
    if re.search(r"\b(vs|v|at|match winner|1x2)\b", text):
        return False
    if re.search(r"\b(world cup|champions league|premier league|league|title|championship)\b", text) and re.search(
        r"\b(winner|win|wins|to win)\b", text
    ):
        return True
    if re.search(r"\bwill\s+.+\s+win\s+(?:the\s+)?(?:\d{4}\s+)?(?:fifa\s+)?world cup\b", text):
        return True
    return False


def _has_three_way_outcomes(outcomes: Sequence[str]) -> bool:
    normalized = {_normalize(outcome) for outcome in outcomes}
    return "draw" in normalized and len(normalized) >= 3


def _normalize(value: str) -> str:
    lowered = str(value).lower()
    lowered = re.sub(r"[^a-z0-9]+", " ", lowered)
    return re.sub(r"\s+", " ", lowered).strip()
