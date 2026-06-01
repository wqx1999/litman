"""LLM-extracted metadata importer (M4.1).

Bridge from a JSON file produced by an LLM (e.g. by Claude Code following the
``lit-library`` skill) into the standard litman metadata dict consumed by
``lit add``. The architecture stays clean of LLM dependencies: this module
does NOT call any LLM API. The agent runs separately (in the user's chat
session), reads the PDF, drafts metadata as a JSON file, and the CLI reads
that file.

Two input channels, same validation:
- **stdin** (skill default): the agent pipes the JSON straight into
  ``lit add ... --from-llm-json -`` so nothing touches disk. Parsed via
  :func:`parse_llm_json_text`.
- **file** (inspectable / re-feedable): the agent writes the JSON to a path
  and points ``lit add --from-llm-json <path>`` at it. A file survives the
  round-trip past Click's argv parsing (no shell-quoting headaches around
  titles with quotes / commas) and can be inspected, edited, and re-fed if
  the first attempt was wrong. Parsed via :func:`parse_llm_json`, a thin
  file-reading wrapper around :func:`parse_llm_json_text`.

Both channels run the identical json.loads -> isinstance(dict) -> pydantic
validate -> normalize pipeline; only error-message labelling differs.

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

from litman.core.dedup import canonicalize_doi
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


def _normalize_meta(meta: LLMCandidateMeta) -> dict[str, Any]:
    """Project a validated ``LLMCandidateMeta`` onto ``parse_crossref``'s shape.

    Single source of truth for the normalized output, shared by both the
    stdin and the file input channels. The 6 M12.0 fields default to ``""``
    (not None) to match parse_crossref's shape.
    """
    return {
        "title": meta.title,
        "authors": list(meta.authors),
        "year": meta.year,
        "journal": meta.journal or "",
        "doi": canonicalize_doi(meta.doi or ""),
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


def parse_llm_json_text(
    raw_text: str, *, source: str = "<stdin>"
) -> dict[str, Any]:
    """Parse + validate LLM metadata JSON held in a string.

    Runs the json.loads -> isinstance(dict) -> pydantic validate -> normalize
    pipeline. Used directly for the stdin channel and by
    :func:`parse_llm_json` for the file channel.

    Args:
        raw_text: The raw JSON text (e.g. read from stdin or a file).
        source: A label for error messages identifying where the text came
            from (``"<stdin>"`` for piped input, or the file path string).

    Returns:
        Dict with the same shape ``parse_crossref`` produces.

    Raises:
        ImporterError: malformed JSON, top-level not a mapping, or schema
            validation failure (missing required field, unknown key, wrong
            type, empty title / authors).
    """
    try:
        raw = json.loads(raw_text)
    except json.JSONDecodeError as e:
        raise ImporterError(
            f"Failed to parse {source} as JSON: {e.msg} "
            f"(line {e.lineno}, col {e.colno})"
        ) from e
    if not isinstance(raw, dict):
        raise ImporterError(
            f"{source} must contain a JSON object at the top level, "
            f"got {type(raw).__name__}."
        )
    try:
        meta = LLMCandidateMeta.model_validate(raw)
    except ValidationError as e:
        first = e.errors()[0]
        loc = ".".join(str(p) for p in first["loc"]) or "<root>"
        raise ImporterError(
            f"Invalid LLM metadata JSON at {source}: "
            f"field {loc!r}: {first['msg']}"
        ) from e

    return _normalize_meta(meta)


def parse_llm_json(json_path: Path) -> dict[str, Any]:
    """Load + validate an LLM metadata JSON file.

    Thin wrapper around :func:`parse_llm_json_text`: checks the file exists,
    reads it as UTF-8, and delegates parsing. Returns a dict shaped like
    ``parse_crossref``'s output (``title``, ``authors``, ``year``,
    ``journal``, ``doi``) so callers don't need to branch on importer source.

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
        text = json_path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as e:
        # is_file() above covers "missing"; the file can still be unreadable
        # (permissions) or not valid UTF-8. The docstring promises every such
        # case surfaces as ImporterError, so callers only need to catch one
        # exception type — honour that contract instead of leaking the raw
        # OSError / UnicodeDecodeError.
        raise ImporterError(
            f"Cannot read LLM metadata JSON at {json_path}: {e}"
        ) from e
    return parse_llm_json_text(text, source=str(json_path))
