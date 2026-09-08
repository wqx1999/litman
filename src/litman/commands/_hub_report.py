"""What a project-link rebuild did with hub positions real folders occupied.

``settle_hub_entry`` deletes a folder that matches the vault and preserves one
that does not — a real deletion and a real move, in the user's own directory,
from a command the user ran for something else. Three commands report it, and
report it through here so one event is not worded three ways:
``lit health-check --fix``, ``lit refresh-views``, ``lit link --rebuild-all``.

Other paths reach the same settle and currently say nothing: ``lit link <id>
--project P``, ``lit trash restore``, the code-link reconcile behind ``lit code
add`` / ``lit unlink``, and ``lit modify`` / ``lit rename`` through
``reconcile_derived``. Whether they should is an open question, not a settled
boundary. The GUI's rebuild is the one deliberate exception — it stays silent
and the health badge re-runs the checks instead.
"""

from __future__ import annotations

from collections.abc import Sequence

from rich.markup import escape


def hub_settlement_lines(
    n_replaced: int, aside_paths: Sequence[str]
) -> list[str]:
    """Rich lines describing one project's settled hub positions.

    No leading indent — the caller places them under its own line. Empty when
    nothing was settled, which is every ordinary run.

    "does not match" rather than "differs from": a ``litman_code/<repo>`` copy
    whose clone was never restored here is *absent* from the vault, not
    different from it — and that copy may be the only checkout on the machine.
    """
    lines: list[str] = []
    if n_replaced == 1:
        lines.append("[dim]replaced 1 folder copy with a link[/]")
    elif n_replaced > 1:
        lines.append(
            f"[dim]replaced {n_replaced} folder copies with links[/]"
        )
    kept = list(aside_paths)
    if len(kept) == 1:
        lines.append(
            "[dim]kept 1 folder that does not match the vault: "
            f"{escape(kept[0])}[/]"
        )
    elif len(kept) > 1:
        lines.append(
            f"[dim]kept {len(kept)} folders that do not match the vault:[/]"
        )
        lines.extend(f"  [dim]{escape(p)}[/]" for p in kept)
    return lines
