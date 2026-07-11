"""Integration tests for the M28 drift hook on the root CLI group.

These tests exercise ``LitGroup.invoke`` end-to-end (via ``CliRunner``) so
the skip list, prompt path, and CliRunner's non-TTY default are all wired
correctly. The unit-level behavior of ``check_and_prompt_registry_drift``
itself is covered in ``test_drift.py``.
"""

from __future__ import annotations

from pathlib import Path

import click
import pytest
from click.testing import CliRunner

from litman.cli import cli
from litman.commands import _drift
from litman.core.library import create_vault
from litman.core.vault_registry import (
    VaultEntry,
    VaultRegistry,
    load_registry,
    save_registry,
)


@pytest.fixture(autouse=True)
def fake_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirect $HOME + clear cross-cutting env vars so the drift hook
    reads/writes a tmp registry rather than the real one."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("LITMAN_REGISTRY_DIR", raising=False)
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    return home


def _seed_dangling_plus_active(tmp_path: Path) -> Path:
    """Persist a registry with one real (active) vault + one dangling entry.

    Returns the path of the real vault.
    """
    real_parent = tmp_path / "real_parent"
    real_parent.mkdir()
    real_vault = create_vault(real_parent)  # active vault
    ghost = tmp_path / "ghost"  # never created on disk

    reg = VaultRegistry(
        vaults=[
            VaultEntry(name="real", path=str(real_vault), is_active=True),
            VaultEntry(name="ghost", path=str(ghost), is_active=False),
        ]
    )
    save_registry(reg)
    return real_vault


def _add_missing_project(vault: Path, tmp_path: Path) -> None:
    """Configure the active vault with one project whose dir does not exist.

    Makes the unified ``project_path_exists`` cheap check fire so the hook's
    project-drift corrector is dispatched (M30 Phase 2: correctors run only
    when their category is detected).
    """
    missing = tmp_path / "gone_project"  # never created
    (vault / "lit-config.yaml").write_text(
        f"library_name: {vault.name}\nprojects:\n  ghostproj: {missing}\n",
        encoding="utf-8",
    )


def test_lit_list_triggers_drift_prompt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Running ``lit list`` with a TTY-forced drift hook prunes the dangling
    entry before list executes — proves the root-group hook is wired into
    every non-skipped subcommand."""
    _seed_dangling_plus_active(tmp_path)

    # CliRunner is non-TTY by default. Force the drift probe to True so we
    # exercise the prompt branch, then auto-answer Y.
    monkeypatch.setattr(_drift, "_default_tty_probe", lambda: True)
    monkeypatch.setattr(click, "confirm", lambda *a, **kw: True)

    runner = CliRunner()
    result = runner.invoke(cli, ["list"])

    # ``lit list`` itself runs cleanly on the real (active) vault.
    assert result.exit_code == 0, result.output
    # And the dangling entry has been pruned by the hook.
    remaining = load_registry().vaults
    assert [v.name for v in remaining] == ["real"]
    # Positive output assertion: the TTY-yes branch's "Removed N dangling"
    # rendering must have reached the user. Without this we would only know
    # the registry mutated, not that the user saw why.
    assert "dangling" in result.output.lower()
    assert "Removed" in result.output


def test_lit_help_skips_drift_prompt(tmp_path: Path) -> None:
    """``lit help`` is in the skip list — running it must not touch the
    registry even when a dangling entry exists. CliRunner is non-TTY by
    default; if the hook fired we'd see the stderr warning, but we'd never
    see a prompt regardless. The contract is "skip means no-op": the
    registry stays exactly as-is."""
    _seed_dangling_plus_active(tmp_path)

    runner = CliRunner()
    result = runner.invoke(cli, ["help"])

    assert result.exit_code == 0, result.output
    remaining = load_registry().vaults
    assert sorted(v.name for v in remaining) == ["ghost", "real"]
    # Skip-list AC: drift output must NOT appear at all (not even the
    # non-TTY stderr warning). ``result.output`` is the mixed stdout+stderr
    # stream in Click 8.2+, so this catches a silently broken _DRIFT_SKIP
    # that lets the hook fire and emit the non-TTY warning.
    assert "lit vault remove" not in result.output
    assert "dangling" not in result.output.lower()


def test_hook_calls_registry_then_project_drift(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A non-skipped subcommand fires BOTH drift correctors, registry first
    then project, in that order (registry-drift owns the missing-vault case,
    so project-drift must run after it).

    M30 Phase 2: the hook runs the cheap detection subset, then dispatches a
    corrector only for a category that fired. We seed a dangling registry entry
    (fires ``vault_registry_drift``) AND a missing project dir (fires
    ``project_path_exists``) so both correctors are dispatched.
    """
    real_vault = _seed_dangling_plus_active(tmp_path)
    _add_missing_project(real_vault, tmp_path)

    order: list[str] = []
    monkeypatch.setattr(
        _drift,
        "check_and_prompt_registry_drift",
        lambda *a, **kw: order.append("registry"),
    )
    monkeypatch.setattr(
        _drift,
        "check_and_prompt_project_drift",
        lambda *a, **kw: order.append("project"),
    )

    runner = CliRunner()
    result = runner.invoke(cli, ["list"])

    assert result.exit_code == 0, result.output
    assert order == ["registry", "project"]


def test_hook_dispatches_only_fired_correctors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The hook gates each corrector on its unified check firing (Phase 2).

    With a dangling registry entry but NO project drift, only the registry
    corrector is dispatched; the project corrector is not called."""
    _seed_dangling_plus_active(tmp_path)  # registry drift only, no project map

    fired: list[str] = []
    monkeypatch.setattr(
        _drift,
        "check_and_prompt_registry_drift",
        lambda *a, **kw: fired.append("registry"),
    )
    monkeypatch.setattr(
        _drift,
        "check_and_prompt_project_drift",
        lambda *a, **kw: fired.append("project"),
    )

    runner = CliRunner()
    result = runner.invoke(cli, ["list"])

    assert result.exit_code == 0, result.output
    assert fired == ["registry"]


def test_hook_project_drift_exception_does_not_crash_command(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A raise inside project-drift heal must not crash the user's command —
    the hook wraps it defensively and the actual subcommand still runs."""
    real_vault = _seed_dangling_plus_active(tmp_path)
    _add_missing_project(real_vault, tmp_path)  # makes project_path_exists fire

    monkeypatch.setattr(
        _drift, "check_and_prompt_registry_drift", lambda *a, **kw: None
    )

    def _boom(*a: object, **kw: object) -> None:
        raise RuntimeError("heal blew up")

    monkeypatch.setattr(_drift, "check_and_prompt_project_drift", _boom)

    runner = CliRunner()
    result = runner.invoke(cli, ["list"])

    assert result.exit_code == 0, result.output


def test_hook_help_skips_both_drift_segments(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC7: ``lit help`` skips BOTH drift segments (neither fires)."""
    _seed_dangling_plus_active(tmp_path)

    fired: list[str] = []
    monkeypatch.setattr(
        _drift,
        "check_and_prompt_registry_drift",
        lambda *a, **kw: fired.append("registry"),
    )
    monkeypatch.setattr(
        _drift,
        "check_and_prompt_project_drift",
        lambda *a, **kw: fired.append("project"),
    )

    runner = CliRunner()
    for argv in (["help"], ["hello"], []):
        fired.clear()
        result = runner.invoke(cli, argv)
        assert result.exit_code in (0, 2), result.output
        assert fired == [], f"{argv!r} should skip the drift hook"


def test_lit_no_args_skips_drift_prompt(tmp_path: Path) -> None:
    """``lit`` with no subcommand has ``invoked_subcommand is None`` — also
    in the skip list (the user is about to see the help message; don't
    ambush them with a registry prompt)."""
    _seed_dangling_plus_active(tmp_path)

    runner = CliRunner()
    result = runner.invoke(cli, [])

    # Click renders help and exits 0 (or 2 depending on the version) — either
    # way the contract is "registry unchanged".
    assert result.exit_code in (0, 2), result.output
    remaining = load_registry().vaults
    assert sorted(v.name for v in remaining) == ["ghost", "real"]
    # Skip-list AC: drift output must NOT appear at all (not even the
    # non-TTY stderr warning). ``result.output`` is the mixed stdout+stderr
    # stream in Click 8.2+, so this catches a silently broken _DRIFT_SKIP
    # that lets the hook fire and emit the non-TTY warning.
    assert "lit vault remove" not in result.output
    assert "dangling" not in result.output.lower()


def test_hook_non_tty_reports_registry_drift_without_mutating(
    tmp_path: Path,
) -> None:
    """Non-TTY (CliRunner default): the hook surfaces registry drift as a
    stderr warning and does NOT prune (spec §6: agent / non-TTY = report-only,
    no auto-mutate). Preserves the M28 behavior through the Phase-2 rewire."""
    _seed_dangling_plus_active(tmp_path)

    runner = CliRunner()
    result = runner.invoke(cli, ["list"])

    assert result.exit_code == 0, result.output
    # The registry is untouched — non-TTY never mutates without consent.
    remaining = load_registry().vaults
    assert sorted(v.name for v in remaining) == ["ghost", "real"]
    # ...but the drift IS surfaced (one warning naming the entry + the fix).
    assert "ghost" in result.output
    assert "lit vault remove" in result.output


def test_hook_project_drift_non_tty_no_mutation(
    tmp_path: Path,
) -> None:
    """Non-TTY project drift via the hook: warn, never rewrite lit-config.yaml.

    Preserves the non-destructive, no-auto-mutate default for project-path
    drift through the unified-detection rewire (spec §6)."""
    real_vault = _seed_dangling_plus_active(tmp_path)
    _add_missing_project(real_vault, tmp_path)
    config_before = (real_vault / "lit-config.yaml").read_bytes()

    runner = CliRunner()
    result = runner.invoke(cli, ["list"])

    assert result.exit_code == 0, result.output
    # Config untouched (no heal in non-TTY) ...
    assert (real_vault / "lit-config.yaml").read_bytes() == config_before
    # ... but the project drift is surfaced.
    assert "ghostproj" in result.output
    assert "lit project set-path" in result.output


# ---------------------------------------------------------------------------
# M30 Phase 3: Tier-1 INDEX↔papers vanished-id repair (spec §6)
# ---------------------------------------------------------------------------


def _seed_active_vault_with_papers(tmp_path: Path, n: int) -> Path:
    """Register one active vault, seed ``n`` papers, and build INDEX + views.

    Returns the active vault path. No dangling registry entry / project drift,
    so the only cheap check that can fire is ``index_vs_disk`` once a paper dir
    is removed out of band.
    """
    from litman.core.correctors import regen

    parent = tmp_path / "real_parent"
    parent.mkdir()
    vault = create_vault(parent)
    save_registry(
        VaultRegistry(
            vaults=[VaultEntry(name="real", path=str(vault), is_active=True)]
        )
    )
    for i in range(n):
        pid = f"2024_P{i}_Foo"
        pdir = vault / "papers" / pid
        pdir.mkdir(parents=True)
        (pdir / "metadata.yaml").write_text(
            f"id: {pid}\ntitle: Foo {i}\nstatus: inbox\n", encoding="utf-8"
        )
        (pdir / "paper.pdf").write_bytes(b"%PDF stub\n")
        (pdir / "notes.md").write_text("", encoding="utf-8")
    regen(vault)
    return vault


def test_hook_vanished_id_tty_regens_index_and_annotates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Manual rm of one paper → next command (TTY) drops it from INDEX (metadata-
    free) and annotates ``[[id]] (deleted)`` in notes (targeted)."""
    import json
    import shutil

    vault = _seed_active_vault_with_papers(tmp_path, 2)
    # A note in the surviving paper references the soon-to-be-deleted one.
    (vault / "papers" / "2024_P0_Foo" / "notes.md").write_text(
        "see [[2024_P1_Foo]]\n", encoding="utf-8"
    )
    shutil.rmtree(vault / "papers" / "2024_P1_Foo")

    monkeypatch.setattr(_drift, "_default_tty_probe", lambda: True)

    runner = CliRunner()
    result = runner.invoke(cli, ["list"])
    assert result.exit_code == 0, result.output

    # INDEX no longer lists the vanished paper.
    index = json.loads((vault / "INDEX.json").read_text(encoding="utf-8"))
    ids = {p["id"] for p in index["papers"]}
    assert ids == {"2024_P0_Foo"}
    # The surviving note's wikilink is annotated (deleted).
    note = (vault / "papers" / "2024_P0_Foo" / "notes.md").read_text(
        encoding="utf-8"
    )
    assert note == "see [[2024_P1_Foo]] (deleted)\n"


def test_hook_vanished_id_reads_no_per_paper_metadata(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Invariant #15 guard: the Tier-1 hook's INDEX repair opens no metadata.yaml.

    Reviewer suggestion 2: assert the dead id was actually dropped from
    INDEX.json after the hook ran. Without this post-condition the test would
    pass even if the repair did nothing (no metadata read while doing nothing),
    or if the hook's outer ``except Exception: pass`` swallowed a failure — so
    we verify the metadata-free repair path both ran AND completed.
    """
    import json
    import shutil

    vault = _seed_active_vault_with_papers(tmp_path, 2)
    shutil.rmtree(vault / "papers" / "2024_P1_Foo")
    # Pre-condition: the dead id is still in INDEX before the hook runs.
    before = json.loads((vault / "INDEX.json").read_text(encoding="utf-8"))
    assert "2024_P1_Foo" in {p["id"] for p in before["papers"]}

    monkeypatch.setattr(_drift, "_default_tty_probe", lambda: True)

    real_read_text = Path.read_text

    def _guard(self: Path, *a, **kw):  # type: ignore[no-untyped-def]
        if self.name == "metadata.yaml":
            raise AssertionError(
                f"Tier-1 hook read per-paper metadata (invariant #15): {self}"
            )
        return real_read_text(self, *a, **kw)

    monkeypatch.setattr(Path, "read_text", _guard)

    runner = CliRunner()
    # `lit list` itself reads metadata; restrict the guard to the hook by
    # invoking a command whose own logic does NOT read metadata. `vault list`
    # is registry-only. The hook still runs the cheap checks + repair.
    result = runner.invoke(cli, ["vault", "list"])
    assert result.exit_code == 0, result.output

    # Post-condition: the metadata-free repair actually completed — the dead id
    # is gone from INDEX (not merely "no metadata read while doing nothing").
    after = json.loads((vault / "INDEX.json").read_text(encoding="utf-8"))
    assert {p["id"] for p in after["papers"]} == {"2024_P0_Foo"}


def test_hook_vanished_id_bulk_defers_to_health_check(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """> 5 vanished ids → single 'run health-check' line + INDEX regen, NO per-id
    annotate."""
    import json
    import shutil

    vault = _seed_active_vault_with_papers(tmp_path, 7)
    # A note referencing one of the bulk-deleted papers.
    (vault / "papers" / "2024_P0_Foo" / "notes.md").write_text(
        "see [[2024_P6_Foo]]\n", encoding="utf-8"
    )
    # Remove 6 papers (> 5) out of band.
    for i in range(1, 7):
        shutil.rmtree(vault / "papers" / f"2024_P{i}_Foo")

    monkeypatch.setattr(_drift, "_default_tty_probe", lambda: True)

    runner = CliRunner()
    result = runner.invoke(cli, ["list"])
    assert result.exit_code == 0, result.output

    # INDEX regenerated once (all 6 dead ids dropped).
    index = json.loads((vault / "INDEX.json").read_text(encoding="utf-8"))
    ids = {p["id"] for p in index["papers"]}
    assert ids == {"2024_P0_Foo"}
    # Single deferral line, and NO per-id wikilink annotation happened.
    assert "run `lit health-check`" in result.output
    note = (vault / "papers" / "2024_P0_Foo" / "notes.md").read_text(
        encoding="utf-8"
    )
    assert note == "see [[2024_P6_Foo]]\n"  # NOT annotated


def test_hook_vanished_id_exactly_5_uses_per_id_annotate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Boundary (suggestion 1): exactly 5 vanished → per-id targeted annotate.

    Pins ``len(vanished) > 5`` against an accidental ``>= 5`` regression: 5 is
    NOT bulk, so the surviving note's wikilink IS annotated and the bulk
    deferral line does NOT appear.
    """
    import shutil

    vault = _seed_active_vault_with_papers(tmp_path, 6)
    (vault / "papers" / "2024_P0_Foo" / "notes.md").write_text(
        "see [[2024_P5_Foo]]\n", encoding="utf-8"
    )
    # Remove exactly 5 papers (== threshold, NOT > 5).
    for i in range(1, 6):
        shutil.rmtree(vault / "papers" / f"2024_P{i}_Foo")

    monkeypatch.setattr(_drift, "_default_tty_probe", lambda: True)

    runner = CliRunner()
    result = runner.invoke(cli, ["list"])
    assert result.exit_code == 0, result.output

    # Per-id targeted annotate ran (5 is the per-id path, not bulk).
    note = (vault / "papers" / "2024_P0_Foo" / "notes.md").read_text(
        encoding="utf-8"
    )
    assert note == "see [[2024_P5_Foo]] (deleted)\n"
    # And the bulk deferral line is absent.
    assert "run `lit health-check`" not in result.output


def test_hook_vanished_id_exactly_6_uses_bulk_defer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Boundary (suggestion 1): exactly 6 vanished → bulk path, no per-id annotate.

    6 is the first value satisfying ``len(vanished) > 5``: a single
    'run health-check' line and NO per-id wikilink annotation.
    """
    import shutil

    vault = _seed_active_vault_with_papers(tmp_path, 7)
    (vault / "papers" / "2024_P0_Foo" / "notes.md").write_text(
        "see [[2024_P6_Foo]]\n", encoding="utf-8"
    )
    # Remove exactly 6 papers (> 5 → bulk).
    for i in range(1, 7):
        shutil.rmtree(vault / "papers" / f"2024_P{i}_Foo")

    monkeypatch.setattr(_drift, "_default_tty_probe", lambda: True)

    runner = CliRunner()
    result = runner.invoke(cli, ["list"])
    assert result.exit_code == 0, result.output

    # Single deferral line, NO per-id annotation.
    assert "run `lit health-check`" in result.output
    note = (vault / "papers" / "2024_P0_Foo" / "notes.md").read_text(
        encoding="utf-8"
    )
    assert note == "see [[2024_P6_Foo]]\n"  # NOT annotated


def test_hook_vanished_id_non_tty_report_only(
    tmp_path: Path,
) -> None:
    """Non-TTY (agent): vanished-id drift is reported, INDEX is NOT mutated."""
    import json
    import shutil

    vault = _seed_active_vault_with_papers(tmp_path, 2)
    shutil.rmtree(vault / "papers" / "2024_P1_Foo")
    index_before = (vault / "INDEX.json").read_bytes()

    runner = CliRunner()  # non-TTY by default
    result = runner.invoke(cli, ["list"])
    assert result.exit_code == 0, result.output

    # No mutation in automation ...
    assert (vault / "INDEX.json").read_bytes() == index_before
    # ... but the drift is surfaced with the explicit fix command. Rich may
    # wrap the line, so normalize whitespace before matching the command.
    assert "2024_P1_Foo" in result.output
    assert "health-check --fix" in " ".join(result.output.split())


def test_hook_unindexed_dir_does_not_mutate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An un-indexed dir (warning, no vanished id) triggers no INDEX rewrite."""
    import json

    vault = _seed_active_vault_with_papers(tmp_path, 1)
    # Add a paper dir on disk WITHOUT rebuilding INDEX → un-indexed warning only.
    extra = vault / "papers" / "2025_New_Paper"
    extra.mkdir(parents=True)
    (extra / "metadata.yaml").write_text(
        "id: 2025_New_Paper\ntitle: New\n", encoding="utf-8"
    )
    (extra / "paper.pdf").write_bytes(b"%PDF stub\n")
    index_before = (vault / "INDEX.json").read_bytes()

    monkeypatch.setattr(_drift, "_default_tty_probe", lambda: True)

    runner = CliRunner()
    result = runner.invoke(cli, ["list"])
    assert result.exit_code == 0, result.output
    # No vanished id → the hook must not rewrite INDEX (the un-indexed dir needs
    # metadata → Tier-2).
    assert (vault / "INDEX.json").read_bytes() == index_before


def test_hook_corrupt_registry_surfaces_finding(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A corrupt registry surfaces a finding via the hook (no silent-skip, #14).

    The unified ``check_vault_registry_drift`` emits a ``vault_registry_drift``
    error for an unreadable registry, which dispatches
    ``check_and_prompt_registry_drift`` — now printing a stderr line instead of
    swallowing the parse error (Phase-2 deferral resolved)."""
    from litman.core.vault_registry import registry_path

    path = registry_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("not a mapping\n", encoding="utf-8")

    runner = CliRunner()
    result = runner.invoke(cli, ["list"])
    # The command still runs (no vault resolves, list degrades), and the hook
    # surfaced the unreadable registry.
    assert "unreadable" in " ".join(result.output.split())


def test_hook_corrupt_config_surfaces_finding(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A present-but-unparseable lit-config.yaml surfaces via the hook
    (review F6/F27, no silent-skip #14).

    The cheap-tier ``check_config_readable`` emits a ``config_unreadable``
    error, which the hook prints to stderr — consistent with the corrupt-
    registry line — instead of the config-keyed checks each reporting clean.
    """
    real_parent = tmp_path / "real_parent"
    real_parent.mkdir()
    vault = create_vault(real_parent)
    save_registry(
        VaultRegistry(
            vaults=[VaultEntry(name="real", path=str(vault), is_active=True)]
        )
    )
    # Corrupt the active vault's config (file present, unparseable YAML).
    (vault / "lit-config.yaml").write_text(": : [bad", encoding="utf-8")

    runner = CliRunner()
    result = runner.invoke(cli, ["list"])
    flat = " ".join(result.output.split())
    assert "lit-config.yaml is unreadable" in flat


# ---------------------------------------------------------------------------
# Bridge drift (#3's cheap arm) — hook dispatch + end-to-end heal
# ---------------------------------------------------------------------------


def _seed_active_vault_with_dangling_bridge(
    tmp_path: Path,
) -> tuple[Path, Path]:
    """An active vault whose project bridges dangle: linked, then MOVED.

    Builds a vault with one paper linked into one project (INDEX kept
    consistent so ``lit list`` runs cleanly), relocates the whole vault, and
    registers the NEW location as the active entry — the exact post-recovery
    state after the user re-registers a moved library. Returns
    ``(moved_vault, project_dir)``.
    """
    from ruamel.yaml import YAML

    from litman.core.correctors import reconcile_derived
    from litman.core.project_link import rebuild_all_project_links

    parent = tmp_path / "bridge_parent"
    parent.mkdir()
    vault = create_vault(parent)
    project_dir = tmp_path / "pepforge"
    project_dir.mkdir()
    (vault / "lit-config.yaml").write_text(
        f"library_name: {vault.name}\nprojects:\n  pepforge: {project_dir}\n",
        encoding="utf-8",
    )

    y = YAML()
    paper_dir = vault / "papers" / "p1"
    paper_dir.mkdir(parents=True)
    with (paper_dir / "metadata.yaml").open("w", encoding="utf-8") as f:
        y.dump(
            {
                "id": "p1",
                "title": "Test paper",
                "authors": ["Doe, Jane"],
                "year": 2024,
                "doi": "10.test/p1",
                "status": "inbox",
                "priority": "B",
                "type": "research",
                "projects": ["pepforge"],
                "topics": [],
                "methods": [],
                "code-clones": [],
                "created-at": "2026-05-11T10:00:00+02:00",
                "updated-at": "2026-05-11T10:00:00+02:00",
            },
            f,
        )
    reconcile_derived(vault, project_refs=False)  # INDEX + views current
    rebuild_all_project_links(vault, {"pepforge": str(project_dir)})
    assert (project_dir / "litman_reflib" / "p1").exists()

    moved_parent = tmp_path / "bridge_moved"
    moved_parent.mkdir()
    moved = moved_parent / vault.name
    vault.rename(moved)
    save_registry(
        VaultRegistry(
            vaults=[VaultEntry(name="v", path=str(moved), is_active=True)]
        )
    )
    return moved, project_dir


def test_hook_dispatches_bridge_corrector_when_dangling(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``project_bridge_dangling`` fires → the hook dispatches its corrector
    (and only because the category fired: the two sibling correctors are
    stubbed to record and stay silent)."""
    _seed_active_vault_with_dangling_bridge(tmp_path)

    called: list[str] = []
    monkeypatch.setattr(
        _drift,
        "check_and_prompt_bridge_drift",
        lambda *a, **kw: called.append("bridge"),
    )

    runner = CliRunner()
    result = runner.invoke(cli, ["list"])

    assert result.exit_code == 0, result.output
    assert called == ["bridge"]


def test_hook_bridge_heal_end_to_end(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No stubs anywhere: real cheap detection (through the hook's shared
    bounded-stat threading), real corrector, real rebuild. ``lit list`` on a
    just-recovered vault ends with every bridge re-pointing at the vault's
    new location."""
    moved, project_dir = _seed_active_vault_with_dangling_bridge(tmp_path)

    monkeypatch.setattr(_drift, "_default_tty_probe", lambda: True)
    monkeypatch.setattr(click, "confirm", lambda *a, **kw: True)

    runner = CliRunner()
    result = runner.invoke(cli, ["list"])

    assert result.exit_code == 0, result.output
    assert "Rebuilt project links" in result.output
    link = project_dir / "litman_reflib" / "p1"
    assert link.is_symlink()
    assert link.resolve() == (moved / "papers" / "p1").resolve()
