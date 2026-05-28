"""Tests for `litman.core.checks` schema validation.

Focused on M29's split of fixed-enum fields into required (``status``) and
optional (``priority`` / ``type``): None is a legitimate "not yet evaluated"
state for the optional ones, but still an error for ``status``; non-None
values must still be in the allowed whitelist for all three.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from litman.core.checks import check_schema
from litman.core.library import create_vault


@pytest.fixture
def vault(tmp_path: Path) -> Path:
    return create_vault(tmp_path)


def _minimal_paper(**overrides: object) -> dict[str, object]:
    """Return a schema-valid paper dict; pass overrides to mutate fields."""
    base: dict[str, object] = {
        "id": "2024_Test_Paper",
        "created-at": "2024-01-01T00:00:00+00:00",
        "updated-at": "2024-01-01T00:00:00+00:00",
        "type": "research",
        "status": "inbox",
        "priority": "B",
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# M29: None is OK for the optional fixed enums
# ---------------------------------------------------------------------------


def test_schema_priority_none_ok(vault: Path) -> None:
    paper = _minimal_paper(priority=None)
    assert check_schema(vault, [paper]) == []


def test_schema_type_none_ok(vault: Path) -> None:
    paper = _minimal_paper(type=None)
    assert check_schema(vault, [paper]) == []


# ---------------------------------------------------------------------------
# M29: status remains required; non-None values still whitelist-checked
# ---------------------------------------------------------------------------


def test_schema_status_none_still_errors(vault: Path) -> None:
    paper = _minimal_paper(status=None)
    issues = check_schema(vault, [paper])
    assert len(issues) == 1
    issue = issues[0]
    assert issue.severity == "error"
    assert issue.category == "schema"
    assert "status" in issue.message
    assert "missing" in issue.message


def test_schema_priority_invalid_value_still_errors(vault: Path) -> None:
    paper = _minimal_paper(priority="X")
    issues = check_schema(vault, [paper])
    assert len(issues) == 1
    issue = issues[0]
    assert issue.severity == "error"
    assert issue.category == "schema"
    assert "'X'" in issue.message
    assert "['A', 'B', 'C']" in issue.message


def test_schema_type_invalid_value_still_errors(vault: Path) -> None:
    paper = _minimal_paper(type="position-paper")
    issues = check_schema(vault, [paper])
    assert len(issues) == 1
    issue = issues[0]
    assert issue.severity == "error"
    assert issue.category == "schema"
    assert "'position-paper'" in issue.message
