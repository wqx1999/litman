"""The ``lit`` commands the skills teach are the ones agents type back.

An agent's permission system reads a command before it runs it, and one token
it cannot make sense of can send the whole command to a sandbox: an id starting
with a digit, a ``<`` left bare, ``key='value'`` with the quote halfway through,
a curly quote. So the skills write every argument as one quoted token, and their
examples are what teaches it.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

import litman.skills

# The one place a skill spells out the form it forbids.
_NEGATIVE_EXAMPLE = "never `topics='x'`"
_TYPOGRAPHIC = "‘’“”–—―"

_ROOT = Path(litman.skills.__file__).parent
_FILES = sorted(_ROOT.rglob("*.md"))
_INLINE = re.compile(r"`((?:lit |--set |--add-tag |--rm-tag |[\w-]+=<)[^`]*)`")
_QUOTED = re.compile(r"'[^']*'|\"[^\"]*\"")
_GLUED = re.compile(r"[\w>-]=['\"]")
_GLUED_ANYWHERE = re.compile(r"[\w>-]='")


def _commands(text: str) -> list[str]:
    """Every command in ``text``: inline code, and lines of a fenced block."""
    found: list[str] = []
    fenced = False
    for line in text.splitlines():
        if line.lstrip().startswith("```"):
            fenced = not fenced
        elif fenced:
            if line.lstrip().startswith("lit "):
                found.append(line.strip())
        else:
            found.extend(_INLINE.findall(line))
    return found


def _problems(text: str) -> list[str]:
    """What is wrong with the commands in ``text``, and in its text at large."""
    problems = [
        f"key='value': {hit!r}"
        for hit in _GLUED_ANYWHERE.findall(text.replace(_NEGATIVE_EXAMPLE, ""))
    ]
    for command in _commands(text):
        bare = _QUOTED.sub("", command)
        if _GLUED.search(command):
            problems.append(f"key=\"value\": {command}")
        if re.search(r"<(?!cmd>)", bare):
            problems.append(f"placeholder not quoted: {command}")
        if any(t[0].isdigit() and not t.isdigit() for t in bare.split()):
            problems.append(f"value starting with a digit not quoted: {command}")
        if any(char in command for char in _TYPOGRAPHIC):
            problems.append(f"curly quote or long dash: {command}")
    return problems


@pytest.mark.parametrize("path", _FILES, ids=lambda p: f"{p.parent.name}/{p.name}")
def test_a_skills_commands_quote_their_arguments(path: Path) -> None:
    assert _problems(path.read_text(encoding="utf-8")) == []


def test_the_scan_found_the_commands_it_is_there_for() -> None:
    assert len(_FILES) == 7
    found = [c for p in _FILES for c in _commands(p.read_text(encoding="utf-8"))]
    assert "lit show '<id>' --format json" in found
    assert "lit rename '<old-id>' '<new-id>'" in found
    assert len(found) > 100


@pytest.mark.parametrize(
    "bad",
    [
        "`lit show <id>`",
        "`lit modify '<id>' --add-tag topics=<value>`",
        "`--add-tag extends=<other-id>`",
        "`lit modify '<id>' --set relevance-x='...'`",
        "`lit modify '<id>' --set title=\"x\"`",
        "`lit pdf-text '<pdf>' --pages 1-3`",
        "`lit show 2009_Smith_x`",
        "`lit add '10.1/x' — a note`",
        "`lit modify '<id>' --set-author “Smith, J”`",
        "```\nlit rename <old> <new>\n```",
        "write it as key='value' in the command",
    ],
)
def test_the_scan_fails_on_what_it_forbids(bad: str) -> None:
    assert _problems(bad) != []


@pytest.mark.parametrize(
    "good",
    [
        "`lit show '<id>' --format json`",
        "`lit <cmd> --help`",
        "`lit modify '<id>' --add-tag 'topics=<value>'`",
        "`lit list --limit 20`",
        "`lit add 'it''s.pdf' --doi \"10.1/x\"`",
        "`lit … | head`",
    ],
)
def test_the_scan_passes_the_forms_a_skill_may_use(good: str) -> None:
    assert _problems(good) == []


@pytest.mark.parametrize("skill", ["lit-library", "lit-reading"])
def test_the_rule_comes_before_every_numbered_rule(skill: str) -> None:
    text = (_ROOT / skill / "SKILL.md").read_text(encoding="utf-8")
    rule = text.index("in single quotes, the whole token")
    assert text.index("## Rules") < rule < text.index("1. **Read with `--format json`")
