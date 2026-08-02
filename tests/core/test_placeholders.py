"""Filler-value detection shared by the ingest guard and the health check."""

from __future__ import annotations

import pytest

from litman.core.placeholders import PLACEHOLDER_VALUES, is_placeholder


@pytest.mark.parametrize(
    "value",
    [
        "Unknown",
        "unknown",
        "UNKNOWN",
        "  Unknown  ",
        "Unknown Author",
        "N/A",
        "n/a",
        "NA",
        "None",
        "null",
        "TBD",
        "untitled",
        "Untitled",
        "et al.",
        "-",
        "?",
        "",
        "   ",
    ],
)
def test_fillers_are_detected(value: str) -> None:
    assert is_placeholder(value)


@pytest.mark.parametrize(
    "value",
    [
        # The escape hatch: a claim about the document, not about the
        # extractor. `lit add`'s rejection message points users here, so it
        # must never join the filler set.
        "Anonymous",
        "anonymous",
        # Institutional authorship — how a patent or standards document is
        # attributed.
        "Bayer AG",
        "World Health Organization",
        # Real values that merely CONTAIN a filler word. Substring matching
        # would reject all of these.
        "Unknown, Robert",
        "Nakamoto, Unknown",
        "The Unknown Structure of Amanitin",
        "Nagy, Anna",
        "Nash, John",
        "Untitled Goose Game: An Analysis",
    ],
)
def test_real_values_pass(value: str) -> None:
    assert not is_placeholder(value)


def test_set_is_lowercase_and_stripped() -> None:
    """Entries must be pre-normalized or the case-folded lookup silently misses."""
    for entry in PLACEHOLDER_VALUES:
        assert entry == entry.strip().casefold(), entry


def test_anonymous_stays_out_of_the_set() -> None:
    """Guards the escape hatch named in `lit add`'s rejection message."""
    assert "anonymous" not in PLACEHOLDER_VALUES
