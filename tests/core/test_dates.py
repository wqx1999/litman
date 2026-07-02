"""Tests for the read-date / last-revisited ordering rule (invariant #11).

``date_ordering_violations`` is the single source shared by the modify-time
guard (raises ``ModifyError``) and ``check_schema`` (emits ``Issue``), so the
rule is unit-tested here once.
"""

from __future__ import annotations

from datetime import date

from litman.core.dates import date_ordering_violations


def test_consistent_pair_has_no_violations() -> None:
    assert (
        date_ordering_violations("2026-05-01", "2026-05-10", today="2026-06-01")
        == []
    )


def test_read_date_only_is_fine() -> None:
    assert date_ordering_violations("2026-05-01", None, today="2026-06-01") == []


def test_both_empty_is_fine() -> None:
    assert date_ordering_violations(None, None, today="2026-06-01") == []
    assert date_ordering_violations("", "", today="2026-06-01") == []


def test_equal_dates_allowed() -> None:
    # Read + revisit on the same day is consistent (≤, not <).
    assert (
        date_ordering_violations("2026-05-01", "2026-05-01", today="2026-06-01")
        == []
    )


def test_last_revisited_without_read_date_flagged() -> None:
    out = date_ordering_violations(None, "2026-05-10", today="2026-06-01")
    assert len(out) == 1
    assert "read-date is not" in out[0]


def test_read_date_after_last_revisited_flagged() -> None:
    out = date_ordering_violations("2026-05-20", "2026-05-10", today="2026-06-01")
    assert any("after last-revisited" in m for m in out)


def test_future_read_date_flagged() -> None:
    out = date_ordering_violations("2026-07-01", None, today="2026-06-01")
    assert any("future" in m for m in out)


def test_future_last_revisited_flagged() -> None:
    out = date_ordering_violations("2026-05-01", "2026-07-01", today="2026-06-01")
    assert any("last-revisited" in m and "future" in m for m in out)


def test_accepts_date_objects() -> None:
    # ruamel round-trips an unquoted YAML date into datetime.date; the helper
    # must normalise and still order correctly.
    out = date_ordering_violations(
        date(2026, 5, 20), date(2026, 5, 10), today="2026-06-01"
    )
    assert any("after last-revisited" in m for m in out)


def test_unparseable_values_are_skipped_not_crashed() -> None:
    # A format breach is reported elsewhere (is_iso_date in check_schema); the
    # ordering helper simply skips what it cannot compare rather than raising.
    assert date_ordering_violations("garbage", "also-bad", today="2026-06-01") == []


def test_today_defaults_to_real_today() -> None:
    # Without an explicit `today`, a clearly-past consistent pair is clean.
    assert date_ordering_violations("2020-01-01", "2020-01-02") == []
