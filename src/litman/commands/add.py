"""``lit add`` — import a paper into the literature vault.

Pipeline (M2.9 + M4.1):
    1. Fetch metadata via the chosen importer:
       - ``--doi``: CrossRef API
       - ``--from-llm-json <path>``: LLM-prepared JSON file (M4.1)
    2. **DOI precheck**: refuse if the DOI already exists anywhere in
       ``papers/*/metadata.yaml`` (live scan, case-insensitive).
    3. Derive the canonical id (M2.9 upgraded keyword heuristic) or accept
       ``--id`` override.
    4. **Id collision resolution**: if ``papers/<id>/`` exists, either
       prompt the user (TTY) or auto-suffix ``_b`` / ``_c`` / ... when
       ``--auto-suffix`` is passed.
    5. Create ``papers/<id>/``, atomically populated with::
       paper.pdf
       metadata.yaml
       notes.md         (placeholder)
       discussion.md    (empty log + its format reminder)
    6. Remove the source PDF (``mv`` semantics — file disappearing from the
       source dir is the user-visible success signal). The atomic block uses
       ``copy2`` so a mid-write failure does not strand the original; the
       ``unlink`` happens only once the vault is consistent.
    7. Print a summary panel.
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path
from typing import Any

import click
from rich.console import Console
from rich.markup import escape
from rich.panel import Panel

from litman.core.code_scan import scan_code_urls
from litman.core.correctors import reconcile_derived
from litman.core.dates import now_iso
from litman.core.dedup import (
    auto_suffix_id,
    canonicalize_doi,
    find_paper_by_doi,
    suggest_alternative_ids,
)
from litman.core.id import derive_id, find_case_fold_collision, is_valid_id
from litman.core.library import find_vault, resolve_library_or_vault
from litman.core.locking import lock_truth_file, rmtree
from litman.core.notes import WIKILINK_REMINDER, discussion_scaffold
from litman.core.yaml_pool import ThreadLocalYAML
from litman.exceptions import AddError, DuplicateDOIError, IDError
from litman.importers.crossref import fetch_crossref, parse_crossref
from litman.importers.llm import parse_llm_json, parse_llm_json_text

console = Console()

_yaml = ThreadLocalYAML(
    indent={"mapping": 2, "sequence": 4, "offset": 2},
    default_flow_style=False,
    preserve_quotes=True,
)

# PDF magic number. A valid PDF opens with "%PDF-"; readers tolerate a little
# junk before it, so sniff the first block rather than demanding offset 0.
_PDF_MAGIC = b"%PDF-"
_PDF_SNIFF_BYTES = 1024


def _looks_like_pdf(path: Path) -> bool:
    """True if the file carries the PDF magic number in its first block.

    `lit add` copies the PDF into the vault and then unlinks the source (mv
    semantics), so ingesting a non-PDF — a renamed .txt, a truncated download,
    an HTML error page saved as .pdf — would land junk in the vault AND remove
    the only copy of the source. This guard runs before any vault write so the
    source stays put on rejection. An unreadable file counts as "not a PDF";
    the caller turns a False into a friendly error.
    """
    try:
        with path.open("rb") as f:
            head = f.read(_PDF_SNIFF_BYTES)
    except OSError:
        return False
    return _PDF_MAGIC in head


def _build_metadata(
    parsed: dict[str, Any],
    paper_id: str,
    now: str | None = None,
) -> dict[str, Any]:
    """Assemble the full metadata.yaml dict in design-doc field order.

    Schema-less by intent (§7.3): unknown-yet fields are emitted as ``None`` /
    ``[]`` so the user can fill them later with ``lit modify``.

    The audit fields ``created-at`` and ``updated-at`` are technical (machine-
    maintained); ``read-date`` and ``last-revisited`` are semantic (user-set).
    Never merge the two.
    """
    timestamp = now or now_iso()
    return {
        # === identity layer (auto from CrossRef) ===
        "id": paper_id,
        "title": parsed.get("title", ""),
        "authors": parsed.get("authors", []),
        "year": parsed.get("year"),
        "journal": parsed.get("journal", ""),
        "doi": parsed.get("doi", ""),
        "arxiv-id": parsed.get("arxiv-id"),
        "github": None,
        # M12.0 bib-oriented fields. Schema-less: empty string = "not
        # applicable to this paper" (a preprint typically has no volume /
        # pages; a book chapter has no journal). The exporter drops
        # empty fields rather than emitting `volume = {}`.
        "volume": parsed.get("volume", "") or "",
        "issue": parsed.get("issue", "") or "",
        "pages": parsed.get("pages", "") or "",
        "publisher": parsed.get("publisher", "") or "",
        "venue-type": parsed.get("venue-type", "") or "",
        "booktitle": parsed.get("booktitle", "") or "",
        # === audit layer (machine-maintained) ===
        "created-at": timestamp,
        "updated-at": timestamp,
        # === classification layer (TAXONOMY-controlled, M2 validates) ===
        "projects": [],
        "topics": [],
        "methods": [],
        "data": [],
        "type": None,
        # === personal evaluation layer ===
        "status": "inbox",
        "priority": None,
        "read-date": None,
        "last-revisited": None,
        # === relations layer ===
        "related": [],
        "contradicts": [],
        "contradicted-by": [],
        "extends": [],
        "extended-by": [],
        "code-clones": [],
    }


def _first_author_family(authors: list[str]) -> str:
    """Extract the family name from the first 'Family, Given' author string."""
    if not authors:
        return ""
    return authors[0].split(",", 1)[0].strip()


def _refuse_doi_duplicate(doi: str, existing_id: str, meta: dict[str, Any]) -> None:
    """Raise a friendly DuplicateDOIError describing the existing paper."""
    title = meta.get("title") or "(no title)"
    added = meta.get("created-at") or "(no timestamp)"
    raise DuplicateDOIError(
        f"DOI {doi!r} already registered in this vault:\n"
        f"  id:    {existing_id}\n"
        f"  title: {title}\n"
        f"  added: {added}\n"
        f"\n"
        f"Next steps:\n"
        f"  lit show {existing_id}                       # inspect\n"
        f"  lit rm {existing_id} && lit add <pdf> --doi {doi}   # replace"
    )


def _resolve_collision(
    vault: Path,
    primary_id: str,
    year: int,
    family: str,
    title: str,
    auto_suffix: bool,
) -> str:
    """Resolve an id collision by auto-suffix or interactive prompt.

    Returns the chosen id (guaranteed not to collide). Raises AddError if
    the user cancels, supplies an invalid custom id, or stdin is non-TTY
    without ``--auto-suffix``.
    """
    if auto_suffix:
        return auto_suffix_id(vault, primary_id)

    if not sys.stdin.isatty():
        raise AddError(
            f"Paper id {primary_id!r} already exists at "
            f"{vault / 'papers' / primary_id} and stdin is not a TTY. "
            "Pass --auto-suffix for batch mode or --id <new-id> to specify manually."
        )

    alternatives = suggest_alternative_ids(vault, primary_id, year, family, title)
    fallback_id = auto_suffix_id(vault, primary_id)

    console.print(
        f"\n[yellow]Paper id [bold]{primary_id}[/] already exists.[/yellow]"
    )
    console.print("Pick one of:")
    menu: list[tuple[str, str]] = []
    for alt in alternatives:
        menu.append(("alt", alt))
    menu.append(("suffix", fallback_id))
    menu.append(("custom", "<enter a custom id>"))
    menu.append(("cancel", "<cancel>"))

    for idx, (_kind, label) in enumerate(menu, 1):
        console.print(f"  [{idx}] {label}")

    valid_choices = [str(i) for i in range(1, len(menu) + 1)]
    suffix_idx = len(alternatives) + 1  # default = auto-suffix entry
    choice = click.prompt(
        "Choice",
        type=click.Choice(valid_choices),
        default=str(suffix_idx),
        show_choices=False,
    )
    kind, label = menu[int(choice) - 1]
    if kind == "alt" or kind == "suffix":
        return label
    if kind == "custom":
        custom = click.prompt("Enter custom id", type=str).strip()
        if not is_valid_id(custom):
            raise AddError(f"Invalid id format: {custom!r}")
        if (vault / "papers" / custom).exists():
            raise AddError(f"Id {custom!r} also already exists.")
        return custom
    raise AddError("Cancelled by user.")


def _validate_id_override(
    ctx: click.Context, param: click.Parameter, value: str | None
) -> str | None:
    """Click callback: reject a malformed ``--id`` during argument parsing.

    Review F23: ``--id ../../escape`` (or any value with a slash / ``..`` /
    leading dot) used to be written verbatim as the paper folder, escaping the
    vault and then deleting the source PDF. The interactive custom-id branch
    already validated; the flag did not. A callback fails before the command
    body runs, so the bad id can never reach the materialize-and-delete path.
    """
    if value is not None and not is_valid_id(value):
        raise click.BadParameter(
            f"Invalid id {value!r}. Ids contain only ASCII letters, digits, "
            "dots, underscores, and hyphens; no leading dot, no slashes, "
            "no '..'.",
            ctx=ctx,
            param=param,
        )
    return value


@click.command("add")
@click.argument(
    "pdf_path",
    type=click.Path(exists=True, file_okay=True, dir_okay=False, path_type=Path),
)
@click.option(
    "--doi",
    default=None,
    help=(
        "DOI of the paper. Fetched from CrossRef. Mutually exclusive with "
        "--from-llm-json; exactly one must be provided."
    ),
)
@click.option(
    "--from-llm-json",
    "from_llm_json",
    type=click.Path(
        exists=True, file_okay=True, dir_okay=False, allow_dash=True, path_type=Path
    ),
    default=None,
    help=(
        "LLM-prepared metadata JSON (see "
        "litman.importers.llm.LLMCandidateMeta for the schema). Pass a file "
        "path, or '-' to read the JSON from stdin (no temp file). Used by "
        "the lit-library Claude Code skill. Mutually exclusive with --doi."
    ),
)
@click.option(
    "--id",
    "id_override",
    default=None,
    callback=_validate_id_override,
    help="Override the auto-derived id (format: <year>_<Family>_<Keyword>).",
)
@click.option(
    "--auto-suffix",
    is_flag=True,
    default=False,
    help=(
        "On id collision, auto-append _b / _c / ... without prompting. "
        "Required for non-interactive (non-TTY) batch use."
    ),
)
@click.option(
    "--library",
    type=click.Path(file_okay=False, dir_okay=True, path_type=Path),
    default=None,
    envvar="LIT_LIBRARY",
    help="Override the active vault. Discovery order: this flag / $LIT_LIBRARY, then the active registered vault, then cwd-walk.",
)
@click.option(
    "--vault",
    "vault_name",
    default=None,
    help=(
        "Vault name from ~/.config/litman/vaults.yaml. "
        "Mutually exclusive with --library."
    ),
)
def add_cmd(
    pdf_path: Path,
    doi: str | None,
    from_llm_json: Path | None,
    id_override: str | None,
    auto_suffix: bool,
    library: Path | None,
    vault_name: str | None,
) -> None:
    """Import a paper PDF into the vault.

    Source of metadata: either --doi (CrossRef fetch) or
    --from-llm-json <path> (LLM-prepared JSON file). Exactly one is
    required. Refuses on duplicate DOI, derives a canonical id, and
    creates papers/<id>/ containing paper.pdf, metadata.yaml, an empty
    notes.md, and an empty discussion.md.

    The source PDF is moved into the vault: the original file is removed
    after a successful ingest.
    """
    if (doi is None) == (from_llm_json is None):
        raise AddError(
            "Provide exactly one of --doi <doi> or --from-llm-json <path>. "
            "These flags are mutually exclusive."
        )

    # Validate the file is a PDF before any network fetch or vault write. Both
    # ingest paths (--doi / --from-llm-json) pass through here, and the source
    # is unlinked on success, so a non-PDF must fail while the source is still
    # untouched — never after the vault has been written or the source removed.
    if not _looks_like_pdf(pdf_path):
        raise AddError(
            f"{str(pdf_path)!r} does not look like a PDF (missing the %PDF- "
            "header). Pass the paper's PDF file."
        )

    vault = find_vault(resolve_library_or_vault(library, vault_name))

    from_stdin = from_llm_json is not None and str(from_llm_json) == "-"
    if from_llm_json is not None:
        if from_stdin:
            # click.get_text_stream forces UTF-8 regardless of the Windows
            # legacy code page, and is CliRunner-aware for tests. Pairs with
            # the existing UTF-8 stdout/stderr forcing.
            stdin_stream = click.get_text_stream("stdin", encoding="utf-8")
            # Guard the human-misuse case: `--from-llm-json -` with nothing
            # piped would block forever on .read() waiting for EOF. The skill
            # always pipes, so a TTY here means someone typed the flag by hand.
            if stdin_stream.isatty():
                raise AddError(
                    "--from-llm-json - expects the metadata JSON piped on "
                    "stdin, but stdin is a terminal (nothing piped). Pipe the "
                    "JSON in (e.g. '{...}' | lit add ... --from-llm-json -) "
                    "or pass a file path instead."
                )
            raw_text = stdin_stream.read()
            parsed = parse_llm_json_text(raw_text, source="<stdin>")
        else:
            parsed = parse_llm_json(from_llm_json)
        # The DOI used for dedup precheck and user-facing error messages
        # comes from the JSON; an LLM-imported paper without a DOI just
        # skips the precheck (id collision is still enforced below).
        doi_for_dedup = parsed.get("doi") or ""
    else:
        assert doi is not None  # type narrowing for mypy
        # Canonicalize before the CrossRef fetch: a URL-form / `doi:` DOI in
        # the path segment 404s (review F11), and the bare form is what we
        # want for the dedup fallback below (review F10).
        doi = canonicalize_doi(doi)
        raw = fetch_crossref(doi)
        parsed = parse_crossref(raw)
        doi_for_dedup = parsed.get("doi") or doi

    # Layer 1: DOI precheck. Refuse before id derivation — a true duplicate
    # never gets to write anything. Skipped only when no DOI is available
    # (LLM path with doi=null); the user accepted that risk by choosing the
    # LLM path with no DOI.
    if doi_for_dedup:
        existing = find_paper_by_doi(vault, doi_for_dedup)
        if existing is not None:
            _refuse_doi_duplicate(doi_for_dedup, existing[0], existing[1])

    source_label = (
        f"DOI {doi!r}" if doi is not None
        else "LLM JSON (stdin)" if from_stdin
        else f"LLM JSON {str(from_llm_json)!r}"
    )

    if id_override:
        # Already shape-validated by the _validate_id_override callback during
        # parsing (review F23), so it is a safe single-segment folder name here.
        paper_id = id_override
        year = parsed.get("year")
        family = _first_author_family(parsed.get("authors", []))
    else:
        if parsed["year"] is None:
            raise IDError(
                f"Metadata from {source_label} has no year; "
                "pass --id explicitly."
            )
        family_raw = _first_author_family(parsed["authors"])
        if not family_raw:
            raise IDError(
                f"Metadata from {source_label} has no first-author "
                "family name; pass --id explicitly."
            )
        paper_id = derive_id(parsed["year"], family_raw, parsed["title"])
        year = parsed["year"]
        family = paper_id.split("_")[1]  # already-slugged & capitalized

    # Layer 3: id collision resolution. The --id override gets the same
    # treatment so batch scripts behave predictably; if you explicitly passed
    # --id you almost certainly want the failure to be loud and immediate
    # rather than silently auto-suffixing.
    paper_dir = vault / "papers" / paper_id
    if paper_dir.exists():
        if id_override:
            raise AddError(
                f"Paper folder already exists: {paper_dir}. "
                "Use a different --id or remove the existing folder first."
            )
        # Reaching here means the non-override path, where year is guarded
        # non-None above (371); narrow int|None -> int for _resolve_collision.
        assert year is not None
        paper_id = _resolve_collision(
            vault,
            paper_id,
            year,
            family,
            parsed["title"],
            auto_suffix,
        )
        paper_dir = vault / "papers" / paper_id

    # Cross-platform safety (ADR-005): refuse ids that differ only in case
    # from an existing paper. ``paper_dir.exists()`` above is case-sensitive
    # on Linux, so an id like ``2023_pandi_X`` slips past when
    # ``2023_Pandi_X/`` is on disk; moving the vault to Windows / default
    # macOS then collapses the two and silently loses one paper.
    papers_root = vault / "papers"
    if papers_root.is_dir():
        existing_ids = [
            d.name for d in papers_root.iterdir() if d.is_dir()
        ]
        case_clash = find_case_fold_collision(existing_ids, paper_id)
        if case_clash is not None:
            raise AddError(
                f"Paper id {paper_id!r} differs only in case from existing "
                f"paper {case_clash!r}. Two ids that case-fold to the same "
                "string collide on Windows / default macOS filesystems "
                "(case-insensitive) and the vault loses data when moved "
                "between OSes. Pass --id <substantially-different-name> "
                "to pick a distinct id."
            )

    # Atomic creation: any failure rolls back the half-built folder.
    # The source PDF is copied (not moved) inside the atomic block so that a
    # mid-write failure leaves the original intact; the unlink runs after the
    # block once the vault is known-consistent.
    try:
        paper_dir.mkdir(parents=True)
        with (paper_dir / "metadata.yaml").open("w", encoding="utf-8") as f:
            _yaml.dump(_build_metadata(parsed, paper_id), f)
        (paper_dir / "notes.md").write_text(
            f"# {parsed['title']}\n\n"
            f"{WIKILINK_REMINDER}\n\n"
            "(Personal notes go here.)\n",
            encoding="utf-8",
        )
        # The discussion log starts empty but not absent: its header carries the
        # append-format contract every writer reads before adding a section, and
        # a paper folder whose file set never varies is one less special case in
        # the Web UI, the skills, and health-check.
        (paper_dir / "discussion.md").write_text(
            discussion_scaffold(paper_id), encoding="utf-8"
        )
        shutil.copy2(pdf_path, paper_dir / "paper.pdf")
        # Read-only lock the two new TRUTH files (M32). These are fresh
        # creates (the dir did not pre-exist) so the writes above succeed; we
        # only chmod after. notes.md is intentionally left writable. Inside the
        # rollback try so a later failure still rmtree's the whole dir.
        lock_truth_file(paper_dir / "metadata.yaml")
        lock_truth_file(paper_dir / "paper.pdf")
    except Exception:
        if paper_dir.exists():
            # locking.rmtree (not bare shutil): metadata.yaml / paper.pdf may
            # already be chmod'd read-only above, which Windows os.unlink refuses
            # — clear the bit via onexc so the half-built dir is fully removed.
            rmtree(paper_dir, ignore_errors=True)
        raise

    # Vault is consistent: the new paper dir is fully committed (the rollback
    # try/except above either finished cleanly or rmtree'd a half-built dir).
    # Reconcile the derived artifacts through the single shared funnel (M30
    # Phase 4) so the paper is in INDEX.json + views/ immediately — `add`
    # previously indexed NOTHING (a pre-existing lag bug: a freshly-added paper
    # was absent from INDEX until the next write command or `lit refresh`).
    # Placed OUTSIDE the rollback try: the paper is a valid, committed truth, so
    # a derived-rebuild failure must NOT rmtree it — INDEX merely lags and is
    # recoverable via `lit refresh` (same post-commit best-effort semantics as
    # modify/rename). project_refs=False: a freshly-added paper has an empty
    # `projects` list, so there are no project symlinks / REFERENCES.md to
    # rebuild.
    #
    # The call is wrapped (review F25): a rebuild failure (e.g. an OSError
    # writing views/ on a flaky mount) must not crash `lit add` with a raw
    # traceback after the paper is already committed, nor skip the source-PDF
    # cleanup below — that would strand the source (mv semantics broken) AND
    # leave INDEX lagging with no warning. Treat it as the same best-effort the
    # comment already promised.
    try:
        reconcile_derived(vault, project_refs=False)
    except Exception as exc:
        console.print(
            f"[yellow]Warning:[/] INDEX/views rebuild failed "
            f"({exc.__class__.__name__}); the paper was ingested but is not "
            "yet in INDEX.json. Run `lit refresh-views` to reconcile."
        )

    # Remove the source PDF (mv semantics). Tolerate failure: a successful
    # ingest must not be reported as failure just because the source could not
    # be removed (e.g. read-only source dir).
    source_removed = True
    try:
        pdf_path.unlink()
    except OSError as exc:
        source_removed = False
        console.print(
            f"[yellow]Warning:[/] could not remove source PDF "
            f"{str(pdf_path)!r} ({exc.__class__.__name__}); paper was still "
            "ingested. Delete it manually if no longer needed."
        )

    # Pure recall increment, AFTER the atomic block: the paper is already
    # safely on disk, so the scan can never roll it back. Double defense:
    # scan_code_urls() already guarantees no-throw, but ingestion must NOT
    # be held hostage to scan success/failure (same principle as the
    # two-transaction "paper ingest not bound to clone success" rule), so
    # the call site has its own try/except fallback.
    try:
        code_candidates = scan_code_urls(paper_dir / "paper.pdf")
    except Exception as exc:  # pragma: no cover - defense in depth
        code_candidates = []
        console.print(
            f"[yellow]Warning:[/] code-URL scan failed "
            f"({exc.__class__.__name__}); paper was still ingested."
        )

    authors = parsed["authors"]
    if not authors:
        author_summary = "(no authors)"
    elif len(authors) == 1:
        author_summary = authors[0]
    else:
        # Use a single first-author + count to avoid confusion: each "Family,
        # Given" string contains a comma, so naively joining with ", " makes
        # 3 authors look like 6 people.
        author_summary = f"{authors[0]} et al. ({len(authors)} authors)"

    # Structurally-stable code_candidates block. A skill prompt can teach an
    # agent to parse it: the block is fenced by the literal markers
    # "[code_candidates]" / "[/code_candidates]", one candidate per line as
    # "<url> (p<page>, x<count>)". No false positive when empty: a single
    # explicit "no code repo URL found in full text" line instead.
    #
    # The fence markers are escaped with a leading backslash so Rich emits
    # them as literal text (an unescaped "[code_candidates]" would be parsed
    # as an unknown style tag and stripped, breaking the agent-parseable
    # contract).
    candidate_lines = [
        f"{escape(str(c['url']))} (p{c['page']}, ×{c['count']})"
        for c in code_candidates
    ]
    body = (
        "\n".join(candidate_lines)
        if candidate_lines
        else "no code repo URL found in full text"
    )
    code_block = (
        "\n\n[bold]Code candidates (full-text scan):[/]\n"
        "\\[code_candidates]\n"
        f"{body}\n"
        "\\[/code_candidates]"
    )

    # mv semantics: tell the user the source PDF is now in the vault, not a
    # copy. Only claim it when the unlink above actually succeeded — a
    # read-only source dir already printed a warning and left the file behind.
    source_line = (
        "\n[dim]Source PDF moved into the vault.[/]" if source_removed else ""
    )

    # Escape every interpolated metadata value (review F24): a title / journal
    # / author / url carrying Rich markup like "[/]" or "[red]" would otherwise
    # raise MarkupError here — AFTER the paper is ingested and the source PDF
    # deleted — making a successful add look like a failure. The literal markup
    # tags and the \[code_candidates] fences are author-controlled and stay.
    console.print(
        Panel.fit(
            f"[bold green]Paper added:[/] {escape(paper_id)}\n"
            f"[dim]Folder:[/] {escape(str(paper_dir))}\n\n"
            f"[bold]Title:[/] {escape(str(parsed['title']))}\n"
            f"[bold]Year:[/] {escape(str(parsed['year']))}    "
            f"[bold]Journal:[/] {escape(str(parsed['journal']))}\n"
            f"[bold]Authors:[/] {escape(author_summary)}"
            f"{source_line}"
            f"{code_block}",
            title="lit add",
            border_style="green",
        )
    )
