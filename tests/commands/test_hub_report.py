"""Every command that reports settled hub folders prints them soft-wrapped.

``hub_settlement_lines`` (``commands/_hub_report``) hands back plain strings;
the indent and the printing are each caller's own job, and seven commands do
it themselves. The kept-folder line ends in an absolute path the user is meant
to copy out of the terminal, so each of those prints has to pass
``soft_wrap=True``: rich's own wrapping puts a real newline inside the path at
80 columns, and the pasted result points nowhere.

That the flag keeps the path in one piece is covered end to end in
``test_health.py`` (``..._copyable_at_80_columns``). What one command's test
cannot cover is the other six, or the eighth site somebody adds later — so the
sweep below reads the sources instead of running them.

Out of scope on purpose: ``lit link``'s other call site feeds its lines into a
``Panel``, which wraps its own content and takes no ``soft_wrap``. That path
folds the path the same way and needs a different fix, not this flag.
"""

from __future__ import annotations

import ast
from pathlib import Path

import litman.commands

# The commands that print the lines themselves. Kept explicit so a command
# that silently stops reporting shows up here rather than nowhere: if this
# list changes, `commands/_hub_report`'s module docstring enumerates the same
# set and has to change with it.
_REPORTING_MODULES = {
    "health.py",
    "link.py",
    "modify.py",
    "project.py",
    "refresh.py",
    "rename.py",
    "trash.py",
}


def _settlement_prints(tree: ast.Module) -> list[ast.Call]:
    """Every ``*.print(...)`` inside a ``for … in hub_settlement_lines(…)``."""
    calls: list[ast.Call] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.For):
            continue
        iterated = node.iter
        if not (
            isinstance(iterated, ast.Call)
            and isinstance(iterated.func, ast.Name)
            and iterated.func.id == "hub_settlement_lines"
        ):
            continue
        calls.extend(
            inner
            for inner in ast.walk(node)
            if isinstance(inner, ast.Call)
            and isinstance(inner.func, ast.Attribute)
            and inner.func.attr == "print"
        )
    return calls


def _passes_soft_wrap(call: ast.Call) -> bool:
    return any(
        kw.arg == "soft_wrap"
        and isinstance(kw.value, ast.Constant)
        and kw.value.value is True
        for kw in call.keywords
    )


def test_every_hub_settlement_print_is_soft_wrapped() -> None:
    found: dict[str, list[tuple[int, bool]]] = {}
    for path in sorted(Path(litman.commands.__file__).parent.glob("*.py")):
        prints = _settlement_prints(ast.parse(path.read_text(encoding="utf-8")))
        if prints:
            found[path.name] = [(c.lineno, _passes_soft_wrap(c)) for c in prints]

    # Control: without this the sweep passes by finding nothing, which is
    # exactly what a change to the loop's shape would produce.
    assert set(found) == _REPORTING_MODULES, (
        "the set of commands printing hub settlement lines moved — update "
        "_REPORTING_MODULES and commands/_hub_report's docstring together"
    )

    unwrapped = [
        f"{name}:{lineno}"
        for name, sites in found.items()
        for lineno, ok in sites
        if not ok
    ]
    assert not unwrapped, (
        "hub settlement lines printed without soft_wrap=True — the embedded "
        f"path gets a hard newline at 80 columns: {sorted(unwrapped)}"
    )
