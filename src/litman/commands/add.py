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
       paper.pdf  (copied, not moved — original PDF is preserved)
       metadata.yaml
       notes.md   (placeholder)
    6. Print a summary panel.
"""

from __future__ import annotations

import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import click
from rich.console import Console
from rich.panel import Panel
from ruamel.yaml import YAML

from litman.core.dedup import (
    auto_suffix_id,
    find_paper_by_doi,
    suggest_alternative_ids,
)
from litman.core.id import derive_id, find_case_fold_collision, is_valid_id
from litman.core.library import find_vault, resolve_library_or_vault
from litman.exceptions import AddError, DuplicateDOIError, IDError
from litman.importers.crossref import fetch_crossref, parse_crossref
from litman.importers.llm import parse_llm_json

console = Console()

_yaml = YAML()
_yaml.indent(mapping=2, sequence=4, offset=2)
_yaml.default_flow_style = False
_yaml.preserve_quotes = True


def _now_iso() -> str:
    """Local-timezone ISO 8601 timestamp with seconds precision."""
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def _build_metadata(
    parsed: dict[str, Any],
    paper_id: str,
    now: str | None = None,
) -> dict[str, Any]:
    """Assemble the full metadata.yaml dict in design-doc field order.

    Schema-less by intent (§7.3): unknown-yet fields are emitted as ``None`` /
    ``[]`` so the user can fill them later in ``lit edit`` / ``lit modify``.

    The audit fields ``created-at`` and ``updated-at`` are technical (machine-
    maintained); ``read-date`` and ``last-revisited`` are semantic (user-set).
    Never merge the two.
    """
    timestamp = now or _now_iso()
    return {
        # === identity layer (auto from CrossRef) ===
        "id": paper_id,
        "title": parsed.get("title", ""),
        "authors": parsed.get("authors", []),
        "year": parsed.get("year"),
        "journal": parsed.get("journal", ""),
        "doi": parsed.get("doi", ""),
        "arxiv-id": None,
        "github": None,
        # === audit layer (machine-maintained) ===
        "created-at": timestamp,
        "updated-at": timestamp,
        # === classification layer (TAXONOMY-controlled, M2 validates) ===
        "projects": [],
        "topics": [],
        "methods": [],
        "data": [],
        "type": "research",
        # === personal evaluation layer ===
        "status": "inbox",
        "priority": "B",
        "read-date": None,
        "last-revisited": None,
        # === relations layer ===
        "related": [],
        "contradicts": [],
        "extends": [],
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
    type=click.Path(exists=True, file_okay=True, dir_okay=False, path_type=Path),
    default=None,
    help=(
        "Path to an LLM-prepared metadata JSON file (see "
        "`litman.importers.llm.LLMCandidateMeta` for the schema). Used by "
        "the lit-library Claude Code skill. Mutually exclusive with --doi."
    ),
)
@click.option(
    "--id",
    "id_override",
    default=None,
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
    help="Vault path. Defaults to $LIT_LIBRARY or cwd-walk discovery.",
)
@click.option(
    "--vault",
    "vault_name",
    default=None,
    help=(
        "Vault name from ~/.config/litman/vaults.yaml (M8). "
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

    Source of metadata: either ``--doi`` (CrossRef fetch) or
    ``--from-llm-json <path>`` (LLM-prepared JSON file). Exactly one is
    required. Refuses on duplicate DOI, derives a canonical id, and
    creates ``papers/<id>/`` containing ``paper.pdf``, ``metadata.yaml``,
    and an empty ``notes.md``.
    """
    if (doi is None) == (from_llm_json is None):
        raise AddError(
            "Provide exactly one of --doi <doi> or --from-llm-json <path>. "
            "These flags are mutually exclusive."
        )

    vault = find_vault(resolve_library_or_vault(library, vault_name))

    if from_llm_json is not None:
        parsed = parse_llm_json(from_llm_json)
        # The DOI used for dedup precheck and user-facing error messages
        # comes from the JSON; an LLM-imported paper without a DOI just
        # skips the precheck (id collision is still enforced below).
        doi_for_dedup = parsed.get("doi") or ""
    else:
        assert doi is not None  # type narrowing for mypy
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
        else f"LLM JSON {str(from_llm_json)!r}"
    )

    if id_override:
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
        if year is None or not family:
            # Should not happen given the derive_id path above, but defensive.
            raise AddError(
                f"Paper folder already exists: {paper_dir} and collision "
                "resolution requires year + family. Pass --id explicitly."
            )
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
    try:
        paper_dir.mkdir(parents=True)
        with (paper_dir / "metadata.yaml").open("w", encoding="utf-8") as f:
            _yaml.dump(_build_metadata(parsed, paper_id), f)
        (paper_dir / "notes.md").write_text(
            f"# {parsed['title']}\n\n"
            "(Personal notes go here. The `/read-paper` skill will draft a "
            "discussion.md alongside this file in M3.)\n",
            encoding="utf-8",
        )
        shutil.copy2(pdf_path, paper_dir / "paper.pdf")
    except Exception:
        if paper_dir.exists():
            shutil.rmtree(paper_dir, ignore_errors=True)
        raise

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

    console.print(
        Panel.fit(
            f"[bold green]Paper added:[/] {paper_id}\n"
            f"[dim]Folder:[/] {paper_dir}\n\n"
            f"[bold]Title:[/] {parsed['title']}\n"
            f"[bold]Year:[/] {parsed['year']}    "
            f"[bold]Journal:[/] {parsed['journal']}\n"
            f"[bold]Authors:[/] {author_summary}\n\n"
            "[dim]Next:[/] edit metadata.yaml to fill projects/topics/methods, "
            "then `lit refresh-views` to update INDEX.json.",
            title="lit add",
            border_style="green",
        )
    )
