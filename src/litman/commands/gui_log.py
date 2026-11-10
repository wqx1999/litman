"""``lit gui-log`` — what the litman window could not do, in full.

The window shows a failure as one short line; the person brings it to their
agent. This prints what the GUI server wrote behind that line
(:mod:`litman.core.gui_log`): when, in which library, which request, the code
the page picked its line by, and the full text, which says what to do. An
unexpected failure carries its traceback (``--format json``).

Newest first, the last 7 days, at most 20 entries. Off a terminal the output
is JSON: the log's own objects, as they are.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import click
from rich.console import Console
from rich.markup import escape

from litman.commands._options import format_option
from litman.core.gui_log import entry_time, log_path, read_entries
from litman.core.portable_link import _os_error_reason
from litman.exceptions import LitmanError

console = Console()


def _window_words(days: int) -> str:
    return "the last day" if days == 1 else f"the last {days} days"


def _library_names() -> dict[str, str]:
    """Registered library paths → names, for the table; empty when unreadable."""
    from litman.core.vault_registry import load_registry

    try:
        registry = load_registry()
    except Exception:
        return {}
    names: dict[str, str] = {}
    for entry in registry.vaults:
        names[str(Path(entry.path).expanduser())] = entry.name
        try:
            names[str(Path(entry.path).expanduser().resolve())] = entry.name
        except OSError:
            pass
    return names


def _newest_first(entries: list[dict[str, Any]]) -> list[tuple[datetime, dict[str, Any]]]:
    timed = []
    for order, entry in enumerate(entries):
        at = entry_time(entry)
        if at is not None:
            timed.append((at, order, entry))
    timed.sort(key=lambda item: (item[0], item[1]), reverse=True)
    return [(at, entry) for at, _order, entry in timed]


def _print_entry(at: datetime, entry: dict[str, Any], names: dict[str, str]) -> None:
    vault = entry.get("vault")
    library = names.get(str(vault), str(vault)) if vault else "(no library)"
    head = [f"[bold]{at.astimezone():%Y-%m-%d %H:%M}[/]", escape(library)]
    if entry.get("route"):
        head.append(escape(str(entry["route"])))
    if entry.get("code"):
        head.append(f"[yellow]{escape(str(entry['code']))}[/]")
    elif entry.get("kind") not in (None, "error"):
        head.append(f"[dim]{escape(str(entry['kind']))}[/]")
    console.print("  ".join(head), soft_wrap=True)
    for line in str(entry.get("text") or "").splitlines() or [""]:
        console.print(f"  {escape(line)}", soft_wrap=True, highlight=False)
    if entry.get("traceback"):
        console.print("  [dim]traceback: see --format json[/]")


@click.command("gui-log")
@click.option(
    "--days",
    type=click.IntRange(min=1),
    default=7,
    show_default=True,
    help="How many days back to look.",
)
@click.option(
    "--limit",
    type=click.IntRange(min=1),
    default=20,
    show_default=True,
    help="Show at most this many entries.",
)
@format_option
def gui_log_cmd(days: int, limit: int, output_format: str) -> None:
    """Show what the litman window could not do, newest first.

    Each entry is a failure (or a doubt, such as an SI file the window could
    not confirm) that the GUI showed as one short line: when, in which
    library, the request, the code the page showed, and the full text with
    what to do. Read it first when someone quotes a message from the litman
    window. The log is gui-log.jsonl beside the vault registry, one per
    machine.
    """
    try:
        entries = read_entries()
    except OSError as exc:
        raise LitmanError(
            f"Cannot read {log_path()} ({_os_error_reason(exc)}). "
            "Fix its permissions, then run `lit gui-log` again."
        ) from exc
    ordered = _newest_first(entries)
    since = datetime.now(UTC) - timedelta(days=days)
    shown = [(at, entry) for at, entry in ordered if at >= since][:limit]

    if output_format == "json":
        click.echo(json.dumps([entry for _at, entry in shown], ensure_ascii=False, indent=2))
        return

    if not shown:
        if not ordered:
            console.print("The GUI log is empty: the litman window has reported nothing here.")
            return
        newest = ordered[0][0].astimezone()
        need = (datetime.now(UTC) - newest).days + 1
        console.print(
            f"No entries in {_window_words(days)}; the newest is from "
            f"{newest:%Y-%m-%d}. --days {need} to see it."
        )
        return

    names = _library_names()
    console.print(
        f"[bold]lit gui-log[/] — {_window_words(days)}, newest first "
        f"({len(shown)} shown)"
    )
    for at, entry in shown:
        console.print()
        _print_entry(at, entry, names)
