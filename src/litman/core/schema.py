"""The metadata schema: required fields, fixed enums, per-project grades and
retired fields.

One source for both sides: the health check (``core.checks.check_schema``)
reports what breaks these rules, and the write paths (``lit modify --set``,
``lit link --priority``, ``GET /api/fixed-enums``) accept exactly the same
values. A leaf module — it imports nothing from litman — so any module can
import it at the top without closing a load-time cycle.
"""

from __future__ import annotations

# Schema rules: required fields and their allowed values.
_REQUIRED_NONEMPTY_FIELDS: tuple[str, ...] = (
    "id",
    "created-at",
    "updated-at",
)

_FIXED_ENUM_VALUES: dict[str, frozenset[str]] = {
    "type": frozenset(
        {
            "research",
            "review",
            "position",
            "benchmark",
            "dataset",
            "tutorial",
            "thesis",
            "book-chapter",
        }
    ),
    "status": frozenset({"deep-read", "skim", "inbox", "dropped"}),
}

# Fixed enums where ``None`` is a legitimate "not yet evaluated" state
# rather than a schema error. M29: `type` is a personal-evaluation field
# the user fills after reading; `lit add` writes None, and `lit-reading`
# B10 self-check is the surfacing path. ``status`` stays required (its
# "not yet evaluated" state is the explicit value "inbox", not None).
_OPTIONAL_FIXED_ENUMS: frozenset[str] = frozenset({"type"})

# Per-project grade. Not a fixed enum in the table above: the key is
# variable (priority-<project>), so it never enters the INDEX projection
# and is validated per key below (schema) + per membership (orphan).
# ``None`` is legal and means "linked, not graded yet" (ADR-025 decision 5):
# a link happens at ingest, a grade after reading, so the gap is a real state
# and health-check must not nag about it. The migration still never WRITES a
# null — it drops the key instead — but `lit modify --set priority-<P>=`
# produces one, and nothing can delete a key, so rejecting it would strand
# the vault with an unfixable error that blocks `lit sync push`.
PROJECT_PRIORITY_PREFIX = "priority-"
PROJECT_PRIORITY_VALUES: frozenset[str] = frozenset({"A", "B", "C"})

# Fields retired from the schema. Present => schema error + a one-Enter
# `--fix` migration (apply_autofix). Permanent by design - see ADR-025.
_RETIRED_FIELDS: dict[str, str] = {"priority": "priority-<project>"}


def fixed_enum_values(field: str) -> frozenset[str] | None:
    """Allowed values for a fixed-enum metadata field, or ``None`` if ``field``
    is not a fixed enum.

    Read-only accessor so write commands (``lit modify --set``) can enforce the
    same enum the read-side ``check_schema`` enforces, without reaching into
    this module's private table. ``None`` is *additionally* a legal value for
    the fields reported by :func:`fixed_enum_allows_none`.
    """
    return _FIXED_ENUM_VALUES.get(field)


def retired_field_replacement(field: str) -> str | None:
    """What replaced ``field``, or ``None`` if ``field`` is not retired.

    Read-only accessor over the private ``_RETIRED_FIELDS`` table, for the
    same reason :func:`fixed_enum_values` exists: the write commands
    (``lit modify --set``) must refuse exactly what the read-side
    ``check_schema`` reports, without reaching into this module's privates.
    """
    return _RETIRED_FIELDS.get(field)


def fixed_enum_allows_none(field: str) -> bool:
    """Whether ``field`` is a fixed enum for which ``None`` ("not yet
    evaluated") is legal (``type``; M29). ``status`` is not — its
    unevaluated state is the explicit value ``inbox``."""
    return field in _OPTIONAL_FIXED_ENUMS


# Display order for ``status``: a curation lifecycle (inbox → skim → deep-read,
# then dropped), not alphabetical. Every other fixed enum has no natural order,
# so it is sorted. This is the order the webUI dropdowns render (one source, not
# a second list hard-coded in the frontend).
_STATUS_ORDER: tuple[str, ...] = ("inbox", "skim", "deep-read", "dropped")


def all_fixed_enums() -> dict[str, list[str]]:
    """Every fixed-enum field's allowed values, in display order.

    Public accessor over the private ``_FIXED_ENUM_VALUES`` table so the webUI
    can populate its status / type dropdowns from the SAME source
    ``check_schema`` / ``lit modify --set`` validate against (no second list in
    the frontend). ``status`` comes first, in curation-lifecycle order; every
    other field follows, sorted by name, with its values sorted.
    ``None``-as-legal is reported separately via :func:`fixed_enum_allows_none`.

    Derived from the table rather than naming each field a second time:
    hard-indexing it meant retiring a field (``priority``, ADR-025) raised
    KeyError here — and this is what ``GET /api/fixed-enums`` returns.
    """
    ordered = ["status", *sorted(f for f in _FIXED_ENUM_VALUES if f != "status")]
    return {
        field: (
            list(_STATUS_ORDER)
            if field == "status"
            else sorted(_FIXED_ENUM_VALUES[field])
        )
        for field in ordered
    }
