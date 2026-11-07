"""Every check category the server can report has a heading in the GUI's health panel.

The panel titles each group of findings with ``healthPanel.categories[<category>]``
(frontend/src/i18n/en/topbar.ts; the Messages type makes zh match en key for
key). A category with no key falls back to the raw id (``paper_dir_validity``),
which is the GUI showing an internal name. So every category emitted under
``core/checks`` must have one. The categories are read from the source, the
way each check names them: ``Issue(category="x", …)`` or ``Issue("x", …)``,
``category=`` passed to a shared helper, a conditional between two names, and
the SI check's own ``add("x", …)``.

The panel shows each finding's message and not its hint: a hint names the CLI
command that repairs it, which is the agent's side (``lit health-check``
prints it). So no message may name a command either.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CHECKS = ROOT / "src" / "litman" / "core" / "checks"
EN_TOPBAR = ROOT / "frontend" / "src" / "i18n" / "en" / "topbar.ts"


def _names(node: ast.expr | None) -> set[str]:
    """The category names an expression can take; empty when it is not literal."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return {node.value}
    if isinstance(node, ast.IfExp):
        return _names(node.body) | _names(node.orelse)
    return set()


def emitted_categories() -> set[str]:
    found: set[str] = set()
    for source in sorted(CHECKS.glob("*.py")):
        tree = ast.parse(source.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", "")
            for keyword in node.keywords:
                if keyword.arg == "category":
                    found |= _names(keyword.value)
            if name in ("Issue", "add") and node.args:
                found |= _names(node.args[0])
    return found


def panel_headings() -> set[str]:
    text = EN_TOPBAR.read_text(encoding="utf-8")
    block = re.search(r"\n(\s*)categories: \{\n(.*?)\n\1\},", text, re.S)
    assert block is not None, "healthPanel.categories not found in en/topbar.ts"
    return set(re.findall(r"^\s*(\w+):\s*'", block.group(2), re.M))


def test_every_reported_category_has_a_heading() -> None:
    missing = emitted_categories() - panel_headings()

    assert not missing, f"no healthPanel.categories key for: {sorted(missing)}"


def test_the_source_walk_finds_every_way_a_category_is_named() -> None:
    """The control: each spelling the checks use is seen, so a pass above means something."""
    found = emitted_categories()

    assert "schema" in found  # Issue(category="schema", …)
    assert {"unfinished_rename", "paper_dir_validity"} <= found  # a conditional
    assert {"relevance_orphan", "priority_orphan"} <= found  # a shared helper's category=
    assert "supplementary_missing" in found  # add("supplementary_missing", …)
    assert len(found) >= 40
    # And the check bites: a category with no heading is reported.
    assert "a_new_check" not in panel_headings()


def _literal_text(node: ast.expr) -> str:
    """The literal parts of a message expression (f-string pieces included)."""
    return "".join(
        part.value
        for part in ast.walk(node)
        if isinstance(part, ast.Constant) and isinstance(part.value, str)
    )


def messages_naming_a_command(source: str) -> list[tuple[int, str]]:
    """``(line, command)`` for each message in ``source`` that names a backticked command.

    A message is ``message=`` on any call, or the fourth argument of a
    positional ``Issue(…)`` / ``add(…)``.
    """
    found: list[tuple[int, str]] = []
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", "")
        messages = [kw.value for kw in node.keywords if kw.arg == "message"]
        if name in ("Issue", "add") and len(node.args) >= 4:
            messages.append(node.args[3])
        for message in messages:
            for command in re.findall(r"`([^`]+)`", _literal_text(message)):
                found.append((node.lineno, command))
    return found


def test_no_finding_message_names_a_command() -> None:
    named = {
        source.name: hits
        for source in sorted(CHECKS.glob("*.py"))
        if (hits := messages_naming_a_command(source.read_text(encoding="utf-8")))
    }

    assert not named, f"move the command into the hint: {named}"


def test_the_message_scan_sees_each_way_a_message_is_written() -> None:
    """The control: a command in any of these shapes is reported."""
    planted = (
        'Issue(category="x", severity="info", paper_id=None, message="run `lit a`")\n'
        'Issue(category="x", message=(f"{p} " "then `lit b`"), hint="h")\n'
        'Issue("x", "info", None, "see `lit c`", "h")\n'
        'add("x", "info", "p", f"{w} — `lit d`", "h")\n'
        'Issue(category="x", message="plain", hint="`lit e` in a hint is fine")\n'
    )

    assert [command for _, command in messages_naming_a_command(planted)] == [
        "lit a", "lit b", "lit c", "lit d",
    ]

