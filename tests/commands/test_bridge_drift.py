"""Dangling project-bridge detection + the one-Enter rebuild corrector.

The ``litman_reflib/<id>`` / ``litman_code/<repo>`` symlinks encode the
vault's location at link-creation time, so moving the vault dangles every
bridge while the vault itself stays perfectly healthy — the exact state
``check_project_references`` cannot see (it compares link NAMES against
membership, and the names still match). Covered here:

* ``check_project_bridge_dangling`` — fires on a moved vault, stays silent on
  a healthy one, skips projects whose dir is missing/unknown (ADR-014), and
  ignores non-symlink hub entries.
* ``find_dangling_bridges`` — the shared collector's unknown-target gate.
* ``check_and_prompt_bridge_drift`` — TTY-yes rebuild, TTY-no keep, non-TTY
  warn-only, clean/ no-active silence.

The move-then-detect and move-then-heal tests run the REAL ``_exists_bounded``
probe end-to-end (no injected exists_fn) so the live default path is what is
proven, not a test double.
"""

from __future__ import annotations

from pathlib import Path

import click
import pytest

from litman.commands import _drift
from litman.core.checks import check_project_bridge_dangling
from litman.core.library import create_vault
from litman.core.project_link import (
    find_dangling_bridges,
    rebuild_all_project_links,
)
from litman.core.vault_registry import (
    VaultEntry,
    VaultRegistry,
    save_registry,
)


@pytest.fixture(autouse=True)
def fake_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirect $HOME + clear cross-cutting env vars so the corrector
    reads a tmp-path-rooted registry (mirrors test_drift.py)."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("LITMAN_REGISTRY_DIR", raising=False)
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    return home


def _write_config_with_project(vault: Path, project: str, path: Path) -> None:
    (vault / "lit-config.yaml").write_text(
        f"library_name: {vault.name}\nprojects:\n  {project}: {path}\n",
        encoding="utf-8",
    )


def _make_paper(vault: Path, paper_id: str, *, projects: list[str]) -> None:
    from ruamel.yaml import YAML

    y = YAML()
    y.indent(mapping=2, sequence=4, offset=2)
    paper_dir = vault / "papers" / paper_id
    paper_dir.mkdir(parents=True, exist_ok=True)
    meta = {
        "id": paper_id,
        "title": "Test paper",
        "authors": ["Doe, Jane"],
        "year": 2024,
        "doi": f"10.test/{paper_id}",
        "status": "inbox",
        "priority": "B",
        "type": "research",
        "projects": projects,
        "topics": [],
        "methods": [],
        "code-clones": [],
        "created-at": "2026-05-11T10:00:00+02:00",
        "updated-at": "2026-05-11T10:00:00+02:00",
    }
    with (paper_dir / "metadata.yaml").open("w", encoding="utf-8") as f:
        y.dump(meta, f)


def _linked_vault(tmp_path: Path) -> tuple[Path, Path]:
    """A vault with one paper linked into one project — bridges healthy.

    Returns ``(vault, project_dir)``.
    """
    parent = tmp_path / "vault_parent"
    parent.mkdir()
    vault = create_vault(parent)
    project_dir = tmp_path / "pepforge"
    project_dir.mkdir()
    _write_config_with_project(vault, "pepforge", project_dir)
    _make_paper(vault, "p1", projects=["pepforge"])
    rebuild_all_project_links(vault, {"pepforge": str(project_dir)})
    link = project_dir / "litman_reflib" / "p1"
    assert link.is_symlink() and link.exists()
    return vault, project_dir


def _move_vault(vault: Path, tmp_path: Path) -> Path:
    """Relocate the whole vault — the user's ``mv`` — and return the new path.

    The project's bridges now encode a location that no longer holds a vault.
    """
    moved_parent = tmp_path / "moved_parent"
    moved_parent.mkdir()
    moved = moved_parent / vault.name
    vault.rename(moved)
    return moved


def _activate(vault: Path) -> None:
    """Register ``vault`` as the active entry (post-recovery registry state)."""
    save_registry(
        VaultRegistry(
            vaults=[VaultEntry(name="v", path=str(vault), is_active=True)]
        )
    )


# ---------------------------------------------------------------------------
# check_project_bridge_dangling
# ---------------------------------------------------------------------------


def test_check_clean_vault_silent(tmp_path: Path) -> None:
    """Healthy bridges → no issues (real bounded probe)."""
    vault, _ = _linked_vault(tmp_path)
    assert check_project_bridge_dangling(vault, []) == []


def test_check_flags_dangling_after_vault_move(tmp_path: Path) -> None:
    """The scenario nothing else sees: vault moved, link names still match
    membership, every bridge dangles. Runs the REAL ``_exists_bounded``."""
    vault, _ = _linked_vault(tmp_path)
    moved = _move_vault(vault, tmp_path)

    issues = check_project_bridge_dangling(moved, [])
    assert len(issues) == 1
    issue = issues[0]
    assert issue.category == "project_bridge_dangling"
    assert issue.severity == "error"
    assert "pepforge" in issue.message
    assert "points at nothing" in issue.message  # n=1 → singular verb
    assert "health-check --fix" in issue.hint


def test_check_unknown_project_dir_never_flagged(tmp_path: Path) -> None:
    """ADR-014 via the threaded ``exists_status``: an unknown (None) project
    dir is skipped even though its bridges genuinely dangle — a slow mount
    must never look like a moved vault."""
    vault, project_dir = _linked_vault(tmp_path)
    moved = _move_vault(vault, tmp_path)

    issues = check_project_bridge_dangling(
        moved, [], exists_status={str(project_dir): None}
    )
    assert issues == []


def test_check_missing_project_dir_owned_elsewhere(tmp_path: Path) -> None:
    """A definitely-absent project dir is ``project_path_exists``'s finding,
    not a bridge problem — the check must not double-report."""
    vault, project_dir = _linked_vault(tmp_path)
    issues = check_project_bridge_dangling(
        vault, [], exists_status={str(project_dir): False}
    )
    assert issues == []


def test_check_ignores_non_symlink_hub_entries(tmp_path: Path) -> None:
    """REFERENCES.md (content, not a link), stray files and subdirectories in
    the hubs are never probed as bridges."""
    vault, project_dir = _linked_vault(tmp_path)
    assert (project_dir / "litman_reflib" / "REFERENCES.md").is_file()
    (project_dir / "litman_reflib" / "stray.txt").write_text(
        "not a link", encoding="utf-8"
    )
    (project_dir / "litman_code").mkdir(exist_ok=True)
    (project_dir / "litman_code" / "subdir").mkdir()

    assert check_project_bridge_dangling(vault, []) == []


def test_check_no_projects_silent(tmp_path: Path) -> None:
    """A vault with no configured projects has no bridges to probe."""
    parent = tmp_path / "vault_parent"
    parent.mkdir()
    vault = create_vault(parent)
    assert check_project_bridge_dangling(vault, []) == []


def test_find_dangling_unknown_target_never_flagged(tmp_path: Path) -> None:
    """The collector's own ADR-014 gate: a target probe that comes back
    ``None`` (budget expired / OSError) is not dangling."""
    _, project_dir = _linked_vault(tmp_path)

    out = find_dangling_bridges(
        {"pepforge": str(project_dir)},
        {str(project_dir): True},
        lambda paths: dict.fromkeys(paths),
    )
    assert out == {}


def test_check_flags_dangling_code_bridge(tmp_path: Path) -> None:
    """The litman_code/ arm fires too — a dangling repo bridge, healthy
    litman_reflib beside it."""
    vault, project_dir = _linked_vault(tmp_path)
    code_hub = project_dir / "litman_code"
    code_hub.mkdir(exist_ok=True)  # rebuild_all already made the hub
    ghost = code_hub / "ghostrepo"
    ghost.symlink_to("../nowhere/repo")
    assert ghost.is_symlink() and not ghost.exists()

    issues = check_project_bridge_dangling(vault, [])
    assert len(issues) == 1
    assert issues[0].category == "project_bridge_dangling"
    assert "ghostrepo" in issues[0].message  # the example names the code link


def test_find_dangling_unreadable_hub_skips_that_project(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A hub that refuses listing (chmod'd dir, dying mount) skips THAT
    project and keeps scanning the rest — an OSError escaping the collector
    on the Tier-1 hot path would silently disable the WHOLE drift hook
    (registry/project prompts included), since the hook's outer wrapper
    swallows everything."""
    proj_a = tmp_path / "proja"
    (proj_a / "litman_reflib").mkdir(parents=True)
    (proj_a / "litman_reflib" / "x").symlink_to("../nowhere/x")
    proj_b = tmp_path / "projb"
    (proj_b / "litman_reflib").mkdir(parents=True)
    (proj_b / "litman_reflib" / "y").symlink_to("../nowhere/y")

    real_iterdir = Path.iterdir

    def _guarded(self: Path):  # type: ignore[no-untyped-def]
        if self == proj_a / "litman_reflib":
            raise PermissionError(13, "Permission denied", str(self))
        return real_iterdir(self)

    monkeypatch.setattr(Path, "iterdir", _guarded)

    out = find_dangling_bridges(
        {"proja": str(proj_a), "projb": str(proj_b)},
        {str(proj_a): True, str(proj_b): True},
        _drift._exists_bounded,
    )
    assert "proja" not in out  # skipped, not raised
    assert [p.name for p in out["projb"]] == ["y"]  # the rest still scanned


# ---------------------------------------------------------------------------
# check_and_prompt_bridge_drift
# ---------------------------------------------------------------------------


def test_bridge_drift_tty_yes_rebuilds(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The recovery seam end-to-end, real probes: vault moved, registry
    re-pointed (the user re-registered it), [Y] → every bridge re-points at
    the vault's NEW location."""
    vault, project_dir = _linked_vault(tmp_path)
    moved = _move_vault(vault, tmp_path)
    _activate(moved)

    monkeypatch.setattr(_drift, "_default_tty_probe", lambda: True)
    monkeypatch.setattr(click, "confirm", lambda *a, **kw: True)

    _drift.check_and_prompt_bridge_drift()

    link = project_dir / "litman_reflib" / "p1"
    assert link.is_symlink()
    assert link.resolve() == (moved / "papers" / "p1").resolve()
    out = capsys.readouterr().out
    assert "points at nothing" in out  # n=1 → singular verb
    assert "Rebuilt project links" in out


def test_bridge_drift_tty_no_keeps(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """[n] keeps the drift and says it will ask again — no mutation."""
    vault, project_dir = _linked_vault(tmp_path)
    moved = _move_vault(vault, tmp_path)
    _activate(moved)

    monkeypatch.setattr(_drift, "_default_tty_probe", lambda: True)
    monkeypatch.setattr(click, "confirm", lambda *a, **kw: False)

    _drift.check_and_prompt_bridge_drift()

    link = project_dir / "litman_reflib" / "p1"
    assert link.is_symlink() and not link.exists()  # still dangling
    assert "Kept for now" in capsys.readouterr().out


def test_bridge_drift_non_tty_warns_no_mutation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Automation branch: one stderr warning naming the project + the fix
    commands, zero mutation."""
    vault, project_dir = _linked_vault(tmp_path)
    moved = _move_vault(vault, tmp_path)
    _activate(moved)

    monkeypatch.setattr(_drift, "_default_tty_probe", lambda: False)

    _drift.check_and_prompt_bridge_drift()

    link = project_dir / "litman_reflib" / "p1"
    assert link.is_symlink() and not link.exists()
    err = capsys.readouterr().err
    assert "pepforge" in err
    assert "health-check --fix" in err


def test_bridge_drift_clean_silent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Healthy bridges: no prompt, no output — the common path adds zero
    noise."""
    vault, _ = _linked_vault(tmp_path)
    _activate(vault)

    monkeypatch.setattr(_drift, "_default_tty_probe", lambda: True)

    def _never(*a: object, **kw: object) -> bool:
        raise AssertionError("confirm must not be called on a clean vault")

    monkeypatch.setattr(click, "confirm", _never)

    _drift.check_and_prompt_bridge_drift()

    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == ""


def test_bridge_drift_no_active_vault_silent(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Empty registry → silent return (the registry-drift segment owns the
    missing-vault story)."""
    save_registry(VaultRegistry(vaults=[]))
    _drift.check_and_prompt_bridge_drift()
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == ""
