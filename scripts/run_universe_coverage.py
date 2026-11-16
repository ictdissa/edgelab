from __future__ import annotations

import argparse
import csv
import json
from collections import Counter
from dataclasses import asdict, dataclass
from pathlib import Path
from statistics import median
from typing import Any, Iterable, Literal

try:
    from _bootstrap import ROOT  # noqa: F401
except ModuleNotFoundError:
    from scripts._bootstrap import ROOT  # noqa: F401

from edgelab.config.settings import Settings
from edgelab.data.schemas import ExternalAnchor, OrderBook, PredictionMarket

CATEGORIES = (
    "sports",
    "crypto",
    "weather",
    "politics",
    "macro",
    "internal_relative_value",
)

Recommendation = Literal["IGNORE", "WATCH", "COLLECT_SNAPSHOTS", "PAPER_TRADE_CANDIDATE"]


@dataclass(frozen=True)
class UniverseCoverageRow:
    category: str
    polymarket_markets_found: int
    markets_with_valid_clob_books: int
    markets_with_liquidity_above_threshold: int
    median_spread: float | None
    fair_value_source_available: bool
    matched_markets: int
    unmatched_markets: int
    top_rejection_reasons: str
    possible_signals: int
    risk_approved_paper_signals: int
    coverage_score: float
    liquidity_score: float
    fair_value_availability_score: float
    matching_score: float
    spread_score: float
    signal_score: float
    research_score: float
    profitability_score: float
    recommendation: Recommendation
    missing_data_explanation: str


def main() -> None:
    parser = argparse.ArgumentParser(description="Summarize offline universe coverage")
    parser.add_argument("--output", type=Path, default=Path("reports/universe_coverage.md"))
    parser.add_argument("--csv", type=Path, default=Path("reports/universe_coverage.csv"))
    parser.add_argument("--live-data-dir", type=Path, default=None)
    parser.add_argument("--signals", type=Path, default=Path("reports/latest_signals.csv"))
    parser.add_argument("--match-audit", type=Path, default=Path("reports/event_match_audit.csv"))
    parser.add_argument(
        "--relative-value",
        type=Path,
        default=Path("reports/relative_value_opportunities.csv"),
    )
    args = parser.parse_args()

    settings = Settings()
    live_data_dir = args.live_data_dir or settings.live_data_dir
    rows = scan_universe_coverage(
        live_data_dir=live_data_dir,
        signals_path=args.signals,
        match_audit_path=args.match_audit,
        relative_value_path=args.relative_value,
        settings=settings,
    )
    write_outputs(args.output, args.csv, rows)

    highest_research = max(rows, key=lambda row: row.research_score)
    highest_profitability = max(rows, key=lambda row: row.profitability_score)
    weakest = min(rows, key=lambda row: row.research_score)
    sports = next(row for row in rows if row.category == "sports")
    print(f"highest_research_potential={highest_research.category}")
    print(f"highest_profitability_potential={highest_profitability.category}")
    print(f"weakest_category={weakest.category}")
    print(f"sports_failure_mode={sports_failure_mode(sports)}")
    print(f"report={args.output}")
    print(f"csv={args.csv}")


def scan_universe_coverage(
    live_data_dir: Path,
    signals_path: Path,
    match_audit_path: Path,
    relative_value_path: Path,
    settings: Settings,
) -> list[UniverseCoverageRow]:
    markets = load_markets(live_data_dir)
    orderbooks = load_orderbooks(live_data_dir)
    anchors = load_anchors(live_data_dir)
    signals = read_csv_rows(signals_path)
    match_audit = read_csv_rows(match_audit_path)
    rv_rows = read_csv_rows(relative_value_path)

    rows = [
        summarize_market_category(category, markets, orderbooks, anchors, signals, match_audit, settings)
        for category in CATEGORIES
        if category != "internal_relative_value"
    ]
    rows.append(summarize_internal_relative_value(rv_rows, settings))
    return rows


def summarize_market_category(
    category: str,
    markets: list[PredictionMarket],
    orderbooks: list[OrderBook],
    anchors: list[ExternalAnchor],
    signals: list[dict[str, str]],
    match_audit: list[dict[str, str]],
    settings: Settings,
) -> UniverseCoverageRow:
    category_markets = [market for market in markets if market_belongs_to_category(market, category)]
    market_ids = {market.market_id for market in category_markets}
    category_books = [book for book in orderbooks if book.market_id in market_ids]
    category_anchors = [anchor for anchor in anchors if anchor_belongs_to_category(anchor, category)]
    category_signals = [row for row in signals if signal_belongs_to_category(row, category)]
    category_matches = [row for row in match_audit if row.get("market_id") in market_ids]

    valid_book_market_ids = valid_book_markets(category_books)
    liquid_market_ids = liquid_markets(category_books, settings.min_liquidity)
    spreads = valid_spreads(category_books)
    matched = sum(1 for row in category_matches if row.get("decision") == "MATCH")
    unmatched = sum(1 for row in category_matches if row.get("decision") and row.get("decision") != "MATCH")
    if not category_matches and category_markets:
        unmatched = len(category_markets)
    rejection_reasons = top_rejection_reasons(category_matches)
    fair_value_available = bool(category_anchors) or any(has_signal_fair_value(row) for row in category_signals)
    possible = sum(1 for row in category_signals if is_possible_signal(row))
    risk_approved = sum(1 for row in category_signals if is_risk_approved_paper_signal(row))
    median_spread = median(spreads) if spreads else None

    missing = missing_data_notes(
        category,
        category_markets,
        category_books,
        category_anchors,
        category_signals,
        category_matches,
    )
    scores = score_row(
        markets_found=len(category_markets),
        valid_books=len(valid_book_market_ids),
        liquid_markets=len(liquid_market_ids),
        median_spread=median_spread,
        fair_value_available=fair_value_available,
        matched=matched,
        possible_signals=possible,
        risk_approved=risk_approved,
        settings=settings,
    )
    recommendation = recommendation_for(
        markets_found=len(category_markets),
        fair_value_available=fair_value_available,
        liquid_markets=len(liquid_market_ids),
        possible_signals=possible,
        risk_approved=risk_approved,
        research_score=scores["research_score"],
    )
    return UniverseCoverageRow(
        category=category,
        polymarket_markets_found=len(category_markets),
        markets_with_valid_clob_books=len(valid_book_market_ids),
        markets_with_liquidity_above_threshold=len(liquid_market_ids),
        median_spread=median_spread,
        fair_value_source_available=fair_value_available,
        matched_markets=matched,
        unmatched_markets=unmatched,
        top_rejection_reasons=rejection_reasons,
        possible_signals=possible,
        risk_approved_paper_signals=risk_approved,
        coverage_score=scores["coverage_score"],
        liquidity_score=scores["liquidity_score"],
        fair_value_availability_score=scores["fair_value_availability_score"],
        matching_score=scores["matching_score"],
        spread_score=scores["spread_score"],
        signal_score=scores["signal_score"],
        research_score=scores["research_score"],
        profitability_score=scores["profitability_score"],
        recommendation=recommendation,
        missing_data_explanation=missing,
    )


def summarize_internal_relative_value(
    rv_rows: list[dict[str, str]],
    settings: Settings,
) -> UniverseCoverageRow:
    if not rv_rows:
        scores = score_row(
            markets_found=0,
            valid_books=0,
            liquid_markets=0,
            median_spread=None,
            fair_value_available=False,
            matched=0,
            possible_signals=0,
            risk_approved=0,
            settings=settings,
        )
        return UniverseCoverageRow(
            category="internal_relative_value",
            polymarket_markets_found=0,
            markets_with_valid_clob_books=0,
            markets_with_liquidity_above_threshold=0,
            median_spread=None,
            fair_value_source_available=False,
            matched_markets=0,
            unmatched_markets=0,
            top_rejection_reasons="relative_value_opportunities.csv missing or empty",
            possible_signals=0,
            risk_approved_paper_signals=0,
            coverage_score=scores["coverage_score"],
            liquidity_score=scores["liquidity_score"],
            fair_value_availability_score=scores["fair_value_availability_score"],
            matching_score=scores["matching_score"],
            spread_score=scores["spread_score"],
            signal_score=scores["signal_score"],
            research_score=scores["research_score"],
            profitability_score=scores["profitability_score"],
            recommendation="IGNORE",
            missing_data_explanation="No relative-value output was available.",
        )

    market_refs = {
        value
        for row in rv_rows
        for value in (row.get("child_market"), row.get("parent_market"))
        if value
    }
    executable = sum(1 for row in rv_rows if row.get("status") in {"EXECUTABLE", "EXECUTABLE_PACKAGE", "PAPER_ELIGIBLE"})
    possible = sum(1 for row in rv_rows if row.get("status") not in {"", "NO_VIOLATION", "MISSING_BOOK", "PRICING_INCOMPLETE"})
    valid_books = sum(1 for row in rv_rows if row.get("status") not in {"MISSING_BOOK", "PRICING_INCOMPLETE"})
    liquid = sum(1 for row in rv_rows if parse_float(row.get("available_liquidity")) >= settings.min_liquidity)
    status_reasons = "; ".join(
        f"{status}: {count}"
        for status, count in Counter(row.get("status") or "UNKNOWN" for row in rv_rows).most_common(4)
    )
    scores = score_row(
        markets_found=len(market_refs),
        valid_books=valid_books,
        liquid_markets=liquid,
        median_spread=None,
        fair_value_available=True,
        matched=len(rv_rows),
        possible_signals=possible,
        risk_approved=executable,
        settings=settings,
    )
    recommendation = recommendation_for(
        markets_found=len(market_refs),
        fair_value_available=True,
        liquid_markets=liquid,
        possible_signals=possible,
        risk_approved=executable,
        research_score=scores["research_score"],
    )
    return UniverseCoverageRow(
        category="internal_relative_value",
        polymarket_markets_found=len(market_refs),
        markets_with_valid_clob_books=valid_books,
        markets_with_liquidity_above_threshold=liquid,
        median_spread=None,
        fair_value_source_available=True,
        matched_markets=len(rv_rows),
        unmatched_markets=0,
        top_rejection_reasons=status_reasons,
        possible_signals=possible,
        risk_approved_paper_signals=executable,
        coverage_score=scores["coverage_score"],
        liquidity_score=scores["liquidity_score"],
        fair_value_availability_score=scores["fair_value_availability_score"],
        matching_score=scores["matching_score"],
        spread_score=scores["spread_score"],
        signal_score=scores["signal_score"],
        research_score=scores["research_score"],
        profitability_score=scores["profitability_score"],
        recommendation=recommendation,
        missing_data_explanation="Uses internal constraint output, not an external fair-value feed.",
    )


def score_row(
    markets_found: int,
    valid_books: int,
    liquid_markets: int,
    median_spread: float | None,
    fair_value_available: bool,
    matched: int,
    possible_signals: int,
    risk_approved: int,
    settings: Settings,
) -> dict[str, float]:
    coverage_score = min(20.0, markets_found * 2.0)
    liquidity_score = 0.0 if markets_found == 0 else 20.0 * min(1.0, liquid_markets / markets_found)
    fair_value_score = 15.0 if fair_value_available else 0.0
    matching_score = 0.0 if markets_found == 0 else 15.0 * min(1.0, matched / markets_found)
    spread_score = spread_component(median_spread, settings.max_spread)
    signal_score = min(15.0, possible_signals * 3.0 + risk_approved * 5.0)
    research_score = (
        coverage_score
        + liquidity_score
        + fair_value_score
        + matching_score
        + spread_score
        + signal_score
    )
    profitability_score = liquidity_score + spread_score + signal_score + (20.0 if risk_approved else 0.0)
    return {
        "coverage_score": round(coverage_score, 2),
        "liquidity_score": round(liquidity_score, 2),
        "fair_value_availability_score": round(fair_value_score, 2),
        "matching_score": round(matching_score, 2),
        "spread_score": round(spread_score, 2),
        "signal_score": round(signal_score, 2),
        "research_score": round(research_score, 2),
        "profitability_score": round(profitability_score, 2),
    }


def spread_component(median_spread: float | None, max_spread: float) -> float:
    if median_spread is None:
        return 0.0
    if median_spread <= max_spread:
        return 15.0
    if median_spread <= max_spread * 2:
        return 8.0
    return 0.0


def recommendation_for(
    markets_found: int,
    fair_value_available: bool,
    liquid_markets: int,
    possible_signals: int,
    risk_approved: int,
    research_score: float,
) -> Recommendation:
    if markets_found == 0:
        return "IGNORE"
    if risk_approved > 0:
        return "PAPER_TRADE_CANDIDATE"
    if fair_value_available and liquid_markets > 0 and possible_signals > 0:
        return "COLLECT_SNAPSHOTS"
    if research_score >= 25:
        return "WATCH"
    return "IGNORE"


def sports_failure_mode(row: UniverseCoverageRow) -> str:
    if row.polymarket_markets_found == 0:
        return "no-market"
    if not row.fair_value_source_available:
        return "no-fair-value"
    if row.matched_markets == 0:
        return "no-match"
    return "not-failing"


def load_markets(live_data_dir: Path) -> list[PredictionMarket]:
    rows: list[PredictionMarket] = []
    for path in sorted(live_data_dir.glob("*_markets.json")) if live_data_dir.exists() else []:
        rows.extend(load_models(path, PredictionMarket, "markets"))
    by_id = {market.market_id: market for market in rows}
    return list(by_id.values())


def load_orderbooks(live_data_dir: Path) -> list[OrderBook]:
    rows: list[OrderBook] = []
    for path in sorted(live_data_dir.glob("*_orderbooks.json")) if live_data_dir.exists() else []:
        rows.extend(load_models(path, OrderBook, "orderbooks"))
    by_key: dict[tuple[str, str], OrderBook] = {}
    for book in rows:
        key = (book.market_id, book.token_id)
        existing = by_key.get(key)
        if existing is None or book.timestamp >= existing.timestamp:
            by_key[key] = book
    return list(by_key.values())


def load_anchors(live_data_dir: Path) -> list[ExternalAnchor]:
    rows: list[ExternalAnchor] = []
    for path in sorted(live_data_dir.glob("*_anchors.json")) if live_data_dir.exists() else []:
        rows.extend(load_models(path, ExternalAnchor, "anchors"))
    return rows


def load_models(path: Path, model_type: type[Any], key: str) -> list[Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    records = payload.get(key, payload) if isinstance(payload, dict) else payload
    if not isinstance(records, list):
        return []
    output = []
    for row in records:
        if not isinstance(row, dict):
            continue
        try:
            output.append(model_type(**row))
        except ValueError:
            continue
    return output


def read_csv_rows(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    with path.open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def valid_book_markets(orderbooks: Iterable[OrderBook]) -> set[str]:
    output = set()
    for book in orderbooks:
        bba = book.best_bid_ask()
        if bba.best_bid is not None and bba.best_ask is not None:
            output.add(book.market_id)
    return output


def liquid_markets(orderbooks: Iterable[OrderBook], min_liquidity: float) -> set[str]:
    output = set()
    for book in orderbooks:
        bba = book.best_bid_ask()
        if bba.best_bid is None or bba.best_ask is None:
            continue
        liquidity = min(float(bba.best_bid_size or 0.0), float(bba.best_ask_size or 0.0))
        if liquidity >= min_liquidity:
            output.add(book.market_id)
    return output


def valid_spreads(orderbooks: Iterable[OrderBook]) -> list[float]:
    spreads = []
    for book in orderbooks:
        spread = book.best_bid_ask().spread
        if spread is not None:
            spreads.append(float(spread))
    return spreads


def market_belongs_to_category(market: PredictionMarket, category: str) -> bool:
    market_category = str(market.category)
    if category == "sports":
        return market_category.startswith("sports")
    if category == "crypto":
        return market_category == "crypto_threshold"
    if category == "weather":
        return market_category == "weather_threshold"
    if category == "politics":
        return market_category == "politics_election"
    if category == "macro":
        text = f"{market.title} {market.description or ''}".lower()
        return any(
            term in text
            for term in (
                "cpi",
                "inflation",
                "fed",
                "interest rate",
                "unemployment",
                "gdp",
                "recession",
                "macro",
            )
        )
    return False


def anchor_belongs_to_category(anchor: ExternalAnchor, category: str) -> bool:
    anchor_category = str(anchor.category)
    if category == "sports":
        return anchor_category.startswith("sports")
    if category == "crypto":
        return anchor_category == "crypto_threshold"
    if category == "weather":
        return anchor_category == "weather_threshold"
    if category == "politics":
        return anchor_category == "politics_election"
    if category == "macro":
        text = f"{anchor.outcome} {anchor.metadata}".lower()
        return any(term in text for term in ("cpi", "inflation", "fed", "gdp", "macro"))
    return False


def signal_belongs_to_category(row: dict[str, str], category: str) -> bool:
    signal_category = row.get("category", "")
    if category == "sports":
        return signal_category.startswith("sports")
    if category == "crypto":
        return signal_category == "crypto_threshold"
    if category == "weather":
        return signal_category == "weather_threshold"
    if category == "politics":
        return signal_category == "politics_election"
    if category == "macro":
        text = f"{row.get('market_id', '')} {row.get('reason', '')}".lower()
        return any(term in text for term in ("cpi", "inflation", "fed", "gdp", "macro"))
    return False


def has_signal_fair_value(row: dict[str, str]) -> bool:
    source = (row.get("fair_value_source") or "").strip().lower()
    return bool(source and source != "none")


def is_possible_signal(row: dict[str, str]) -> bool:
    action = row.get("action", "")
    adjusted = parse_float(row.get("adjusted_edge"))
    return action in {"BUY_YES", "SELL_YES"} or (action == "HOLD" and adjusted > 0)


def is_risk_approved_paper_signal(row: dict[str, str]) -> bool:
    return row.get("action") in {"BUY_YES", "SELL_YES"} and row.get("risk_approved") == "True"


def top_rejection_reasons(rows: list[dict[str, str]]) -> str:
    reasons = Counter(
        reason
        for reason in (row.get("rejection_reason") or row.get("risk_reasons") or "" for row in rows)
        if reason
    )
    if not reasons:
        return "none recorded"
    return "; ".join(f"{reason}: {count}" for reason, count in reasons.most_common(3))


def missing_data_notes(
    category: str,
    markets: list[PredictionMarket],
    orderbooks: list[OrderBook],
    anchors: list[ExternalAnchor],
    signals: list[dict[str, str]],
    matches: list[dict[str, str]],
) -> str:
    missing = []
    if not markets:
        missing.append("no local Polymarket market snapshot")
    if markets and not orderbooks:
        missing.append("no local CLOB order-book snapshot")
    if markets and not anchors:
        missing.append("no local external fair-value anchors")
    if markets and not signals:
        missing.append("no local signal rows")
    if markets and not matches:
        missing.append("no local match audit rows")
    return "; ".join(missing) if missing else f"{category} has local coverage inputs"


def write_outputs(report_path: Path, csv_path: Path, rows: list[UniverseCoverageRow]) -> None:
    report_path.parent.mkdir(parents=True, exist_ok=True)
    csv_path.parent.mkdir(parents=True, exist_ok=True)
    write_csv(csv_path, rows)
    report_path.write_text(render_report(rows, csv_path), encoding="utf-8")


def write_csv(path: Path, rows: list[UniverseCoverageRow]) -> None:
    columns = list(UniverseCoverageRow.__dataclass_fields__)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns)
        writer.writeheader()
        for row in rows:
            payload = asdict(row)
            payload["median_spread"] = "" if row.median_spread is None else row.median_spread
            writer.writerow(payload)


def render_report(rows: list[UniverseCoverageRow], csv_path: Path) -> str:
    highest_research = max(rows, key=lambda row: row.research_score)
    highest_profitability = max(rows, key=lambda row: row.profitability_score)
    weakest = min(rows, key=lambda row: row.research_score)
    sports = next(row for row in rows if row.category == "sports")
    lines = [
        "# EdgeLab Universe Coverage",
        "",
        "Offline coverage scanner. It reads existing local snapshots and reports only; it does not "
        "fetch provider data, authenticate providers, or place trades.",
        "",
        "## Summary",
        "",
        f"- highest research-potential category: `{highest_research.category}`",
        f"- highest profitability-potential category: `{highest_profitability.category}`",
        f"- weakest category: `{weakest.category}`",
        f"- sports failure mode: `{sports_failure_mode(sports)}`",
        f"- CSV output: `{csv_path}`",
        "",
        "## Score Formula",
        "",
        "`research_score = coverage_score + liquidity_score + fair_value_availability_score + "
        "matching_score + spread_score + signal_score`",
        "",
        "Component caps: coverage 20, liquidity 20, fair value 15, matching 15, spread 15, signals 15.",
        "",
        "## Category Coverage",
        "",
    ]
    lines.extend(
        markdown_table(
            [
                "category",
                "markets",
                "books",
                "liquid",
                "median_spread",
                "fv",
                "matched",
                "unmatched",
                "signals",
                "risk",
                "score",
                "recommendation",
            ],
            [
                [
                    row.category,
                    str(row.polymarket_markets_found),
                    str(row.markets_with_valid_clob_books),
                    str(row.markets_with_liquidity_above_threshold),
                    format_optional_float(row.median_spread),
                    str(row.fair_value_source_available),
                    str(row.matched_markets),
                    str(row.unmatched_markets),
                    str(row.possible_signals),
                    str(row.risk_approved_paper_signals),
                    f"{row.research_score:.2f}",
                    row.recommendation,
                ]
                for row in rows
            ],
        )
    )
    lines.extend(["", "## Missing Data And Rejection Reasons", ""])
    for row in rows:
        lines.extend(
            [
                f"### {row.category}",
                "",
                f"- top rejection reasons: {row.top_rejection_reasons}",
                f"- missing data: {row.missing_data_explanation}",
                (
                    "- components: "
                    f"coverage={row.coverage_score:.2f}, liquidity={row.liquidity_score:.2f}, "
                    f"fair_value={row.fair_value_availability_score:.2f}, "
                    f"matching={row.matching_score:.2f}, spread={row.spread_score:.2f}, "
                    f"signals={row.signal_score:.2f}"
                ),
                "",
            ]
        )
    return "\n".join(lines)


def markdown_table(headers: list[str], rows: list[list[str]]) -> list[str]:
    output = ["| " + " | ".join(headers) + " |", "| " + " | ".join("---" for _ in headers) + " |"]
    for row in rows:
        output.append("| " + " | ".join(str(cell).replace("|", "\\|") for cell in row) + " |")
    return output


def parse_float(value: str | None) -> float:
    if value in {None, ""}:
        return 0.0
    try:
        return float(value)
    except ValueError:
        return 0.0


def format_optional_float(value: float | None) -> str:
    return "" if value is None else f"{value:.4f}"


if __name__ == "__main__":
    main()
