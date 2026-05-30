"""Tests for the M28 vault registry drift surfacing hook.

``check_and_prompt_registry_drift`` — TTY vs non-TTY branches, clean
state silence, corrupt-registry silence. The function reads/writes the
user-level registry via ``load_registry`` / ``save_registry``, so every
test runs under a ``fake_home`` autouse fixture that redirects HOME +
clears ``LITMAN_REGISTRY_DIR`` / ``XDG_CONFIG_HOME`` to a tmp dir.
"""

from __future__ import annotations

import time
from pathlib import Path

import click
import pytest

from litman.commands import _drift
from litman.core.library import create_vault
from litman.core.vault_registry import (
    VaultEntry,
    VaultRegistry,
    VaultRegistryError,
    load_registry,
    registry_path,
    save_registry,
)


# ---------------------------------------------------------------------------
# Registry isolation
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def fake_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirect $HOME + clear cross-cutting env vars so every test in this
    module reads/writes a tmp-path-rooted registry instead of the real
    ``~/.config/litman/vaults.yaml``.

    Mirrors the ``fake_home`` fixture in test_init.py / test_vault.py.
    """
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("LITMAN_REGISTRY_DIR", raising=False)
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    return home


# ---------------------------------------------------------------------------
# check_and_prompt_registry_drift — helpers
# ---------------------------------------------------------------------------


def _seed_two_entries_one_dangling(tmp_path: Path) -> tuple[Path, Path]:
    """Persist a registry with one real entry + one dangling entry.

    Returns (real_path, ghost_path).
    """
    real = tmp_path / "real"
    real.mkdir()
    ghost = tmp_path / "ghost"  # intentionally not created
    reg = VaultRegistry(
        vaults=[
            VaultEntry(name="real", path=str(real), is_active=True),
            VaultEntry(name="ghost", path=str(ghost), is_active=False),
        ]
    )
    save_registry(reg)
    return real, ghost


# ---------------------------------------------------------------------------
# TTY branch
# ---------------------------------------------------------------------------


def test_drift_prompt_tty_yes_prunes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed_two_entries_one_dangling(tmp_path)
    monkeypatch.setattr(_drift, "_default_tty_probe", lambda: True)
    monkeypatch.setattr(click, "confirm", lambda *a, **kw: True)

    _drift.check_and_prompt_registry_drift()

    remaining = load_registry().vaults
    assert [v.name for v in remaining] == ["real"]


def test_drift_prompt_tty_no_keeps(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed_two_entries_one_dangling(tmp_path)
    monkeypatch.setattr(_drift, "_default_tty_probe", lambda: True)
    monkeypatch.setattr(click, "confirm", lambda *a, **kw: False)

    _drift.check_and_prompt_registry_drift()

    remaining = load_registry().vaults
    assert sorted(v.name for v in remaining) == ["ghost", "real"]


# ---------------------------------------------------------------------------
# Non-TTY branch
# ---------------------------------------------------------------------------


def test_drift_prompt_non_tty_warns(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _seed_two_entries_one_dangling(tmp_path)
    monkeypatch.setattr(_drift, "_default_tty_probe", lambda: False)

    _drift.check_and_prompt_registry_drift()

    captured = capsys.readouterr()
    # Registry unchanged: non-TTY must never mutate without consent.
    remaining = load_registry().vaults
    assert sorted(v.name for v in remaining) == ["ghost", "real"]
    # One stderr warning carrying the dangling name and the remediation hint.
    # Normalize whitespace because Rich wraps long lines at the auto-detected
    # console width; the literal "lit vault remove" can straddle a wrap point.
    err_flat = " ".join(captured.err.split())
    assert "ghost" in err_flat
    assert "lit vault remove" in err_flat


# ---------------------------------------------------------------------------
# Clean / corrupt states
# ---------------------------------------------------------------------------


def test_drift_prompt_clean_registry_silent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    real = tmp_path / "real"
    real.mkdir()
    save_registry(
        VaultRegistry(
            vaults=[VaultEntry(name="real", path=str(real), is_active=True)]
        )
    )
    # Force TTY so we exercise the path that WOULD print; the early return
    # on "no dangling" must still produce zero output.
    monkeypatch.setattr(_drift, "_default_tty_probe", lambda: True)

    _drift.check_and_prompt_registry_drift()

    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == ""
    remaining = load_registry().vaults
    assert [v.name for v in remaining] == ["real"]


def test_drift_prompt_corrupt_registry_reports_to_stderr(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    # Write a malformed registry: top-level YAML is a string, not a mapping —
    # ``load_registry`` raises ``VaultRegistryError``. M30 Phase 3 (no
    # silent-skip, invariant #14): the drift function now surfaces this to
    # stderr instead of swallowing it (a registry it cannot parse means drift
    # detection is blind, which is itself a finding). It still does NOT mutate
    # the file and does NOT prompt.
    path = registry_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("not a mapping\n", encoding="utf-8")
    original_bytes = path.read_bytes()

    with pytest.raises(VaultRegistryError):
        load_registry()

    # TTY is irrelevant — the function should report before any probe/prompt.
    monkeypatch.setattr(_drift, "_default_tty_probe", lambda: True)

    # Must not raise.
    _drift.check_and_prompt_registry_drift()

    captured = capsys.readouterr()
    assert captured.out == ""
    assert "unreadable" in captured.err
    # File unchanged (no mutation).
    assert path.read_bytes() == original_bytes


# ---------------------------------------------------------------------------
# _exists_bounded (AC1)
# ---------------------------------------------------------------------------


def test_exists_bounded_all_present(tmp_path: Path) -> None:
    a = tmp_path / "a"
    b = tmp_path / "b"
    a.mkdir()
    b.mkdir()
    out = _drift._exists_bounded([str(a), str(b)])
    assert out == {str(a): True, str(b): True}


def test_exists_bounded_definite_false(tmp_path: Path) -> None:
    ghost = tmp_path / "ghost"  # not created
    out = _drift._exists_bounded([str(ghost)])
    assert out == {str(ghost): False}


def test_exists_bounded_timeout_returns_none_within_budget(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """AC1: a path whose Path.exists sleeps longer than the budget maps to
    None, and the call still returns within budget + a small margin."""
    slow = tmp_path / "slow"

    real_exists = Path.exists

    def _slow_exists(self: Path) -> bool:
        if str(self) == str(slow):
            time.sleep(5.0)  # >> budget; daemon thread is abandoned
            return True
        return real_exists(self)

    monkeypatch.setattr(Path, "exists", _slow_exists)

    start = time.monotonic()
    out = _drift._exists_bounded([str(slow)], budget_s=0.2)
    elapsed = time.monotonic() - start

    assert out[str(slow)] is None  # unknown, NOT False
    assert elapsed < 1.0  # returned promptly despite the 5s sleeper


def test_exists_bounded_oserror_maps_to_none(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    bad = tmp_path / "bad"

    def _raise(self: Path) -> bool:
        raise OSError("stale handle")

    monkeypatch.setattr(Path, "exists", _raise)
    out = _drift._exists_bounded([str(bad)], budget_s=0.5)
    assert out[str(bad)] is None


# ---------------------------------------------------------------------------
# registry drift via bounded-stat (AC2)
# ---------------------------------------------------------------------------


def test_registry_drift_none_status_no_prompt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """AC2: when bounded-stat returns None (timeout) for the only entry, the
    registry-drift segment must NOT treat it as dangling — no prompt, no
    mutation."""
    real, ghost = _seed_two_entries_one_dangling(tmp_path)

    # Force both entries to "unknown".
    monkeypatch.setattr(
        _drift,
        "_exists_bounded",
        lambda paths, budget_s=0.5: {p: None for p in paths},
    )
    monkeypatch.setattr(_drift, "_default_tty_probe", lambda: True)

    def _no_confirm(*a: object, **kw: object) -> bool:
        raise AssertionError("must not prompt when status is None")

    monkeypatch.setattr(click, "confirm", _no_confirm)

    _drift.check_and_prompt_registry_drift()

    captured = capsys.readouterr()
    assert captured.out == ""
    # Registry unchanged — None never prunes.
    remaining = load_registry().vaults
    assert sorted(v.name for v in remaining) == ["ghost", "real"]


def test_registry_drift_false_status_prompts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC2: a definite False still triggers the prune prompt (M28 behavior
    preserved through the bounded-stat retrofit)."""
    real, ghost = _seed_two_entries_one_dangling(tmp_path)
    monkeypatch.setattr(
        _drift,
        "_exists_bounded",
        lambda paths, budget_s=0.5: {
            p: (False if p == str(ghost) else True) for p in paths
        },
    )
    monkeypatch.setattr(_drift, "_default_tty_probe", lambda: True)
    monkeypatch.setattr(click, "confirm", lambda *a, **kw: True)

    _drift.check_and_prompt_registry_drift()

    remaining = load_registry().vaults
    assert [v.name for v in remaining] == ["real"]


# ---------------------------------------------------------------------------
# check_and_prompt_project_drift — fixtures / helpers
# ---------------------------------------------------------------------------


class _FakeConfig:
    def __init__(self, projects: dict[str, str]) -> None:
        self.projects = projects


def _seed_active_vault(tmp_path: Path) -> Path:
    """Register a single active vault (its dir exists) and return its path."""
    parent = tmp_path / "vault_parent"
    parent.mkdir()
    vault = create_vault(parent)
    save_registry(
        VaultRegistry(
            vaults=[VaultEntry(name="v", path=str(vault), is_active=True)]
        )
    )
    return vault


def _exists_map(**status: bool | None):
    """Build an exists_fn returning a fixed map keyed by the requested paths.

    ``status`` maps path-string → verdict; any path not listed defaults to
    True (present). Used so unit tests never touch the real FS.
    """
    def _fn(paths: list[str], budget_s: float = 0.5) -> dict[str, bool | None]:
        return {p: status.get(p, True) for p in paths}

    return _fn


# ---------------------------------------------------------------------------
# project drift — unit-level (AC4, AC5, AC6, AC8)
# ---------------------------------------------------------------------------


def test_project_drift_no_active_vault_silent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """AC8: no active vault → return, no raise, no output."""
    real = tmp_path / "real"
    real.mkdir()
    save_registry(
        VaultRegistry(
            vaults=[VaultEntry(name="real", path=str(real), is_active=False)]
        )
    )
    monkeypatch.setattr(_drift, "_default_tty_probe", lambda: True)

    _drift.check_and_prompt_project_drift()

    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == ""


def test_project_drift_unresolvable_config_silent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """AC8: active vault present but config load raises → safe skip."""
    vault = _seed_active_vault(tmp_path)

    def _boom(_v: Path) -> object:
        raise RuntimeError("broken config")

    _drift.check_and_prompt_project_drift(
        stdin_is_tty=lambda: True,
        exists_fn=_exists_map(),  # vault present
        load_config_fn=_boom,
    )

    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == ""
    assert vault.exists()  # untouched


def test_project_drift_clean_silent(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """AC6: all project dirs present → zero output, no mutation."""
    vault = _seed_active_vault(tmp_path)
    proj = tmp_path / "proj"
    proj.mkdir()

    _drift.check_and_prompt_project_drift(
        stdin_is_tty=lambda: True,
        exists_fn=_exists_map(),  # everything present
        load_config_fn=lambda _v: _FakeConfig({"p": str(proj)}),
    )

    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == ""


def test_project_drift_none_status_silent(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """AC5: project dir probe returns None (timeout) → silent skip."""
    vault = _seed_active_vault(tmp_path)
    proj = tmp_path / "proj"  # not created

    _drift.check_and_prompt_project_drift(
        stdin_is_tty=lambda: True,
        exists_fn=_exists_map(**{str(proj): None}),
        load_config_fn=lambda _v: _FakeConfig({"p": str(proj)}),
    )

    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == ""


def test_project_drift_vault_dir_unknown_skips(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """AC8 / dedup: if the active vault's own dir is not definitely present
    (None), the project segment defers to the registry-drift segment and
    skips — never even loads the config."""
    vault = _seed_active_vault(tmp_path)
    loaded: list[bool] = []

    def _track(_v: Path) -> object:
        loaded.append(True)
        return _FakeConfig({})

    _drift.check_and_prompt_project_drift(
        stdin_is_tty=lambda: True,
        exists_fn=_exists_map(**{str(vault): None}),
        load_config_fn=_track,
    )

    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == ""
    assert loaded == []  # config never read when vault dir is unknown


def test_project_drift_non_tty_warns_no_mutation(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """AC4: non-TTY → single stderr warning, zero mutation."""
    vault = _seed_active_vault(tmp_path)
    proj = tmp_path / "gone"  # missing

    config_before = (vault / "lit-config.yaml").read_bytes()

    _drift.check_and_prompt_project_drift(
        stdin_is_tty=lambda: False,
        exists_fn=_exists_map(**{str(proj): False}),
        load_config_fn=lambda _v: _FakeConfig({"pepforge": str(proj)}),
    )

    captured = capsys.readouterr()
    err_flat = " ".join(captured.err.split())
    assert "pepforge" in err_flat
    assert "lit project set-path" in err_flat
    # Config file untouched.
    assert (vault / "lit-config.yaml").read_bytes() == config_before


def test_project_drift_tty_blank_skips_no_mutation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """TTY + blank answer (the default) → skip, no mutation."""
    vault = _seed_active_vault(tmp_path)
    proj = tmp_path / "gone"

    config_before = (vault / "lit-config.yaml").read_bytes()
    monkeypatch.setattr(click, "prompt", lambda *a, **kw: "")

    _drift.check_and_prompt_project_drift(
        stdin_is_tty=lambda: True,
        exists_fn=_exists_map(**{str(proj): False}),
        load_config_fn=lambda _v: _FakeConfig({"pepforge": str(proj)}),
    )

    captured = capsys.readouterr()
    assert "pepforge" in captured.out
    assert (vault / "lit-config.yaml").read_bytes() == config_before


def test_project_drift_multiple_missing_sequential_prompts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Multiple missing projects → one prompt each (all skipped here)."""
    vault = _seed_active_vault(tmp_path)
    g1 = tmp_path / "g1"
    g2 = tmp_path / "g2"

    calls: list[object] = []

    def _prompt(*a: object, **kw: object) -> str:
        calls.append(a)
        return ""  # skip both

    monkeypatch.setattr(click, "prompt", _prompt)

    _drift.check_and_prompt_project_drift(
        stdin_is_tty=lambda: True,
        exists_fn=_exists_map(**{str(g1): False, str(g2): False}),
        load_config_fn=lambda _v: _FakeConfig(
            {"alpha": str(g1), "beta": str(g2)}
        ),
    )

    assert len(calls) == 2  # one prompt per missing project


# ---------------------------------------------------------------------------
# project drift — integration heal (AC3)
# ---------------------------------------------------------------------------


def _write_config_with_project(vault: Path, project: str, path: Path) -> None:
    (vault / "lit-config.yaml").write_text(
        f"library_name: {vault.name}\nprojects:\n  {project}: {path}\n",
        encoding="utf-8",
    )


def _write_config_with_projects(vault: Path, mapping: dict[str, Path]) -> None:
    lines = [f"library_name: {vault.name}", "projects:"]
    lines += [f"  {name}: {path}" for name, path in mapping.items()]
    (vault / "lit-config.yaml").write_text(
        "\n".join(lines) + "\n", encoding="utf-8"
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


def test_project_drift_tty_heal_rebuilds_at_new_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC3: missing project dir + a new path fed via click.prompt → config
    projects path updated AND litman_reflib (symlink + REFERENCES.md) rebuilt
    at the new location."""
    from litman.core.config import load_config
    from litman.core.project_link import rebuild_all_project_links

    # Build a real, active vault.
    parent = tmp_path / "vault_parent"
    parent.mkdir()
    vault = create_vault(parent)
    save_registry(
        VaultRegistry(
            vaults=[VaultEntry(name="v", path=str(vault), is_active=True)]
        )
    )

    # Register a project at its original location + link a paper to it.
    old_dir = tmp_path / "pepforge_old"
    old_dir.mkdir()
    _write_config_with_project(vault, "pepforge", old_dir)
    _make_paper(vault, "p1", projects=["pepforge"])
    rebuild_all_project_links(vault, {"pepforge": str(old_dir)})
    assert (old_dir / "litman_reflib" / "p1").is_symlink()

    # Simulate "user moved the directory": rename it on disk. The config still
    # points at old_dir, which no longer exists.
    new_dir = tmp_path / "pepforge_new"
    old_dir.rename(new_dir)
    assert not old_dir.exists()

    # TTY heal: feed the new path.
    monkeypatch.setattr(_drift, "_default_tty_probe", lambda: True)
    monkeypatch.setattr(click, "prompt", lambda *a, **kw: str(new_dir))

    _drift.check_and_prompt_project_drift()

    # Config now points at the new location.
    assert load_config(vault).projects["pepforge"] == str(new_dir)

    # litman_reflib rebuilt at the NEW location.
    new_link = new_dir / "litman_reflib" / "p1"
    assert new_link.is_symlink()
    assert new_link.resolve() == (vault / "papers" / "p1").resolve()
    refs = new_dir / "litman_reflib" / "REFERENCES.md"
    assert refs.is_file()
    assert "p1" in refs.read_text(encoding="utf-8")


def test_project_drift_tty_heal_multiple_projects_one_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two missing projects, both healed to DIFFERENT new paths in one run.

    Proves (1) the single merged staged_write of lit-config.yaml persisted
    BOTH new paths (load_config reflects both updates), and (2) the single
    rebuild pass recreated litman_reflib (symlink + REFERENCES.md) at BOTH
    new locations.
    """
    from litman.core.config import load_config
    from litman.core.project_link import rebuild_all_project_links

    parent = tmp_path / "vault_parent"
    parent.mkdir()
    vault = create_vault(parent)
    save_registry(
        VaultRegistry(
            vaults=[VaultEntry(name="v", path=str(vault), is_active=True)]
        )
    )

    # Two projects, each with its own linked paper.
    alpha_old = tmp_path / "alpha_old"
    beta_old = tmp_path / "beta_old"
    alpha_old.mkdir()
    beta_old.mkdir()
    _write_config_with_projects(
        vault, {"alpha": alpha_old, "beta": beta_old}
    )
    _make_paper(vault, "pa", projects=["alpha"])
    _make_paper(vault, "pb", projects=["beta"])
    rebuild_all_project_links(
        vault, {"alpha": str(alpha_old), "beta": str(beta_old)}
    )
    assert (alpha_old / "litman_reflib" / "pa").is_symlink()
    assert (beta_old / "litman_reflib" / "pb").is_symlink()

    # Both directories "moved".
    alpha_new = tmp_path / "alpha_new"
    beta_new = tmp_path / "beta_new"
    alpha_old.rename(alpha_new)
    beta_old.rename(beta_new)
    assert not alpha_old.exists()
    assert not beta_old.exists()

    # Feed a distinct new path per project. click.prompt is called once per
    # missing project, in the iteration order of the projects map (alpha,
    # then beta), so a simple side-effect iterator suffices.
    new_paths = iter([str(alpha_new), str(beta_new)])
    monkeypatch.setattr(_drift, "_default_tty_probe", lambda: True)
    monkeypatch.setattr(click, "prompt", lambda *a, **kw: next(new_paths))

    _drift.check_and_prompt_project_drift()

    # BOTH config entries persisted in the single merged staged_write.
    config = load_config(vault)
    assert config.projects["alpha"] == str(alpha_new)
    assert config.projects["beta"] == str(beta_new)

    # BOTH litman_reflib rebuilt at their new locations in the single pass.
    alpha_link = alpha_new / "litman_reflib" / "pa"
    beta_link = beta_new / "litman_reflib" / "pb"
    assert alpha_link.is_symlink()
    assert beta_link.is_symlink()
    assert alpha_link.resolve() == (vault / "papers" / "pa").resolve()
    assert beta_link.resolve() == (vault / "papers" / "pb").resolve()

    alpha_refs = alpha_new / "litman_reflib" / "REFERENCES.md"
    beta_refs = beta_new / "litman_reflib" / "REFERENCES.md"
    assert alpha_refs.is_file()
    assert beta_refs.is_file()
    assert "pa" in alpha_refs.read_text(encoding="utf-8")
    assert "pb" in beta_refs.read_text(encoding="utf-8")


def test_project_drift_tty_heal_preserves_code_symlink(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Invariant #12 dual: heal recreates the litman_code/ code symlink.

    A paper with a real ``code-clones: [<name>]`` entry plus a
    ``<vault>/codes/<name>/repo`` directory on disk is linked to a project.
    After the project dir moves and is healed to a new path, the code half
    of the clone↔link dual (``new_dir/litman_code/<name>``) must survive.
    """
    from litman.core.config import load_config
    from litman.core.project_link import rebuild_all_project_links

    parent = tmp_path / "vault_parent"
    parent.mkdir()
    vault = create_vault(parent)
    save_registry(
        VaultRegistry(
            vaults=[VaultEntry(name="v", path=str(vault), is_active=True)]
        )
    )

    # A real code clone on disk: <vault>/codes/<name>/repo must exist for
    # rebuild_all_project_links to create the litman_code symlink.
    repo_name = "pepforge-net"
    (vault / "codes" / repo_name / "repo").mkdir(parents=True)

    old_dir = tmp_path / "proj_old"
    old_dir.mkdir()
    _write_config_with_project(vault, "pepforge", old_dir)

    # Paper carries the code-clone binding (override the empty default).
    _make_paper(vault, "p1", projects=["pepforge"])
    from litman.core.document import read_metadata
    from ruamel.yaml import YAML

    meta_path = vault / "papers" / "p1" / "metadata.yaml"
    meta = read_metadata(meta_path)
    meta["code-clones"] = [repo_name]
    y = YAML()
    y.indent(mapping=2, sequence=4, offset=2)
    with meta_path.open("w", encoding="utf-8") as f:
        y.dump(meta, f)

    rebuild_all_project_links(vault, {"pepforge": str(old_dir)})
    assert (old_dir / "litman_code" / repo_name).is_symlink()

    # Project dir moves; config still points at old_dir.
    new_dir = tmp_path / "proj_new"
    old_dir.rename(new_dir)
    assert not old_dir.exists()

    monkeypatch.setattr(_drift, "_default_tty_probe", lambda: True)
    monkeypatch.setattr(click, "prompt", lambda *a, **kw: str(new_dir))

    _drift.check_and_prompt_project_drift()

    assert load_config(vault).projects["pepforge"] == str(new_dir)

    # Code half of the clone↔link dual rebuilt at the new location.
    code_link = new_dir / "litman_code" / repo_name
    assert code_link.is_symlink()
    assert code_link.resolve() == (vault / "codes" / repo_name / "repo").resolve()


def test_project_drift_tty_heal_nonexistent_new_path_no_false_rebuild_claim(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Healing to a path that is not a directory here updates the config but
    must NOT claim litman_reflib was rebuilt.

    A new path that does not exist on this machine (typo, or a legitimately
    not-yet-mounted / other-machine location per ADR-014) is "skipped" by
    rebuild_all_*; the config is updated but litman_reflib is not recreated,
    so the success message must reflect "config only", not a false rebuild.
    """
    from litman.core.config import load_config

    parent = tmp_path / "vault_parent"
    parent.mkdir()
    vault = create_vault(parent)
    save_registry(
        VaultRegistry(
            vaults=[VaultEntry(name="v", path=str(vault), is_active=True)]
        )
    )

    old_dir = tmp_path / "proj_old"
    old_dir.mkdir()
    _write_config_with_project(vault, "pepforge", old_dir)
    _make_paper(vault, "p1", projects=["pepforge"])

    # Config still points at old_dir, which is now gone.
    old_dir.rename(tmp_path / "proj_gone")

    # The new path the user types does not exist here.
    new_path = tmp_path / "elsewhere_not_mounted"
    assert not new_path.exists()

    monkeypatch.setattr(_drift, "_default_tty_probe", lambda: True)
    monkeypatch.setattr(click, "prompt", lambda *a, **kw: str(new_path))

    _drift.check_and_prompt_project_drift()

    # Config updated to the new path...
    assert load_config(vault).projects["pepforge"] == str(new_path)
    # ...but no litman_reflib materialized there (the dir doesn't exist).
    assert not (new_path / "litman_reflib").exists()
    # ...and the message did NOT falsely claim a rebuild. "rebuilt" appears
    # only in the success branch; the skip branch says "rebuild" + "refresh".
    out = capsys.readouterr().out
    assert "rebuilt" not in out
    assert "refresh" in out
