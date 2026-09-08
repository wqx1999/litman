"""What a project-link rebuild did with hub positions real folders occupied.

``settle_hub_entry`` deletes a folder that matches the vault and preserves one
that does not — a real deletion and a real move, in the user's own directory,
from a command they ran for another reason. Every command that triggers the
rebuild has to say so, and say it the same way: ``lit health-check --fix``,
``lit refresh-views`` and ``lit link --rebuild-all`` all reach the same code
and must not word the same event three different ways.

The GUI's rebuild stays silent by design (the health badge re-runs the checks
instead), and ``lit modify`` / ``lit rename`` reach it through
``reconcile_derived`` without a per-project report block to hang this under.
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
