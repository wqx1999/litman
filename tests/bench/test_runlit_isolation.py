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
