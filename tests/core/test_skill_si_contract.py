"""The shipped skills teach the SI contract the CLI actually prints.

An agent learns what `lit add --si <id>` means only from lit-reading's si.md,
which its SKILL.md says to read before running the command: it reads the exit
code and the first stdout line and acts on them. Both live in
``commands/_si_gui.py``; the exit table copies them by hand, so a reworded
line or a renumbered code would leave the skill teaching something the CLI no
longer does. The skill's description is the only signal that
loads it at all, so it has to carry an SI trigger, early (Codex trims
description tails when many skills are installed), and stay within the
1024-character limit of the Agent Skills standard. Both skills' never-write
lists name the SI folder.
"""

from __future__ import annotations

import io
import re

import pytest
from ruamel.yaml import YAML

from litman.commands import _si_gui
from litman.core.skill import bundled_skill_root

# The Agent Skills standard caps a description here; ZCode drops a skill
# whose description is longer.
_DESCRIPTION_LIMIT = 1024
# An SI trigger past this point is the first thing a trimmed listing loses.
_TRIGGER_WITHIN = 200
_SI_WORD = re.compile(r"\bSI\b")


def _skill_text(name: str, filename: str = "SKILL.md") -> str:
    return bundled_skill_root(name).joinpath(filename).read_text(encoding="utf-8")


def _description(name: str) -> str:
    text = _skill_text(name)
    assert text.startswith("---\n"), f"{name}: no frontmatter"
    frontmatter = text.split("---\n", 2)[1]
    data = YAML(typ="safe").load(io.StringIO(frontmatter))
    description = data["description"]
    assert isinstance(description, str) and description
    return description


# The exit code and the first stdout line `lit add --si PAPER` prints with it.
_SI_READY = "SI ready: N file(s)"
_EXIT_ROWS = [
    (0, _SI_READY),
    (_si_gui.EXIT_WAITING, _si_gui.STILL_WAITING),
    (_si_gui.EXIT_CANCELLED, _si_gui.CANCELLED_LINE),
    (_si_gui.EXIT_NO_GUI, _si_gui.NO_PAGE),
    (_si_gui.EXIT_NOT_MENTIONED, _si_gui.NOT_MENTIONED),
]


@pytest.mark.parametrize(("code", "line"), _EXIT_ROWS)
def test_lit_reading_exit_table_pairs_each_code_with_its_line(code: int, line: str) -> None:
    assert line
    assert f"| {code} | `{line}`" in _skill_text("lit-reading", "si.md")


def test_lit_reading_sends_the_agent_to_the_exit_table() -> None:
    """The table only helps if SKILL.md sends the agent there before the command."""
    pointers = [
        line for line in _skill_text("lit-reading").splitlines() if "read `si.md`" in line
    ]
    assert pointers, "lit-reading SKILL.md never says to read si.md"
    assert any("`lit add --si`" in line for line in pointers), pointers


def test_si_ready_is_what_the_cli_prints(capsys: pytest.CaptureFixture[str]) -> None:
    """Control for the exit-0 row: the line print_si_ready writes, N filled in."""
    _si_gui.print_si_ready([])
    assert capsys.readouterr().out == _SI_READY.replace("N", "0") + "\n"


@pytest.mark.parametrize("name", ["lit-reading", "lit-library"])
def test_description_fits_and_carries_an_si_trigger_early(name: str) -> None:
    description = _description(name)
    assert len(description) <= _DESCRIPTION_LIMIT, (
        f"{name}: description is {len(description)} characters"
    )
    match = _SI_WORD.search(description)
    assert match is not None, f"{name}: description names no SI trigger"
    assert match.start() < _TRIGGER_WITHIN, (
        f"{name}: first SI trigger at character {match.start()}"
    )


@pytest.mark.parametrize("name", ["lit-reading", "lit-library"])
def test_never_write_list_names_the_si_folder(name: str) -> None:
    lines = [
        line
        for line in _skill_text(name).splitlines()
        if "**Never** write" in line and "metadata.yaml" in line
    ]
    assert lines, f"{name}: no never-write list found"
    for line in lines:
        assert "supplementary/" in line, f"{name}: never-write list omits it: {line[:80]}"
