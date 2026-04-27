"""``lit add`` — import a paper into the literature vault.

Pipeline:
    1. Fetch metadata via the chosen importer (M1.3: CrossRef only).
    2. Derive the canonical id (or accept ``--id`` override).
    3. Create ``papers/<id>/``, atomically populated with::
       paper.pdf  (copied, not moved — original PDF is preserved)
       metadata.yaml
       notes.md   (placeholder)
    4. Print a summary panel.

Schema validation, TAXONOMY enforcement, INDEX update, and duplicate detection
land in M2. M1.3 deliberately keeps the path short.
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

import click
from rich.console import Console
from rich.panel import Panel
from ruamel.yaml import YAML

from litman.core.id import derive_id
from litman.core.library import find_vault
from litman.exceptions import AddError, IDError
from litman.importers.crossref import fetch_crossref, parse_crossref

console = Console()

_yaml = YAML()
_yaml.indent(mapping=2, sequence=4, offset=2)
_yaml.default_flow_style = False
_yaml.preserve_quotes = True


def _build_metadata(parsed: dict[str, Any], paper_id: str) -> dict[str, Any]:
    """Assemble the full metadata.yaml dict in design-doc field order.

    Schema-less by intent (§7.3): unknown-yet fields are emitted as ``None`` /
    ``[]`` so the user can fill them later in ``lit edit`` / ``lit modify``.
    """
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
    }


def _first_author_family(authors: list[str]) -> str:
    """Extract the family name from the first 'Family, Given' author string."""
    if not authors:
        return ""
    return authors[0].split(",", 1)[0].strip()


@click.command("add")
@click.argument(
    "pdf_path",
    type=click.Path(exists=True, file_okay=True, dir_okay=False, path_type=Path),
)
@click.option(
    "--doi",
    required=True,
    help="DOI of the paper. Used to fetch metadata from CrossRef.",
)
@click.option(
    "--id",
    "id_override",
    default=None,
    help="Override the auto-derived id (format: <year>_<Family>_<Keyword>).",
)
@click.option(
    "--library",
    type=click.Path(file_okay=False, dir_okay=True, path_type=Path),
    default=None,
    envvar="LIT_LIBRARY",
    help="Vault path. Defaults to $LIT_LIBRARY or cwd-walk discovery.",
)
def add_cmd(
    pdf_path: Path,
    doi: str,
    id_override: str | None,
    library: Path | None,
) -> None:
    """Import a paper PDF + DOI into the vault.

    Fetches metadata from CrossRef, derives a canonical id, and creates
    ``papers/<id>/`` containing ``paper.pdf``, ``metadata.yaml``, and an
    empty ``notes.md``.
    """
    vault = find_vault(library)

    raw = fetch_crossref(doi)
    parsed = parse_crossref(raw)

    if id_override:
        paper_id = id_override
    else:
        if parsed["year"] is None:
            raise IDError(
                f"CrossRef returned no year for DOI {doi!r}; "
                "pass --id explicitly."
            )
        family = _first_author_family(parsed["authors"])
        if not family:
            raise IDError(
                f"CrossRef returned no first-author family name for DOI {doi!r}; "
                "pass --id explicitly."
            )
        paper_id = derive_id(parsed["year"], family, parsed["title"])

    paper_dir = vault / "papers" / paper_id
    if paper_dir.exists():
        raise AddError(
            f"Paper folder already exists: {paper_dir}. "
            "Use --id to override or remove the existing folder first."
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
    author_summary = ", ".join(authors[:3])
    if len(authors) > 3:
        author_summary += " et al."

    console.print(
        Panel.fit(
            f"[bold green]Paper added:[/] {paper_id}\n"
            f"[dim]Folder:[/] {paper_dir}\n\n"
            f"[bold]Title:[/] {parsed['title']}\n"
            f"[bold]Year:[/] {parsed['year']}    "
            f"[bold]Journal:[/] {parsed['journal']}\n"
            f"[bold]Authors:[/] {author_summary}\n\n"
            "[dim]Next:[/] edit metadata.yaml to fill projects/topics/methods, "
            "then `lit refresh-views` (M2) to update INDEX.md.",
            title="lit add",
            border_style="green",
        )
    )
