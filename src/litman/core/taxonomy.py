"""TAXONOMY.md parser and section-rewriter.

TAXONOMY.md is the controlled-vocabulary registry for paper metadata
list fields. It contains one section per dictionary, with the body
either ``(empty)`` or a sequence of ``- value`` lines.

Two classes of dictionaries:

* **User-extensible**: ``projects``, ``topics``, ``methods``, ``data`` —
  modifiable by ``lit taxonomy {add, rename, merge, rm}``.
* **Fixed enums**: ``type``, ``status``, ``priority`` — read-only here;
  changes require a code release because the application logic enums
  must change in lockstep.

The rewriter is surgical: it replaces only the body of one section and
leaves the rest of the file (preamble paragraph, other sections, fixed-
enum section bodies, header annotations like ``## type (fixed enum,
not extensible)``) byte-for-byte unchanged. This protects any
hand-written annotations the user may have added to the file.

The metadata fields driven by each user dict use the same name as the
dict (``projects`` ↔ ``projects``); :data:`USER_DICT_TO_METADATA_FIELD`
makes that mapping explicit so callers don't hard-code it.
"""

from __future__ import annotations

import re

USER_DICTS: tuple[str, ...] = ("projects", "topics", "methods", "data")
FIXED_DICTS: tuple[str, ...] = ("type", "status", "priority")
ALL_DICTS: tuple[str, ...] = USER_DICTS + FIXED_DICTS

# Each user dict drives the like-named list field on metadata.yaml.
USER_DICT_TO_METADATA_FIELD: dict[str, str] = {d: d for d in USER_DICTS}

_HEADER_RE = re.compile(r"^##\s+(\S+)")
_LIST_ITEM_RE = re.compile(r"^-\s+(.+)$")
_EMPTY_MARKER = "(empty)"


def parse_taxonomy(text: str) -> dict[str, list[str]]:
    """Extract every known dictionary section from a TAXONOMY.md body.

    Returns a dict whose keys are exactly :data:`ALL_DICTS`. Sections
    that are missing or marked ``(empty)`` map to an empty list. Lines
    that aren't ``- value`` are ignored within each section so any
    free-form annotation under a header doesn't pollute the values.
    """
    result: dict[str, list[str]] = {name: [] for name in ALL_DICTS}
    current: str | None = None

    for line in text.splitlines():
        h = _HEADER_RE.match(line)
        if h:
            name = h.group(1)
            current = name if name in ALL_DICTS else None
            continue
        if current is None:
            continue
        m = _LIST_ITEM_RE.match(line)
        if m:
            value = m.group(1).strip()
            if value and value != _EMPTY_MARKER:
                result[current].append(value)

    return result


def render_user_dict_body(values: list[str]) -> list[str]:
    """Render the body of a user-dict section as a list of lines (with \\n).

    Empty list emits ``(empty)``; non-empty emits sorted ``- value`` lines.
    Caller composes the body into the larger file via
    :func:`update_user_dict_section`.
    """
    if not values:
        return [f"{_EMPTY_MARKER}\n"]
    return [f"- {v}\n" for v in sorted(values)]


def update_user_dict_section(
    text: str, dict_name: str, values: list[str]
) -> str:
    """Return TAXONOMY.md text with one user dict's section body replaced.

    Everything else — the preamble, other section headers and bodies,
    fixed-enum sections, and any annotations on the targeted section's
    header line — is preserved verbatim. The body of the matched
    section becomes ``(empty)`` for an empty list or sorted ``- value``
    lines otherwise, separated from the next ``##`` header by exactly
    one blank line.

    Raises:
        ValueError: ``dict_name`` is not user-extensible.
    """
    if dict_name not in USER_DICTS:
        raise ValueError(
            f"Cannot rewrite fixed-enum or unknown dict: {dict_name!r}. "
            f"Writable dicts: {USER_DICTS}."
        )

    lines = text.splitlines(keepends=True)
    out: list[str] = []
    i = 0
    while i < len(lines):
        line = lines[i]
        h = _HEADER_RE.match(line.rstrip("\n"))
        if h and h.group(1) == dict_name:
            # Emit the header verbatim (preserving any header annotation).
            out.append(line)
            i += 1
            # Preserve a single blank line between header and body if the
            # source had one (it always does in our seed format).
            blank_emitted = False
            while i < len(lines) and lines[i].strip() == "":
                if not blank_emitted:
                    out.append(lines[i])
                    blank_emitted = True
                i += 1
            if not blank_emitted:
                out.append("\n")
            # Emit the new body.
            out.extend(render_user_dict_body(values))
            # Skip the original body until the next ##-header (or EOF).
            while i < len(lines):
                if _HEADER_RE.match(lines[i].rstrip("\n")):
                    break
                i += 1
            # Insert exactly one blank line before the next section, if any.
            if i < len(lines):
                out.append("\n")
            continue
        out.append(line)
        i += 1
    return "".join(out)


def find_referencing_papers(
    papers: list[dict], dict_name: str, value: str
) -> list[str]:
    """Return ids of papers whose ``dict_name`` list contains ``value``.

    Used by ``lit taxonomy rm`` to refuse removal when references exist
    and by ``lit taxonomy rename / merge`` to scope which papers need
    their metadata.yaml rewritten.
    """
    field = USER_DICT_TO_METADATA_FIELD.get(dict_name)
    if field is None:
        return []
    matches: list[str] = []
    for p in papers:
        values = p.get(field) or []
        if value in values:
            paper_id = p.get("id")
            if paper_id:
                matches.append(str(paper_id))
    return sorted(matches)


def replace_value_in_field(
    metadata: dict, field: str, replacements: dict[str, str]
) -> bool:
    """Apply ``old → new`` substitutions inside ``metadata[field]``.

    Used by both rename (one-to-one mapping) and merge (many-to-one
    mapping). Order is preserved; duplicates produced by the substitution
    are deduped while keeping first occurrence. Returns True iff the
    field was actually changed.
    """
    current = metadata.get(field)
    if not current:
        return False
    new_list: list = []
    seen: set = set()
    changed = False
    for item in current:
        replaced = replacements.get(item, item)
        if replaced != item:
            changed = True
        if replaced not in seen:
            new_list.append(replaced)
            seen.add(replaced)
        else:
            # Substitution produced a duplicate — collapse silently.
            changed = True
    if changed:
        metadata[field] = new_list
    return changed
