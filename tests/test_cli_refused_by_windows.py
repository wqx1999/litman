"""A refusal that outlasts the wait reaches ``main()``: one line, not a traceback."""

from __future__ import annotations

import errno
import io
import os
import sys
from pathlib import Path
from typing import Any

import pytest
from rich.console import Console

from litman import cli as cli_mod
from litman.core import replace as replace_mod
from litman.core import views
from litman.core.document import list_papers
from litman.core.library import create_vault
from litman.core.replace import refusal_message
from tests.commands.test_link import _make_paper

PAPER = "2024_Foo_Bar"


def _refused(winerror: int | None, *paths: str | None) -> PermissionError:
    err = PermissionError(errno.EACCES, "Access is denied", *paths[:1], None, *paths[1:])
    if winerror is not None:
        err.winerror = winerror  # type: ignore[attr-defined]  # Windows sets it itself
    return err


def _main(monkeypatch: pytest.MonkeyPatch, *argv: str) -> tuple[int | str | None, str]:
    """Run the real ``main()``; its exit code, and what its console printed."""
    printed = io.StringIO()
    with monkeypatch.context() as run:
        run.setattr(sys, "argv", ["lit", *argv])
        # For the module's console: no width folding, no colour whatever
        # FORCE_COLOR says. main() reconfigures the streams: hand it throwaway
        # ones rather than pytest's capture, and put back what it records.
        run.setattr(cli_mod, "console", Console(file=printed, width=80, color_system=None))
        for name in ("stdout", "stderr"):
            run.setattr(sys, name, io.TextIOWrapper(io.BytesIO(), encoding="utf-8"))
        run.setattr(cli_mod, "_LAUNCHED_HEADLESS", cli_mod._LAUNCHED_HEADLESS)
        with pytest.raises(SystemExit) as exit_info:
            cli_mod.main()
    return exit_info.value.code, printed.getvalue()


@pytest.fixture
def vault(tmp_path: Path) -> Path:
    vault = create_vault(tmp_path)
    _make_paper(vault, PAPER, status="inbox")
    views.write_index(vault, list_papers(vault))
    views.rebuild_views(vault, list_papers(vault))
    return vault


def _on_windows_with_no_wait(monkeypatch: Any) -> None:
    monkeypatch.setattr(replace_mod, "_platform", lambda: "win32")
    monkeypatch.setattr(replace_mod, "_BUDGET_SECONDS", 0)


def _refuse_promoting(monkeypatch: Any, name: str) -> None:
    """A staged write's replace of ``name`` is refused the way Windows does it."""
    real = os.replace

    def replace(src: Any, dst: Any) -> None:
        if ".litman-staging" in str(src) and Path(dst).name == name:
            raise _refused(5, str(src), str(dst))
        real(src, dst)

    monkeypatch.setattr(os, "replace", replace)


def test_a_staged_write_refused_for_good_says_one_line_and_rolls_forward_after(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    args = ("modify", PAPER, "--set", "status=skim", "--library", str(vault))

    with monkeypatch.context() as held:
        _on_windows_with_no_wait(held)
        _refuse_promoting(held, "INDEX.json")
        code, printed = _main(monkeypatch, *args)

    assert code == 1
    assert printed == (
        f"error: Windows refused access to {vault / 'INDEX.json'}; another program "
        "may have it open. Close it and run the command again.\n"
    )
    assert (vault / ".litman-staging").is_dir() and any((vault / ".litman-staging").iterdir())

    code, _ = _main(monkeypatch, *args)

    assert code == 0
    assert not any((vault / ".litman-staging").iterdir())
    assert '"status": "skim"' in (vault / "INDEX.json").read_text(encoding="utf-8")


@pytest.mark.parametrize(
    ("platform", "failure"),
    [
        ("win32", _refused(None, "x")),
        ("win32", _refused(1314, "x")),
        ("linux", _refused(5, "x")),
        ("win32", ValueError("a bug")),
    ],
    ids=["no-code", "another-code", "not-windows", "not-a-refusal"],
)
def test_anything_else_still_propagates(
    monkeypatch: pytest.MonkeyPatch, platform: str, failure: Exception
) -> None:
    def cli() -> None:
        raise failure

    monkeypatch.setattr(replace_mod, "_platform", lambda: platform)
    monkeypatch.setattr(cli_mod, "cli", cli)
    for name in ("stdout", "stderr"):
        monkeypatch.setattr(sys, name, io.TextIOWrapper(io.BytesIO(), encoding="utf-8"))
    monkeypatch.setattr(cli_mod, "_LAUNCHED_HEADLESS", cli_mod._LAUNCHED_HEADLESS)

    with pytest.raises(type(failure)) as raised:
        cli_mod.main()

    assert raised.value is failure


def test_the_message_names_the_file_a_replace_would_overwrite(tmp_path: Path) -> None:
    target = tmp_path / "INDEX.json"
    target.write_text("{}", encoding="utf-8")
    message = refusal_message(_refused(5, str(tmp_path / "staged"), str(target)))
    assert str(target) in message
    assert "staged" not in message


def test_the_message_names_the_folder_a_move_could_not_take(tmp_path: Path) -> None:
    src, dst = tmp_path / "papers" / PAPER, tmp_path / "trash-entry"
    message = refusal_message(_refused(5, str(src), str(dst)))
    assert str(src) in message
    assert str(dst) not in message


def test_the_message_has_a_length_budget_and_does_not_assert_who_holds_it() -> None:
    message = refusal_message(_refused(5, r"C:\lib\INDEX.json"))
    assert len(message) <= 120
    assert "may have it open" in message


def test_the_message_still_reads_with_no_path() -> None:
    assert refusal_message(_refused(5)).startswith("Windows refused access to a file;")
