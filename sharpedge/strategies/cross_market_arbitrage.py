"""Cross-market consistency checks for MMA and soccer markets."""

from __future__ import annotations

from collections import defaultdict

from sharpedge.config.settings import Settings
from sharpedge.data.schemas import BestBidAsk, CrossMarketInconsistency, FairValue, Market
from sharpedge.research.fair_value_model import infer_market_type, normalize_outcome_name


def detect_cross_market_inconsistencies(
    markets: list[Market],
    fair_values: list[FairValue],
    best_bid_asks: list[BestBidAsk],
    settings: Settings,
) -> list[CrossMarketInconsistency]:
    results: list[CrossMarketInconsistency] = []
    results.extend(_detect_probability_sum(markets, fair_values, best_bid_asks, settings))
    results.extend(_detect_mma_method_constraints(markets, fair_values, best_bid_asks, settings))
    results.extend(_detect_soccer_constraints(markets, fair_values, best_bid_asks, settings))
    return sorted(results, key=lambda item: item.implied_edge, reverse=True)


def _fv_by_market(fair_values: list[FairValue]) -> dict[str, dict[str, float]]:
    grouped: dict[str, dict[str, float]] = defaultdict(dict)
    for value in fair_values:
        grouped[value.market_id][value.outcome] = value.fair_probability
    return grouped


def _market_liquidity(market_id: str, bbas: list[BestBidAsk]) -> float:
    liquidity = 0.0
    for bba in bbas:
        if bba.market_id == market_id:
            liquidity += float(bba.best_bid_size or 0.0) + float(bba.best_ask_size or 0.0)
    return liquidity


def _detect_probability_sum(
    markets: list[Market],
    fair_values: list[FairValue],
    bbas: list[BestBidAsk],
    settings: Settings,
) -> list[CrossMarketInconsistency]:
    grouped = _fv_by_market(fair_values)
    results: list[CrossMarketInconsistency] = []
    cost = settings.trading_cost_bps / 10_000.0
    for market in markets:
        market_type = infer_market_type(market)
        if market_type == "top_4":
            continue
        probs = grouped.get(market.market_id, {})
        if len(probs) < 2:
            continue
        prob_sum = sum(probs.values())
        gap = abs(prob_sum - 1.0)
        if gap <= max(0.025, cost):
            continue
        liquidity = _market_liquidity(market.market_id, bbas)
        executable = liquidity >= settings.min_liquidity and gap > cost
        results.append(
            CrossMarketInconsistency(
                inconsistency_type="probability_sum_not_one",
                involved_markets=[market.market_id],
                implied_edge=gap,
                executable_legs=market.token_ids if executable else [],
                liquidity_available=liquidity,
                executable_after_costs=executable,
                theoretical_only=not executable,
                reason=f"Fair probabilities sum to {prob_sum:.3f}; cost buffer {cost:.3f}",
            )
        )
    return results


def _detect_mma_method_constraints(
    markets: list[Market],
    fair_values: list[FairValue],
    bbas: list[BestBidAsk],
    settings: Settings,
) -> list[CrossMarketInconsistency]:
    by_event = defaultdict(list)
    for market in markets:
        if market.sport.lower() in {"mma", "ufc"}:
            by_event[market.event_id].append(market)
    values = _fv_by_market(fair_values)
    results: list[CrossMarketInconsistency] = []
    for event_id, event_markets in by_event.items():
        moneyline: dict[str, float] = {}
        methods: list[tuple[Market, str, float]] = []
        distance: dict[str, float] = {}
        for market in event_markets:
            title = market.title.lower()
            probs = values.get(market.market_id, {})
            if any(term in title for term in ("moneyline", "winner", "to win")):
                moneyline.update(probs)
            elif any(term in title for term in ("method", "ko", "submission", "decision")):
                for outcome, prob in probs.items():
                    methods.append((market, outcome, prob))
            elif "distance" in title:
                distance.update(probs)
        for market, outcome, prob in methods:
            normalized = normalize_outcome_name(outcome)
            fighter_match = _find_contained_fighter(normalized, moneyline)
            if not fighter_match:
                continue
            win_prob = moneyline[fighter_match]
            gap = prob - win_prob
            if gap <= 0.01:
                continue
            liquidity = _market_liquidity(market.market_id, bbas)
            executable = liquidity >= settings.min_liquidity and gap > settings.trading_cost_bps / 10_000.0
            results.append(
                CrossMarketInconsistency(
                    inconsistency_type="mma_method_exceeds_moneyline",
                    involved_markets=[market.market_id],
                    implied_edge=gap,
                    executable_legs=market.token_ids if executable else [],
                    liquidity_available=liquidity,
                    executable_after_costs=executable,
                    theoretical_only=not executable,
                    reason=f"{outcome} probability {prob:.3f} exceeds {fighter_match} win probability {win_prob:.3f}",
                )
            )
        if {"Yes", "No"}.issubset(distance):
            gap = abs(distance["Yes"] + distance["No"] - 1.0)
            if gap > 0.025:
                involved = [
                    market.market_id
                    for market in event_markets
                    if "distance" in market.title.lower()
                ]
                liquidity = sum(_market_liquidity(mid, bbas) for mid in involved)
                executable = liquidity >= settings.min_liquidity
                results.append(
                    CrossMarketInconsistency(
                        inconsistency_type="mma_distance_complement_break",
                        involved_markets=involved,
                        implied_edge=gap,
                        executable_legs=involved if executable else [],
                        liquidity_available=liquidity,
                        executable_after_costs=executable,
                        theoretical_only=not executable,
                        reason="Fight goes distance and not distance do not sum near one",
                    )
                )
    return results


def _detect_soccer_constraints(
    markets: list[Market],
    fair_values: list[FairValue],
    bbas: list[BestBidAsk],
    settings: Settings,
) -> list[CrossMarketInconsistency]:
    by_event = defaultdict(list)
    for market in markets:
        if market.sport.lower() in {"soccer", "football"}:
            by_event[market.event_id].append(market)
    values = _fv_by_market(fair_values)
    results: list[CrossMarketInconsistency] = []
    for _event_id, event_markets in by_event.items():
        match_winner: dict[str, float] = {}
        exact_scores: list[tuple[Market, str, float]] = []
        league_winners: dict[str, tuple[Market, float]] = {}
        top4: dict[str, tuple[Market, float]] = {}
        for market in event_markets:
            title = market.title.lower()
            probs = values.get(market.market_id, {})
            if "exact score" in title:
                exact_scores.extend((market, outcome, prob) for outcome, prob in probs.items())
            elif "top 4" in title or "top four" in title:
                for outcome, prob in probs.items():
                    top4[outcome] = (market, prob)
            elif "league winner" in title or "title winner" in title:
                for outcome, prob in probs.items():
                    league_winners[outcome] = (market, prob)
            elif "1x2" in title or "match winner" in title:
                match_winner.update(probs)
        for market, outcome, prob in exact_scores:
            team = outcome.split()[0] if outcome else ""
            winner = _find_contained_fighter(normalize_outcome_name(team), match_winner)
            if winner and prob - match_winner[winner] > 0.01:
                gap = prob - match_winner[winner]
                liquidity = _market_liquidity(market.market_id, bbas)
                executable = liquidity >= settings.min_liquidity
                results.append(
                    CrossMarketInconsistency(
                        inconsistency_type="soccer_exact_score_exceeds_match_win",
                        involved_markets=[market.market_id],
                        implied_edge=gap,
                        executable_legs=market.token_ids if executable else [],
                        liquidity_available=liquidity,
                        executable_after_costs=executable,
                        theoretical_only=not executable,
                        reason=f"Exact score {outcome} exceeds {winner} match-win probability",
                    )
                )
        for team, (league_market, league_prob) in league_winners.items():
            if team in top4 and league_prob - top4[team][1] > 0.01:
                top4_market, top4_prob = top4[team]
                gap = league_prob - top4_prob
                liquidity = _market_liquidity(league_market.market_id, bbas) + _market_liquidity(top4_market.market_id, bbas)
                executable = liquidity >= settings.min_liquidity
                results.append(
                    CrossMarketInconsistency(
                        inconsistency_type="soccer_title_exceeds_top4",
                        involved_markets=[league_market.market_id, top4_market.market_id],
                        implied_edge=gap,
                        executable_legs=[league_market.market_id, top4_market.market_id]
                        if executable
                        else [],
                        liquidity_available=liquidity,
                        executable_after_costs=executable,
                        theoretical_only=not executable,
                        reason=f"{team} title probability {league_prob:.3f} exceeds top-4 probability {top4_prob:.3f}",
                    )
                )
    return results


def _find_contained_fighter(normalized_text: str, probabilities: dict[str, float]) -> str | None:
    for name in probabilities:
        normalized_name = normalize_outcome_name(name)
        if normalized_name in normalized_text or normalized_text in normalized_name:
            return name
    return None
