"""What a success panel cannot hold: paths, and anything meant to be pasted.

A rich ``Panel`` wraps its own content to the terminal and takes no
``soft_wrap``. At 80 columns — and for an agent, whose non-TTY console is 80
columns wide — it folded every absolute path, URL and command inside it, with
the border character landing in the middle, so nothing in the box could be
copied out whole. ``lit add``, ``lit code add``, ``lit init`` and ``lit link``
therefore keep the summary in the panel and print these lines underneath, each
soft-wrapped (the same contract as ``commands/_hub_report``).
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence

from rich.console import Console
from rich.markup import escape


def path_lines(rows: Sequence[tuple[str, object]]) -> list[str]:
    """``label  path`` lines, labels padded to one column, paths escaped."""
    width = max(len(label) for label, _ in rows)
    return [
        f"  [dim]{label:<{width}}[/] {escape(str(path))}" for label, path in rows
    ]


def print_unwrapped(console: Console, lines: Iterable[str]) -> None:
    for line in lines:
        console.print(line, soft_wrap=True)
