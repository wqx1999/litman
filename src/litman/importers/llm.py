"""LLM-extracted metadata importer (M4.1).

Bridge from a JSON file produced by an LLM (e.g. by Claude Code following the
``lit-library`` skill) into the standard litman metadata dict consumed by
``lit add``. The architecture stays clean of LLM dependencies: this module
does NOT call any LLM API. The agent runs separately (in the user's chat
session), reads the PDF, drafts metadata as a JSON file, and the CLI reads
that file.

Why a file and not stdin/argv:
- JSON in a file survives the round-trip past Click's argv parsing
  (avoiding shell-quoting headaches around titles with quotes / commas).
- The same file can be inspected, edited, and re-fed if the first attempt
  was wrong.
- The skill workflow produces a temp file naturally (``/tmp/lit-llm-<id>.json``)
  and points ``lit add --from-llm-json`` at it.

Schema (validated via pydantic ``extra='forbid'`` so typos surface):

    {
        "title":      str,           # required, the paper's title
        "authors":    list[str],     # required, each "Family, Given"
        "year":       int | null,    # publication year
        "doi":        str | null,    # canonical DOI (no URL prefix)
        "journal":    str | null,    # venue / preprint server
        "arxiv-id":   str | null,    # optional; currently informational
        "abstract":   str | null,    # optional; currently informational

        # M12.0 bib-oriented fields (all optional; agent may omit them):
        "volume":     str | null,    # @article volume
        "issue":      str | null,    # @article issue
        "pages":      str | null,    # "45-67" -> rendered "45--67" by exporter
        "publisher":  str | null,    # @book / @inproceedings publisher
        "venue-type": str | null,    # CrossRef-style: "journal-article",
                                     # "proceedings-article", "posted-content",
                                     # "book", "book-chapter", "dissertation",
                                     # "report". Drives bibtex entry type.
        "booktitle":  str | null,    # @inproceedings / @incollection;
                                     # leave null when journal already holds
                                     # the venue.
    }

The returned dict matches ``parse_crossref``'s output (``title``,
``authors``, ``year``, ``journal``, ``doi`` + the 6 M12.0 fields) so
the rest of ``lit add`` sees a uniform shape regardless of whether
metadata came from CrossRef or from an LLM.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from litman.exceptions import ImporterError


class LLMCandidateMeta(BaseModel):
    """Typed view of the LLM-produced metadata JSON."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    title: str = Field(..., min_length=1, description="Paper title.")
    authors: list[str] = Field(
        ...,
        min_length=1,
        description=(
            "Authors as 'Family, Given' strings, ordered as they appear "
            "on the paper. At least one entry required; the first author's "
            "family name drives id derivation."
        ),
    )
    year: int | None = Field(default=None, description="Publication year.")
    doi: str | None = Field(default=None, description="Canonical DOI.")
    journal: str | None = Field(
        default=None,
        description="Venue / journal / preprint server.",
    )
    arxiv_id: str | None = Field(
        default=None,
        alias="arxiv-id",
        description="arXiv identifier; informational for now.",
    )
    abstract: str | None = Field(
        default=None,
        description="Optional abstract; not currently written to disk.",
    )

    # M12.0 bib-oriented fields. All optional; older skill JSON payloads
    # that omit them are accepted unchanged (pydantic defaults to None
    # when a field is missing, and ``extra='forbid'`` only fires on
    # unknown keys, not absent ones).
    volume: str | None = Field(
        default=None,
        description="Journal volume (@article).",
    )
    issue: str | None = Field(
        default=None,
        description="Journal issue / number (@article).",
    )
    pages: str | None = Field(
        default=None,
        description="Page range like '45-67'; exporter renders as '45--67'.",
    )
    publisher: str | None = Field(
        default=None,
        description="Publisher (@book / @inproceedings / @phdthesis).",
    )
    venue_type: str | None = Field(
        default=None,
        alias="venue-type",
        description=(
            "CrossRef-style venue type ('journal-article', "
            "'proceedings-article', 'posted-content', 'book', "
            "'book-chapter', 'dissertation', 'report'). Drives the "
            "bibtex entry type chosen by `lit export`. Distinct from "
            "the editorial `type` field (research / review / position) "
            "in metadata.yaml."
        ),
    )
    booktitle: str | None = Field(
        default=None,
        description=(
            "Conference / book title (@inproceedings / @incollection). "
            "Leave null for @article — the journal field holds the venue."
        ),
    )


def parse_llm_json(json_path: Path) -> dict[str, Any]:
    """Load + validate an LLM metadata JSON file.

    Returns a dict shaped like ``parse_crossref``'s output (``title``,
    ``authors``, ``year``, ``journal``, ``doi``) so callers don't need to
    branch on importer source.

    Args:
        json_path: Path to a JSON file the agent has prepared.

    Returns:
        Dict with the same shape ``parse_crossref`` produces.

    Raises:
        ImporterError: file missing, unreadable, malformed JSON, top-level
            not a mapping, schema validation failure (missing required
            field, unknown key, wrong type, empty title / authors).
    """
    if not json_path.is_file():
        raise ImporterError(
            f"No LLM metadata JSON at {json_path}. "
            "Pass a path that the agent has written."
        )
    try:
        raw = json.loads(json_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise ImporterError(
            f"Failed to parse {json_path} as JSON: {e.msg} "
            f"(line {e.lineno}, col {e.colno})"
        ) from e
    if not isinstance(raw, dict):
        raise ImporterError(
            f"{json_path} must contain a JSON object at the top level, "
            f"got {type(raw).__name__}."
        )
    try:
        meta = LLMCandidateMeta.model_validate(raw)
    except ValidationError as e:
        first = e.errors()[0]
        loc = ".".join(str(p) for p in first["loc"]) or "<root>"
        raise ImporterError(
            f"Invalid LLM metadata JSON at {json_path}: "
            f"field {loc!r}: {first['msg']}"
        ) from e

    # Normalize to the parse_crossref output shape so downstream add.py
    # logic does not branch on importer. The 6 M12.0 fields default to
    # "" (not None) to match parse_crossref's shape.
    return {
        "title": meta.title,
        "authors": list(meta.authors),
        "year": meta.year,
        "journal": meta.journal or "",
        "doi": (meta.doi or "").strip(),
        # M12.0 bib-oriented fields.
        "volume": meta.volume or "",
        "issue": meta.issue or "",
        "pages": meta.pages or "",
        "publisher": meta.publisher or "",
        "venue-type": meta.venue_type or "",
        "booktitle": meta.booktitle or "",
        # Optional pass-throughs — currently unused by _build_metadata but
        # available if a future caller wants them.
        "arxiv-id": meta.arxiv_id,
        "abstract": meta.abstract,
    }
