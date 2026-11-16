"""Archived predecessor model; use EdgeLab models for new research.

Source sharpness is not established. This compatibility module has no reviewed
settlement/receipt-time certification and does not support outright valuation.
"""

from __future__ import annotations

import statistics
import warnings
from collections import defaultdict
from datetime import UTC, datetime
from difflib import SequenceMatcher

from sharpedge.data.market_type import classify_market_type
from sharpedge.data.schemas import ExternalOdds, FairValue, Market, OrderBook
from sharpedge.research.no_vig_odds import generic_no_vig


def normalize_outcome_name(value: str) -> str:
    return (
        value.lower()
        .replace("/", " ")
        .replace("-", " ")
        .replace("_", " ")
        .replace("tko", "ko")
        .replace("  ", " ")
        .strip()
    )


def infer_market_type(market: Market) -> str:
    market_type = classify_market_type(market.title, market.sport, market.outcomes)
    return "moneyline" if market_type == "unknown" else market_type


def match_outcome(target: str, candidates: list[str]) -> str | None:
    """Map a Polymarket outcome name to an external odds outcome name."""

    target_norm = normalize_outcome_name(target)
    normalized = {normalize_outcome_name(candidate): candidate for candidate in candidates}
    if target_norm in normalized:
        return normalized[target_norm]
    for normalized_candidate, original in normalized.items():
        if target_norm in normalized_candidate or normalized_candidate in target_norm:
            return original
    best: tuple[float, str | None] = (0.0, None)
    for candidate in candidates:
        score = SequenceMatcher(
            None, target_norm, normalize_outcome_name(candidate)
        ).ratio()
        if score > best[0]:
            best = (score, candidate)
    return best[1] if best[0] >= 0.72 else None


class FairValueModel:
    """Deprecated historical compatibility, not an alternative live research engine."""

    def __init__(
        self,
        base_uncertainty_bps: float = 75.0,
        now: datetime | None = None,
    ) -> None:
        self.base_uncertainty = base_uncertainty_bps / 10_000.0
        self.now = now or datetime.now(UTC)

    def compute_fair_values(
        self,
        markets: list[Market],
        odds: list[ExternalOdds],
        orderbooks: list[OrderBook] | None = None,
        as_of: datetime | None = None,
    ) -> list[FairValue]:
        warnings.warn(
            "SharpEdge pricing is deprecated; use EdgeLab SportsOddsModel with "
            "event, payoff, settlement and receipt-time checks for new research.",
            DeprecationWarning,
            stacklevel=2,
        )
        as_of = as_of or self.now
        latest_odds = self._latest_odds(odds, as_of)
        books_by_event_market: dict[tuple[str, str], dict[str, list[ExternalOdds]]] = (
            defaultdict(lambda: defaultdict(list))
        )
        for row in latest_odds:
            books_by_event_market[(row.event_id, row.market_type)][row.book].append(row)

        liquidity_by_market = self._liquidity_by_market(orderbooks or [])
        values: list[FairValue] = []
        for market in markets:
            market_type = infer_market_type(market)
            if market_type == "outright":
                continue
            books = books_by_event_market.get((market.event_id, market_type), {})
            if not books:
                continue
            if market_type == "top_4":
                values.extend(
                    self._compute_non_exhaustive_binary_values(
                        market=market,
                        books=books,
                        liquidity_by_market=liquidity_by_market,
                        as_of=as_of,
                    )
                )
                continue
            probabilities_by_book: dict[str, dict[str, float]] = {}
            for book, rows in books.items():
                odds_by_outcome = {row.outcome: row.decimal_odds for row in rows}
                if len(odds_by_outcome) < 2:
                    continue
                try:
                    probabilities_by_book[book] = generic_no_vig(odds_by_outcome)
                except ValueError:
                    continue
            if not probabilities_by_book:
                continue

            external_outcomes = sorted(
                {
                    outcome
                    for probabilities in probabilities_by_book.values()
                    for outcome in probabilities
                }
            )
            latest_timestamp = max(
                row.timestamp for book_rows in books.values() for row in book_rows
            )
            for outcome in market.outcomes:
                matched = match_outcome(outcome, external_outcomes)
                if not matched:
                    continue
                book_probs = [
                    probs[matched]
                    for probs in probabilities_by_book.values()
                    if matched in probs
                ]
                if not book_probs:
                    continue
                fair_prob = statistics.fmean(book_probs)
                uncertainty = self.estimate_uncertainty(
                    book_probs=book_probs,
                    books_count=len(book_probs),
                    latest_odds_timestamp=latest_timestamp,
                    market_liquidity=liquidity_by_market.get(market.market_id, 0.0),
                    event_start=market.start_time,
                    as_of=as_of,
                )
                values.append(
                    FairValue(
                        event_id=market.event_id,
                        market_id=market.market_id,
                        outcome=outcome,
                        fair_probability=fair_prob,
                        source=f"external_no_vig:{market_type}",
                        timestamp=as_of,
                        uncertainty=uncertainty,
                    )
                )
        return values

    def _compute_non_exhaustive_binary_values(
        self,
        market: Market,
        books: dict[str, list[ExternalOdds]],
        liquidity_by_market: dict[str, float],
        as_of: datetime,
    ) -> list[FairValue]:
        """Compute per-outcome fair value for markets where many outcomes can win.

        Top-4 markets are not mutually exclusive across clubs, so normalizing Arsenal,
        City, Liverpool, etc. to sum to one is structurally wrong. If a provider gives
        yes/no odds for each team, remove vig inside that binary pair. If only a yes
        price is available, use raw implied probability with a larger uncertainty.
        """

        values: list[FairValue] = []
        latest_timestamp = max(row.timestamp for rows in books.values() for row in rows)
        for outcome in market.outcomes:
            book_probs: list[float] = []
            has_binary_pair = False
            for rows in books.values():
                yes_row, no_row = _find_binary_rows_for_outcome(outcome, rows)
                if not yes_row:
                    continue
                if no_row:
                    probs = generic_no_vig(
                        {"yes": yes_row.decimal_odds, "no": no_row.decimal_odds}
                    )
                    book_probs.append(probs["yes"])
                    has_binary_pair = True
                else:
                    book_probs.append(1.0 / yes_row.decimal_odds)
            if not book_probs:
                continue
            fair_prob = statistics.fmean(book_probs)
            uncertainty = self.estimate_uncertainty(
                book_probs=book_probs,
                books_count=len(book_probs),
                latest_odds_timestamp=latest_timestamp,
                market_liquidity=liquidity_by_market.get(market.market_id, 0.0),
                event_start=market.start_time,
                as_of=as_of,
            )
            if not has_binary_pair:
                uncertainty = min(0.25, uncertainty + 0.04)
            values.append(
                FairValue(
                    event_id=market.event_id,
                    market_id=market.market_id,
                    outcome=outcome,
                    fair_probability=max(0.0, min(1.0, fair_prob)),
                    source="external_binary_or_implied:top_4",
                    timestamp=as_of,
                    uncertainty=uncertainty,
                )
            )
        return values

    def estimate_uncertainty(
        self,
        book_probs: list[float],
        books_count: int,
        latest_odds_timestamp: datetime,
        market_liquidity: float,
        event_start: datetime | None,
        as_of: datetime,
    ) -> float:
        disagreement = statistics.pstdev(book_probs) if len(book_probs) > 1 else 0.025
        book_penalty = 0.012 if books_count < 2 else 0.005 if books_count < 3 else 0.0
        age_seconds = max(0.0, (as_of - latest_odds_timestamp).total_seconds())
        age_penalty = min(0.03, age_seconds / 86_400.0 * 0.02)
        liquidity_penalty = (
            0.02 if market_liquidity < 25 else 0.01 if market_liquidity < 100 else 0.0
        )
        start_penalty = 0.0
        if event_start:
            seconds_to_start = (event_start - as_of).total_seconds()
            if seconds_to_start < 0:
                start_penalty = 0.03
            elif seconds_to_start < 900:
                start_penalty = 0.015
        return min(
            0.25,
            max(
                0.0,
                self.base_uncertainty
                + disagreement
                + book_penalty
                + age_penalty
                + liquidity_penalty
                + start_penalty,
            ),
        )

    def _latest_odds(
        self, odds: list[ExternalOdds], as_of: datetime
    ) -> list[ExternalOdds]:
        latest: dict[tuple[str, str, str, str], ExternalOdds] = {}
        for row in odds:
            if row.timestamp > as_of:
                continue
            key = (row.event_id, row.book, row.market_type, row.outcome)
            current = latest.get(key)
            if current is None or row.timestamp > current.timestamp:
                latest[key] = row
        return list(latest.values())

    def _liquidity_by_market(self, orderbooks: list[OrderBook]) -> dict[str, float]:
        liquidity: dict[str, float] = defaultdict(float)
        for book in orderbooks:
            bba = book.best_bid_ask()
            liquidity[book.market_id] += float(bba.best_bid_size or 0.0)
            liquidity[book.market_id] += float(bba.best_ask_size or 0.0)
        return dict(liquidity)


def _find_binary_rows_for_outcome(
    outcome: str, rows: list[ExternalOdds]
) -> tuple[ExternalOdds | None, ExternalOdds | None]:
    outcome_norm = normalize_outcome_name(outcome)
    yes_row: ExternalOdds | None = None
    no_row: ExternalOdds | None = None
    for row in rows:
        row_norm = normalize_outcome_name(row.outcome)
        is_no = (
            row_norm.startswith("no ")
            or row_norm.startswith("not ")
            or row_norm.endswith(" no")
            or row_norm.endswith(" not")
        )
        stripped = (
            row_norm.removeprefix("no ")
            .removeprefix("not ")
            .removesuffix(" no")
            .removesuffix(" not")
            .strip()
        )
        if stripped == outcome_norm or outcome_norm in stripped or stripped in outcome_norm:
            if is_no:
                no_row = row
            else:
                yes_row = row
    return yes_row, no_row
