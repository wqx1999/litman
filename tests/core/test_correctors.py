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


def test_regen_index_drop_ids_removes_dead_entry(vault: Path) -> None:
    """The metadata-free Tier-1 helper drops the dead id from INDEX in place."""
    _write_paper(vault, "2024_A_Foo", title="Foo")
    correctors.regen(vault)  # build a real INDEX with one entry
    # Seed a second (dead) entry directly into INDEX without a paper dir.
    index = json.loads((vault / "INDEX.json").read_text(encoding="utf-8"))
    index["papers"].append({"id": "2099_Z_Ghost", "title": "Ghost", "doi": "x"})
    index["by_doi"]["x"] = "2099_Z_Ghost"
    index["n_papers"] = len(index["papers"])
    (vault / "INDEX.json").write_text(json.dumps(index), encoding="utf-8")

    n = correctors.regen_index_drop_ids(vault, ["2099_Z_Ghost"])

    assert n == 1
    after = json.loads((vault / "INDEX.json").read_text(encoding="utf-8"))
    ids = {p["id"] for p in after["papers"]}
    assert ids == {"2024_A_Foo"}
    assert after["n_papers"] == 1
    # by_doi entry for the dropped paper is gone too.
    assert "x" not in after["by_doi"]


def test_regen_index_drop_ids_reads_no_metadata(vault: Path, monkeypatch) -> None:
    """Invariant #15: the Tier-1 INDEX repair never opens a metadata.yaml."""
    _write_paper(vault, "2024_A_Foo", title="Foo")
    correctors.regen(vault)
    index = json.loads((vault / "INDEX.json").read_text(encoding="utf-8"))
    index["papers"].append({"id": "2099_Z_Ghost"})
    (vault / "INDEX.json").write_text(json.dumps(index), encoding="utf-8")

    real_read_text = Path.read_text

    def _guard(self: Path, *a, **kw):  # type: ignore[no-untyped-def]
        if self.name == "metadata.yaml":
            raise AssertionError(
                f"regen_index_drop_ids read per-paper metadata (#15): {self}"
            )
        return real_read_text(self, *a, **kw)

    monkeypatch.setattr(Path, "read_text", _guard)
    n = correctors.regen_index_drop_ids(vault, ["2099_Z_Ghost"])
    assert n == 1


def test_regen_index_drop_ids_noop_when_absent(vault: Path) -> None:
    _write_paper(vault, "2024_A_Foo", title="Foo")
    correctors.regen(vault)
    assert correctors.regen_index_drop_ids(vault, ["2099_Z_Ghost"]) == 0
    assert correctors.regen_index_drop_ids(vault, []) == 0


def test_annotate_targeted_skips_unmentioned_files(vault: Path) -> None:
    """Targeted annotate grep-narrows: a note with no mention is left alone."""
    a = vault / "papers" / "2024_A_Foo"
    a.mkdir(parents=True)
    (a / "notes.md").write_text("See [[2099_Z_Ghost]] here.\n", encoding="utf-8")
    b = vault / "papers" / "2024_B_Bar"
    b.mkdir(parents=True)
    (b / "notes.md").write_text("Nothing relevant.\n", encoding="utf-8")

    n = correctors.annotate(vault, ["2099_Z_Ghost"], targeted=True)

    assert n == 1
    assert (a / "notes.md").read_text(encoding="utf-8") == (
        "See [[2099_Z_Ghost]] (deleted) here.\n"
    )
    assert (b / "notes.md").read_text(encoding="utf-8") == "Nothing relevant.\n"


def test_regen_skips_project_rebuild_on_broken_config(
    vault: Path, monkeypatch
) -> None:
    """A broken lit-config.yaml skips the project-side rebuild (not an error).

    check_project_references already returns [] on a broken config, so there is
    no #3 drift to repair. The narrowed except swallows ONLY the config load;
    regen still rebuilds INDEX + views and reports project_refs == 0.
    """
    _write_paper(vault, "2024_A_Foo", title="Foo")
    (vault / "lit-config.yaml").write_text(
        "library_name: x\nbogus_key: 1\n", encoding="utf-8"
    )

    # If the config error were NOT swallowed, this would raise; if a rebuild
    # were reachable it would be called — guard that it is not.
    import litman.core.project_refs as project_refs

    def _boom(*a, **kw):  # type: ignore[no-untyped-def]
        raise AssertionError("rebuild must not run when config is broken")

    monkeypatch.setattr(project_refs, "rebuild_all_project_refs", _boom)

    counts = correctors.regen(vault)

    assert counts["project_refs"] == 0
    assert counts["index"] == 1
    index = json.loads((vault / "INDEX.json").read_text(encoding="utf-8"))
    assert {p["id"] for p in index["papers"]} == {"2024_A_Foo"}


def test_regen_propagates_project_refs_rebuild_failure(
    vault: Path, monkeypatch
) -> None:
    """A genuine project-refs rebuild failure must propagate, not be swallowed.

    Reviewer fix (M30 Phase 3): the old broad ``except Exception: pass`` hid a
    real filesystem failure (permission error / symlink failure) on a reachable
    project dir, after which health.py:_apply_fixes still printed a false
    "project_references: 1". With the except narrowed to the config load only,
    the rebuild exception escapes regen — so the caller cannot claim success
    for a repair that failed (invariant #14).
    """
    _write_paper(vault, "2024_A_Foo", title="Foo", projects=["pep"])
    proj_dir = vault.parent / "pep_project"
    proj_dir.mkdir()
    (vault / "lit-config.yaml").write_text(
        f"library_name: {vault.name}\nprojects:\n  pep: {proj_dir}\n",
        encoding="utf-8",
    )

    import litman.core.project_refs as project_refs

    def _boom(*a, **kw):  # type: ignore[no-untyped-def]
        raise PermissionError("cannot write REFERENCES.md")

    monkeypatch.setattr(project_refs, "rebuild_all_project_refs", _boom)

    with pytest.raises(PermissionError, match="cannot write REFERENCES.md"):
        correctors.regen(vault)


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
