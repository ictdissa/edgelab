"""Real CLOB timestamps are numeric strings, not necessarily JSON numbers."""

from datetime import UTC, datetime

import pytest

from edgelab.data.external_sources.odds_provider import parse_timestamp
from edgelab.data.polymarket_collector import parse_clob_order_book


@pytest.mark.parametrize(
    "value",
    [
        "1791902094456",
        1791902094456,
        1791902094456.0,
        "1791902094.456",
        1791902094.456,
        "2026-10-13T14:34:54.456Z",
        "2026-10-13T22:34:54.456+08:00",
        "2026-10-13T14:34:54.456",
        datetime(2026, 10, 13, 14, 34, 54, 456000),
    ],
)
def test_numeric_and_iso_timestamps_preserve_source_clock(value):
    expected = datetime(2026, 10, 13, 14, 34, 54, 456000, UTC)
    parsed = parse_timestamp(value)
    assert abs((parsed - expected).total_seconds()) < 0.00001
    received = datetime(2026, 10, 13, 14, 35, tzinfo=UTC)
    book = parse_clob_order_book("m", "t", {"timestamp": value}, observed_at=received)
    assert abs((book.timestamp - expected).total_seconds()) < 0.00001
    assert book.available_at == received


@pytest.mark.parametrize(
    "value",
    [None, "", "not-a-time", True, False, float("nan"), float("inf"), 1e100, "9" * 400, {}, []],
)
def test_invalid_timestamp_fails_closed_without_receipt_time_substitution(value):
    assert parse_timestamp(value) is None
    received = datetime(2026, 10, 13, 14, 35, tzinfo=UTC)
    book = parse_clob_order_book("m", "t", {"timestamp": value}, observed_at=received)
    assert book.timestamp == datetime.fromtimestamp(0, UTC)
    assert book.available_at == received


def test_future_source_clock_is_not_backdated_to_receipt():
    received = datetime(2026, 10, 13, 14, tzinfo=UTC)
    book = parse_clob_order_book("m", "t", {"timestamp": "1791902094456"}, observed_at=received)
    assert book.timestamp > received
