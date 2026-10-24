"""Commands that would ask a question, run with nobody to answer it.

Each runs ``lit`` as its own process with ``stdin=DEVNULL``: the NUL device on
Windows, which Python there reports as a tty, so an agent's run used to walk
into the prompt and stop at a bare ``Aborted!``. Every one must instead print
the sentence that names the flag to pass, without asking.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import litman


def _lit(tmp_path: Path, *args: str) -> subprocess.CompletedProcess[str]:
    """``lit ARGS`` as its own process, stdin the null device, HOME and the
    registry under ``tmp_path``."""
    env = os.environ.copy()
    env.pop("FORCE_COLOR", None)
    env["COLUMNS"] = "200"
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    env["HOME"] = env["USERPROFILE"] = str(home)
    env["LITMAN_REGISTRY_DIR"] = str(tmp_path / "registry")
    env["LITMAN_NO_UPDATE_CHECK"] = "1"
    src_dir = Path(litman.__file__).resolve().parents[1]
    env["PYTHONPATH"] = str(src_dir) + os.pathsep + env.get("PYTHONPATH", "")
    return subprocess.run(
        [sys.executable, "-m", "litman", *args],
        stdin=subprocess.DEVNULL,
        env=env,
        capture_output=True,
        encoding="utf-8",
        timeout=120,
    )


def test_install_skill_over_a_stale_copy_says_pass_force(tmp_path: Path) -> None:
    parent = tmp_path / "skills"
    skill_md = parent / "lit-library" / "SKILL.md"
    skill_md.parent.mkdir(parents=True)
    skill_md.write_text("an older lit-library\n", encoding="utf-8")

    proc = _lit(tmp_path, "install-skill", "--skill", "lit-library", "--parent-dir", str(parent))

    out = proc.stdout + proc.stderr
    assert proc.returncode != 0, out
    assert "Pass --force to refresh" in out
    assert "Aborted!" not in out
    assert skill_md.read_text(encoding="utf-8") == "an older lit-library\n"


def test_add_onto_a_taken_id_says_pass_auto_suffix(
    vault_with_paper: tuple[Path, str], tmp_path: Path
) -> None:
    vault, paper_id = vault_with_paper
    pdf = tmp_path / "other.pdf"
    pdf.write_bytes(b"%PDF-1.4\n%other\n%%EOF\n")
    meta = tmp_path / "other.json"
    meta.write_text(
        json.dumps({"title": "Bar", "authors": ["Foo, Bob"], "year": 2024, "doi": "10.1/other"}),
        encoding="utf-8",
    )

    proc = _lit(tmp_path, "add", str(pdf), "--from-llm-json", str(meta), "--library", str(vault))

    out = proc.stdout + proc.stderr
    assert proc.returncode != 0, out
    assert f"Paper id '{paper_id}' already exists" in out
    assert "Pass --auto-suffix" in out
    assert "Choice" not in out
    assert "Aborted!" not in out
    assert pdf.exists()


def test_rm_without_yes_says_pass_yes(vault_with_paper: tuple[Path, str], tmp_path: Path) -> None:
    vault, paper_id = vault_with_paper

    proc = _lit(tmp_path, "rm", paper_id, "--library", str(vault))

    out = proc.stdout + proc.stderr
    assert proc.returncode != 0, out
    assert "pass --yes to confirm" in out
    assert "Aborted!" not in out
    # The warning block belongs to the interactive branch only.
    assert "About to move to .trash/" not in out
    assert (vault / "papers" / paper_id).is_dir()
