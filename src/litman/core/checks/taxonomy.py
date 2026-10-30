"""Checks on the topics / methods / data vocabulary: drift against
TAXONOMY.md, look-alike values, unused values and dicts made mostly of
once-used values.
"""

from __future__ import annotations

import shlex
from pathlib import Path
from typing import Any

from litman.core.checks._base import Issue
from litman.core.taxonomy import USER_DICTS, count_usage, parse_taxonomy, read_taxonomy_text
from litman.core.taxonomy_similarity import similar_values
from litman.exceptions import UnreadableTaxonomyError


def check_taxonomy_drift(
    vault: Path, papers: list[dict[str, Any]]
) -> list[Issue]:
    """``projects`` / ``topics`` / ``methods`` / ``data`` values absent from TAXONOMY.md."""
    taxonomy_file = vault / "TAXONOMY.md"
    if not taxonomy_file.is_file():
        return [
            Issue(
                category="taxonomy_drift",
                severity="error",
                paper_id=None,
                message="TAXONOMY.md is missing from the vault root",
                hint="run `lit init` in a fresh dir and copy the seed",
            )
        ]
    try:
        registered = parse_taxonomy(read_taxonomy_text(vault))
    except UnreadableTaxonomyError as exc:
        # No silent-skip (invariant #14): the file is present but unreadable
        # (permissions, non-UTF-8, dropped mount). Swallowing it would report a
        # clean vault while taxonomy governance is actually un-checkable — and
        # would contradict the missing-file branch above, which DOES emit.
        return [
            Issue(
                category="taxonomy_drift",
                severity="error",
                paper_id=None,
                message=(
                    f"TAXONOMY.md is present but unreadable ({exc.reason}): "
                    "taxonomy drift cannot be checked"
                ),
                hint=(
                    "re-save TAXONOMY.md as UTF-8, then re-run"
                    if exc.not_utf8
                    else "make TAXONOMY.md readable, then re-run"
                ),
            )
        ]
    out: list[Issue] = []
    for p in papers:
        pid = p.get("id") or "(unknown)"
        for dict_name in USER_DICTS:
            field_values = p.get(dict_name) or []
            for value in field_values:
                if str(value) not in registered.get(dict_name, []):
                    out.append(
                        Issue(
                            category="taxonomy_drift",
                            severity="warning",
                            paper_id=str(pid),
                            message=(
                                f"{dict_name} value {value!r} not in TAXONOMY.md "
                                f"(unregistered)"
                            ),
                            hint=(
                                f"register via `lit taxonomy add {dict_name} {value}` "
                                f"or rename it"
                            ),
                        )
                    )
    return out


# The dicts the vocabulary checks below read: the words the user made up. An
# unused project is normal (a new one has no papers yet), and the fixed enums
# are not the user's vocabulary.
_VOCAB_DICTS: tuple[str, ...] = ("topics", "methods", "data")

# taxonomy_singletons fires when at least this many values of a dict are used
# by exactly one paper each AND they are at least this share of the dict. The
# share alone would fire on any small library (7 of 9 methods in an 18-paper
# one), the count alone on any large one.
SINGLETON_MIN = 10
SINGLETON_PERCENT = 30


def _registered_vocab(vault: Path) -> dict[str, list[str]] | None:
    """TAXONOMY.md's dicts, or None when the file is missing or unreadable.

    Either case is check_taxonomy_drift's finding; the vocabulary checks stay
    silent rather than report it again.
    """
    try:
        text = read_taxonomy_text(vault)
    except UnreadableTaxonomyError:
        return None
    return parse_taxonomy(text)


def check_taxonomy_similar(
    vault: Path, papers: list[dict[str, Any]]
) -> list[Issue]:
    """Pairs of registered topics / methods / data values that look alike.

    One finding per pair, worded and ranked by
    :func:`~litman.core.taxonomy_similarity.similar_values`. The hint merges
    the less-used value into the more-used one (alphabetical on a tie); which
    name to keep, or whether to keep both, is the user's call — so this
    reports and never fixes.
    """
    registered = _registered_vocab(vault)
    if registered is None:
        return []
    out: list[Issue] = []
    for dict_name in _VOCAB_DICTS:
        values = registered[dict_name]
        used_by = count_usage(papers, dict_name, values)
        for i, value in enumerate(values):
            for other, reason in similar_values(value, values[i + 1 :]):
                keep, drop = sorted((value, other), key=lambda v: (-used_by[v], v))
                out.append(
                    Issue(
                        category="taxonomy_similar",
                        severity="info",
                        paper_id=None,
                        message=f"{dict_name}: {drop!r} looks like {keep!r} ({reason})",
                        hint=(
                            f"merge with `lit taxonomy merge {dict_name} "
                            f"{shlex.quote(drop)} --into {shlex.quote(keep)}`"
                        ),
                    )
                )
    return out


def check_taxonomy_unused(
    vault: Path, papers: list[dict[str, Any]]
) -> list[Issue]:
    """Registered topics / methods / data values that no paper uses.

    Report-only: a value can be registered ahead of the papers it is for.
    """
    registered = _registered_vocab(vault)
    if registered is None:
        return []
    out: list[Issue] = []
    for dict_name in _VOCAB_DICTS:
        used_by = count_usage(papers, dict_name, registered[dict_name])
        for value, n in used_by.items():
            if n == 0:
                out.append(
                    Issue(
                        category="taxonomy_unused",
                        severity="info",
                        paper_id=None,
                        message=f"{dict_name}: {value!r} is used by no paper",
                        hint=f"remove with `lit taxonomy rm {dict_name} {shlex.quote(value)}`",
                    )
                )
    return out


def check_taxonomy_singletons(
    vault: Path, papers: list[dict[str, Any]]
) -> list[Issue]:
    """Dicts where many values are each used by a single paper.

    One finding per dict past :data:`SINGLETON_MIN` and
    :data:`SINGLETON_PERCENT`. Which of those values belong together is a
    judgment about meaning, so the hint hands it to the agent's vocabulary
    review rather than naming a command.
    """
    registered = _registered_vocab(vault)
    if registered is None:
        return []
    out: list[Issue] = []
    for dict_name in _VOCAB_DICTS:
        values = registered[dict_name]
        used_by = count_usage(papers, dict_name, values)
        once = sum(1 for n in used_by.values() if n == 1)
        if once >= SINGLETON_MIN and once * 100 >= SINGLETON_PERCENT * len(values):
            out.append(
                Issue(
                    category="taxonomy_singletons",
                    severity="info",
                    paper_id=None,
                    message=(
                        f"{dict_name}: {once} of {len(values)} values are used by one paper"
                    ),
                    hint=(
                        f"see the counts with `lit taxonomy list {dict_name}`, "
                        "then ask your agent to tidy the vocabulary"
                    ),
                )
            )
    return out
