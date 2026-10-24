"""``lit open <id>`` — resolve id and launch a PDF viewer.

Thin wrapper: resolves the paper id (exact or substring match), validates
``paper.pdf`` exists, dispatches to a viewer per ``core.viewer``. No state
file, no skill hook — the rationale lives in ADR-004 (lit-open-no-state).
``--si NAME`` opens one of the paper's SI files instead, with this machine's
default app.

The viewer is waited on only long enough to hear it fail
(``core.viewer.OPEN_WAIT_S``), so the user's shell isn't blocked waiting for
the GUI viewer to close.

Exit codes:
    0 — viewer launched.
    1 — paper resolve failure (id not found, multiple matches, pdf missing);
        with --si, no such SI file, or one the default app would run (path
        printed).
    2 — viewer launch failure (no usable viewer, or no app took the file;
        with --si, also an SSH session); path printed for manual handling so
        the shell pipe / scripts can distinguish "paper missing" from "you
        don't have a viewer".

The ``Opened`` line and the ``error:`` lines this command prints itself are
soft-wrapped, and path lines unwrapped (``commands/_path_lines``): piped, the
console is 80 columns wide, and a message or a path folded across lines reads
as two.
"""

from __future__ import annotations

import sys
from pathlib import Path

import click
from rich.console import Console
from rich.markup import escape

from litman.commands._options import library_option, vault_option
from litman.commands._path_lines import print_unwrapped
from litman.core.config import load_config
from litman.core.library import find_vault, resolve_library_or_vault
from litman.core.paper_lookup import (
    complete_paper_id,
    find_paper_id_by_doi,
    most_recent_paper_id,
)
from litman.core.viewer import (
    is_ssh_session,
    launch_file,
    launch_pdf,
    resolve_paper_id,
)
from litman.exceptions import (
    AmbiguousPaperIdError,
    LitmanError,
    PaperNotFoundError,
)

console = Console()


def _open_si(vault: Path, paper_id: str, name: str) -> None:
    """``lit open PAPER --si NAME``: one recorded SI file, in the default app.

    A PDF goes to ``default_pdf_viewer`` when one is set, as the paper's PDF
    does — that viewer may well work without a desktop of this machine's own
    (``ssh -X``). Anything else is refused up front in an SSH session, where
    it would reach no screen in front of the user; elsewhere
    :func:`~litman.core.viewer.launch_file` names what is missing (a display,
    an opener). A file the default app would run is never handed to it.
    """
    from litman.core import supplementary
    from litman.core.document import find_paper
    from litman.exceptions import SupplementaryError

    meta = find_paper(vault, paper_id)
    records = supplementary.list_supplementary(meta)
    if not records:
        console.print(
            f"[bold red]error:[/] {escape(paper_id)} has no SI files. "
            f"Run lit add --si {escape(paper_id)} to add them.",
            soft_wrap=True,
        )
        sys.exit(1)
    try:
        (record,) = supplementary.resolve_si_records(paper_id, meta, [name])
    except SupplementaryError:
        console.print(
            f"[bold red]error:[/] {escape(paper_id)} has no SI file "
            f"{escape(repr(name))}. Its SI files:",
            soft_wrap=True,
        )
        for rec in records:
            label = supplementary.original_label(rec) or "?"
            console.print(f"  {escape(rec['file'])} ({escape(label)})", soft_wrap=True)
        sys.exit(1)

    file = record["file"]
    path, _record = supplementary.recorded_si_file(vault, paper_id, file)
    path_line = [f"[dim]SI path:[/] {escape(str(path))}"]
    if supplementary.is_runnable_si(file):
        console.print(
            f"[bold red]error:[/] {escape(file)} could be run, not viewed.",
            soft_wrap=True,
        )
        print_unwrapped(console, path_line)
        sys.exit(1)
    configured = load_config(vault).default_pdf_viewer if file.endswith(".pdf") else None
    if not configured and is_ssh_session():
        console.print(
            f"[bold red]error:[/] This session has no desktop to open {escape(file)} on.",
            soft_wrap=True,
        )
        print_unwrapped(console, path_line)
        sys.exit(2)
    try:
        cmd, source = launch_file(path, configured)
    except FileNotFoundError as e:
        console.print(f"[bold red]error:[/] {escape(str(e))}", soft_wrap=True)
        print_unwrapped(console, path_line)
        sys.exit(2)
    console.print(
        f"[green]Opened[/] [bold]{escape(paper_id)} {escape(file)}[/] "
        f"[dim](viewer: {escape(cmd)}, {source})[/]",
        soft_wrap=True,
    )


@click.command("open")
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
    "--si",
    "si_name",
    default=None,
    metavar="NAME",
    help=(
        "Open one of the paper's SI files (si-02, or si-02.xlsx) instead of "
        "its PDF, in this machine's default app."
    ),
)
@library_option
@vault_option
def open_cmd(
    paper_id: str | None,
    paper_doi: str | None,
    si_name: str | None,
    library: Path | None,
    vault_name: str | None,
) -> None:
    """Open a paper's PDF in the configured (or platform default) viewer.

    With no argument, opens the paper you engaged with most recently — the
    same paper `lit list --sort recent` puts at the top. The paper id
    otherwise accepts a full id, a unique case-insensitive substring, or
    omit it and pass --paper-doi <DOI> instead. Multiple substring matches
    print the candidate list and exit 1 so the user can re-run with a more
    specific id. With --si NAME, opens one of the paper's SI files instead
    (`lit show` lists them).
    """
    vault = find_vault(resolve_library_or_vault(library, vault_name))

    has_id = paper_id is not None and paper_id != ""
    has_doi = paper_doi is not None and paper_doi != ""
    if has_id and has_doi:
        raise LitmanError(
            "--paper-doi and the paper-id input are mutually exclusive. "
            "Pass one or the other, not both."
        )
    if not has_id and not has_doi:
        # Coming back to the paper you just closed should not cost you its id.
        # Announced on stderr, because it reports how the input was read rather
        # than being part of the command's output. Raises on an empty vault.
        resolved_id = most_recent_paper_id(vault)
        Console(stderr=True).print(
            f"[dim]No paper given — opening the most recently engaged: "
            f"{escape(resolved_id)}[/]"
        )
    elif has_doi:
        assert paper_doi is not None
        resolved_id = find_paper_id_by_doi(vault, paper_doi)
    else:
        assert paper_id is not None
        try:
            resolved_id = resolve_paper_id(vault, paper_id)
        except AmbiguousPaperIdError as e:
            console.print(
                f"[bold yellow]Ambiguous id[/] '{escape(paper_id)}' — "
                f"{len(e.candidates)} matches:"
            )
            for candidate in e.candidates:
                console.print(f"  - {escape(candidate)}")
            console.print(
                "[dim]Re-run `lit open` with a longer / exact id.[/]"
            )
            sys.exit(1)

    if si_name is not None:
        _open_si(vault, resolved_id, si_name)
        return

    pdf_path = vault / "papers" / resolved_id / "paper.pdf"
    if not pdf_path.is_file():
        raise PaperNotFoundError(
            f"Paper {resolved_id!r} has no paper.pdf at {pdf_path}."
        )

    config = load_config(vault)
    try:
        cmd, source = launch_pdf(pdf_path, config.default_pdf_viewer)
    except FileNotFoundError as e:
        console.print(f"[bold red]error:[/] {escape(str(e))}", soft_wrap=True)
        print_unwrapped(console, [f"[dim]PDF path:[/] {escape(str(pdf_path))}"])
        sys.exit(2)

    console.print(
        f"[green]Opened[/] [bold]{escape(resolved_id)}[/] "
        f"[dim](viewer: {escape(cmd)}, {source})[/]",
        soft_wrap=True,
    )
