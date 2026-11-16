"""Live-data research pipeline.

This module is research-only. It collects and compares live data, but never routes an order.
"""

from __future__ import annotations

import json
import logging
import re
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from edgelab.config.settings import Settings
from edgelab.data.external_sources.crypto_provider import CryptoSpotProvider
from edgelab.data.external_sources.odds_provider import (
    ProviderDiagnosis,
    key_fingerprint,
    normalize_provider_name,
    recommendation_for_failure,
    select_sports_odds_provider,
    split_csv,
)
from edgelab.data.external_sources.weather_provider import WeatherForecastProvider
from edgelab.data.freshness import FreshnessRecord, freshness_record, write_freshness_report
from edgelab.data.polymarket_collector import CATEGORY_SEARCH_QUERIES, PolymarketCollector
from edgelab.data.schemas import (
    BestBidAsk,
    ExternalAnchor,
    FairValue,
    OrderBook,
    PredictionMarket,
    Signal,
)
from edgelab.execution.risk_manager import RiskManager
from edgelab.fair_value.crypto_threshold_model import CryptoThresholdModel
from edgelab.fair_value.sports_odds_model import SportsOddsModel
from edgelab.fair_value.weather_model import WeatherThresholdModel
from edgelab.matching.event_resolver import EventMatch, resolve_events, write_event_match_audit
from edgelab.matching.risk_identity import bind_fixture_risk
from edgelab.matching.settlement import apply_settlement_registry
from edgelab.matching.sports_discovery import (
    audit_polymarket_first,
    audit_sports_matching,
    write_sports_match_failure_audit,
)
from edgelab.strategies.taker_edge import generate_signals

LOGGER = logging.getLogger(__name__)


def collect_live_market_snapshot(
    category: str,
    limit: int,
    settings: Settings,
    refresh: bool = False,
) -> tuple[list[PredictionMarket], list[OrderBook], Path]:
    collector = PolymarketCollector(settings, mock=False)
    markets = collector.fetch_markets(category=category, limit=limit, refresh=refresh)
    orderbooks = collector.fetch_orderbooks(markets, limit=limit, refresh=refresh)
    settings.live_data_dir.mkdir(parents=True, exist_ok=True)
    markets_path = settings.live_data_dir / f"{category}_markets.json"
    books_path = settings.live_data_dir / f"{category}_orderbooks.json"
    write_json(markets_path, [market.model_dump(mode="json") for market in markets])
    write_json(books_path, [book.model_dump(mode="json") for book in orderbooks])
    freshness_path = write_live_freshness_report(
        category,
        settings,
        markets,
        orderbooks,
        anchors=[],
        provider_records=[],
    )
    return markets, orderbooks, freshness_path


def run_live_research(
    category: str,
    output: Path,
    settings: Settings,
    limit: int = 50,
    refresh: bool = False,
    skip_provider_diagnosis: bool = False,
    odds_provider: str | None = None,
    sport: str | None = None,
) -> str:
    if category == "sports":
        return run_live_sports_research(
            output=output,
            settings=settings,
            limit=limit,
            refresh=refresh,
            skip_provider_diagnosis=skip_provider_diagnosis,
            odds_provider=odds_provider,
            sport=sport,
        )
    markets, orderbooks, _freshness = collect_live_market_snapshot(
        category,
        limit,
        settings,
        refresh=refresh,
    )
    anchors, provider_records = fetch_live_anchors(category, markets, settings, refresh)
    if anchors:
        write_json(
            settings.live_data_dir / f"{category}_anchors.json",
            [anchor.model_dump(mode="json") for anchor in anchors],
        )
    matches = resolve_events(markets, anchors, as_of=datetime.now(UTC)) if anchors else []
    markets = bind_fixture_risk(markets, matches, anchors)
    write_event_match_audit(Path("reports/event_match_audit.csv"), matches)
    rewritten_anchors = rewrite_anchors_for_matches(anchors, matches)
    as_of = datetime.now(UTC)
    fair_values = compute_live_fair_values(
        category, markets, rewritten_anchors, orderbooks, settings, as_of
    )
    bbas = [book.best_bid_ask() for book in orderbooks]
    generated = generate_signals(markets, fair_values, bbas, settings, as_of=as_of)
    signals = add_avoid_signals(
        markets, generated, fair_values, bbas, matches, anchors, settings, as_of
    )
    risk_rows = risk_diagnostics(signals, markets, settings, as_of)
    signals = annotate_signals(signals, fair_values, matches, risk_rows)
    freshness_path = write_live_freshness_report(
        category,
        settings,
        markets,
        orderbooks,
        anchors,
        provider_records,
        as_of=as_of,
    )
    report = render_live_report(
        category=category,
        markets=markets,
        orderbooks=orderbooks,
        anchors=anchors,
        matches=matches,
        fair_values=fair_values,
        signals=signals,
        risk_rows=risk_rows,
        freshness_path=freshness_path,
        as_of=as_of,
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(report, encoding="utf-8")
    write_signals_csv(Path("reports/latest_signals.csv"), signals)
    return report


def write_live_failure_report(
    category: str, output: Path, settings: Settings, error: Exception
) -> Path:
    """Invalidate latest decisions and preserve an explicit diagnostic after collection failure."""
    from edgelab.data.external_sources.odds_provider import redact_secret_text

    reason = redact_secret_text(
        str(error),
        secrets=[settings.odds_api_key, settings.odds_api_io_key, settings.the_odds_api_key],
    )
    as_of = datetime.now(UTC)
    record = FreshnessRecord("collection_failure", 0, None, None, "error", reason)
    freshness = write_live_freshness_report(category, settings, [], [], [], [record], as_of)
    write_signals_csv(Path("reports/latest_signals.csv"), [])
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        f"# EdgeLab {category.title()} Collection Failure\n\nAVOID. No actionable edge found.\n\n{reason}\n\nFreshness report: `{freshness}`. No orders placed. Inspect provider/cache configuration and budget before an explicit retry.\n",
        encoding="utf-8",
    )
    return freshness


def run_live_sports_research(
    output: Path,
    settings: Settings,
    limit: int = 50,
    refresh: bool = False,
    skip_provider_diagnosis: bool = False,
    odds_provider: str | None = None,
    sport: str | None = None,
) -> str:
    selected_sport = sport or _default_sports_key(settings, odds_provider)
    provider_diagnosis: ProviderDiagnosis | None = None
    provider_records: list[FreshnessRecord] = []
    provider = None
    if not skip_provider_diagnosis:
        try:
            provider = select_sports_odds_provider(settings, odds_provider, refresh=refresh)
            provider_diagnosis = provider.diagnose(selected_sport, limit=3)
        except Exception as exc:
            provider_diagnosis = _provider_configuration_diagnosis(
                settings, odds_provider, exc, selected_sport
            )
        write_provider_diagnosis_reports(provider_diagnosis)
        provider_records.append(provider_diagnosis_record(provider_diagnosis))
    else:
        provider = select_sports_odds_provider(settings, odds_provider, refresh=refresh)

    collector = PolymarketCollector(settings, mock=False)
    markets: list[PredictionMarket] = []
    anchors: list[ExternalAnchor] = []
    matches: list[EventMatch] = []
    orderbooks: list[OrderBook] = []
    provider_runtime_error: str | None = None

    diagnosis_failed = provider_diagnosis is not None and not provider_diagnosis.succeeded
    if diagnosis_failed and provider_diagnosis is not None:
        LOGGER.warning(
            "sports_provider_diagnosis_failed provider=%s reason=%s",
            provider_diagnosis.selected_provider,
            provider_diagnosis.failure_reason,
        )
    else:
        try:
            if provider is None:
                provider = select_sports_odds_provider(settings, odds_provider, refresh=refresh)
            anchors = provider.fetch_odds(
                sport=selected_sport,
                limit=settings.odds_provider_event_limit,
                refresh=refresh,
            )
            write_json(
                settings.live_data_dir / "sports_anchors.json",
                [anchor.model_dump(mode="json") for anchor in anchors],
            )
        except Exception as exc:
            provider_runtime_error = _redact(str(exc))
            LOGGER.warning(
                "live_anchor_fetch_failed provider=sports_odds error=%s", provider_runtime_error
            )
            provider_records.append(_provider_error_record("sports_odds", exc))
            anchors = []
        if anchors:
            markets = collector.fetch_fixture_markets(anchors, limit=limit, refresh=refresh)
            markets, anchors = apply_settlement_registry(
                markets, anchors, settings.settlement_registry_file
            )
        matches = resolve_events(markets, anchors, as_of=datetime.now(UTC)) if anchors else []
        markets = bind_fixture_risk(markets, matches, anchors)
        write_sports_match_failure_audit(
            Path("reports/sports_match_failure_audit.md"),
            audit_sports_matching(anchors, markets) if anchors else [],
            audit_polymarket_first(markets, anchors),
        )
        matched_market_ids = {match.market_id for match in matches if match.decision == "MATCH"}
        relevant_markets = [market for market in markets if market.market_id in matched_market_ids]
        if relevant_markets:
            orderbooks = collector.fetch_orderbooks(
                relevant_markets,
                limit=len(relevant_markets),
                refresh=refresh,
            )
    if not anchors:
        write_sports_match_failure_audit(
            Path("reports/sports_match_failure_audit.md"),
            [],
            audit_polymarket_first(markets, anchors),
        )

    settings.live_data_dir.mkdir(parents=True, exist_ok=True)
    write_json(
        settings.live_data_dir / "sports_markets.json", [m.model_dump(mode="json") for m in markets]
    )
    write_json(
        settings.live_data_dir / "sports_orderbooks.json",
        [b.model_dump(mode="json") for b in orderbooks],
    )
    write_event_match_audit(Path("reports/event_match_audit.csv"), matches)

    rewritten_anchors = rewrite_anchors_for_matches(anchors, matches)
    as_of = datetime.now(UTC)
    fair_values = compute_live_fair_values(
        "sports", markets, rewritten_anchors, orderbooks, settings, as_of
    )
    bbas = [book.best_bid_ask() for book in orderbooks]
    generated = generate_signals(markets, fair_values, bbas, settings, as_of=as_of)
    signals = add_avoid_signals(
        markets, generated, fair_values, bbas, matches, anchors, settings, as_of
    )
    risk_rows = risk_diagnostics(signals, markets, settings, as_of)
    signals = annotate_signals(signals, fair_values, matches, risk_rows)
    freshness_path = write_live_freshness_report(
        "sports",
        settings,
        markets,
        orderbooks,
        anchors,
        provider_records,
        as_of=as_of,
    )
    report = render_live_report(
        category="sports",
        markets=markets,
        orderbooks=orderbooks,
        anchors=anchors,
        matches=matches,
        fair_values=fair_values,
        signals=signals,
        risk_rows=risk_rows,
        freshness_path=freshness_path,
        as_of=as_of,
        provider_diagnosis=provider_diagnosis,
        selected_sport=selected_sport,
        provider_runtime_error=provider_runtime_error,
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(report, encoding="utf-8")
    write_signals_csv(Path("reports/latest_signals.csv"), signals)
    return report


def fetch_live_anchors(
    category: str,
    markets: list[PredictionMarket],
    settings: Settings,
    refresh: bool,
) -> tuple[list[ExternalAnchor], list[FreshnessRecord]]:
    if category == "sports":
        try:
            provider = select_sports_odds_provider(settings, refresh=refresh)
            anchors = provider.fetch_odds(
                sport=_default_sports_key(settings, settings.odds_provider),
                limit=settings.odds_provider_event_limit,
                refresh=refresh,
            )
        except Exception as exc:
            LOGGER.warning(
                "live_anchor_fetch_failed provider=sports_odds error=%s", _redact(str(exc))
            )
            return [], [_provider_error_record("sports_odds", exc)]
        as_of = datetime.now(UTC)
        return anchors, [
            freshness_record(
                "sports_odds",
                [anchor.timestamp for anchor in anchors],
                len(anchors),
                as_of,
                settings.max_data_age_seconds,
                missing_reason="odds provider missing or no comparable sports odds returned",
            )
        ]
    if category == "crypto":
        try:
            anchors = CryptoSpotProvider(settings, refresh=refresh).fetch_anchors_for_markets(
                markets
            )
        except Exception as exc:
            LOGGER.warning(
                "live_anchor_fetch_failed provider=crypto_spot error=%s", _redact(str(exc))
            )
            return [], [_provider_error_record("crypto_spot", exc)]
        as_of = datetime.now(UTC)
        return anchors, [
            freshness_record(
                "crypto_spot",
                [anchor.timestamp for anchor in anchors],
                len(anchors),
                as_of,
                settings.max_data_age_seconds,
                missing_reason="no crypto threshold markets parsed or no spot data returned",
            )
        ]
    if category == "weather":
        try:
            anchors = WeatherForecastProvider(settings, refresh=refresh).fetch_anchors_for_markets(
                markets
            )
        except Exception as exc:
            LOGGER.warning(
                "live_anchor_fetch_failed provider=weather_forecast error=%s", _redact(str(exc))
            )
            return [], [_provider_error_record("weather_forecast", exc)]
        as_of = datetime.now(UTC)
        return anchors, [
            freshness_record(
                "weather_forecast",
                [anchor.timestamp for anchor in anchors],
                len(anchors),
                as_of,
                settings.max_data_age_seconds,
                missing_reason="no weather markets parsed or no forecast data returned",
            )
        ]
    return [], [FreshnessRecord(category, 0, None, None, "missing", "unsupported category")]


def _provider_error_record(provider: str, exc: Exception) -> FreshnessRecord:
    return FreshnessRecord(
        provider, 0, None, None, "missing", f"provider error: {_redact(str(exc))}"
    )


def _redact(value: str) -> str:
    return re.sub(r"((?:apiKey|api_key|key|token)=)[^&\s]+", r"\1***", value, flags=re.IGNORECASE)


def _default_sports_key(settings: Settings, provider: str | None) -> str:
    try:
        selected = normalize_provider_name(provider or settings.odds_provider)
    except ValueError:
        selected = "auto"
    if selected == "odds_api_io":
        return "football"
    if selected == "auto" and settings.odds_api_io_key and not settings.the_odds_api_key:
        return "football"
    sport_keys = split_csv(settings.the_odds_api_sport_keys or settings.odds_api_sport_keys)
    return sport_keys[0] if sport_keys else "soccer_epl"


def _provider_configuration_diagnosis(
    settings: Settings,
    provider: str | None,
    exc: Exception,
    sport: str,
) -> ProviderDiagnosis:
    selected = normalize_provider_name(provider or settings.odds_provider)
    any_key = bool(settings.odds_api_io_key or settings.the_odds_api_key or settings.odds_api_key)
    key = settings.odds_api_io_key or settings.the_odds_api_key or settings.odds_api_key
    failure_reason = "key missing" if not any_key else "unknown response shape"
    return ProviderDiagnosis(
        selected_provider=selected,
        base_url="",
        key_present=any_key,
        key_fingerprint=key_fingerprint(key),
        key_source=None,
        endpoint_path="",
        http_status=None,
        provider_error_body=_redact(str(exc)),
        auth_succeeded=False,
        sport_key_valid=None,
        odds_or_events_returned=False,
        returned_count=0,
        failure_reason=failure_reason,
        recommended_next_action=recommendation_for_failure(failure_reason, selected, None, sport),
    )


def write_provider_diagnosis_reports(diagnosis: ProviderDiagnosis) -> None:
    json_path = Path("reports/odds_provider_diagnosis.json")
    md_path = Path("reports/odds_provider_diagnosis.md")
    json_path.parent.mkdir(parents=True, exist_ok=True)
    json_path.write_text(
        json.dumps(diagnosis.to_dict(), indent=2, sort_keys=True), encoding="utf-8"
    )
    lines = [
        "# Odds Provider Diagnosis",
        "",
        f"- selected provider: `{diagnosis.selected_provider}`",
        f"- base URL: `{diagnosis.base_url}`",
        f"- key present: `{diagnosis.key_present}`",
        f"- key source: `{diagnosis.key_source or ''}`",
        f"- key fingerprint: `{diagnosis.key_fingerprint}`",
        f"- endpoint path: `{diagnosis.endpoint_path}`",
        f"- HTTP status: `{'' if diagnosis.http_status is None else diagnosis.http_status}`",
        f"- auth succeeded: `{diagnosis.auth_succeeded}`",
        f"- sport key valid: `{diagnosis.sport_key_valid}`",
        f"- odds/events returned: `{diagnosis.odds_or_events_returned}`",
        f"- returned count: `{diagnosis.returned_count}`",
        f"- failure reason: `{diagnosis.failure_reason or ''}`",
        f"- recommended next action: {diagnosis.recommended_next_action}",
        "",
        "## Provider Error Body",
        "",
        diagnosis.provider_error_body or "No provider error body.",
    ]
    md_path.write_text("\n".join(lines), encoding="utf-8")


def provider_diagnosis_record(diagnosis: ProviderDiagnosis) -> FreshnessRecord:
    status = "unverified" if diagnosis.succeeded else "missing"
    reason = (
        "provider response accepted (may be cached); price freshness is checked separately"
        if diagnosis.succeeded
        else f"provider diagnosis failed: {diagnosis.failure_reason or 'unknown response shape'}"
    )
    return FreshnessRecord(
        diagnosis.selected_provider,
        diagnosis.returned_count,
        None,
        None,
        status,
        reason,
    )


def rewrite_anchors_for_matches(
    anchors: list[ExternalAnchor],
    matches: list[EventMatch],
) -> list[ExternalAnchor]:
    output: list[ExternalAnchor] = []
    for match in matches:
        if match.decision != "MATCH":
            continue
        for anchor in anchors:
            if anchor.event_id == match.external_event_id and anchor.market_id in {
                None,
                match.market_id,
            }:
                output.append(
                    anchor.model_copy(
                        update={
                            "event_id": match.prediction_market_event_id,
                            "market_id": match.market_id,
                        }
                    )
                )
    return output


def compute_live_fair_values(
    category: str,
    markets: list[PredictionMarket],
    anchors: list[ExternalAnchor],
    orderbooks: list[OrderBook],
    settings: Settings,
    as_of: datetime,
) -> list[FairValue]:
    if category == "sports":
        return SportsOddsModel(settings, as_of=as_of).compute(markets, anchors, orderbooks)
    if category == "crypto":
        return CryptoThresholdModel(settings, as_of=as_of).compute(markets, anchors, orderbooks)
    if category == "weather":
        return WeatherThresholdModel(settings, as_of=as_of).compute(markets, anchors, orderbooks)
    return []


def add_avoid_signals(
    markets: list[PredictionMarket],
    generated: list[Signal],
    fair_values: list[FairValue],
    bbas: list[BestBidAsk],
    matches: list[EventMatch],
    anchors: list[ExternalAnchor],
    settings: Settings,
    as_of: datetime,
) -> list[Signal]:
    signals_by_market = {signal.market_id: signal for signal in generated}
    fair_by_market = {fair.market_id: fair for fair in fair_values}
    bba_by_market = {bba.market_id: bba for bba in bbas}
    match_by_market = {match.market_id: match for match in matches}
    output = list(generated)
    for market in markets:
        if market.market_id in signals_by_market:
            continue
        reason = avoid_reason(market, fair_by_market, bba_by_market, match_by_market, bool(anchors))
        bba = bba_by_market.get(market.market_id)
        output.append(
            Signal(
                signal_id=f"{market.market_id}:AVOID:{as_of.isoformat()}",
                timestamp=as_of,
                market_id=market.market_id,
                event_id=market.event_id,
                risk_event_id=market.risk_event_id,
                outcome="Yes"
                if "Yes" in market.outcomes
                else market.outcomes[0]
                if market.outcomes
                else "",
                category=market.category,
                fair_probability=0.0,
                prediction_bid=bba.best_bid if bba else None,
                prediction_ask=bba.best_ask if bba else None,
                raw_edge=0.0,
                cost_drag=0.0,
                adjusted_edge=-1.0,
                available_size=0.0,
                confidence=0.0,
                action="AVOID",
                reason=reason,
                fair_value_source="none",
                fair_value_timestamp=None,
                orderbook_timestamp=bba.timestamp if bba else None,
                data_age_seconds=None,
                freshness_status="unknown",
            )
        )
    return sorted(output, key=lambda signal: signal.adjusted_edge, reverse=True)


def avoid_reason(
    market: PredictionMarket,
    fair_by_market: dict[str, FairValue],
    bba_by_market: dict[str, BestBidAsk],
    match_by_market: dict[str, EventMatch],
    has_anchors: bool,
) -> str:
    match = match_by_market.get(market.market_id)
    if not has_anchors:
        return "no fair-value source"
    if match and match.decision != "MATCH":
        return f"no comparable market: {match.rejection_reason or 'match rejected'}"
    if not match and market.category.startswith("sports"):
        return "no comparable market"
    if market.market_id not in bba_by_market:
        return "no executable order book"
    if market.market_id not in fair_by_market:
        return "no fair-value source"
    return "unmatched research state"


def risk_diagnostics(
    signals: list[Signal],
    markets: list[PredictionMarket],
    settings: Settings,
    as_of: datetime,
) -> dict[str, list[str]]:
    risk = RiskManager(settings)
    markets_by_id = {market.market_id: market for market in markets}
    output: dict[str, list[str]] = {}
    for signal in signals:
        market = markets_by_id.get(signal.market_id)
        if not market:
            output[signal.signal_id] = ["missing market metadata"]
            continue
        timestamp = signal.orderbook_timestamp or signal.fair_value_timestamp or signal.timestamp
        decision = risk.check(
            signal=signal,
            market=market,
            proposed_size=min(settings.max_trade_size, signal.available_size),
            data_timestamp=timestamp,
            live=False,
            as_of=as_of,
        )
        output[signal.signal_id] = decision.reasons
    return output


def annotate_signals(
    signals: list[Signal],
    fair_values: list[FairValue],
    matches: list[EventMatch],
    risk_rows: dict[str, list[str]],
) -> list[Signal]:
    fair_by_market = {fair.market_id: fair for fair in fair_values}
    match_by_market = {match.market_id: match for match in matches}
    output: list[Signal] = []
    for signal in signals:
        fair = fair_by_market.get(signal.market_id)
        match = match_by_market.get(signal.market_id)
        reasons = risk_rows.get(signal.signal_id, [])
        provider = signal.fair_value_provider
        if fair and not provider:
            provider = fair.metadata.get("provider")
        output.append(
            signal.model_copy(
                update={
                    "fair_value_provider": provider,
                    "match_score": match.score if match else None,
                    "risk_approved": not reasons,
                    "risk_reasons": "; ".join(reasons),
                    "risk_decision": "REJECTED" if reasons else "APPROVED",
                    "final_decision": "REJECTED_BY_RISK" if reasons else "PAPER_CANDIDATE",
                    "action": "AVOID" if reasons else signal.action,
                    "signal_action": "AVOID" if reasons else signal.action,
                }
            )
        )
    return output


def write_live_freshness_report(
    category: str,
    settings: Settings,
    markets: list[PredictionMarket],
    orderbooks: list[OrderBook],
    anchors: list[ExternalAnchor],
    provider_records: list[FreshnessRecord],
    as_of: datetime | None = None,
) -> Path:
    as_of = as_of or datetime.now(UTC)
    records = [
        freshness_record(
            "polymarket_markets",
            [
                datetime.fromisoformat(str(m.raw_metadata["collected_at"]))
                for m in markets
                if m.raw_metadata.get("collected_at")
            ],
            len(markets),
            as_of,
            settings.max_data_age_seconds,
            missing_reason="no Polymarket markets returned",
        ),
        freshness_record(
            "polymarket_orderbooks",
            [book.timestamp for book in orderbooks],
            len(orderbooks),
            as_of,
            settings.max_data_age_seconds,
            missing_reason="no CLOB books returned",
        ),
        freshness_record(
            "external_anchors",
            [anchor.timestamp for anchor in anchors],
            len(anchors),
            as_of,
            settings.max_data_age_seconds,
            missing_reason="no external fair-value anchors returned",
        ),
        *provider_records,
    ]
    path = Path("reports") / f"data_freshness_{category}.md"
    write_freshness_report(path, records, as_of)
    return path


def render_live_report(
    category: str,
    markets: list[PredictionMarket],
    orderbooks: list[OrderBook],
    anchors: list[ExternalAnchor],
    matches: list[EventMatch],
    fair_values: list[FairValue],
    signals: list[Signal],
    risk_rows: dict[str, list[str]],
    freshness_path: Path,
    as_of: datetime,
    provider_diagnosis: ProviderDiagnosis | None = None,
    selected_sport: str | None = None,
    provider_runtime_error: str | None = None,
) -> str:
    if category == "sports":
        return render_live_sports_report(
            markets=markets,
            orderbooks=orderbooks,
            anchors=anchors,
            matches=matches,
            fair_values=fair_values,
            signals=signals,
            risk_rows=risk_rows,
            freshness_path=freshness_path,
            as_of=as_of,
            provider_diagnosis=provider_diagnosis,
            selected_sport=selected_sport,
            provider_runtime_error=provider_runtime_error,
        )
    avoid_counts = Counter(
        signal.reason for signal in signals if signal.action in {"AVOID", "HOLD"}
    )
    actionable = [signal for signal in signals if signal.action in {"BUY_YES", "SELL_YES"}]
    lines = [
        f"# EdgeLab Live {category.title()} Research Report",
        "",
        f"- as_of: `{as_of.isoformat()}`",
        f"- markets scanned: {len(markets)}",
        f"- order books collected: {len(orderbooks)}",
        f"- external anchors collected: {len(anchors)}",
        f"- matches: {sum(1 for match in matches if match.decision == 'MATCH')}",
        f"- fair values: {len(fair_values)}",
        f"- actionable signals: {len(actionable)}",
        f"- freshness report: `{freshness_path}`",
        "",
        "Live trading is disabled. This report is research-only.",
        "",
        "## Outcome",
        "",
    ]
    if actionable:
        lines.append(
            "At least one signal survived edge calculation. It still requires paper trading and CLV validation."
        )
    else:
        lines.append("No actionable edge found.")
    lines.extend(["", "## Why No Edge / Avoid Reasons", ""])
    if avoid_counts:
        lines.extend(
            markdown_table(
                ["reason", "count"],
                [[reason, str(count)] for reason, count in avoid_counts.items()],
            )
        )
    else:
        lines.append("No avoid reasons recorded.")
    lines.extend(["", "## Edge Table", ""])
    if signals:
        lines.extend(
            markdown_table(
                [
                    "market",
                    "category",
                    "fair",
                    "bid",
                    "ask",
                    "adjusted",
                    "freshness",
                    "action",
                    "reason",
                    "risk",
                ],
                [
                    [
                        signal.market_id,
                        signal.category,
                        f"{signal.fair_probability:.3f}",
                        "" if signal.prediction_bid is None else f"{signal.prediction_bid:.3f}",
                        "" if signal.prediction_ask is None else f"{signal.prediction_ask:.3f}",
                        f"{signal.adjusted_edge:.3f}",
                        signal.freshness_status,
                        signal.action,
                        signal.reason,
                        "; ".join(risk_rows.get(signal.signal_id, [])),
                    ]
                    for signal in signals[:100]
                ],
            )
        )
    else:
        lines.append("No markets were converted into research signals.")
    lines.extend(
        [
            "",
            "## Interpretation",
            "",
            "If no edge is shown, the most common causes are no comparable market, no fair-value "
            "source, low liquidity, wide spread, stale data, high uncertainty, or risk rejection. "
            "The table above records the concrete cause per market.",
        ]
    )
    return "\n".join(lines)


def render_live_sports_report(
    markets: list[PredictionMarket],
    orderbooks: list[OrderBook],
    anchors: list[ExternalAnchor],
    matches: list[EventMatch],
    fair_values: list[FairValue],
    signals: list[Signal],
    risk_rows: dict[str, list[str]],
    freshness_path: Path,
    as_of: datetime,
    provider_diagnosis: ProviderDiagnosis | None,
    selected_sport: str | None,
    provider_runtime_error: str | None,
) -> str:
    actionable = [signal for signal in signals if signal.action in {"BUY_YES", "SELL_YES"}]
    risk_approved = [signal for signal in actionable if signal.risk_approved]
    matched = [match for match in matches if match.decision == "MATCH"]
    rejected = [match for match in matches if match.decision != "MATCH"]
    review_count = sum(1 for match in matches if match.decision == "REVIEW")
    avoid_counts = Counter(
        signal.reason for signal in signals if signal.action in {"AVOID", "HOLD"}
    )
    rejection_counts = Counter(match.rejection_reason or "none" for match in rejected)
    provider_passed = provider_diagnosis.succeeded if provider_diagnosis else None
    paper_summary = _paper_trade_summary()
    clv_summary = _clv_summary()
    adjusted_survivors = [signal for signal in risk_approved if signal.adjusted_edge > 0]
    event_titles = sorted(
        {
            str(anchor.metadata.get("event_title") or anchor.event_id)
            for anchor in anchors
            if anchor.metadata.get("event_title") or anchor.event_id
        }
    )
    lines = [
        "# EdgeLab Live Sports Research Report",
        "",
        "## Executive Summary",
        "",
        f"- as_of: `{as_of.isoformat()}`",
        f"- selected sport: `{selected_sport or ''}`",
        f"- provider diagnosis passed: `{provider_passed}`",
        f"- markets scanned: {len(markets)}",
        f"- odds events/anchors collected: {len(event_titles)} events / {len(anchors)} anchors",
        f"- matches / review / rejected: {len(matched)} / {review_count} / {len(rejected)}",
        f"- fair values: {len(fair_values)}",
        f"- signals: {len(signals)}",
        f"- risk-approved edges: {len(risk_approved)}",
        f"- freshness report: `{freshness_path}`",
        "",
    ]
    if not anchors:
        lines.append("No fair-value source was available; every sports market is marked AVOID.")
    elif not matched:
        lines.append("External odds were collected, but no Polymarket sports market matched them.")
    elif not risk_approved:
        lines.append(
            "Matched markets did not survive execution costs, data quality checks, and risk rules."
        )
    else:
        lines.append(
            "At least one live signal survived risk checks. This is not profitability evidence without paper fills and CLV."
        )

    lines.extend(
        [
            "",
            "## Data Sources And Provider Diagnosis",
            "",
        ]
    )
    if provider_diagnosis:
        lines.extend(
            markdown_table(
                [
                    "provider",
                    "base_url",
                    "key",
                    "endpoint",
                    "status",
                    "auth",
                    "sport",
                    "returned",
                    "failure",
                ],
                [
                    [
                        provider_diagnosis.selected_provider,
                        provider_diagnosis.base_url,
                        provider_diagnosis.key_fingerprint,
                        provider_diagnosis.endpoint_path,
                        ""
                        if provider_diagnosis.http_status is None
                        else str(provider_diagnosis.http_status),
                        str(provider_diagnosis.auth_succeeded),
                        str(provider_diagnosis.sport_key_valid),
                        str(provider_diagnosis.returned_count),
                        provider_diagnosis.failure_reason or "",
                    ]
                ],
            )
        )
        lines.extend(["", f"Recommended next action: {provider_diagnosis.recommended_next_action}"])
    else:
        lines.append(
            "Provider diagnosis was skipped by flag; this run should not be used as auth evidence."
        )
    if provider_runtime_error:
        lines.extend(["", f"Runtime provider fetch error: `{provider_runtime_error}`"])

    lines.extend(
        [
            "",
            "## Market Coverage",
            "",
            f"- Polymarket candidate markets: {len(markets)}",
            f"- CLOB books fetched: {len(orderbooks)}",
            f"- Polymarket category queries tried: {', '.join(CATEGORY_SEARCH_QUERIES.get('sports', ()))}",
            f"- Odds events searched: {len(event_titles)}",
        ]
    )
    if event_titles:
        lines.extend(["", "Top odds events searched:"])
        lines.extend(f"- {title}" for title in event_titles[:10])

    lines.extend(["", "## Event Matching Audit Summary", ""])
    if matches:
        lines.extend(
            markdown_table(
                ["decision", "count"],
                [
                    [decision, str(count)]
                    for decision, count in Counter(m.decision for m in matches).items()
                ],
            )
        )
        if rejection_counts:
            lines.extend(["", "Top rejection reasons:"])
            lines.extend(
                f"- {reason}: {count}" for reason, count in rejection_counts.most_common(8)
            )
    else:
        lines.append("No event matching was attempted because no external anchors were available.")

    lines.extend(
        [
            "",
            "## Fair-Value Methodology",
            "",
            "Sports fair values use external sportsbook h2h/1X2 odds, convert decimal odds to implied probabilities, remove overround by normalizing probabilities within each bookmaker, then average comparable bookmaker probabilities for the mapped Polymarket Yes outcome.",
            "",
            "## Edge Calculation Formula",
            "",
            "`raw buy edge = fair_probability - executable_ask`; `raw sell edge = executable_bid - fair_probability`; `adjusted_edge = raw_edge - slippage - fees - uncertainty - resolution_risk - safety_margin`.",
            "",
            "## Cost Decomposition",
            "",
        ]
    )
    lines.extend(
        markdown_table(
            ["cost", "setting"],
            [
                [
                    "slippage",
                    f"{signals[0].cost_drag:.4f} total drag includes all costs"
                    if signals
                    else "n/a",
                ],
                ["fee_bps", "included in cost drag"],
                ["model uncertainty", "max(model estimate, configured floor)"],
                ["resolution risk", "configured bps"],
                ["safety margin", "configured bps"],
            ],
        )
    )

    lines.extend(["", "## Top Apparent Edges", ""])
    edge_rows = [signal for signal in signals if signal.adjusted_edge > -1.0][:10]
    if edge_rows:
        lines.extend(_signal_table(edge_rows, risk_rows))
    else:
        lines.append("No apparent edge rows were produced.")

    lines.extend(["", "## Risk-Approved Edges", ""])
    if risk_approved:
        lines.extend(_signal_table(risk_approved[:10], risk_rows))
    else:
        lines.append("No risk-approved sports edges.")

    lines.extend(["", "## Rejected Signals By Reason", ""])
    if avoid_counts:
        lines.extend(
            markdown_table(
                ["reason", "count"],
                [[reason, str(count)] for reason, count in avoid_counts.items()],
            )
        )
    else:
        lines.append("No rejected signals recorded.")

    lines.extend(
        [
            "",
            "## Paper Trades, If Any",
            "",
            paper_summary,
            "",
            "## CLV Evidence, If Any",
            "",
            clv_summary,
            "",
            "## Failure Modes",
            "",
        ]
    )
    lines.extend(
        _failure_modes(provider_diagnosis, provider_runtime_error, anchors, matches, signals)
    )
    lines.extend(
        [
            "",
            "## Whether There Is Evidence Of Profitability",
            "",
        ]
    )
    if risk_approved and "Average CLV" in clv_summary and "No CLV" not in clv_summary:
        lines.append(
            "There is preliminary evidence to review, but it is not enough to claim profitability without a meaningful paper sample."
        )
    else:
        lines.append("There is no evidence of profitability from this run.")
    lines.extend(
        [
            "",
            "## Next Experiment",
            "",
            _next_experiment(
                provider_diagnosis, anchors, matched, risk_approved, adjusted_survivors
            ),
        ]
    )
    return "\n".join(lines)


def _signal_table(signals: list[Signal], risk_rows: dict[str, list[str]]) -> list[str]:
    return markdown_table(
        ["market", "fair", "bid", "ask", "adjusted", "action", "risk_approved", "reason"],
        [
            [
                signal.market_id,
                f"{signal.fair_probability:.3f}",
                "" if signal.prediction_bid is None else f"{signal.prediction_bid:.3f}",
                "" if signal.prediction_ask is None else f"{signal.prediction_ask:.3f}",
                f"{signal.adjusted_edge:.3f}",
                signal.action,
                str(signal.risk_approved),
                str(
                    signal.risk_reasons
                    or signal.reason
                    or "; ".join(risk_rows.get(signal.signal_id, []))
                ),
            ]
            for signal in signals
        ],
    )


def _failure_modes(
    provider_diagnosis: ProviderDiagnosis | None,
    provider_runtime_error: str | None,
    anchors: list[ExternalAnchor],
    matches: list[EventMatch],
    signals: list[Signal],
) -> list[str]:
    modes: list[str] = []
    if provider_diagnosis and not provider_diagnosis.succeeded:
        modes.append(f"- provider diagnosis failed: {provider_diagnosis.failure_reason}")
    if provider_runtime_error:
        modes.append(f"- provider odds fetch failed: {provider_runtime_error}")
    if not anchors:
        modes.append("- no fair-value source")
    if anchors and not any(match.decision == "MATCH" for match in matches):
        modes.append("- no matched Polymarket markets")
    if signals and not any(signal.risk_approved for signal in signals):
        modes.append("- no signal survived risk checks")
    return modes or [
        "- no major failure mode detected, but paper trading and CLV evidence are still required"
    ]


def _next_experiment(
    provider_diagnosis: ProviderDiagnosis | None,
    anchors: list[ExternalAnchor],
    matched: list[EventMatch],
    risk_approved: list[Signal],
    adjusted_survivors: list[Signal],
) -> str:
    if provider_diagnosis and not provider_diagnosis.succeeded:
        return "Fix provider identity and auth first, then rerun the sports workflow."
    if not anchors:
        return "Collect a provider-backed odds snapshot for a sport with active Polymarket markets."
    if not matched:
        return "Run repeated football/soccer scans and expand alias rules around teams, league names, and event start times."
    if not risk_approved:
        return "Collect repeated snapshots to identify whether rejections are mostly liquidity, spread, stale data, or model uncertainty."
    if not adjusted_survivors:
        return "Paper trade only risk-approved rows and track CLV before considering any production trading design."
    return "Collect at least several dozen paper fills with 5m/30m/1h CLV before making any profitability claim."


def _paper_trade_summary() -> str:
    path = Path("data/paper_trades/ledger.csv")
    if not path.exists():
        return "No paper ledger exists yet."
    import csv

    with path.open(newline="", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    approved = sum(1 for row in rows if row.get("approved") in {"True", "true", "1"})
    skipped = len(rows) - approved
    return f"Ledger rows: {len(rows)}; approved paper trades: {approved}; skipped: {skipped}."


def _clv_summary() -> str:
    path = Path("reports/clv_report.md")
    if not path.exists():
        return "No CLV report exists yet."
    text = path.read_text(encoding="utf-8")
    lines = [
        line
        for line in text.splitlines()
        if line.startswith("- ") or "insufficient follow-up data" in line
    ]
    return (
        "\n".join(lines[:8]) if lines else "CLV report exists but has no summarized observations."
    )


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")


def write_signals_csv(path: Path, signals: list[Signal]) -> None:
    import csv

    path.parent.mkdir(parents=True, exist_ok=True)
    columns = list(Signal.model_fields)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns)
        writer.writeheader()
        for signal in signals:
            writer.writerow(signal.model_dump(mode="json"))


def markdown_table(headers: list[str], rows: list[list[str]]) -> list[str]:
    output = ["| " + " | ".join(headers) + " |", "| " + " | ".join("---" for _ in headers) + " |"]
    for row in rows:
        output.append("| " + " | ".join(str(cell).replace("|", "\\|") for cell in row) + " |")
    return output
