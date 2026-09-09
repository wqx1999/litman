"""What a project-link rebuild did with hub positions real folders occupied.

``settle_hub_entry`` deletes a folder that matches the vault and preserves one
that does not. The deletion is silent on purpose — the original is in the vault,
nothing is lost. The preservation is not: that folder is the ONLY copy of what
was in it (a note written on the other machine, the one git checkout on this
one), and relocating it under ``.trash/`` without saying so is not acceptable
whichever command happened to trigger the rebuild.

So every command that can move one reports it, and reports it through here so
one event is not worded eight ways: ``lit health-check --fix``,
``lit refresh-views`` and ``lit link --rebuild-all`` (which also report the
replaced copies, being the commands you run *to* repair the hubs), plus
``lit link <id> --project P``, ``lit trash restore``, ``lit project rename``,
``lit modify`` and ``lit rename`` (move-asides only).

Two paths stay silent. The GUI's rebuild by design — the health badge re-runs
the checks instead (decision #9). And ``reconcile_project_code_links``, behind
``lit code add`` / ``lit unlink``, which has no per-project report block of its
own; `lit health-check` surfaces anything it parked.
"""

from __future__ import annotations

from collections.abc import Sequence

from rich.markup import escape


def hub_settlement_lines(
    n_replaced: int, aside_paths: Sequence[str]
) -> list[str]:
    """Rich lines describing one project's settled hub positions.

    No leading indent — the caller places them under its own line, and prints
    them with ``soft_wrap=True``: the kept-folder line ends in an absolute
    path meant to be copied straight out of the terminal, and rich's own
    wrapping puts a real newline inside it at 80 columns. Empty when nothing
    was settled, which is every ordinary run.

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
