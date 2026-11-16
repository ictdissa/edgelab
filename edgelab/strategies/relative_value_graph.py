"""Internal relative-value graph for prediction-market constraints."""

from __future__ import annotations

import csv
import re
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from itertools import combinations
from pathlib import Path
from typing import Iterable, Literal, Sequence, Final

from edgelab.config.settings import Settings
from edgelab.data.schemas import BestBidAsk, OrderBook, PredictionMarket, PriceLevel, MarketCategory

RV_RELATION_IMPLICATION: Final = "implication"
RV_RELATION_EXHAUSTIVE: Final = "exhaustive_group"
RV_RELATION_MUTUALLY_EXCLUSIVE: Final = "mutually_exclusive"

RV_STATUS_THEORETICAL_ONLY: Final = "THEORETICAL_VIOLATION"
RV_STATUS_NO_VIOLATION: Final = "NO_VIOLATION"
RV_STATUS_INSUFFICIENT_LIQUIDITY: Final = "INSUFFICIENT_LIQUIDITY"
RV_STATUS_MISSING_BOOK: Final = "PRICING_INCOMPLETE"
RV_STATUS_EXECUTABLE: Final = "EXECUTABLE_PACKAGE"
RV_STATUS_PAPER_ELIGIBLE: Final = "PAPER_ELIGIBLE"

RVRelationType = Literal["implication", "exhaustive_group", "mutually_exclusive"]
RVStatus = Literal[
    "THEORETICAL_VIOLATION",
    "NO_VIOLATION",
    "INSUFFICIENT_LIQUIDITY",
    "PRICING_INCOMPLETE",
    "EXECUTABLE_PACKAGE",
    "PAPER_ELIGIBLE",
]


@dataclass(frozen=True)
class RVNode:
    node_id: str
    market_id: str
    token_id: str
    event_id: str
    outcome: str
    title: str

    @property
    def label(self) -> str:
        return f"{self.market_id}:{self.outcome}"


@dataclass(frozen=True)
class RVEdge:
    relation_type: RVRelationType
    parent_market: str
    child_market: str
    involved_outcomes: tuple[str, ...]
    node_ids: tuple[str, ...]
    explanation: str


@dataclass(frozen=True)
class RVGraph:
    nodes: list[RVNode]
    edges: list[RVEdge]


@dataclass(frozen=True)
class RVOpportunity:
    relation_type: RVRelationType
    parent_market: str
    child_market: str
    involved_outcomes: tuple[str, ...]
    theoretical_violation: float
    executable_violation: float
    required_legs: str
    available_liquidity: float
    max_trade_size: float
    status: RVStatus
    explanation: str

    @property
    def relation(self) -> str:
        return self.relation_type

    @property
    def parent_market_id(self) -> str:
        return self.parent_market

    @property
    def child_market_id(self) -> str:
        return self.child_market

    @property
    def liquidity_available(self) -> float:
        return self.available_liquidity

    @property
    def legs_required(self) -> str:
        return self.required_legs

    @property
    def actually_executable(self) -> bool:
        return self.status == RV_STATUS_EXECUTABLE

    @property
    def risk_warning(self) -> str:
        return self.explanation


def build_nodes(markets: Sequence[PredictionMarket]) -> list[RVNode]:
    nodes: list[RVNode] = []
    for market in markets:
        for outcome, token_id in _outcome_tokens(market):
            nodes.append(
                RVNode(
                    node_id=_node_id(market.market_id, token_id),
                    market_id=market.market_id,
                    token_id=token_id,
                    event_id=market.event_id,
                    outcome=outcome,
                    title=market.title,
                )
            )
    return nodes


def build_graph(markets: Sequence[PredictionMarket]) -> RVGraph:
    nodes = build_nodes(markets)
    nodes_by_market: dict[str, list[RVNode]] = {}
    for node in nodes:
        nodes_by_market.setdefault(node.market_id, []).append(node)

    edges: list[RVEdge] = []
    edges.extend(_implication_edges(markets, nodes_by_market))
    edges.extend(_exhaustive_edges(markets, nodes_by_market))
    edges.extend(_mutually_exclusive_edges(markets, nodes_by_market))
    return RVGraph(nodes=nodes, edges=_dedupe_edges(edges))


def build_edges(markets: Sequence[PredictionMarket]) -> list[RVEdge]:
    return build_graph(markets).edges


def find_relative_value_opportunities(
    markets: Sequence[PredictionMarket],
    orderbooks: Sequence[OrderBook],
    settings: Settings | None = None,
) -> list[RVOpportunity]:
    settings = settings or Settings()
    graph = build_graph(markets)
    nodes_by_id = {node.node_id: node for node in graph.nodes}
    bbas = _bba_by_contract(orderbooks)
    opportunities = [_evaluate_edge(edge, nodes_by_id, bbas, settings) for edge in graph.edges]
    return sorted(opportunities, key=_sort_key)


def write_relative_value_outputs(
    report_path: Path,
    csv_path: Path,
    opportunities: Sequence[RVOpportunity],
    markets: Sequence[PredictionMarket],
    orderbooks: Sequence[OrderBook],
) -> None:
    report_path.parent.mkdir(parents=True, exist_ok=True)
    csv_path.parent.mkdir(parents=True, exist_ok=True)

    status_counts = _status_counts(opportunities)
    lines = [
        "# EdgeLab Relative-Value Report",
        "",
        "This report evaluates internal prediction-market constraints only. It is plumbing and "
        "diagnostics, not evidence of durable real-world edge.",
        "",
        "## Summary",
        "",
        f"- markets scanned: {len(markets)}",
        f"- contract books available: {len(orderbooks)}",
        f"- constraints evaluated: {len(opportunities)}",
        f"- executable package opportunities: {status_counts[RV_STATUS_EXECUTABLE]}",
        f"- theoretical-violation opportunities: {status_counts[RV_STATUS_THEORETICAL_ONLY]}",
        f"- insufficient-liquidity opportunities: {status_counts[RV_STATUS_INSUFFICIENT_LIQUIDITY]}",
        f"- pricing-incomplete constraints: {status_counts[RV_STATUS_MISSING_BOOK]}",
        f"- no-violation constraints: {status_counts[RV_STATUS_NO_VIOLATION]}",
        "",
        "## Status Counts",
        "",
    ]
    lines.extend(
        _markdown_table(
            ["status", "count"],
            [[status, str(status_counts[status])] for status in _status_order()],
        )
    )
    lines.extend(["", "## Constraint Rows", ""])
    if opportunities:
        lines.extend(
            _markdown_table(
                [
                    "relation",
                    "status",
                    "theoretical",
                    "executable",
                    "liquidity",
                    "max_size",
                    "child",
                    "parent",
                    "legs",
                    "explanation",
                ],
                [
                    [
                        row.relation_type,
                        row.status,
                        f"{row.theoretical_violation:.4f}",
                        f"{row.executable_violation:.4f}",
                        f"{row.available_liquidity:.2f}",
                        f"{row.max_trade_size:.2f}",
                        row.child_market,
                        row.parent_market,
                        row.required_legs,
                        row.explanation,
                    ]
                    for row in opportunities[:100]
                ],
            )
        )
    else:
        lines.append("No relative-value constraints were detected in the scanned universe.")
    report_path.write_text("\n".join(lines), encoding="utf-8")

    columns = list(RVOpportunity.__dataclass_fields__)
    with csv_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns)
        writer.writeheader()
        for row in opportunities:
            payload = asdict(row)
            payload["involved_outcomes"] = " | ".join(row.involved_outcomes)
            writer.writerow(payload)


def mock_relative_value_inputs() -> tuple[list[PredictionMarket], list[OrderBook]]:
    """Deterministic local data for the relative-value runner and tests."""

    now = datetime(2026, 5, 19, tzinfo=UTC)
    markets = [
        _market(
            "spain-wins-world-cup",
            "world-cup-2026",
            "sports_outright",
            "Will Spain win the 2026 FIFA World Cup?",
            ["Yes", "No"],
            ["spain-win-yes", "spain-win-no"],
        ),
        _market(
            "spain-reaches-final",
            "world-cup-2026",
            "sports_outright",
            "Will Spain reach the 2026 FIFA World Cup final?",
            ["Yes", "No"],
            ["spain-final-yes", "spain-final-no"],
        ),
        _market(
            "team-wins-league",
            "league-2026",
            "sports_outright",
            "Will City FC win the league?",
            ["Yes", "No"],
            ["city-league-yes", "city-league-no"],
        ),
        _market(
            "team-top-four",
            "league-2026",
            "sports_outright",
            "Will City FC finish top 4?",
            ["Yes", "No"],
            ["city-top4-yes", "city-top4-no"],
        ),
        _market(
            "nomination-alice",
            "election-nomination",
            "politics_election",
            "Will Alice win the nomination?",
            ["Yes", "No"],
            ["alice-nom-yes", "alice-nom-no"],
            {"mutually_exclusive_group": "party-nomination"},
        ),
        _market(
            "nomination-bob",
            "election-nomination",
            "politics_election",
            "Will Bob win the nomination?",
            ["Yes", "No"],
            ["bob-nom-yes", "bob-nom-no"],
            {"mutually_exclusive_group": "party-nomination"},
        ),
        _market(
            "group-winner",
            "group-winner",
            "exhaustive_multi_outcome",
            "Which club wins Group A?",
            ["Alpha", "Bravo", "Charlie"],
            ["alpha-win", "bravo-win", "charlie-win"],
        ),
    ]
    orderbooks = [
        _book("spain-wins-world-cup", "spain-win-yes", 0.56, 0.58, 50, 50, now),
        _book("spain-reaches-final", "spain-final-yes", 0.47, 0.50, 60, 60, now),
        _book("team-wins-league", "city-league-yes", 0.42, 0.44, 6, 6, now),
        OrderBook(
            market_id="team-top-four",
            token_id="city-top4-yes",
            timestamp=now,
            bids=[PriceLevel(price=0.35, size=6)],
            asks=[],
        ),
        _book("nomination-alice", "alice-nom-yes", 0.55, 0.57, 70, 70, now),
        _book("nomination-bob", "bob-nom-yes", 0.53, 0.55, 70, 70, now),
        _book("group-winner", "alpha-win", 0.32, 0.34, 80, 80, now),
        _book("group-winner", "bravo-win", 0.32, 0.34, 80, 80, now),
        _book("group-winner", "charlie-win", 0.33, 0.35, 80, 80, now),
    ]
    return markets, orderbooks


def _evaluate_edge(
    edge: RVEdge,
    nodes_by_id: dict[str, RVNode],
    bbas: dict[tuple[str, str], BestBidAsk],
    settings: Settings,
) -> RVOpportunity:
    if edge.relation_type == RV_RELATION_IMPLICATION:
        return _evaluate_implication(edge, nodes_by_id, bbas, settings)
    if edge.relation_type == RV_RELATION_EXHAUSTIVE:
        return _evaluate_exhaustive(edge, nodes_by_id, bbas, settings)
    return _evaluate_mutually_exclusive(edge, nodes_by_id, bbas, settings)


def _evaluate_implication(
    edge: RVEdge,
    nodes_by_id: dict[str, RVNode],
    bbas: dict[tuple[str, str], BestBidAsk],
    settings: Settings,
) -> RVOpportunity:
    child = nodes_by_id[edge.node_ids[0]]
    parent = nodes_by_id[edge.node_ids[1]]
    child_bba = _bba_for(child, bbas)
    parent_bba = _bba_for(parent, bbas)
    child_ref = _reference_price(child_bba)
    parent_ref = _reference_price(parent_bba)
    missing_book = (
        child_bba is None or parent_bba is None or child_ref is None or parent_ref is None
    )

    theoretical = (
        max(0.0, child_ref - parent_ref)
        if child_ref is not None and parent_ref is not None
        else 0.0
    )
    child_bid, child_bid_size = _bid(child_bba)
    parent_ask, parent_ask_size = _ask(parent_bba)
    missing_executable = child_bid is None or parent_ask is None
    raw_executable = (
        child_bid - parent_ask - _cost_drag(settings) * 2
        if child_bid is not None and parent_ask is not None
        else 0.0
    )
    executable = max(0.0, raw_executable)
    liquidity = _min_size(child_bid_size, parent_ask_size)
    status = _status(theoretical, executable, missing_book, missing_executable, liquidity, settings)
    required_legs = f"SELL {child.label} at bid; BUY {parent.label} at ask"
    return RVOpportunity(
        relation_type=edge.relation_type,
        parent_market=edge.parent_market,
        child_market=edge.child_market,
        involved_outcomes=edge.involved_outcomes,
        theoretical_violation=theoretical,
        executable_violation=executable,
        required_legs=required_legs,
        available_liquidity=liquidity,
        max_trade_size=min(settings.max_trade_size, liquidity),
        status=status,
        explanation=_explanation(status, edge.explanation),
    )


def _evaluate_exhaustive(
    edge: RVEdge,
    nodes_by_id: dict[str, RVNode],
    bbas: dict[tuple[str, str], BestBidAsk],
    settings: Settings,
) -> RVOpportunity:
    nodes = [nodes_by_id[node_id] for node_id in edge.node_ids]
    node_bbas = [_bba_for(node, bbas) for node in nodes]
    refs = [_reference_price(bba) for bba in node_bbas]
    missing_book = any(bba is None or ref is None for bba, ref in zip(node_bbas, refs, strict=True))
    leg_count = len(nodes)
    theoretical = 0.0
    if not missing_book:
        raw_theoretical = max(0.0, sum(ref for ref in refs if ref is not None) - 1.0)
        raw_theoretical = max(raw_theoretical, 1.0 - sum(ref for ref in refs if ref is not None))
        theoretical = max(0.0, raw_theoretical - _cost_drag(settings) * leg_count)

    bids = [_bid(bba) for bba in node_bbas]
    asks = [_ask(bba) for bba in node_bbas]
    over_raw = (
        sum(price for price, _size in bids if price is not None)
        - 1.0
        - _cost_drag(settings) * leg_count
        if all(price is not None for price, _size in bids)
        else None
    )
    under_raw = (
        1.0
        - sum(price for price, _size in asks if price is not None)
        - _cost_drag(settings) * leg_count
        if all(price is not None for price, _size in asks)
        else None
    )
    direction, raw_executable = _best_exhaustive_direction(over_raw, under_raw)
    executable = max(0.0, raw_executable or 0.0)
    side_sizes = [size for _price, size in (bids if direction == "SELL" else asks)]
    liquidity = _min_size(*side_sizes)
    missing_executable = raw_executable is None
    required_legs = (
        f"{direction} all exhaustive outcomes: " + ", ".join(node.label for node in nodes)
        if direction
        else "No executable exhaustive legs"
    )
    status = _status(theoretical, executable, missing_book, missing_executable, liquidity, settings)
    return RVOpportunity(
        relation_type=edge.relation_type,
        parent_market=edge.parent_market,
        child_market=edge.child_market,
        involved_outcomes=edge.involved_outcomes,
        theoretical_violation=theoretical,
        executable_violation=executable,
        required_legs=required_legs,
        available_liquidity=liquidity,
        max_trade_size=min(settings.max_trade_size, liquidity),
        status=status,
        explanation=_explanation(status, edge.explanation),
    )


def _evaluate_mutually_exclusive(
    edge: RVEdge,
    nodes_by_id: dict[str, RVNode],
    bbas: dict[tuple[str, str], BestBidAsk],
    settings: Settings,
) -> RVOpportunity:
    left = nodes_by_id[edge.node_ids[0]]
    right = nodes_by_id[edge.node_ids[1]]
    left_bba = _bba_for(left, bbas)
    right_bba = _bba_for(right, bbas)
    left_ref = _reference_price(left_bba)
    right_ref = _reference_price(right_bba)
    missing_book = left_bba is None or right_bba is None or left_ref is None or right_ref is None
    theoretical = (
        max(0.0, left_ref + right_ref - 1.0)
        if left_ref is not None and right_ref is not None
        else 0.0
    )
    left_bid, left_bid_size = _bid(left_bba)
    right_bid, right_bid_size = _bid(right_bba)
    missing_executable = left_bid is None or right_bid is None
    raw_executable = (
        left_bid + right_bid - 1.0 - _cost_drag(settings) * 2
        if left_bid is not None and right_bid is not None
        else 0.0
    )
    executable = max(0.0, raw_executable)
    liquidity = _min_size(left_bid_size, right_bid_size)
    status = _status(theoretical, executable, missing_book, missing_executable, liquidity, settings)
    required_legs = f"SELL both mutually exclusive outcomes: {left.label}; {right.label}"
    return RVOpportunity(
        relation_type=edge.relation_type,
        parent_market=edge.parent_market,
        child_market=edge.child_market,
        involved_outcomes=edge.involved_outcomes,
        theoretical_violation=theoretical,
        executable_violation=executable,
        required_legs=required_legs,
        available_liquidity=liquidity,
        max_trade_size=min(settings.max_trade_size, liquidity),
        status=status,
        explanation=_explanation(status, edge.explanation),
    )


def _implication_edges(
    markets: Sequence[PredictionMarket],
    nodes_by_market: dict[str, list[RVNode]],
) -> list[RVEdge]:
    binary_markets = [market for market in markets if _is_binary_market(market)]
    edges: list[RVEdge] = []
    for child in binary_markets:
        child_node = _yes_node(child, nodes_by_market)
        if child_node is None:
            continue
        for parent in binary_markets:
            if child.market_id == parent.market_id:
                continue
            if child.event_id != parent.event_id:
                continue
            if child.category == "politics_election" or parent.category == "politics_election":
                continue
            parent_node = _yes_node(parent, nodes_by_market)
            if parent_node is None:
                continue
            rule = _implication_rule(child.title, parent.title)
            if not rule:
                continue
            edges.append(
                RVEdge(
                    relation_type=RV_RELATION_IMPLICATION,
                    parent_market=parent.market_id,
                    child_market=child.market_id,
                    involved_outcomes=(child_node.label, parent_node.label),
                    node_ids=(child_node.node_id, parent_node.node_id),
                    explanation=rule,
                )
            )
    return edges


def _exhaustive_edges(
    markets: Sequence[PredictionMarket],
    nodes_by_market: dict[str, list[RVNode]],
) -> list[RVEdge]:
    edges: list[RVEdge] = []
    for market in markets:
        if market.category == "exhaustive_multi_outcome" and not _is_binary_market(market):
            nodes = nodes_by_market.get(market.market_id, [])
            if len(nodes) >= 2:
                edges.append(_group_edge(RV_RELATION_EXHAUSTIVE, market.market_id, nodes))

    grouped_binary: dict[str, list[RVNode]] = {}
    explicit_groups: dict[str, list[RVNode]] = {}
    for market in markets:
        positive = _positive_nodes(market, nodes_by_market)
        if market.category == "exhaustive_multi_outcome" and _is_binary_market(market):
            grouped_binary.setdefault(market.event_id, []).extend(positive)
        explicit = _metadata_group(market, "exhaustive_group", "rv_exhaustive_group")
        if explicit:
            explicit_groups.setdefault(explicit, []).extend(positive)

    for group_key, nodes in {**grouped_binary, **explicit_groups}.items():
        if len(nodes) >= 2:
            edges.append(_group_edge(RV_RELATION_EXHAUSTIVE, group_key, nodes))
    return edges


def _mutually_exclusive_edges(
    markets: Sequence[PredictionMarket],
    nodes_by_market: dict[str, list[RVNode]],
) -> list[RVEdge]:
    groups: dict[str, list[RVNode]] = {}
    for market in markets:
        positive = _positive_nodes(market, nodes_by_market)
        explicit = _metadata_group(
            market, "mutually_exclusive_group", "exclusive_group", "rv_group"
        )
        if explicit:
            groups.setdefault(f"explicit:{explicit}", []).extend(positive)
        if market.category in {"sports_1x2", "sports_exact_score"}:
            groups.setdefault(f"sports:{market.category}:{market.event_id}", []).extend(positive)
        if market.category == "politics_election":
            groups.setdefault(f"politics:{market.event_id}", []).extend(positive)
        if market.category == "exhaustive_multi_outcome":
            groups.setdefault(f"exhaustive:{market.event_id}", []).extend(positive)

    edges: list[RVEdge] = []
    for nodes in groups.values():
        unique_nodes = _dedupe_nodes(nodes)
        for left, right in combinations(unique_nodes, 2):
            edges.append(
                RVEdge(
                    relation_type=RV_RELATION_MUTUALLY_EXCLUSIVE,
                    parent_market=right.market_id,
                    child_market=left.market_id,
                    involved_outcomes=(left.label, right.label),
                    node_ids=(left.node_id, right.node_id),
                    explanation="Mutually exclusive contracts cannot both resolve Yes.",
                )
            )
    return edges


def _group_edge(relation_type: RVRelationType, group_key: str, nodes: list[RVNode]) -> RVEdge:
    node_ids = tuple(node.node_id for node in _dedupe_nodes(nodes))
    labels = tuple(node.label for node in _dedupe_nodes(nodes))
    return RVEdge(
        relation_type=relation_type,
        parent_market=group_key,
        child_market=group_key,
        involved_outcomes=labels,
        node_ids=node_ids,
        explanation="Exhaustive outcomes should sum close to one after costs.",
    )


def _implication_rule(child_title: str, parent_title: str) -> str | None:
    child = _normalize(child_title)
    parent = _normalize(parent_title)
    if not _same_subject(child, parent):
        return None
    if _looks_like_tournament_win(child) and _looks_like_reaches_final(parent):
        return "Tournament winner should imply reaching the final."
    if _looks_like_league_win(child) and _looks_like_top_four(parent):
        return "League winner should imply finishing top 4."
    if _looks_like_election_win(child) and _looks_like_nomination_win(parent):
        return "Election winner should imply winning the nomination."
    if _looks_like_ko_win(child) and _contains_win_market(parent):
        return "Fighter winning by KO should imply fighter wins."
    return None


def _looks_like_tournament_win(text: str) -> bool:
    return _contains_win_market(text) and any(
        word in text for word in ("tournament", "world cup", "championship", "cup")
    )


def _looks_like_reaches_final(text: str) -> bool:
    return ("reach" in text or "make" in text or "advance" in text) and "final" in text


def _looks_like_league_win(text: str) -> bool:
    return _contains_win_market(text) and "league" in text


def _looks_like_top_four(text: str) -> bool:
    return "top 4" in text or "top four" in text


def _looks_like_election_win(text: str) -> bool:
    return "election" in text or "elected" in text or "president" in text


def _looks_like_nomination_win(text: str) -> bool:
    return "nomination" in text or "nominee" in text or "primary" in text


def _looks_like_ko_win(text: str) -> bool:
    return (" ko " in f" {text} " or "knockout" in text) and _contains_win_market(text)


def _contains_win_market(text: str) -> bool:
    return (
        " wins " in f" {text} "
        or " win " in f" {text} "
        or " winner " in f" {text} "
        or "to win" in text
    )


def _same_subject(left: str, right: str) -> bool:
    stop = {
        "will",
        "win",
        "wins",
        "winner",
        "the",
        "by",
        "final",
        "reach",
        "reaches",
        "make",
        "advance",
        "league",
        "top",
        "four",
        "election",
        "nomination",
        "nominee",
        "president",
        "tournament",
        "championship",
        "world",
        "cup",
        "fifa",
        "finish",
        "ko",
        "tko",
        "knockout",
    }
    left_tokens = {
        token
        for token in left.split()
        if len(token) > 2 and token not in stop and not token.isdigit()
    }
    right_tokens = {
        token
        for token in right.split()
        if len(token) > 2 and token not in stop and not token.isdigit()
    }
    return bool(left_tokens) and left_tokens == right_tokens


def _outcome_tokens(market: PredictionMarket) -> list[tuple[str, str]]:
    if market.token_ids:
        return list(zip(market.outcomes, market.token_ids, strict=True))
    return [(outcome, _slug(outcome)) for outcome in market.outcomes]


def _positive_nodes(
    market: PredictionMarket,
    nodes_by_market: dict[str, list[RVNode]],
) -> list[RVNode]:
    if _is_binary_market(market):
        yes = _yes_node(market, nodes_by_market)
        return [yes] if yes else []
    return nodes_by_market.get(market.market_id, [])


def _yes_node(
    market: PredictionMarket,
    nodes_by_market: dict[str, list[RVNode]],
) -> RVNode | None:
    for node in nodes_by_market.get(market.market_id, []):
        if node.outcome.lower() == "yes":
            return node
    return None


def _is_binary_market(market: PredictionMarket) -> bool:
    return {outcome.lower() for outcome in market.outcomes} == {"yes", "no"}


def _metadata_group(market: PredictionMarket, *keys: str) -> str | None:
    for key in keys:
        value = market.raw_metadata.get(key)
        if value:
            return str(value)
    return None


def _bba_by_contract(orderbooks: Sequence[OrderBook]) -> dict[tuple[str, str], BestBidAsk]:
    output: dict[tuple[str, str], BestBidAsk] = {}
    for book in orderbooks:
        key = (book.market_id, book.token_id)
        bba = book.best_bid_ask()
        existing = output.get(key)
        if existing is None or bba.timestamp >= existing.timestamp:
            output[key] = bba
    return output


def _bba_for(node: RVNode, bbas: dict[tuple[str, str], BestBidAsk]) -> BestBidAsk | None:
    return bbas.get((node.market_id, node.token_id))


def _reference_price(bba: BestBidAsk | None) -> float | None:
    if bba is None:
        return None
    if bba.mid is not None:
        return bba.mid
    if bba.best_bid is not None:
        return bba.best_bid
    return bba.best_ask


def _bid(bba: BestBidAsk | None) -> tuple[float | None, float | None]:
    if bba is None:
        return None, None
    return bba.best_bid, bba.best_bid_size


def _ask(bba: BestBidAsk | None) -> tuple[float | None, float | None]:
    if bba is None:
        return None, None
    return bba.best_ask, bba.best_ask_size


def _status(
    theoretical: float,
    executable: float,
    missing_book: bool,
    missing_executable: bool,
    liquidity: float,
    settings: Settings,
) -> RVStatus:
    if missing_book:
        return RV_STATUS_MISSING_BOOK
    if theoretical <= 0 and executable <= 0:
        return RV_STATUS_NO_VIOLATION
    if missing_executable:
        return RV_STATUS_THEORETICAL_ONLY
    if executable <= 0:
        return RV_STATUS_THEORETICAL_ONLY
    if liquidity < settings.min_liquidity:
        return RV_STATUS_INSUFFICIENT_LIQUIDITY
    return RV_STATUS_EXECUTABLE


def _explanation(status: RVStatus, base: str) -> str:
    prefixes = {
        RV_STATUS_EXECUTABLE: "Executable package after bid/ask and liquidity checks.",
        RV_STATUS_PAPER_ELIGIBLE: "Paper-eligible package after bid/ask, liquidity, and risk checks.",
        RV_STATUS_THEORETICAL_ONLY: "Theoretical violation exists, but executable package pricing is incomplete or does not clear costs.",
        RV_STATUS_NO_VIOLATION: "Constraint is not violated after costs.",
        RV_STATUS_INSUFFICIENT_LIQUIDITY: "Package prices cross the constraint, but size is below minimum liquidity.",
        RV_STATUS_MISSING_BOOK: "At least one required contract book or reference price is missing.",
    }
    return f"{prefixes[status]} {base}"


def _best_exhaustive_direction(
    over_raw: float | None,
    under_raw: float | None,
) -> tuple[str | None, float | None]:
    candidates = [("SELL", over_raw), ("BUY", under_raw)]
    present = [(side, value) for side, value in candidates if value is not None]
    if not present:
        return None, None
    return max(present, key=lambda row: row[1])


def _cost_drag(settings: Settings) -> float:
    return (
        settings.default_slippage_bps
        + settings.fee_bps
        + settings.resolution_risk_bps
        + settings.safety_margin_bps
    ) / 10_000.0


def _min_size(*sizes: float | None) -> float:
    if not sizes or any(size is None for size in sizes):
        return 0.0
    return min(float(size) for size in sizes if size is not None)


def _dedupe_edges(edges: Iterable[RVEdge]) -> list[RVEdge]:
    seen: set[tuple[str, tuple[str, ...]]] = set()
    output: list[RVEdge] = []
    for edge in edges:
        key = (edge.relation_type, tuple(sorted(edge.node_ids)))
        if key in seen:
            continue
        seen.add(key)
        output.append(edge)
    return output


def _dedupe_nodes(nodes: Iterable[RVNode]) -> list[RVNode]:
    seen: set[str] = set()
    output: list[RVNode] = []
    for node in nodes:
        if node.node_id in seen:
            continue
        seen.add(node.node_id)
        output.append(node)
    return output


def _sort_key(row: RVOpportunity) -> tuple[int, float, float, str, str]:
    priority = {
        RV_STATUS_PAPER_ELIGIBLE: 0,
        RV_STATUS_EXECUTABLE: 0,
        RV_STATUS_INSUFFICIENT_LIQUIDITY: 1,
        RV_STATUS_THEORETICAL_ONLY: 2,
        RV_STATUS_MISSING_BOOK: 3,
        RV_STATUS_NO_VIOLATION: 4,
    }[row.status]
    return (
        priority,
        -row.executable_violation,
        -row.theoretical_violation,
        row.relation_type,
        row.child_market,
    )


def _status_counts(opportunities: Sequence[RVOpportunity]) -> dict[RVStatus, int]:
    return {
        status: sum(1 for row in opportunities if row.status == status)
        for status in _status_order()
    }


def _status_order() -> tuple[RVStatus, ...]:
    return (
        RV_STATUS_PAPER_ELIGIBLE,
        RV_STATUS_EXECUTABLE,
        RV_STATUS_THEORETICAL_ONLY,
        RV_STATUS_INSUFFICIENT_LIQUIDITY,
        RV_STATUS_MISSING_BOOK,
        RV_STATUS_NO_VIOLATION,
    )


def _markdown_table(headers: list[str], rows: list[list[str]]) -> list[str]:
    output = ["| " + " | ".join(headers) + " |", "| " + " | ".join("---" for _ in headers) + " |"]
    for row in rows:
        output.append("| " + " | ".join(str(cell).replace("|", "\\|") for cell in row) + " |")
    return output


def _node_id(market_id: str, token_id: str) -> str:
    return f"{market_id}:{token_id}"


def _normalize(value: str) -> str:
    lowered = value.lower().replace("$", " ")
    lowered = re.sub(r"[^a-z0-9]+", " ", lowered)
    return re.sub(r"\s+", " ", lowered).strip()


def _slug(value: str) -> str:
    return _normalize(value).replace(" ", "-") or "outcome"


def _market(
    market_id: str,
    event_id: str,
    category: MarketCategory,
    title: str,
    outcomes: list[str],
    token_ids: list[str],
    raw_metadata: dict[str, object] | None = None,
) -> PredictionMarket:
    return PredictionMarket(
        market_id=market_id,
        event_id=event_id,
        category=category,
        title=title,
        outcomes=outcomes,
        token_ids=token_ids,
        resolution_source="mock relative-value fixture",
        raw_metadata=raw_metadata or {},
    )


def _book(
    market_id: str,
    token_id: str,
    bid: float,
    ask: float,
    bid_size: float,
    ask_size: float,
    timestamp: datetime,
) -> OrderBook:
    return OrderBook(
        market_id=market_id,
        token_id=token_id,
        timestamp=timestamp,
        bids=[PriceLevel(price=bid, size=bid_size)],
        asks=[PriceLevel(price=ask, size=ask_size)],
    )
