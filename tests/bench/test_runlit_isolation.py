"""Isolation-proof tests for runlit (Phase B; spec §6 AC#1).

The §4 safety red line is welded into ``runlit``: a benchmark run must never
touch the user's real vault or registry. These tests PROVE that without a real
vault present, by standing up a *decoy* ``$LIT_LIBRARY`` vault and asserting:

  1. the decoy vault is byte-for-byte unchanged after a full run;
  2. the platformdirs default registry path (``~/.config/litman/vaults.yaml``)
     is NOT created by the run;
  3. the child's ``find_vault`` resolves to the /tmp run vault, never the decoy.

NEVER references ``/work/wangq/literature_vault`` (the real vault). Everything
lives under tmp_path / /tmp.
"""

from __future__ import annotations

import hashlib
import os
import subprocess
import sys
from pathlib import Path

import pytest

from harness import seeds
from harness.runlit import RunVault, isolated_env
from harness.seeds import LIT_BIN

# Reuse the smallest seed so the test is fast; building it is idempotent.
_SEED_NAME = "seed-empty"


def _dir_digest(root: Path) -> str:
    """A stable digest of every file under ``root`` (path + bytes)."""
    h = hashlib.sha256()
    for p in sorted(root.rglob("*")):
        rel = p.relative_to(root).as_posix()
        h.update(rel.encode())
        if p.is_file():
            h.update(p.read_bytes())
        h.update(b"\0")
    return h.hexdigest()


@pytest.fixture
def decoy_vault(tmp_path: Path) -> Path:
    """A throwaway 'real-looking' vault the run must never touch.

    Built by ``lit init`` in its OWN isolated registry dir so creating it does
    not pollute the platformdirs default either.
    """
    parent = tmp_path / "decoy-parent"
    parent.mkdir()
    env = os.environ.copy()
    env["LITMAN_REGISTRY_DIR"] = str(tmp_path / "decoy-registry")
    env.pop("LIT_LIBRARY", None)
    proc = subprocess.run(
        [str(LIT_BIN), "init", str(parent), "--name", "decoyvault", "--no-register"],
        env=env,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, proc.stderr or proc.stdout
    return parent / "decoyvault"


def test_isolated_env_unsets_lit_library(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LIT_LIBRARY", "/some/real/vault")
    env = isolated_env(Path("/tmp/x/registry"))
    assert "LIT_LIBRARY" not in env
    assert env["LITMAN_REGISTRY_DIR"] == "/tmp/x/registry"
    # The caller's own environment is untouched.
    assert os.environ.get("LIT_LIBRARY") == "/some/real/vault"


def test_default_registry_path_is_outside_tmp() -> None:
    """Sanity: the default registry lives under the real home, not /tmp."""
    from litman.core.vault_registry import registry_path_default

    default = registry_path_default()
    assert "/tmp/" not in str(default)


def test_decoy_vault_unchanged_after_run(decoy_vault: Path, monkeypatch) -> None:
    """AC#1: a decoy $LIT_LIBRARY vault is byte-for-byte unchanged after a run."""
    # Point $LIT_LIBRARY at the decoy in THIS process's env so we can prove the
    # child unsets it (the run targets the /tmp run vault via --library).
    monkeypatch.setenv("LIT_LIBRARY", str(decoy_vault))
    before = _dir_digest(decoy_vault)

    seed = seeds.build_seed(_SEED_NAME)
    with RunVault(seed) as rv:
        # A write command (taxonomy add) + a read command. If $LIT_LIBRARY had
        # leaked, these would land in the decoy.
        r1 = rv.run("taxonomy", "add", "topics", "isolation-probe")
        r2 = rv.run("list")
        assert r1.exit_code == 0, r1.stderr
        assert r2.exit_code == 0, r2.stderr
        # The write landed in the RUN vault, not the decoy.
        run_tax = (rv.vault / "TAXONOMY.md").read_text(encoding="utf-8")
        assert "isolation-probe" in run_tax

    after = _dir_digest(decoy_vault)
    assert before == after, "decoy vault was modified by the run"
    decoy_tax = (decoy_vault / "TAXONOMY.md").read_text(encoding="utf-8")
    assert "isolation-probe" not in decoy_tax


def test_default_registry_not_created_by_run(monkeypatch) -> None:
    """AC#1: the platformdirs default registry path is not created by a run.

    We snapshot whether the default path exists before the run and require that
    the run does not bring it into existence (the run redirects
    LITMAN_REGISTRY_DIR into its own /tmp dir).
    """
    from litman.core.vault_registry import registry_path_default

    default = registry_path_default()
    existed_before = default.exists()

    seed = seeds.build_seed(_SEED_NAME)
    with RunVault(seed) as rv:
        rv.run("taxonomy", "add", "topics", "probe2")
        # The run's registry dir is inside the run dir, never the default.
        assert rv.registry_dir is not None
        assert str(rv.registry_dir).startswith("/tmp/")
        assert Path(rv.env["LITMAN_REGISTRY_DIR"]) == rv.registry_dir

    # The run must not have created the default registry file.
    if not existed_before:
        assert not default.exists(), (
            f"run created the default registry at {default}"
        )


def test_child_find_vault_resolves_to_run_vault(monkeypatch) -> None:
    """AC#1: inside the isolated env, find_vault resolves to the /tmp run vault.

    Run a child python that calls ``find_vault()`` with no explicit arg, under
    the run's isolated env and cwd. With LIT_LIBRARY unset and the redirected
    registry empty (no active vault), discovery falls to cwd-walk and must land
    on the run vault — proving the run is self-contained.
    """
    monkeypatch.setenv("LIT_LIBRARY", "/nonexistent/decoy")  # must be ignored
    seed = seeds.build_seed(_SEED_NAME)
    with RunVault(seed) as rv:
        assert rv.vault is not None and rv.env is not None
        probe = (
            "from litman.core.library import find_vault;"
            "print(find_vault())"
        )
        proc = subprocess.run(
            [sys.executable, "-c", probe],
            env=rv.env,
            cwd=str(rv.vault),
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
        )
        assert proc.returncode == 0, proc.stderr
        resolved = Path(proc.stdout.strip()).resolve()
        assert resolved == rv.vault.resolve(), (
            f"find_vault resolved to {resolved}, not the run vault {rv.vault}"
        )


def test_run_dir_cleaned_on_exit() -> None:
    seed = seeds.build_seed(_SEED_NAME)
    with RunVault(seed) as rv:
        run_dir = rv.run_dir
        assert run_dir is not None and run_dir.exists()
    assert not run_dir.exists()


def test_run_logs_to_jsonl() -> None:
    from harness.runlit import read_run_log

    seed = seeds.build_seed(_SEED_NAME)
    with RunVault(seed) as rv:
        rv.run("list")
        rv.run("taxonomy", "list")
        log = read_run_log(rv.log_path)
        assert len(log) == 2
        assert log[0]["argv"][0] == "list"
        assert "exit_code" in log[0] and "stdout" in log[0] and "stderr" in log[0]
        # --library was injected automatically (target the run vault).
        assert "--library" in log[0]["argv"]


def test_run_no_log_when_disabled() -> None:
    from harness.runlit import read_run_log

    seed = seeds.build_seed(_SEED_NAME)
    with RunVault(seed) as rv:
        rv.run("list", log=False)
        assert read_run_log(rv.log_path) == []


def test_explicit_library_not_overridden() -> None:
    """If the caller passes --library (decoy test), runlit does not append another."""
    seed = seeds.build_seed(_SEED_NAME)
    with RunVault(seed) as rv:
        res = rv.run("list", "--library", str(rv.vault))
        assert res.argv.count("--library") == 1


# ---------------------------------------------------------------------------
# Isolation BETWEEN cards: the seed's projects/ must not be shared
# ---------------------------------------------------------------------------
#
# The second isolation this module's docstring promises — "a failure never
# cascades" — was the one it broke. Copying `vault/` alone left the project's
# ABSOLUTE seed path baked into the copied `lit-config.yaml`, so any card that
# touched a project wrote straight through to the shared, cross-run-cached seed.


def _seed_with_project(tmp_path: Path, *, project: str, folder: str) -> Path:
    """Build a minimal seed root whose project FOLDER name is `folder`.

    Returns the seed vault. Deliberately allows folder != project so the tests
    below can prove the remap follows the configured path, not the name.
    """
    seed_root = tmp_path / "seedroot"
    vault = seed_root / "vault"
    env = os.environ.copy()
    env["LITMAN_REGISTRY_DIR"] = str(tmp_path / "seed-registry")
    env.pop("LIT_LIBRARY", None)
    seed_root.mkdir(parents=True)
    proc = subprocess.run(
        [str(LIT_BIN), "init", str(seed_root), "--name", "vault", "--no-register"],
        env=env, stdin=subprocess.DEVNULL, capture_output=True, text=True,
    )
    assert proc.returncode == 0, proc.stderr
    proj_dir = seed_root / "projects" / folder
    proj_dir.mkdir(parents=True)
    proc = subprocess.run(
        [str(LIT_BIN), "project", "add", project, "--path", str(proj_dir),
         "--library", str(vault)],
        env=env, stdin=subprocess.DEVNULL, capture_output=True, text=True,
    )
    assert proc.returncode == 0, proc.stderr
    return vault


def _configured_project_path(vault: Path, project: str) -> str:
    import yaml

    payload = yaml.safe_load((vault / "lit-config.yaml").read_text(encoding="utf-8"))
    return str(payload["projects"][project])


def test_seed_projects_dir_is_copied_in_as_the_vaults_sibling(tmp_path: Path) -> None:
    """`projects/` must land at <run_dir>/projects, mirroring the seed root.

    Sibling, not child: the bridge symlinks are relative (../../../vault/papers/
    <id>), so this layout — and only this one — makes them resolve to the run's
    own papers without rebuilding a single link.
    """
    seed = _seed_with_project(tmp_path, project="PepCodec", folder="PepCodec")
    (seed.parent / "projects" / "PepCodec" / "marker.txt").write_text("x", encoding="utf-8")

    with RunVault(seed, run_root=tmp_path / "work") as rv:
        assert (rv.run_dir / "projects" / "PepCodec" / "marker.txt").is_file()
        assert rv.vault.parent == rv.run_dir  # the vault's own shape is unchanged


def test_copied_config_no_longer_points_at_the_seed(tmp_path: Path) -> None:
    """The bug itself: a copied vault whose config still names the shared seed."""
    seed = _seed_with_project(tmp_path, project="PepCodec", folder="PepCodec")
    seed_root = seed.parent
    assert str(seed_root) in _configured_project_path(seed, "PepCodec")

    with RunVault(seed, run_root=tmp_path / "work") as rv:
        got = _configured_project_path(rv.vault, "PepCodec")
        assert str(seed_root) not in got, f"run copy still points at the seed: {got}"
        assert got == str(rv.run_dir / "projects" / "PepCodec")


def test_remap_follows_the_configured_path_not_the_project_name(tmp_path: Path) -> None:
    """A project's NAME may differ from its FOLDER — remap by relpath.

    `lit project add --help`: "NAME is a label papers tag with; it may differ
    from the folder's own name". Joining <run_dir>/projects/<name> passes
    whenever the two happen to coincide and silently repoints the project at a
    nonexistent dir the moment they do not.
    """
    seed = _seed_with_project(tmp_path, project="PepCodec", folder="pepcodec-workdir")

    with RunVault(seed, run_root=tmp_path / "work") as rv:
        got = Path(_configured_project_path(rv.vault, "PepCodec"))
        assert got == rv.run_dir / "projects" / "pepcodec-workdir"
        assert got.is_dir(), "the remapped path must actually exist"


def test_a_project_configured_outside_the_seed_root_is_loud(tmp_path: Path) -> None:
    """No seed builds this today; if one starts to, it must not pass silently."""
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    seed = _seed_with_project(tmp_path, project="PepCodec", folder="PepCodec")
    # Re-point the seed itself out of its root, through the product's own CLI.
    env = os.environ.copy()
    env["LITMAN_REGISTRY_DIR"] = str(tmp_path / "seed-registry")
    env.pop("LIT_LIBRARY", None)
    proc = subprocess.run(
        [str(LIT_BIN), "project", "set-path", "PepCodec", str(outside),
         "--library", str(seed)],
        env=env, stdin=subprocess.DEVNULL, capture_output=True, text=True,
    )
    assert proc.returncode == 0, proc.stderr

    with pytest.raises(RuntimeError, match="OUTSIDE the seed root"):
        with RunVault(seed, run_root=tmp_path / "work"):
            pass
    # and it must not leak the run dir it had already created
    assert list((tmp_path / "work").iterdir()) == []


def test_localizing_keeps_the_harnesss_own_commands_out_of_the_evidence(
    tmp_path: Path,
) -> None:
    """`littest-run.jsonl` is the proof the AGENT invoked the CLI.

    The localization spends real `lit project set-path` calls; logging them would
    put commands nobody asked the agent for into the file the cards' `ran:`
    assertions read.
    """
    from harness.runlit import read_run_log

    seed = _seed_with_project(tmp_path, project="PepCodec", folder="PepCodec")
    with RunVault(seed, run_root=tmp_path / "work") as rv:
        assert read_run_log(rv.log_path) == []
        rv.run("list")
        log = read_run_log(rv.log_path)
        assert [r["argv"][0] for r in log] == ["list"]


@pytest.mark.slow
def test_a_project_write_never_reaches_the_shared_seed(tmp_path: Path) -> None:
    """The regression, end to end: `lit unlink` on the real 5-paper seed.

    Three things must hold AT ONCE — the seed is byte-identical, the unlink
    REALLY happened in this card's own copy, and the copy stays healthy. Drop any
    one and the "fix" is a different bug: skipping the copy leaves it dangling,
    and never running the command at all would also leave the seed pristine.
    """
    seed = seeds.build_seed("seed-5papers-tagged")
    seed_root = seed.parent
    before = seeds.seed_digest(seed_root)

    with RunVault(seed, run_root=tmp_path / "work") as rv:
        bridge = rv.run_dir / "projects" / "PepCodec" / "litman_reflib"
        linked = [p.name for p in bridge.iterdir() if p.is_symlink()]
        assert linked, "precondition: #4 is bridged into PepCodec"

        res = rv.run("unlink", linked[0], "--project", "PepCodec")
        assert res.exit_code == 0, res.stderr

        # (a) it really did the work — in THIS copy.
        assert [p.name for p in bridge.iterdir() if p.is_symlink()] == []
        # (b) the copy is consistent afterwards (no dangling bridge).
        health = rv.run("health-check", log=False)
        assert "errors: 0" in health.stdout, health.stdout

    # (c) and the shared seed never moved.
    assert seeds.seed_digest(seed_root) == before
    seeds.assert_seed_intact(seed_root)
