"""Unit tests for the three M30 drift correctors (core/correctors.py).

Phase 1 extracts ``regen`` / ``resolve`` / ``annotate`` as standalone,
testable functions (not yet wired into the CLI). These tests pin their
contracts:

* regen rebuilds INDEX.json + views from the on-disk metadata truth.
* annotate marks ``[[id]]`` → ``[[id]] (deleted)`` in place, never deleting.
* resolve prompts with the configured default and honors the bounded-stat
  confirmation gate (a non-``False`` stat is not actionable drift; non-TTY
  never mutates).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from litman.core import correctors
from litman.core.checks import Issue
from litman.core.library import create_vault


def _write_paper(vault: Path, paper_id: str, **fields: object) -> Path:
    """Create a minimal paper folder with the given metadata fields."""
    paper_dir = vault / "papers" / paper_id
    paper_dir.mkdir(parents=True)
    lines: list[str] = [f"id: {paper_id}"]
    for key, value in fields.items():
        if isinstance(value, list):
            lines.append(f"{key}:")
            for item in value:
                lines.append(f"  - {item}")
        elif value is None:
            lines.append(f"{key}:")
        else:
            lines.append(f"{key}: {value}")
    (paper_dir / "metadata.yaml").write_text(
        "\n".join(lines) + "\n", encoding="utf-8"
    )
    return paper_dir


@pytest.fixture
def vault(tmp_path: Path) -> Path:
    return create_vault(tmp_path)


# ---------------------------------------------------------------------------
# regen
# ---------------------------------------------------------------------------


def test_regen_rebuilds_index_from_truth(vault: Path) -> None:
    _write_paper(vault, "2024_A_Foo", title="Foo", topics=["amp"])
    _write_paper(vault, "2024_B_Bar", title="Bar")

    counts = correctors.regen(vault)

    index = json.loads((vault / "INDEX.json").read_text(encoding="utf-8"))
    ids = {p["id"] for p in index["papers"]}
    assert ids == {"2024_A_Foo", "2024_B_Bar"}
    assert index["n_papers"] == 2
    assert counts["index"] == 1


def test_regen_drops_dead_index_entry(vault: Path) -> None:
    """A stale INDEX entry for a vanished paper is gone after regen."""
    _write_paper(vault, "2024_A_Foo", title="Foo")
    # Seed INDEX with an entry whose paper dir does not exist on disk.
    stale = {
        "_comment": "x",
        "generated_at": "2024-01-01T00:00:00+00:00",
        "n_papers": 1,
        "papers": [{"id": "2099_Z_Ghost", "title": "Ghost"}],
        "by_doi": {},
    }
    (vault / "INDEX.json").write_text(json.dumps(stale), encoding="utf-8")

    correctors.regen(vault)

    index = json.loads((vault / "INDEX.json").read_text(encoding="utf-8"))
    ids = {p["id"] for p in index["papers"]}
    assert ids == {"2024_A_Foo"}


def test_regen_rebuilds_views(vault: Path) -> None:
    _write_paper(vault, "2024_A_Foo", title="Foo", topics=["amp"])
    counts = correctors.regen(vault)
    link = vault / "views" / "by-topic" / "amp" / "2024_A_Foo"
    assert link.exists()
    assert counts["views"] >= 1


# ---------------------------------------------------------------------------
# annotate
# ---------------------------------------------------------------------------


def test_annotate_marks_deleted_wikilink(vault: Path) -> None:
    paper_dir = vault / "papers" / "2024_A_Foo"
    paper_dir.mkdir(parents=True)
    notes = paper_dir / "notes.md"
    notes.write_text("See [[2099_Z_Ghost]] for context.\n", encoding="utf-8")

    n = correctors.annotate(vault, ["2099_Z_Ghost"])

    assert n == 1
    assert notes.read_text(encoding="utf-8") == (
        "See [[2099_Z_Ghost]] (deleted) for context.\n"
    )


def test_annotate_is_idempotent(vault: Path) -> None:
    paper_dir = vault / "papers" / "2024_A_Foo"
    paper_dir.mkdir(parents=True)
    notes = paper_dir / "notes.md"
    notes.write_text("[[2099_Z_Ghost]] (deleted)\n", encoding="utf-8")

    n = correctors.annotate(vault, ["2099_Z_Ghost"])

    assert n == 0
    assert notes.read_text(encoding="utf-8") == "[[2099_Z_Ghost]] (deleted)\n"


def test_annotate_leaves_unrelated_links_untouched(vault: Path) -> None:
    paper_dir = vault / "papers" / "2024_A_Foo"
    paper_dir.mkdir(parents=True)
    notes = paper_dir / "notes.md"
    notes.write_text("[[2024_Other_Live]]\n", encoding="utf-8")

    n = correctors.annotate(vault, ["2099_Z_Ghost"])

    assert n == 0
    assert notes.read_text(encoding="utf-8") == "[[2024_Other_Live]]\n"


def test_annotate_empty_ids_is_noop(vault: Path) -> None:
    assert correctors.annotate(vault, []) == 0


# ---------------------------------------------------------------------------
# resolve
# ---------------------------------------------------------------------------


def _issue() -> Issue:
    return Issue(
        category="project_path_exists",
        severity="warning",
        paper_id=None,
        message="project 'pep' path does not exist: /gone",
    )


def test_resolve_tty_yes_returns_true() -> None:
    answer = correctors.resolve(
        _issue(),
        default_yes=True,
        stdin_is_tty=lambda: True,
        confirm_fn=lambda *a, **kw: True,
    )
    assert answer is True


def test_resolve_tty_no_returns_false() -> None:
    answer = correctors.resolve(
        _issue(),
        default_yes=True,
        stdin_is_tty=lambda: True,
        confirm_fn=lambda *a, **kw: False,
    )
    assert answer is False


def test_resolve_default_is_passed_through() -> None:
    """The destructive-default policy reaches click.confirm."""
    seen: dict[str, object] = {}

    def _confirm(text: str, default: bool = False) -> bool:
        seen["default"] = default
        return default

    correctors.resolve(
        _issue(),
        default_yes=False,
        stdin_is_tty=lambda: True,
        confirm_fn=_confirm,
    )
    assert seen["default"] is False


def test_resolve_non_tty_never_prompts() -> None:
    called = {"confirm": False}

    def _confirm(*a: object, **kw: object) -> bool:
        called["confirm"] = True
        return True

    answer = correctors.resolve(
        _issue(),
        default_yes=True,
        stdin_is_tty=lambda: False,
        confirm_fn=_confirm,
    )
    assert answer is False
    assert called["confirm"] is False


def test_resolve_unknown_stat_is_not_actionable() -> None:
    """A None (slow/dropped mount) stat must not drive a prompt (ADR-014)."""
    called = {"confirm": False}

    def _confirm(*a: object, **kw: object) -> bool:
        called["confirm"] = True
        return True

    answer = correctors.resolve(
        _issue(),
        default_yes=True,
        status={"/gone": None},
        paths=["/gone"],
        stdin_is_tty=lambda: True,
        confirm_fn=_confirm,
    )
    assert answer is False
    assert called["confirm"] is False


def test_resolve_present_path_is_not_drift() -> None:
    answer = correctors.resolve(
        _issue(),
        default_yes=True,
        status={"/here": True},
        paths=["/here"],
        stdin_is_tty=lambda: True,
        confirm_fn=lambda *a, **kw: True,
    )
    assert answer is False


def test_resolve_confirmed_absence_prompts() -> None:
    answer = correctors.resolve(
        _issue(),
        default_yes=True,
        status={"/gone": False},
        paths=["/gone"],
        stdin_is_tty=lambda: True,
        confirm_fn=lambda *a, **kw: True,
    )
    assert answer is True


def test_resolve_paths_without_status_raises() -> None:
    """Probe ownership is the caller's: ``paths`` requires ``status``.

    ``resolve`` never runs its own bounded-stat (Phase 2 wires a single shared
    0.5s budget upstream). Passing ``paths`` without ``status`` is a caller
    bug, not a silent self-probe.
    """
    with pytest.raises(ValueError, match="requires status"):
        correctors.resolve(
            _issue(),
            default_yes=True,
            paths=["/gone"],
            stdin_is_tty=lambda: True,
            confirm_fn=lambda *a, **kw: True,
        )
