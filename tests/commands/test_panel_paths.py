"""Success panels keep paths, URLs and commands to paste outside the box.

A rich ``Panel`` wraps its content to the terminal and takes no ``soft_wrap``,
so at 80 columns — the width an agent's non-TTY console gets as well — every
long path inside one came out split, with the border character in the middle.
``lit add``, ``lit code add``, ``lit init`` and ``lit link`` now print those
lines underneath their panel (``commands/_path_lines``), and ``lit project
add`` / ``set-path`` soft-wrap theirs.

Each test renders at a width the path cannot fit into and asserts the path
comes out whole on a line of its own, not inside the box. The consoles are
pinned rather than set through COLUMNS: they are module globals, and rich
caches a console's width the first time anything reads it.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path

import pytest
from click.testing import CliRunner
from rich.console import Console

from litman.cli import cli
from litman.commands import add as add_cmd
from litman.commands import code as code_cmd
from litman.commands import init as init_cmd
from litman.commands import link as link_cmd
from litman.commands import project as project_cmd
from litman.core.library import create_vault

# Long enough that any path built on it overruns 80 columns by itself.
_LONG = "a-directory-name-long-enough-to-overrun-a-terminal"

_URL = "https://github.com/some-organisation/a-repository-with-a-long-name"


@pytest.fixture(autouse=True)
def narrow(monkeypatch: pytest.MonkeyPatch) -> None:
    for module in (add_cmd, code_cmd, init_cmd, link_cmd, project_cmd):
        monkeypatch.setattr(module, "console", Console(width=80))


@pytest.fixture
def long_root(tmp_path: Path) -> Path:
    root = tmp_path / _LONG
    root.mkdir()
    return root


def _ok(result):  # type: ignore[no-untyped-def]
    assert result.exit_code == 0, result.output
    return result


def _assert_whole_and_outside(output: str, text: str) -> None:
    """``text`` appears unbroken, and never on a line the panel drew."""
    assert text in output, output
    lines = [line for line in output.splitlines() if text in line]
    assert lines and not any("│" in line for line in lines), output


def _add_paper(
    root: Path, vault: Path, make_text_pdf: Callable[..., Path]
) -> str:
    pdf = make_text_pdf(
        [["Title page."], [f"Code availability: {_URL} is released."]],
        name="zeta.pdf",
    )
    meta = root / "meta.json"
    meta.write_text(
        json.dumps({
            "title": "A study of long paths",
            "authors": ["Zeta, Ann"],
            "year": 2024,
        }),
        encoding="utf-8",
    )
    result = _ok(CliRunner().invoke(
        cli,
        ["add", str(pdf), "--from-llm-json", str(meta),
         "--id", "2024_Zeta_Paths", "--library", str(vault)],
    ))
    return result.output


def test_add_prints_the_folder_and_code_candidates_outside_the_panel(
    long_root: Path, make_text_pdf: Callable[..., Path]
) -> None:
    vault = create_vault(long_root)

    output = _add_paper(long_root, vault, make_text_pdf)

    _assert_whole_and_outside(output, str(vault / "papers" / "2024_Zeta_Paths"))
    # The agent-parsed block keeps its fences and its one-line candidates.
    _assert_whole_and_outside(output, f"{_URL} (p2, ×1)")
    _assert_whole_and_outside(output, "[code_candidates]")
    assert "Paper added:" in output  # the panel itself is still there


def test_link_prints_its_paths_and_tips_outside_the_panel(
    long_root: Path, make_text_pdf: Callable[..., Path]
) -> None:
    vault = create_vault(long_root)
    _add_paper(long_root, vault, make_text_pdf)
    project = long_root / "projects" / "pepforge"
    project.mkdir(parents=True)
    runner = CliRunner()
    added = _ok(runner.invoke(
        cli,
        ["project", "add", "pepforge", "--path", str(project),
         "--library", str(vault)],
    ))
    _assert_whole_and_outside(added.output, str(project))

    result = _ok(runner.invoke(
        cli,
        ["link", "2024_Zeta_Paths", "--project", "pepforge",
         "--library", str(vault)],
    ))

    reflib = project / "litman_reflib"
    _assert_whole_and_outside(result.output, str(project))
    _assert_whole_and_outside(result.output, str(reflib / "2024_Zeta_Paths"))
    _assert_whole_and_outside(result.output, str(reflib / "REFERENCES.md"))
    _assert_whole_and_outside(
        result.output,
        "`lit modify 2024_Zeta_Paths --set priority-pepforge=A`",
    )


def test_code_add_prints_its_paths_outside_the_panel(
    long_root: Path, make_text_pdf: Callable[..., Path]
) -> None:
    vault = create_vault(long_root)
    _add_paper(long_root, vault, make_text_pdf)
    source = long_root / "checkout"
    source.mkdir()
    (source / "README.md").write_text("# repo\n", encoding="utf-8")

    result = _ok(CliRunner().invoke(
        cli,
        ["code", "add", str(source), "--paper", "2024_Zeta_Paths",
         "--library", str(vault)],
    ))

    _assert_whole_and_outside(result.output, str(vault / "codes" / "checkout"))
    _assert_whole_and_outside(result.output, str(source))
    _assert_whole_and_outside(result.output, "`lit refresh-views`")


@pytest.mark.parametrize("register", [True, False], ids=["registered", "no-register"])
def test_init_prints_the_vault_outside_the_panel(
    long_root: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    register: bool,
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("LITMAN_REGISTRY_DIR", str(tmp_path / "registry"))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)

    args = ["init", str(long_root)] + ([] if register else ["--no-register"])
    result = _ok(CliRunner().invoke(cli, args))

    vault = long_root / "literature_vault"
    _assert_whole_and_outside(result.output, str(vault))
    if not register:
        _assert_whole_and_outside(result.output, f"--library {vault}")
