"""Unit tests for the INDEX projection (``core/views.py``).

Focused on the M31 change: ``read-date`` joins ``INDEX_PAPER_FIELDS`` as a
scalar (absent -> null, NOT []), while ``created-at`` and ``authors`` stay
out of the thin projection (invariant #10 / spec §7).
"""

from __future__ import annotations

import json
from datetime import date, datetime, timezone

from litman.core.views import INDEX_PAPER_FIELDS, project_paper


def test_index_fields_contain_read_date() -> None:
    assert "read-date" in INDEX_PAPER_FIELDS


def test_index_fields_exclude_created_at_and_authors() -> None:
    assert "created-at" not in INDEX_PAPER_FIELDS
    assert "authors" not in INDEX_PAPER_FIELDS


def test_project_paper_passes_through_read_date() -> None:
    paper = {"id": "p1", "read-date": "2026-05-10"}
    out = project_paper(paper)
    assert "read-date" in out
    assert out["read-date"] == "2026-05-10"


def test_project_paper_coerces_date_object_to_iso_string() -> None:
    # The YAML safe-loader yields a datetime.date for read-date; the
    # projection must coerce it to a YYYY-MM-DD string so json.dumps works.
    out = project_paper({"id": "p1", "read-date": date(2026, 5, 1)})
    assert out["read-date"] == "2026-05-01"
    # Round-trips through json without raising (the bug this guards against).
    json.dumps(out)


def test_project_paper_coerces_datetime_to_calendar_date() -> None:
    out = project_paper(
        {"id": "p1", "read-date": datetime(2026, 5, 1, 9, 0, tzinfo=timezone.utc)}
    )
    assert out["read-date"] == "2026-05-01"
    json.dumps(out)


def test_project_paper_absent_read_date_is_none_not_empty_list() -> None:
    # read-date is a scalar field, so an absent value must become null
    # (None), never [] (which is reserved for the list-typed fields).
    out = project_paper({"id": "p1"})
    assert "read-date" in out
    assert out["read-date"] is None
    assert out["read-date"] != []


def test_project_paper_key_set_equals_index_fields() -> None:
    # The projection's key set must stay byte-identical to INDEX_PAPER_FIELDS
    # (the single source both `lit list --format json` and INDEX.json use).
    out = project_paper({"id": "p1", "year": 2024})
    assert set(out.keys()) == set(INDEX_PAPER_FIELDS)


def test_project_paper_read_date_is_last_key() -> None:
    # Spec open-q #1: read-date appended after doi (last position), so the
    # INDEX.json key order stays stable and predictable.
    out = project_paper({"id": "p1"})
    assert list(out.keys())[-1] == "read-date"
    assert INDEX_PAPER_FIELDS[-1] == "read-date"
