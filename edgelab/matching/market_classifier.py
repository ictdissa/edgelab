"""Conservative market-type classifier."""

from __future__ import annotations

import re
import unicodedata
from typing import Sequence

from edgelab.data.schemas import MarketCategory


def unsupported_fixture_payoff(title: str) -> bool:
    """Reject props, compounds and non-full-game payoffs before winner inference.

    This is a conservative exclusion list, not a complete natural-language parser.
    It must also run at pricing boundaries where a supplied category may be wrong.
    """
    text = normalize(title)
    return bool(
        re.search(
            r"\b(double chance|draw no bet|both teams to score|btts|handicap|spread|"
            r"winning margin|clean sheet|without conceding|first team to score|"
            r"first to score|score first|first half|second half|1st half|2nd half|"
            r"half time|halftime|extra time|overtime|penalties|to qualify|qualifies|"
            r"not|never|fail|fails|avoid|avoids|lose|loses|either|or|no draw|"
            r"doesn t|won t)\b",
            text,
        )
        or re.search(r"\b(win|wins|beat|beats|defeat|defeats)\b.*\bby\b", text)
        or re.search(r"\b(win|wins|draw)\s+(or|and)\b", text)
    )


def classify_market(
    title: str,
    description: str | None = None,
    outcomes: Sequence[str] | None = None,
) -> MarketCategory:
    text = normalize(f"{title} {description or ''} {' '.join(outcomes or [])}")
    if not text or len(text.split()) < 3:
        return "unknown"
    title_text = normalize(title)
    if unsupported_fixture_payoff(title):
        return "unknown"
    # A fixture mentioned in supporting text must not override an outright question.
    if re.search(
        r"\b(world cup|champions league|premier league|league|title)\b", title_text
    ) and (
        re.search(r"\b(win the|wins the)\b", title_text)
        or (
            not re.search(r"\b(vs|versus|against|1x2|match winner)\b", title_text)
            and re.search(r"\b(winner|win the|wins the)\b", title_text)
        )
    ):
        return "sports_outright"
    if re.search(r"\b(btc|bitcoin|eth|ethereum)\b", text) and re.search(
        r"\b(above|below|over|under|hit|reach)\b", text
    ):
        return "crypto_threshold"
    if re.search(r"\b(temp|temperature|rain|rainfall|snow|hurricane|wind)\b", text) and re.search(
        r"\b(above|below|exceed|over|under)\b", text
    ):
        return "weather_threshold"
    if re.search(r"\b(election|nomination|president|senate|governor|mayor)\b", text) and re.search(
        r"\b(win|wins|elected)\b", text
    ):
        return "politics_election"
    fixture = re.search(r"\b(vs|versus|against|1x2|match winner|beats?|defeats?)\b", text)
    if fixture and re.search(r"\b(ufc|mma|boxing|fight|moneyline)\b", text):
        return (
            "sports_moneyline"
            if not re.search(r"\b(method|distance|knockout|submission|decision)\b", text)
            else "unknown"
        )
    if (
        fixture
        and "exact score" not in text
        and "correct score" not in text
        and not re.search(r"\b(goals?|points?|rounds?)\b", text)
    ):
        return "sports_1x2"
    if re.search(
        r"\b(world cup|champions league|premier league|league|title)\b", text
    ) and re.search(r"\b(winner|win the|wins the)\b", text):
        return "sports_outright"
    if "exact score" in text or "correct score" in text:
        return "sports_exact_score"
    if re.search(r"\b(total|o u|over under|over|under)\b", text) and re.search(
        r"\b(goals?|points?|rounds?)\b", text
    ):
        return "sports_total"
    if re.search(r"\bdraw\b", text) and re.search(r"\b(match|football|soccer)\b", text):
        return "sports_1x2"
    if re.search(r"\b(match winner|1x2|vs| v |against)\b", f" {text} "):
        return "sports_1x2"
    if re.search(r"\b(reach(?:es)? (?:the )?final|qualifies|top 4|wins tournament)\b", text):
        return "implication_market"
    if outcomes and len(outcomes) >= 3 and "exhaustive" in text and "mutually exclusive" in text:
        return "exhaustive_multi_outcome"
    return "unknown"


def is_fixture_winner_contract(
    title: str,
    description: str | None = None,
    outcomes: Sequence[str] | None = None,
) -> bool:
    """Shared fail-closed gate for discovery, valuation and execution boundaries."""
    return classify_market(title, description, outcomes) in {"sports_1x2", "sports_moneyline"}


def normalize(value: str) -> str:
    lowered = (
        unicodedata.normalize("NFKD", value)
        .encode("ascii", "ignore")
        .decode()
        .lower()
        .replace("$", " ")
    )
    lowered = re.sub(r"[^a-z0-9]+", " ", lowered)
    return re.sub(r"\s+", " ", lowered).strip()
