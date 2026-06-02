"""Duplicate-detection helpers for ``lit add`` (M2.9).

Three-layer defense against duplicate papers and id collisions, in order of
cost. Each layer is independently testable and called from
``litman.commands.add``:

1. **DOI precheck** (`find_paper_by_doi`): linear scan of
   ``papers/*/metadata.yaml`` looking for a matching ``doi:`` field. Live
   scan, not INDEX.json — INDEX.json may lag the filesystem between
   ``lit refresh-views`` runs, but the per-paper yaml files are always
   authoritative. Match is case-insensitive (DOIs are case-insensitive per
   the DOI Handbook).

2. **Alternative suggestions** (`suggest_alternative_ids`): when the derived
   id collides, propose candidates by sliding the keyword window over later
   words in the title. Returns ids that do NOT collide further, so the
   interactive prompt can offer ready-to-use choices.

3. **Auto-suffix** (`auto_suffix_id`): non-interactive escape hatch. Appends
   ``_b``, ``_c``, ... (BibTeX-style alphabetical suffix) using underscore
   prefix rather than hyphen to avoid visual collision with hyphen-separated
   keywords like ``GPT-2`` or ``Llama-3``. Starts at ``_b`` because the bare
   id is implicitly the first occurrence.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from ruamel.yaml import YAML, YAMLError

from litman.core.id import derive_keyword_alternatives
from litman.exceptions import AddError

_yaml_safe = YAML(typ="safe")

# Resolver-URL / scheme prefixes that wrap an otherwise-bare DOI. Matched
# case-insensitively; the DOI body after the prefix is preserved as-is.
_DOI_PREFIX_RE = re.compile(
    r"^(?:https?://(?:dx\.)?doi\.org/|doi:\s*)",
    re.IGNORECASE,
)


def canonicalize_doi(doi: str) -> str:
    """Strip a resolver-URL or ``doi:`` prefix, returning the bare DOI.

    The same paper is written as ``https://doi.org/10.1/x``, ``doi:10.1/x``,
    or the bare ``10.1/x``. Only the bare form works as a CrossRef path
    segment (review F11), and only consistent canonicalization lets dedup
    treat the three as equal (review F10). The DOI body and its case are
    preserved (the prefix match is case-insensitive); use :func:`normalize_doi`
    for the case-folded *comparison* key. Empty / whitespace input returns ``""``.
    """
    stripped = doi.strip()
    if not stripped:
        return ""
    return _DOI_PREFIX_RE.sub("", stripped).strip()


def normalize_doi(doi: str) -> str:
    """Canonical (prefix-stripped) + case-folded comparison key for a DOI.

    DOIs are case-insensitive per the DOI Handbook, and may carry a resolver
    URL / ``doi:`` prefix; both sides of any comparison must be reduced to the
    same form. Public so ``views.render_index`` can key the ``by_doi`` map the
    same way and tests can assert byte-equality.
    """
    return canonicalize_doi(doi).lower()


def find_paper_by_doi(
    vault: Path, doi: str
) -> tuple[str, dict[str, Any]] | None:
    """Locate an existing paper whose metadata.yaml has a matching DOI.

    Args:
        vault: Vault root.
        doi: DOI to search for. Empty / falsy returns ``None`` directly.

    Returns:
        ``(paper_id, metadata_dict)`` of the first match, or ``None``. Match
        is case-insensitive. Paper folders without a metadata.yaml, or whose
        metadata fails to parse, are skipped — health-check is the right
        surface for those errors, not lit-add.
    """
    if not doi:
        return None
    target = normalize_doi(doi)
    if not target:
        return None
    papers_dir = vault / "papers"
    if not papers_dir.is_dir():
        return None
    for paper_dir in sorted(papers_dir.iterdir()):
        if not paper_dir.is_dir():
            continue
        meta_file = paper_dir / "metadata.yaml"
        if not meta_file.is_file():
            continue
        try:
            meta = _yaml_safe.load(meta_file.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, YAMLError):
            # Corrupt yaml — let health-check surface it; do not let it mask
            # a duplicate elsewhere in the vault. The try only reads + parses,
            # so these are the only failures possible; any other (programming)
            # error propagates.
            continue
        if not meta:
            continue
        existing = meta.get("doi") or ""
        if existing and normalize_doi(str(existing)) == target:
            return paper_dir.name, meta
    return None


def suggest_alternative_ids(
    vault: Path,
    primary_id: str,
    year: int,
    family: str,
    title: str,
    n: int = 3,
) -> list[str]:
    """Propose up to ``n`` alternative ids that do not collide on disk.

    Walks ``derive_keyword_alternatives(title)`` and prefixes each with
    ``<year>_<Family>_``. Skips candidates that collide with existing
    ``papers/<id>/`` folders so the prompt can offer ready-to-use options.

    Args:
        vault: Vault root.
        primary_id: The collided derived id (excluded from results).
        year: Publication year (already validated by caller).
        family: Slugged + capitalized first-author family (already validated).
        title: Paper title.
        n: Maximum suggestions to return.

    Returns:
        Up to ``n`` candidate ids, possibly empty if the title yields no
        usable alternatives or all candidates also collide.
    """
    if n <= 0:
        return []
    papers_dir = vault / "papers"
    out: list[str] = []
    seen: set[str] = {primary_id}
    # Pull more alternatives than we need so we can still satisfy n after
    # filtering out collisions; cap at 2*n + 3 to bound the title-walk cost.
    for keyword in derive_keyword_alternatives(title, n=2 * n + 3):
        candidate = f"{year}_{family}_{keyword}"
        if candidate in seen:
            continue
        seen.add(candidate)
        if (papers_dir / candidate).exists():
            continue
        out.append(candidate)
        if len(out) >= n:
            break
    return out


def auto_suffix_id(vault: Path, base_id: str) -> str:
    """Return ``base_id`` or ``base_id_b`` / ``base_id_c`` / ... — first free.

    Underscore prefix (not hyphen) avoids visual collision with keywords
    like ``GPT-2``, ``Llama-3``. The lowercase letter suffix matches the
    BibTeX disambiguation convention.

    Raises:
        AddError: All of ``_b`` through ``_z`` are taken. Practically
            impossible (25 same-key papers), but fail loudly rather than
            wrap to ``_aa``.
    """
    papers_dir = vault / "papers"
    if not (papers_dir / base_id).exists():
        return base_id
    for suffix_ord in range(ord("b"), ord("z") + 1):
        candidate = f"{base_id}_{chr(suffix_ord)}"
        if not (papers_dir / candidate).exists():
            return candidate
    raise AddError(
        f"Auto-suffix exhausted: {base_id}_b through {base_id}_z all taken. "
        "Pass --id <new-id> to specify a unique id manually."
    )
