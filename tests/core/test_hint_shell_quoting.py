"""The ``lit`` commands litman prints are ones agents and people paste back.

A real value goes through ``shell_arg``, a placeholder sits in double quotes, and a
``FIELD=VALUE`` is quoted as one token: see :mod:`litman.core.shell` for why. This
reads the source, because most of these lines only appear on some rare failure.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

import litman

_ROOT = Path(litman.__file__).parent

# A command's words before its arguments. A title such as `lit vault info — x`
# is not one, and neither is a sentence that only mentions a command.
_COMMAND = re.compile(
    r"\blit (?:add|show|rm|modify|link|unlink|rename|read|revisit|open|list|init"
    r"|trash restore|code (?:add|link|unlink|rm)"
    r"|project (?:add|rm|rename|set-path)"
    r"|taxonomy (?:add|rm|merge|rename)"
    r"|vault (?:add|use|remove|set-path|info)) "
)
_VALUE_MARK = re.compile(r"\0(.*?)\0")
_QUOTED = re.compile(r"\"[^\"]*\"|'[^']*'")

# A value that is a word litman itself picks, never one a user or a paper does.
_CHOSEN_BY_LITMAN = {
    "dict_name",
    "field",
    "flags",
    "fwd",
    "key",
    "shown",
    "escape(flags)",
    "escape(shown)",
    "shell_path(path)",
}
_ALLOWED_PLACEHOLDER = {"<cmd>", "<Tab>", "<shell>"}
# Not a command to paste: text in a project's own REFERENCES.md.
_NOT_OUTPUT = {"core/project_refs.py"}


def _strings(source: str) -> list[str]:
    """Every string in ``source`` but docstrings, values marked off in NULs."""
    tree = ast.parse(source)
    docstrings = set()
    for node in ast.walk(tree):
        if isinstance(
            node, ast.Module | ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef
        ):
            first = node.body[0] if node.body else None
            if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant):
                docstrings.add(id(first.value))
    inside_fstring = {
        id(value)
        for node in ast.walk(tree)
        if isinstance(node, ast.JoinedStr)
        for value in node.values
    }
    found: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.JoinedStr):
            found.append(
                "".join(
                    str(v.value)
                    if isinstance(v, ast.Constant)
                    else f"\0{ast.unparse(v.value)}\0"  # type: ignore[attr-defined]
                    for v in node.values
                )
            )
        elif (
            isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and id(node) not in docstrings
            and id(node) not in inside_fstring
        ):
            found.append(node.value)
    return found


def _commands(text: str) -> list[str]:
    """The commands in one string, each cut at the end of its backticks or line."""
    commands = []
    for match in _COMMAND.finditer(text):
        tail = text[match.start() :]
        in_backticks = match.start() > 0 and text[match.start() - 1] == "`"
        end = (
            re.search(r"`", tail)
            if in_backticks
            else re.search(r"\n|\. |\.$|\[/| \u2014 ", tail)
        )
        commands.append(tail[: end.start()] if end else tail)
    return commands


def _problems(source: str) -> list[str]:
    problems = []
    for text in _strings(source):
        for command in _commands(text):
            shown = _VALUE_MARK.sub(lambda m: "{" + m.group(1) + "}", command)
            if re.search(r"[\w>}-]='", shown):
                problems.append(f"key='value': {shown}")
            for value in _VALUE_MARK.findall(_QUOTED.sub("", command)):
                if "shell_arg(" not in value and value not in _CHOSEN_BY_LITMAN:
                    problems.append(f"{value} not through shell_arg: {shown}")
            bare = _QUOTED.sub("", _VALUE_MARK.sub("", command))
            for placeholder in re.findall(r"<[^>\s]+>", bare):
                if placeholder not in _ALLOWED_PLACEHOLDER:
                    problems.append(f"{placeholder} not in double quotes: {shown}")
    return problems


_FILES = sorted(
    p for p in _ROOT.rglob("*.py") if p.relative_to(_ROOT).as_posix() not in _NOT_OUTPUT
)


@pytest.mark.parametrize("path", _FILES, ids=lambda p: p.relative_to(_ROOT).as_posix())
def test_a_printed_command_quotes_its_arguments(path: Path) -> None:
    assert _problems(path.read_text(encoding="utf-8")) == []


def test_the_scan_found_the_commands_it_is_there_for() -> None:
    found = [
        command
        for path in _FILES
        for text in _strings(path.read_text(encoding="utf-8"))
        for command in _commands(text)
    ]
    assert len(found) > 80
    assert any(c.startswith("lit rename \0shell_arg(pid)\0") for c in found)


@pytest.mark.parametrize(
    "source",
    [
        "x = f'run `lit show {paper_id}`'",
        "x = f'run `lit modify {shell_arg(pid)} --set relevance-{name}=x`'",
        "x = 'run `lit show <id>`'",
        "x = 'lit add <pdf> --doi <doi>'",
        "x = \"run `lit modify x --set relevance-p='...'`\"",
        "x = f\"run `lit modify {shell_arg(p)} --set key='...'`\"",
        "x = f'`lit modify {shell_arg(p)} --set read-date=<YYYY-MM-DD>`'",
    ],
)
def test_the_scan_fails_on_what_it_forbids(source: str) -> None:
    assert _problems(source) != []


@pytest.mark.parametrize(
    "source",
    [
        "x = f'run `lit show {shell_arg(paper_id)}`'",
        "x = f'run `lit show {escape(shell_arg(paper_id))}`'",
        "x = 'run `lit add \"<pdf>\" --doi \"<doi>\"`'",
        "x = f'run `lit modify {shell_arg(p)} --set \"read-date=<YYYY-MM-DD>\"`'",
        "x = f'run `lit modify {shell_arg(p)} --set \"relevance-{escape(p)}=...\"`'",
        "x = 'run `lit <cmd> --help`'",
        "x = f'lit vault info — {name}'",
        "def f():\n    '''Run lit show <id>.'''",
    ],
)
def test_the_scan_passes_what_a_printed_command_may_say(source: str) -> None:
    assert _problems(source) == []
