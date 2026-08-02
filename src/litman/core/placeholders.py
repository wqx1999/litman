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

Two views of the same set. :func:`is_placeholder` judges a metadata field,
which ``lit modify`` can repair in place. :func:`is_placeholder_id_segment`
judges a segment of an id, which only ``lit rename`` can, and which is the
half that outlives the repair — the second half of this module exists because
fixing the field silently stops anything from mentioning the handle it built.

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


# --------------------------------------------------------------------------
# The same fillers, as they survive into a paper id
# --------------------------------------------------------------------------

# Values too short or too ordinary to accuse an id segment of. `Na` is a real
# family name (나 / 娜) and `2024_Na_<Keyword>` is an honest id; flagging it
# would put a permanent warning on exactly the authors this whole line of work
# exists to serve. `Nil`, `?` and the dash runs are here for the same reason
# or because `_slug` deletes them to nothing anyway. What they cost is a miss
# on `2024_NA_...`, which `check_weak_id_keyword` already reports — a segment
# that short says nothing about the paper whatever produced it.
_ID_SEGMENT_EXEMPT: frozenset[str] = frozenset({
    "", "-", "--", "---", "?", "??", "n.a.", "n/a", "na", "nil",
})


def _id_segment_form(value: str) -> str:
    """Normalise to how a value looks after ``_slug`` put it in an id.

    ``_slug`` strips everything but ASCII alphanumerics and hyphens, and it
    *deletes* the spaces rather than turning them into hyphens — so the family
    segment of an id built from ``"Unknown author"`` reads ``Unknownauthor``,
    while the keyword segment for ``"No title"`` reads ``No-title`` (the
    top-N path hyphen-joins the words it kept). Folding both to bare
    alphanumerics makes one comparison cover both segments.
    """
    return "".join(c for c in value if c.isalnum()).casefold()


# Precomputed so the check is a set lookup per segment. Derived from
# PLACEHOLDER_VALUES by subtraction rather than listed independently: a filler
# added to the set above should start being caught in ids too, without anyone
# remembering to add it in two places.
ID_SEGMENT_PLACEHOLDERS: frozenset[str] = frozenset(
    _id_segment_form(v) for v in PLACEHOLDER_VALUES if v not in _ID_SEGMENT_EXEMPT
)


def is_placeholder_id_segment(segment: str) -> bool:
    """True when an id's family or keyword segment is a filler word.

    Unlike :func:`is_placeholder` this needs no corroboration from the stored
    metadata. Nobody types ``--id 2024_Unknown_Untitled`` on purpose, so the
    segment is evidence on its own — which is what lets the health check keep
    reporting a bad id after ``lit modify`` has repaired the fields that
    produced it, right up until ``lit rename`` actually changes the handle.
    """
    return _id_segment_form(segment) in ID_SEGMENT_PLACEHOLDERS
