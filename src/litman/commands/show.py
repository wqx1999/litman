"""``lit show <id>`` — display a single paper's metadata + file paths."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import click
from rich.console import Console
from rich.markup import escape
from rich.panel import Panel
from rich.syntax import Syntax

from litman.commands._options import library_option, vault_option
from litman.core.code import missing_code_clones
from litman.core.document import find_paper
from litman.core.library import find_vault, resolve_library_or_vault
from litman.core.locking import SI_DIRNAME
from litman.core.paper_lookup import (
    complete_paper_id,
    most_recent_paper_id,
    resolve_paper_input,
)
from litman.core.supplementary import (
    SUPPLEMENTARY_FIELD,
    field_problems,
    list_supplementary,
)
from litman.core.sync import humanize_bytes

console = Console()


@click.command("show")
@click.argument(
    "paper_id", required=False, shell_complete=complete_paper_id
)
@click.option(
    "--paper-doi",
    "paper_doi",
    default=None,
    help=(
        "Reverse-lookup the paper by DOI instead of supplying the id. "
        "Mutually exclusive with the positional paper id."
    ),
)
@click.option(
    "--format",
    "output_format",
    type=click.Choice(["table", "json"]),
    default="table",
    help="Output format. 'table' (default) renders a Panel of metadata.yaml + "
    "the PDF, notes, SI and code paths; 'json' emits the FULL metadata dict "
    "(every field, not the INDEX projection) plus a '_paths' object with "
    "those paths, for agent bounded retrieval.",
)
@library_option
@vault_option
def show_cmd(
    paper_id: str | None,
    paper_doi: str | None,
    output_format: str,
    library: Path | None,
    vault_name: str | None,
) -> None:
    """Show one paper's metadata.yaml plus its file paths.

    Below the metadata come the paths of its PDF, notes, SI files (one SI:
    line per file, with its original name and size) and code clones.

    With no argument, shows the paper you engaged with most recently — the
    same paper `lit list --sort recent` puts at the top. The paper id
    otherwise accepts a full id, a unique case-insensitive substring, or
    omit it and pass --paper-doi <DOI> instead. --format json emits the full
    metadata dict (all fields) for agents, plus '_paths': the absolute paths of
    the paper folder, PDF, notes, discussion, SI files and code clones, and a
    'missing' list of those that are not on disk.
    """
    vault = find_vault(resolve_library_or_vault(library, vault_name))

    if not paper_id and not paper_doi:
        # Announced on stderr, not stdout: --format json owns stdout, and a
        # note about how the input was read must not land in the payload.
        # Raises on an empty vault.
        paper_id = most_recent_paper_id(vault)
        Console(stderr=True).print(
            f"[dim]No paper given — showing the most recently engaged: "
            f"{paper_id}[/]"
        )

    paper_id = resolve_paper_input(vault, paper_id, paper_doi)

    meta = find_paper(vault, paper_id)

    if output_format == "json":
        # default=str bridges the YAML safe-loader's datetime (created-at /
        # updated-at) and date (read-date / last-revisited) values, which
        # json.dumps cannot serialize natively — without it a paper WITH a
        # read-date raises TypeError (the M25/M31 trap, spec §9).
        payload = {**meta, "_paths": _paths_block(vault, paper_id, meta)}
        click.echo(
            json.dumps(payload, default=str, ensure_ascii=False, indent=2)
        )
        return

    paper_dir = vault / "papers" / paper_id
    meta_file = paper_dir / "metadata.yaml"
    pdf_file = paper_dir / "paper.pdf"
    notes_file = paper_dir / "notes.md"

    yaml_text = meta_file.read_text(encoding="utf-8")

    console.print(
        Panel(
            Syntax(
                yaml_text,
                "yaml",
                theme="monokai",
                background_color="default",
                line_numbers=False,
            ),
            title=f"[bold cyan]{paper_id}[/]",
            border_style="cyan",
        )
    )

    pdf_status = "" if pdf_file.is_file() else "  [bold red](missing!)[/]"
    notes_status = "" if notes_file.is_file() else "  [bold red](missing!)[/]"
    # soft_wrap: a non-TTY console (an agent's) is 80 columns wide and would
    # fold the path in two (commands/_path_lines.py).
    console.print(f"[dim]PDF:  [/] {escape(str(pdf_file))}{pdf_status}", soft_wrap=True)
    console.print(
        f"[dim]Notes:[/] {escape(str(notes_file))}{notes_status}", soft_wrap=True
    )

    # SI: one line per recorded file — path, the name it was added under, and
    # its size — with the same (missing!) marker. A malformed field lists what
    # still parses and says so, rather than hiding the rest.
    si_dir = paper_dir / SI_DIRNAME
    for record in list_supplementary(meta):
        si_file = si_dir / record["file"]
        try:
            si_status = "" if si_file.is_file() else "  [bold red](missing!)[/]"
        except OSError:
            si_status = "  [bold red](unreadable!)[/]"
        details = []
        if record.get("original"):
            details.append(escape(str(record["original"])))
        if isinstance(record.get("bytes"), int):
            details.append(humanize_bytes(record["bytes"]))
        detail = f"  [dim]({', '.join(details)})[/]" if details else ""
        console.print(
            f"[dim]SI:   [/] {escape(str(si_file))}{detail}{si_status}",
            soft_wrap=True,
        )
    if field_problems(meta.get(SUPPLEMENTARY_FIELD)):
        console.print(
            "[dim]SI:   [/] [yellow]'supplementary' in metadata.yaml is "
            "malformed — `lit health-check` says how[/]"
        )

    # Code-clones: one line per bound repo, with the same (missing!) marker used
    # for PDF/Notes when codes/<name>/ is gone (the dangling-link case lit
    # health-check flags under invariant #12). The link is kept by design (it
    # records the re-clone target); this only stops it from reading as live.
    code_clones = meta.get("code-clones") or []
    if code_clones:
        gone = set(missing_code_clones(vault, code_clones))
        codes_dir = vault / "codes"
        for name in code_clones:
            status = "  [bold red](missing!)[/]" if name in gone else ""
            console.print(
                f"[dim]Code: [/] {escape(str(codes_dir / name))}{status}",
                soft_wrap=True,
            )


def _paths_block(vault: Path, paper_id: str, meta: dict[str, Any]) -> dict[str, Any]:
    """Where the paper's files are: ``_paths`` in ``lit show --format json``.

    What an agent opens or appends to next, so it never assembles
    ``<vault>/papers/<id>/...`` itself. The leading underscore keeps the key
    apart from metadata fields. ``si`` follows the SI records' order and
    ``code`` the ``code-clones`` list; ``missing`` names every path among them
    that is not on disk (the table's ``(missing!)``).
    """
    paper_dir = vault / "papers" / paper_id
    pdf = paper_dir / "paper.pdf"
    notes = paper_dir / "notes.md"
    discussion = paper_dir / "discussion.md"
    si_dir = paper_dir / SI_DIRNAME
    si = [si_dir / record["file"] for record in list_supplementary(meta)]
    # find_paper has already refused a code-clones that is not a list.
    names = [n for n in meta.get("code-clones") or [] if isinstance(n, str) and n]
    gone = set(missing_code_clones(vault, names))
    code = [vault / "codes" / name for name in names]

    missing: list[str] = []
    for path in (pdf, notes, discussion, *si):
        try:
            present = path.is_file()
        except OSError:
            present = False
        if not present:
            missing.append(str(path))
    missing += [str(vault / "codes" / name) for name in names if name in gone]
    return {
        "folder": str(paper_dir),
        "pdf": str(pdf),
        "notes": str(notes),
        "discussion": str(discussion),
        "si": [str(path) for path in si],
        "code": [str(path) for path in code],
        "missing": missing,
    }
