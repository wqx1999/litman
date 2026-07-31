"""Filler-value detection for ingested identity metadata.

``lit add --from-llm-json`` takes metadata an agent extracted from a PDF. When
the agent cannot read a field it tends to write a filler — ``"Unknown"``,
``"N/A"`` — instead of admitting the gap, and litman used to accept it: the
value is a non-empty string, and metadata is schemaless by design (invariant
#7). The filler then bakes into the paper id (``2024_Unknown_...``), the
browse list, and every exported citation, where nothing downstream flags it
because "non-empty" is all the schema check ever asked for.

Shared by the ingest guard (``importers/llm.py``, which refuses these) and the
health check (``core/checks.py``, which surfaces the ones already sitting in a
vault) so the two can never disagree about what counts as a filler.

Deliberately NOT applied to ``lit modify``: a user editing their own vault may
write whatever they like, including clearing a field back to a filler.
Invariant #7 is the foundation; this guard belongs at the machine-generated
ingest boundary only.

``"anonymous"`` is absent from the set on purpose. It is the established
bibliographic value for a genuinely unattributed work, and it makes a
different claim than ``"unknown"``: the former describes the document, the
latter describes the extractor's failure. Only the second is a lie about the
paper, so only the second is refused — and the first is what the rejection
message offers as the way out.
"""

from __future__ import annotations

# Matched against the whole value after stripping and case-folding — never as
# a substring, so a real author named "Unknown, Robert" passes untouched.
PLACEHOLDER_VALUES: frozenset[str] = frozenset({
    "",
    "-",
    "--",
    "---",
    "?",
    "??",
    "author",
    "authors",
    "et al",
    "et al.",
    "n.a.",
    "n/a",
    "na",
    "nil",
    "no author",
    "no authors",
    "no title",
    "none",
    "null",
    "tbd",
    "todo",
    "unknown",
    "unknown author",
    "unknown authors",
    "unknown title",
    "untitled",
})


def is_placeholder(value: str) -> bool:
    """True when ``value`` is blank or a known filler rather than content.

    Whole-value match on the stripped, case-folded string. Substring matching
    would reject legitimate data ("Unknown, Robert"; "The Unknown Structure
    of ..."), which is worse than letting an unusual filler through — the
    health check catches what slips past, but a false rejection blocks an
    honest import with no way around it.
    """
    return value.strip().casefold() in PLACEHOLDER_VALUES
