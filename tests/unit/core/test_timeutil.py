"""core.timeutil: UTC and Unix-nanosecond helpers (plan task P1-06.1, Constitution Art. 5)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone

import pytest
from hypothesis import given
from hypothesis import strategies as st

from reflex_agentlens.core import timeutil
from reflex_agentlens.core.errors import ValidationError


def test_now_ns_is_integer_nanoseconds() -> None:
    n = timeutil.now_ns()
    assert isinstance(n, int)
    assert n > 1_600_000_000 * timeutil.NS_PER_S


def test_datetime_ns_round_trip_keeps_microseconds() -> None:
    dt = datetime(2026, 10, 30, 12, 0, 0, 123456, tzinfo=UTC)
    ns = timeutil.datetime_to_ns(dt)
    assert ns == 1_793_361_600_123_456_000
    assert timeutil.ns_to_datetime(ns) == dt


def test_non_utc_datetimes_are_converted() -> None:
    dt = datetime(2026, 10, 30, 14, 0, tzinfo=timezone(timedelta(hours=2)))
    assert timeutil.datetime_to_ns(dt) == timeutil.datetime_to_ns(
        datetime(2026, 10, 30, 12, 0, tzinfo=UTC)
    )


def test_naive_datetime_is_rejected() -> None:
    with pytest.raises(ValidationError):
        timeutil.datetime_to_ns(datetime(2026, 10, 30))


def test_rfc3339_formatting_and_parsing() -> None:
    ns = 1_793_361_600_123_000_000
    assert timeutil.ns_to_rfc3339(ns) == "2026-10-30T12:00:00.123Z"
    assert timeutil.parse_rfc3339("2026-10-30T12:00:00.123Z") == ns
    assert timeutil.parse_rfc3339("2026-10-30T14:00:00.123+02:00") == ns
    with pytest.raises(ValidationError):
        timeutil.parse_rfc3339("2026-10-30T12:00:00")  # no offset


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("500ms", timedelta(milliseconds=500)),
        ("30s", timedelta(seconds=30)),
        ("5m", timedelta(minutes=5)),
        ("1h", timedelta(hours=1)),
        ("30d", timedelta(days=30)),
        ("0", timedelta(0)),
        (20, timedelta(seconds=20)),
    ],
)
def test_parse_duration(text: str | int, expected: timedelta) -> None:
    assert timeutil.parse_duration(text) == expected


@pytest.mark.parametrize("bad", ["", "5", "-5s", "1.5h", "5 minutes", "5w", True, 2.5])
def test_parse_duration_rejects(bad: object) -> None:
    if bad == "5":
        assert timeutil.parse_duration("5") == timedelta(seconds=5)  # bare integer string = s
        return
    with pytest.raises(ValidationError):
        timeutil.parse_duration(bad)


@given(st.integers(min_value=0, max_value=10**7))
def test_format_duration_round_trips(ms: int) -> None:
    td = timedelta(milliseconds=ms)
    assert timeutil.parse_duration(timeutil.format_duration(td)) == td
