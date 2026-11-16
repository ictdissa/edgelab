"""Outcome mapping between external anchors and prediction-market outcomes."""

from __future__ import annotations

import re
from difflib import SequenceMatcher
from typing import Sequence

from edgelab.matching.alias_registry import ALIASES, STOPWORDS
from edgelab.matching.market_classifier import normalize, unsupported_fixture_payoff


def normalize_name(value: str) -> str:
    tokens = [token for token in normalize(value).split() if token not in STOPWORDS]
    text = " ".join(tokens)
    return ALIASES.get(text, text)


def similarity(left: str, right: str) -> float:
    left_norm = normalize_name(left)
    right_norm = normalize_name(right)
    if not left_norm or not right_norm:
        return 0.0
    if left_norm == right_norm:
        return 1.0
    left_tokens = set(left_norm.split())
    right_tokens = set(right_norm.split())
    overlap = len(left_tokens & right_tokens)
    token_score = overlap / max(len(left_tokens), len(right_tokens), 1)
    containment = 0.86 if left_tokens <= right_tokens or right_tokens <= left_tokens else 0.0
    return max(token_score, containment, SequenceMatcher(None, left_norm, right_norm).ratio())


def map_outcomes(
    prediction_outcomes: Sequence[str],
    external_outcomes: Sequence[str],
    min_score: float = 0.72,
) -> dict[str, str]:
    mapping: dict[str, str] = {}
    for external in external_outcomes:
        scores = sorted(
            ((similarity(external, pred), pred) for pred in prediction_outcomes), reverse=True
        )
        best = scores[0] if scores else (0.0, "")
        if (
            best[0] >= min_score
            and (len(scores) < 2 or best[0] - scores[1][0] >= 0.10)
            and best[1] not in mapping.values()
        ):
            mapping[external] = best[1]
    return mapping


def binary_yes_target_from_title(title: str, home: str, away: str) -> str | None:
    if unsupported_fixture_payoff(title):
        return None
    normalized = normalize(title)
    if re.search(r"\bdraw\b", normalized):
        return "Draw"
    action = re.search(r"\b(win|wins|beat|beats|defeat|defeats)\b", normalized)
    if action is None:
        return None
    prefix = re.sub(r"\bto$", "", normalize_name(normalized[: action.start()])).strip()
    candidates = {
        candidate
        for candidate in (home, away)
        if candidate
        and normalize_name(candidate)
        and re.search(rf"\b{re.escape(normalize_name(candidate))}$", prefix)
    }
    return next(iter(candidates)) if len(candidates) == 1 else None
