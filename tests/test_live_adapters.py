from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from edgelab.config.settings import Settings
from edgelab.data.cache import CachedHttpClient, RollingRateLimiter
from edgelab.data.external_sources.crypto_provider import CryptoSpotProvider
from edgelab.data.external_sources.the_odds_api_provider import TheOddsAPIProvider
from edgelab.data.polymarket_collector import PolymarketCollector, parse_clob_order_book
from edgelab.data.schemas import BestBidAsk, ExternalAnchor, PredictionMarket
from edgelab.fair_value.sports_odds_model import SportsOddsModel
from edgelab.research.live_analysis import add_avoid_signals


class FakeResponse:
    status_code = 200
    text = ""

    def __init__(self, payload):
        self._payload = payload

    def json(self):
        return self._payload


def settings(tmp_path: Path, **overrides) -> Settings:
    return Settings(
        EDGELAB_CACHE_DIR=tmp_path / "cache",
        EDGELAB_LIVE_DATA_DIR=tmp_path / "live",
        ODDS_API_KEY=overrides.pop("odds_api_key", None),
        **overrides,
    )


def test_cached_http_reuses_cache_without_second_call(tmp_path: Path) -> None:
    calls: list[str] = []

    def fake_get(url: str, **kwargs):
        calls.append(url)
        return FakeResponse({"ok": True})

    limiter = RollingRateLimiter(tmp_path / "rate.json", max_requests=10)
    client = CachedHttpClient(tmp_path / "cache", "provider", limiter, fake_get)
    first = client.get_json("https://example.com/data", {"a": "1"})
    second = client.get_json("https://example.com/data", {"a": "1"})

    assert not first.cache_hit
    assert second.cache_hit
    assert len(calls) == 1


def test_cached_http_keeps_clob_token_id_in_cache_key(tmp_path: Path) -> None:
    calls: list[str] = []

    def fake_get(url: str, **kwargs):
        token_id = kwargs["params"]["token_id"]
        calls.append(token_id)
        return FakeResponse({"token_id": token_id})

    limiter = RollingRateLimiter(tmp_path / "rate.json", max_requests=10)
    client = CachedHttpClient(tmp_path / "cache", "polymarket", limiter, fake_get)
    first = client.get_json("https://clob.polymarket.com/book", {"token_id": "yes"})
    second = client.get_json("https://clob.polymarket.com/book", {"token_id": "no"})

    assert first.payload["token_id"] == "yes"
    assert second.payload["token_id"] == "no"
    assert len(calls) == 2


def test_odds_provider_without_key_makes_no_paid_call(tmp_path: Path) -> None:
    def forbidden_get(url: str, **kwargs):
        raise AssertionError("ODDS_API_KEY is missing; provider must not call HTTP")

    provider = TheOddsAPIProvider(settings(tmp_path), http_get=forbidden_get)
    assert provider.fetch_odds() == []


def test_polymarket_collector_parses_markets_and_orderbooks(tmp_path: Path) -> None:
    payloads = {
        "/markets": [
            {
                "conditionId": "m1",
                "eventId": "e1",
                "question": "Will BTC be above $100,000 on June 30, 2026?",
                "outcomes": '["Yes","No"]',
                "clobTokenIds": '["yes","no"]',
                "active": True,
                "closed": False,
                "endDate": "2026-06-30T23:59:00Z",
                "resolutionSource": "BTC index",
            }
        ],
        "/book": {
            "timestamp": "2026-05-18T00:00:00Z",
            "bids": [{"price": "0.39", "size": "10"}],
            "asks": [{"price": "0.41", "size": "12"}],
        },
    }

    def fake_get(url: str, **kwargs):
        if url.endswith("/markets"):
            return FakeResponse(payloads["/markets"])
        if url.endswith("/book"):
            return FakeResponse(payloads["/book"])
        raise AssertionError(url)

    collector = PolymarketCollector(settings(tmp_path), mock=False, http_get=fake_get)
    markets = collector.fetch_markets(category="crypto", limit=1, refresh=True)
    books = collector.fetch_orderbooks(markets, refresh=True)

    assert len(markets) == 1
    assert markets[0].category == "crypto_threshold"
    assert len(books) == 2
    assert {book.token_id for book in books} == {"yes", "no"}
    assert books[0].best_bid_ask().best_ask == 0.41


def test_polymarket_collector_search_fallback_when_category_underfills(tmp_path: Path) -> None:
    def fake_get(url: str, **kwargs):
        if url.endswith("/markets"):
            return FakeResponse([])
        if url.endswith("/public-search"):
            return FakeResponse(
                {
                    "events": [
                        {
                            "id": "e1",
                            "title": "Bitcoin markets",
                            "markets": [
                                {
                                    "conditionId": "m1",
                                    "question": "Will Bitcoin be above $100,000 on June 30, 2026?",
                                    "outcomes": '["Yes","No"]',
                                    "clobTokenIds": '["yes","no"]',
                                    "active": True,
                                    "closed": False,
                                    "endDate": "2026-06-30T23:59:00Z",
                                    "resolutionSource": "BTC index",
                                }
                            ],
                        }
                    ]
                }
            )
        raise AssertionError(url)

    collector = PolymarketCollector(settings(tmp_path), mock=False, http_get=fake_get)
    markets = collector.fetch_markets(category="crypto", limit=1, refresh=True)

    assert len(markets) == 1
    assert markets[0].event_id == "e1"


def test_crypto_provider_builds_anchor_from_cached_spot(tmp_path: Path) -> None:
    market = PredictionMarket(
        market_id="m1",
        event_id="e1",
        category="crypto_threshold",
        title="Will BTC be above $100,000 on June 30, 2026?",
        outcomes=["Yes", "No"],
        token_ids=["yes", "no"],
        end_time=datetime(2026, 6, 30, tzinfo=UTC),
        resolution_source="BTC index",
    )

    def fake_get(url: str, **kwargs):
        return FakeResponse({"bitcoin": {"usd": 95_000}})

    anchors = CryptoSpotProvider(settings(tmp_path), http_get=fake_get, refresh=True).fetch_anchors_for_markets([market])
    assert len(anchors) == 1
    assert anchors[0].metadata["spot"] == 95_000


def test_sports_model_maps_live_anchor_outcomes_without_fixture_names(tmp_path: Path) -> None:
    as_of = datetime(2026, 5, 18, tzinfo=UTC)
    market = PredictionMarket(
        market_id="m1",
        event_id="e1",
        category="sports_moneyline",
        title="Will Liverpool beat Manchester City?",
        outcomes=["Yes", "No"],
        token_ids=["yes", "no"],
        resolution_source="official result",
    )
    anchors = [
        ExternalAnchor(
            source_id="book:e1:liv",
            event_id="e1",
            source="book",
            category="sports_moneyline",
            outcome="Liverpool",
            decimal_odds=2.10,
            timestamp=as_of,
        ),
        ExternalAnchor(
            source_id="book:e1:mci",
            event_id="e1",
            source="book",
            category="sports_moneyline",
            outcome="Manchester City",
            decimal_odds=1.80,
            timestamp=as_of,
        ),
    ]

    values = SportsOddsModel(settings(tmp_path), as_of=as_of).compute([market], anchors, [])

    assert len(values) == 1
    assert values[0].metadata["target_outcome"] == "Liverpool"


def test_missing_live_fair_value_source_outputs_avoid() -> None:
    market = PredictionMarket(
        market_id="m1",
        event_id="e1",
        category="sports_1x2",
        title="Will Arsenal win against Chelsea?",
        outcomes=["Yes", "No"],
        token_ids=["yes", "no"],
        resolution_source="official",
    )
    bba = BestBidAsk(
        market_id="m1",
        token_id="yes",
        timestamp=datetime(2026, 5, 18, tzinfo=UTC),
        best_bid=0.4,
        best_bid_size=10,
        best_ask=0.42,
        best_ask_size=10,
        mid=0.41,
        spread=0.02,
    )
    signals = add_avoid_signals(
        markets=[market],
        generated=[],
        fair_values=[],
        bbas=[bba],
        matches=[],
        anchors=[],
        settings=Settings(),
        as_of=datetime(2026, 5, 18, tzinfo=UTC),
    )
    assert signals[0].action == "AVOID"
    assert signals[0].reason == "no fair-value source"


def test_parse_clob_order_book_tolerates_empty_book() -> None:
    book = parse_clob_order_book("m", "yes", {"bids": [], "asks": []})
    assert book is not None
    assert book.best_bid_ask().best_bid is None
