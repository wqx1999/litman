"""Per-paper checks on metadata.yaml and the paper folder: schema,
placeholders, dirty text, ids and unfinished renames, folder validity, the
discussion log scaffold, duplicate DOIs, INDEX.json against disk, and inbox
staleness.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from ruamel.yaml.error import YAMLError

from litman.core.atomic import op_moving_folders
from litman.core.checks._base import INBOX_STALE_DAYS, Issue, _yaml
from litman.core.checks.links import _HTML_COMMENT_RE, _WIKILINK_RE
from litman.core.code import CODES_DIRNAME, REPO_META_FILENAME
from litman.core.dates import date_ordering_violations, is_iso_date, is_iso_datetime
from litman.core.dedup import normalize_doi
from litman.core.document import malformed_list_fields
from litman.core.id import clean_title_rename_target as _clean_title_rename_target
from litman.core.id import (
    derive_keyword,
    family_segment,
    find_case_fold_collision,
    first_author_family,
    is_valid_id,
    is_weak_keyword,
    suggest_id,
)
from litman.core.notes import (
    enumerate_markdown_files,
    has_discussion_reminder,
    parse_wikilink_target,
)
from litman.core.placeholders import is_placeholder, is_placeholder_id_segment
from litman.core.relations import ALL_REF_FIELDS, relation_refs
from litman.core.schema import (
    _FIXED_ENUM_VALUES,
    _OPTIONAL_FIXED_ENUMS,
    _REQUIRED_NONEMPTY_FIELDS,
    _RETIRED_FIELDS,
    PROJECT_PRIORITY_PREFIX,
    PROJECT_PRIORITY_VALUES,
)
from litman.core.textclean import dirty_text_fields
from litman.exceptions import MalformedMetadataError

# ---------------------------------------------------------------------------
# Per-paper / cross-paper checks
# ---------------------------------------------------------------------------


def check_schema(vault: Path, papers: list[dict[str, Any]]) -> list[Issue]:
    """Required fields present + non-empty; fixed enums in range.

    Also covers the two halves of the retired paper-level ``priority``
    (ADR-025): a surviving ``_RETIRED_FIELDS`` key, reported under its own
    ``retired_priority`` category so ``--fix`` can offer the one-Enter
    migration without marking the whole schema group fixable; and the value
    range of each ``priority-<project>`` key that replaces it, where ``None``
    is legal ("linked, not graded yet" — ADR-025 decision 5).

    The retired-field finding has THREE wordings, because only one of them is
    a warning and it has to stand out from the other two:

    * linked → the grade moves onto each project link, nothing is lost;
    * in no project, WITH a value → the grade has nowhere to go and ``--fix``
      drops it. The only place that loss is disclosed
      (see :data:`AUTO_FIXABLE_CATEGORIES`), so it must not be diluted;
    * in no project, with no value → also nothing to lose. ``lit add`` wrote
      ``priority: null`` for every unread paper, so this is the COMMON case,
      not an edge one; telling those papers their value will be dropped both
      lies and buries the papers that really do lose one.

    "Has a value" is ``is not None`` — deliberately the same test
    ``ripple.migrate_retired_priority`` uses to decide whether to copy
    anything, so the warning and the behaviour cannot disagree.
    """
    out: list[Issue] = []
    for p in papers:
        pid = p.get("id") or "(unknown)"
        for field in _REQUIRED_NONEMPTY_FIELDS:
            value = p.get(field)
            if value is None or (isinstance(value, str) and not value.strip()):
                out.append(
                    Issue(
                        category="schema",
                        severity="error",
                        paper_id=pid,
                        message=f"missing or empty required field {field!r}",
                        hint=(
                            f"set with `lit modify {pid} --set {field}=<value>`"
                            if field != "id"
                            else "metadata.yaml `id` is the load-bearing identity field"
                        ),
                    )
                )
        for field, replacement in _RETIRED_FIELDS.items():
            if field not in p:
                continue
            message = f"field {field!r} was retired; use {replacement}"
            if p.get("projects"):
                hint = "`lit health-check --fix` migrates it in one step"
            elif p.get(field) is not None:
                message = (
                    f"field {field!r} was retired and this paper is in no "
                    "project, so its value is dropped"
                )
                hint = (
                    "`lit health-check --fix` drops it; `lit link` it to a "
                    "project first to keep the grade"
                )
            else:
                hint = "`lit health-check --fix` clears it — this one is unset"
            out.append(
                Issue(
                    category="retired_priority",
                    severity="error",
                    paper_id=pid,
                    message=message,
                    hint=hint,
                )
            )
        for field, allowed in _FIXED_ENUM_VALUES.items():
            value = p.get(field)
            if value is None:
                if field in _OPTIONAL_FIXED_ENUMS:
                    # legitimate "not yet evaluated" — see M29
                    continue
                out.append(
                    Issue(
                        category="schema",
                        severity="error",
                        paper_id=pid,
                        message=f"required fixed-enum field {field!r} is missing",
                        hint=f"set with `lit modify {pid} --set {field}=<value>`",
                    )
                )
            elif value not in allowed:
                out.append(
                    Issue(
                        category="schema",
                        severity="error",
                        paper_id=pid,
                        message=(
                            f"field {field!r} has value {value!r}, "
                            f"not in {sorted(allowed)}"
                        ),
                        hint=f"correct via `lit modify {pid} --set {field}=<value>`",
                    )
                )
        # Per-project grade range. Not driven off _FIXED_ENUM_VALUES because
        # the key is variable (priority-<project>) — the table is keyed by
        # field name and is what the INDEX projection and the webUI dropdowns
        # read, neither of which can hold a per-project key.
        for key, value in p.items():
            if not isinstance(key, str):
                continue
            if not key.startswith(PROJECT_PRIORITY_PREFIX):
                continue
            if value is not None and value not in PROJECT_PRIORITY_VALUES:
                out.append(
                    Issue(
                        category="schema",
                        severity="error",
                        paper_id=pid,
                        message=(
                            f"field {key!r} has value {value!r}, "
                            f"not in {sorted(PROJECT_PRIORITY_VALUES)}"
                        ),
                        hint=f"set with `lit modify {pid} --set {key}=<A|B|C>`",
                    )
                )
        # Timestamp format (invariant #11 + review F9). The two technical
        # audit stamps must parse as ISO 8601 datetimes; the two semantic
        # date fields (None = not yet) must be strict YYYY-MM-DD. A malformed
        # value used to be invisible: check_schema never validated format and
        # check_inbox_staleness silently skipped an unparseable created-at, so
        # garbage could sit forever unreported.
        for field in ("created-at", "updated-at"):
            value = p.get(field)
            if value is None:
                continue  # missing already flagged by the required-field loop
            if not is_iso_datetime(value):
                out.append(
                    Issue(
                        category="schema",
                        severity="error",
                        paper_id=pid,
                        message=(
                            f"field {field!r} value {value!r} is not an "
                            "ISO 8601 datetime"
                        ),
                        hint="machine-maintained audit field; restore a valid timestamp",
                    )
                )
        for field in ("read-date", "last-revisited"):
            value = p.get(field)
            if value is None:
                continue  # not yet read / revisited — legitimate
            if not is_iso_date(value):
                out.append(
                    Issue(
                        category="schema",
                        severity="error",
                        paper_id=pid,
                        message=(
                            f"field {field!r} value {value!r} is not a "
                            "YYYY-MM-DD date"
                        ),
                        hint=f"correct via `lit modify {pid} --set {field}=<YYYY-MM-DD>`",
                    )
                )
        # Date-ordering (invariant #11): read-date ≤ last-revisited ≤ today,
        # and a last-revisited implies a read-date. Shares date_ordering_
        # violations with the modify-time guard so the two cannot drift. The
        # format loop above already reported unparseable values, which the
        # helper skips.
        for msg in date_ordering_violations(
            p.get("read-date"), p.get("last-revisited")
        ):
            out.append(
                Issue(
                    category="schema",
                    severity="error",
                    paper_id=pid,
                    message=msg,
                    hint=(
                        "read-date is the first-read stamp; correct via "
                        f"`lit modify {pid} --set read-date=<YYYY-MM-DD>`"
                    ),
                )
            )
    return out


def check_placeholder_metadata(
    vault: Path, papers: list[dict[str, Any]]
) -> list[Issue]:
    """Identity fields holding a filler value instead of real content.

    ``check_schema`` above asks only whether the required fields are non-empty,
    and ``"Unknown"`` is non-empty — so a paper imported with filler metadata
    passes every probe while its id, its row in the browse list, and its
    exported citation all carry the filler. ``lit add`` refuses these at the
    ingest boundary now (``importers/llm.py``), which leaves the papers added
    before that guard existed; this is how they surface instead of waiting to
    be spotted by eye.

    Warning, not error: the vault is internally consistent, the metadata is
    just wrong about the world. Nothing here can be auto-fixed — only the user
    knows the real author — so the hint carries the two commands that do it.
    """
    out: list[Issue] = []
    for p in papers:
        pid = p.get("id") or "(unknown)"
        title = p.get("title")
        if isinstance(title, str) and is_placeholder(title):
            out.append(
                Issue(
                    category="placeholder_metadata",
                    severity="warning",
                    paper_id=pid,
                    message=f"title is a placeholder: {title.strip()!r}",
                    hint=f'set the real one with `lit modify {pid} --set title="…"`',
                )
            )
        authors = p.get("authors")
        if isinstance(authors, list):
            for name in authors:
                if isinstance(name, str) and is_placeholder(name):
                    out.append(
                        Issue(
                            category="placeholder_metadata",
                            severity="warning",
                            paper_id=pid,
                            message=(
                                f"authors holds a placeholder: {name.strip()!r}"
                            ),
                            # --set-author, not --rm-tag/--add-tag: add-tag
                            # appends, so that recipe moves the corrected
                            # name to the END of the list — for a first-
                            # author filler (the common legacy case) it
                            # would silently install the wrong first author.
                            hint=(
                                f"`lit modify {pid} "
                                '--set-author "Family, Given" ...` '
                                "(one flag per author, the whole list in "
                                "order), then "
                                f"`lit rename {pid} <new-id>` if the id "
                                "carries it too"
                            ),
                        )
                    )
    return out


def check_dirty_metadata(
    vault: Path, papers: list[dict[str, Any]]
) -> list[Issue]:
    """Titles, venues and author names with line breaks, stray spaces or markup.

    CrossRef returns what the publisher deposited, and some deposits are
    pretty-printed XML: a title with line breaks and indentation inside it, an
    ``<i>`` around one word. ``lit add`` cleans them at the door now
    (``core/textclean.py``); these are the papers stored before it did. One
    finding per field — title, journal, booktitle, publisher — and one for the
    author list naming its dirty entries. The message shows the value with
    ``repr`` so the line breaks are visible.

    Warning, not error: nothing is inconsistent, the text just reads wrong.
    Unlike the placeholder checks the repair is mechanical and loses no word,
    so ``--fix`` makes it (:func:`litman.core.ripple.clean_dirty_metadata`,
    reading the same :func:`~litman.core.textclean.dirty_text_fields`). It
    never touches the id; when the id was derived from the broken title, the
    title's hint adds the ``lit rename`` that would derive it from the clean
    one.
    """
    out: list[Issue] = []
    for p in papers:
        dirty = dirty_text_fields(p)
        if not dirty:
            continue
        pid = p.get("id") or "(unknown)"
        for field, clean in dirty.items():
            if field == "authors":
                changed = [
                    (a, c) for a, c in zip(p[field], clean, strict=True) if a != c
                ]
                stored = ", ".join(repr(a) for a, _ in changed)
                written = ", ".join(repr(c) for _, c in changed)
            else:
                stored, written = repr(p[field]), repr(clean)
            hint = f"`lit health-check --fix` writes {written}"
            if field == "title":
                target = _clean_title_rename_target(p, dirty)
                if target is not None:
                    hint += (
                        f", then `lit rename {pid} {target}` if you want the "
                        "id to match"
                    )
            out.append(
                Issue(
                    category="dirty_metadata",
                    severity="warning",
                    paper_id=pid,
                    message=(
                        f"{field} has line breaks, stray spaces or markup: "
                        f"{stored}"
                    ),
                    hint=hint,
                )
            )
    return out


def _rename_target(paper: dict[str, Any], parts: list[str]) -> tuple[str, bool]:
    """The id ``paper`` should be renamed to, and whether it is fully computed.

    Built from the metadata as it stands *now*, which is the point: by the time
    this runs the user may already have repaired the fields that produced the
    bad id, and then the correct new handle is derivable with no PDF to reopen.
    Falls back to a template that keeps whichever segment is honest —
    ``2024_Unknown_Deep-learning`` proposes ``2024_<Family>_Deep-learning``, not
    a blank id — so what is left to decide is only ever the broken half.
    """
    year = paper.get("year")
    title = paper.get("title")
    authors = paper.get("authors")
    family = (
        first_author_family([a for a in authors if isinstance(a, str)])
        if isinstance(authors, list)
        else ""
    )
    computed = suggest_id(
        year if isinstance(year, int) else None,
        family,
        title if isinstance(title, str) else "",
    )
    # A computed id that still carries a filler means the metadata has not been
    # repaired yet — offering it would propose renaming `2024_Unknown_Untitled`
    # to itself.
    if computed is not None:
        segments = computed.split("_", 2)
        if len(segments) == 3 and not any(
            is_placeholder_id_segment(s) for s in segments[1:]
        ):
            return computed, True

    # Only the segments that are actually unknowable become blanks. A Chinese
    # title cannot yield a keyword, but the first author's family name is
    # sitting right there in the metadata — leaving `<Family>` for the user to
    # retype would be the hint charging rent on what it already knows.
    if is_placeholder_id_segment(parts[1]):
        candidate = family_segment(family)
        # The metadata may still hold the very filler that built the id, in
        # which case proposing it back is proposing a rename to itself.
        family_part = (
            candidate
            if candidate and not is_placeholder_id_segment(candidate)
            else "<Family>"
        )
    else:
        family_part = parts[1]
    keyword_part = "<Keyword>" if is_placeholder_id_segment(parts[2]) else parts[2]
    return f"{parts[0]}_{family_part}_{keyword_part}", False


def check_placeholder_id(vault: Path, papers: list[dict[str, Any]]) -> list[Issue]:
    """Paper ids whose family or keyword segment is a filler word.

    ``check_placeholder_metadata`` above reports the *fields* — and its hint
    already ends with "then ``lit rename`` if the id carries it too". But that
    sentence is only visible while the warning is: repair the author with
    ``lit modify`` and the finding disappears, while ``papers/2024_Unknown_
    Untitled/`` keeps its name, keeps being the ``[[wiki-link]]`` target, and
    keeps being the cite key every ``lit export`` writes into a .bib. Fixing
    the easy half silently removed the only pointer to the hard half.

    So this check reads the id and nothing else. It needs no corroboration
    from the metadata — unlike ``check_weak_id_keyword``, which pairs a short
    keyword with an underivable title precisely because a short keyword may
    have been chosen on purpose. Nobody chooses ``Unknown``, so there is no
    deliberate case to protect and no AND to add, and the finding stays put
    until ``lit rename`` actually changes the handle.

    Warning, not error: nothing is broken or inconsistent — the vault works
    perfectly with an ugly handle, and a user may rationally leave one alone
    on a paper they will never cite. The hint carries the whole command,
    computed from the current metadata (see :func:`_rename_target`).
    """
    out: list[Issue] = []
    for p in papers:
        pid = p.get("id")
        if not isinstance(pid, str):
            continue
        # `<year>_<Family>_<Keyword>`; maxsplit=2 so an underscore inside a
        # hand-written keyword does not split the keyword.
        parts = pid.split("_", 2)
        if len(parts) < 3:
            continue
        bad = [s for s in parts[1:] if is_placeholder_id_segment(s)]
        if not bad:
            continue

        target, computed = _rename_target(p, parts)
        out.append(
            Issue(
                category="placeholder_id",
                severity="warning",
                paper_id=pid,
                message=(
                    "id carries a placeholder: "
                    + ", ".join(repr(s) for s in bad)
                ),
                hint=(
                    f"run `lit rename {pid} {target}`"
                    if computed
                    else f"fill in the blank, then `lit rename {pid} {target}`"
                ),
            )
        )
    return out


def check_weak_id_keyword(
    vault: Path, papers: list[dict[str, Any]]
) -> list[Issue]:
    """Ids whose keyword segment identifies nothing, from a title that cannot fix it.

    Before the ``_MIN_ASCII_RATIO`` gate in ``core/id.py``, a title in a
    space-less script arrived at the tokenizer as one token and slugged down
    to whatever Latin fragment it happened to contain: ``关于化合物A的合成方法``
    became ``2018_Zhang_A``. The gate stops new ones; these are the papers
    added before it existed, and nothing else in the vault would ever mention
    them — the metadata is right, only the handle is nonsense.

    Two conditions, and the second one is the point. A short keyword alone is
    not evidence of anything: someone may have chosen ``--id 2020_Chen_ML``
    deliberately, and the vault does not record whether an id was derived or
    supplied. Requiring that the stored title *also* fails to yield a keyword
    narrows this to the papers whose id could only have come from the old
    accident. The cost is a known miss — edit such a paper's title into
    something derivable and its bad id stops being reported — and that is the
    right way round: a warning nobody can clear is worse than one that is
    occasionally silent.

    Warning, not error: the vault is entirely self-consistent. Nothing here is
    auto-fixable either, because only the reader knows what the paper should be
    called, so the hint carries a ready-to-run ``lit rename`` — the one command
    that changes an id without stranding the ``[[wiki-link]]`` targets and
    project symlinks pointing at it.
    """
    out: list[Issue] = []
    for p in papers:
        pid = p.get("id")
        title = p.get("title")
        if not isinstance(pid, str) or not isinstance(title, str):
            continue
        # `<year>_<Family>_<Keyword>`; maxsplit=2 keeps an underscore inside a
        # hand-written keyword instead of splitting the keyword on it.
        parts = pid.split("_", 2)
        if len(parts) < 3 or not is_weak_keyword(parts[2]):
            continue
        # `check_placeholder_id` owns the filler segments and says something
        # sharper about them; two warnings carrying the same `lit rename` would
        # only double the wall of yellow a legacy vault already produces.
        if is_placeholder_id_segment(parts[2]):
            continue
        if derive_keyword(title) != "untitled":
            continue

        year = int(parts[0]) if parts[0].isdigit() else None
        better = suggest_id(year, parts[1], title)
        target = better if better is not None else f"{parts[0]}_{parts[1]}_<Keyword>"
        out.append(
            Issue(
                category="weak_id_keyword",
                severity="warning",
                paper_id=pid,
                message=(
                    f"id keyword {parts[2]!r} says nothing about the paper, "
                    f"and its title cannot produce a better one: {title!r}"
                ),
                hint=f"pick a keyword and run `lit rename {pid} {target}`",
            )
        )
    return out


def _unfinished_rename_blocker(
    vault: Path,
    folder: str,
    metadata: dict[str, Any],
    papers: list[dict[str, Any]],
) -> str | None:
    """Why ``--fix`` must not rename ``papers/<folder>/`` to its metadata id.

    ``None`` when it may: the metadata id is a valid id, it differs from the
    folder name by more than case, no other entry under ``papers/`` is a case
    variant of it, ``papers/<id>`` does not exist, no leftover op in
    ``.litman-staging/`` moves either folder, no other paper in ``papers``
    claims the id, and nothing in the library still names the folder. Otherwise the
    hint saying what to do by hand. The check and the fix both ask here, so
    ``--fix`` renames exactly the folders the report offered.

    The case questions come before the disk is asked: where the filesystem
    ignores case, ``papers/<id>`` "exists" whenever a case variant does, and
    asking them first gives the same verdict on every platform.

    The last question is what makes the move lossless. A folder the library
    still calls by its name — an id edited by hand rather than a rename that
    stopped halfway — would leave every such link dangling, and the next
    ``--fix`` would drop them.
    """
    meta_id = metadata.get("id")
    if not isinstance(meta_id, str) or not is_valid_id(meta_id):
        return (
            f"{meta_id!r} is not a valid paper id: set id in metadata.yaml "
            f"back to {folder!r} by hand"
        )
    if meta_id.casefold() == folder.casefold():
        return (
            "the two differ only in case: rename the folder to "
            f"papers/{meta_id}/ by hand"
        )
    papers_dir = vault / "papers"
    others = [p.name for p in papers_dir.iterdir() if p.name != folder]
    clash = find_case_fold_collision(others, meta_id)
    if clash is not None:
        return (
            f"papers/{clash}/ differs from {meta_id!r} only in case: keep one "
            "of the two by hand"
        )
    if os.path.lexists(papers_dir / meta_id):
        return (
            f"papers/{meta_id}/ exists too: compare the two folders and keep "
            "one by hand"
        )
    op = op_moving_folders(vault, {f"papers/{folder}", f"papers/{meta_id}"})
    if op is not None:
        return (
            f".litman-staging/{op}/ still moves this folder: settle that "
            "finding first"
        )
    if sum(1 for paper in papers if paper.get("id") == meta_id) > 1:
        # Counted from the list in memory; the disk is read only to name it.
        twin = next(
            (n for n in others if _declared_id(papers_dir / n) == meta_id),
            "another folder",
        )
        return (
            f"papers/{folder}/ and papers/{twin}/ both claim {meta_id!r}: "
            "decide by hand which one keeps it"
        )
    where = _still_naming(vault, folder, metadata, papers)
    if where is not None:
        return (
            f"{where} still names {folder!r}: set id in metadata.yaml back to "
            f"{folder!r} by hand, then `lit rename {folder} {meta_id}`"
        )
    return None


def _declared_id(paper_dir: Path) -> object:
    """The ``id`` a folder's metadata.yaml declares, or ``None`` if unreadable."""
    try:
        data = _yaml.load((paper_dir / "metadata.yaml").read_text(encoding="utf-8"))
    except (OSError, YAMLError, UnicodeDecodeError):
        return None
    return data.get("id") if isinstance(data, dict) else None


def _still_naming(
    vault: Path,
    folder: str,
    metadata: dict[str, Any],
    papers: list[dict[str, Any]],
) -> str | None:
    """Where the library still names ``folder`` as a paper id, or ``None``.

    Relation fields come from ``papers`` (already in memory), the bound
    repos from the paper's own ``code-clones``; the notes are read last, and
    only because a mismatch was found.
    """
    for paper in papers:
        for field in ALL_REF_FIELDS:
            if folder in relation_refs(paper.get(field)):
                return f"{paper.get('id')}'s {field}"
    for repo in relation_refs(metadata.get("code-clones")):
        repo_meta = vault / CODES_DIRNAME / repo / REPO_META_FILENAME
        try:
            bound = _yaml.load(repo_meta.read_text(encoding="utf-8"))
        except (OSError, YAMLError, UnicodeDecodeError):
            continue
        if isinstance(bound, dict) and folder in relation_refs(bound.get("papers")):
            return f"{CODES_DIRNAME}/{repo}/{REPO_META_FILENAME}"
    for md_path in enumerate_markdown_files(vault):
        try:
            text = md_path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        for m in _WIKILINK_RE.finditer(_HTML_COMMENT_RE.sub("", text)):
            vault_prefix, target = parse_wikilink_target(m.group(1))
            if vault_prefix is None and target == folder:
                return md_path.relative_to(vault).as_posix()
    return None


def check_paper_dir_validity(
    vault: Path, papers: list[dict[str, Any]]
) -> list[Issue]:
    """Per-``papers/<dir>`` structural integrity (M30; klass=validity, report).

    Truth side is the ``papers/`` directory enumeration — NOT ``list_papers``,
    which silently drops a paper with unparseable YAML (invariant: detection
    truth-side = directory enumeration). Merges and replaces the old
    ``check_invalid_paper_dirs`` + ``check_id_consistency`` so a single pass
    over ``papers/`` reports every structural fault and the
    ``except: continue`` silent skip of corrupt metadata is gone (invariant
    #14, no-silent-skip).

    Per directory child, emits ``error`` for:

    * a non-directory entry under ``papers/`` (``warning``);
    * a directory name that is not a valid paper id;
    * ``metadata.yaml`` missing (orphan from a failed rm / interrupted add);
    * ``metadata.yaml`` present but **unparseable or empty**, or with a list
      field that holds something else — the paper is invisible to
      ``list_papers``, ``INDEX``, and every metadata-keyed check, so it is
      reported here instead of vanishing silently;
    * ``metadata.yaml`` ``id`` field ≠ directory name (half-finished rename)
      — under ``unfinished_rename`` instead when ``--fix`` can rename the
      folder (:func:`_unfinished_rename_blocker`), so one check emits two
      categories, as :func:`check_schema` does;
    * ``paper.pdf`` missing (``lit open`` depends on it; irreplaceable).

    ``notes.md`` absence is NOT checked — empty/deleted is a legitimate state
    (nothing in the system depends on it existing). ``discussion.md`` absence
    is, but by its own check (:func:`check_discussion_scaffold`): its header
    carries the append-format contract, so a paper without one is a paper whose
    next discussion has no format to follow.
    """
    out: list[Issue] = []
    papers_dir = vault / "papers"
    if not papers_dir.is_dir():
        return out
    for child in sorted(papers_dir.iterdir()):
        if not child.is_dir():
            out.append(
                Issue(
                    category="paper_dir_validity",
                    severity="warning",
                    paper_id=None,
                    message=f"non-directory entry under papers/: {child.name}",
                    hint="remove manually if unintended",
                )
            )
            continue
        if not is_valid_id(child.name):
            out.append(
                Issue(
                    category="paper_dir_validity",
                    severity="error",
                    paper_id=child.name,
                    message=(
                        f"papers/{child.name}/ name is not a valid paper id"
                    ),
                    hint="rename the directory or move it out of papers/",
                )
            )
            continue

        meta_file = child / "metadata.yaml"
        if not meta_file.is_file():
            out.append(
                Issue(
                    category="paper_dir_validity",
                    severity="error",
                    paper_id=child.name,
                    message=(
                        f"papers/{child.name}/ has no metadata.yaml "
                        "(orphan from a failed rm or interrupted add)"
                    ),
                    hint=(
                        "inspect the folder; if unwanted, "
                        f"`lit rm {child.name} --purge` or move it manually"
                    ),
                )
            )
        else:
            # No silent-skip: a parse failure / empty metadata is itself a
            # finding. A corrupt metadata.yaml drops the paper out of
            # list_papers / INDEX / every metadata-keyed check, so the
            # structural check (which reads the file directly here) is the
            # only surface that can report it.
            parse_failed = False
            try:
                data = _yaml.load(meta_file.read_text(encoding="utf-8"))
            except (OSError, YAMLError) as exc:
                out.append(
                    Issue(
                        category="paper_dir_validity",
                        severity="error",
                        paper_id=child.name,
                        message=(
                            f"papers/{child.name}/metadata.yaml is unparseable "
                            f"({exc.__class__.__name__}) — paper invisible to "
                            "all checks/INDEX"
                        ),
                        hint=(
                            "fix the YAML syntax by hand; until then this paper "
                            "is silently dropped from list/INDEX"
                        ),
                    )
                )
                data = None
                parse_failed = True
            if parse_failed:
                pass  # already reported above
            elif data is None or data == {}:
                # Empty / comment-only YAML loads to None (or {}). list_papers
                # drops it, so report it here instead of letting it vanish.
                out.append(
                    Issue(
                        category="paper_dir_validity",
                        severity="error",
                        paper_id=child.name,
                        message=(
                            f"papers/{child.name}/metadata.yaml is empty — "
                            "paper invisible to all checks/INDEX"
                        ),
                        hint="populate metadata.yaml (at least id/title/year)",
                    )
                )
            elif not isinstance(data, dict):
                # Non-mapping top level (e.g. a bare list/scalar) is as broken
                # as a syntax error for our purposes.
                out.append(
                    Issue(
                        category="paper_dir_validity",
                        severity="error",
                        paper_id=child.name,
                        message=(
                            f"papers/{child.name}/metadata.yaml is not a mapping "
                            "— paper invisible to all checks/INDEX"
                        ),
                        hint="rewrite metadata.yaml as a key: value mapping",
                    )
                )
            else:
                meta_id = data.get("id")
                if meta_id and meta_id != child.name:
                    blocker = _unfinished_rename_blocker(
                        vault, child.name, data, papers
                    )
                    out.append(
                        Issue(
                            category=(
                                "unfinished_rename"
                                if blocker is None
                                else "paper_dir_validity"
                            ),
                            severity="error",
                            paper_id=child.name,
                            message=(
                                f"directory name {child.name!r} != metadata id "
                                f"{meta_id!r} (likely a half-finished rename)"
                            ),
                            hint=blocker
                            or (
                                "`lit health-check --fix` renames the folder "
                                f"to papers/{meta_id}/"
                            ),
                        )
                    )
                # Parses, but list_papers refuses it all the same (see
                # MalformedMetadataError), so it is reported here too. One
                # finding per field, so a single pass names them all.
                for field in malformed_list_fields(data):
                    example = MalformedMetadataError(field, data[field]).example
                    out.append(
                        Issue(
                            category="paper_dir_validity",
                            severity="error",
                            paper_id=child.name,
                            message=(
                                f"papers/{child.name}/metadata.yaml: {field!r} "
                                "is not a list — paper invisible to all "
                                "checks/INDEX"
                            ),
                            hint=(
                                f"write it as `{example}`; until then "
                                "`--fix` drops this paper from list/INDEX"
                            ),
                        )
                    )

        if not (child / "paper.pdf").is_file():
            out.append(
                Issue(
                    category="paper_dir_validity",
                    severity="error",
                    paper_id=child.name,
                    message=(
                        f"papers/{child.name}/paper.pdf is missing — "
                        "`lit open` cannot show it and the file is irreplaceable"
                    ),
                    hint="restore the PDF into the paper folder",
                )
            )
    return out


def check_discussion_scaffold(
    vault: Path, papers: list[dict[str, Any]]
) -> list[Issue]:
    """``papers/<id>/discussion.md`` exists and carries its format reminder.

    ``lit add`` seeds every new paper with an empty discussion log whose header
    states the append format (one dated ``##`` section per discussion, wikilinks
    for cross-references). That header is the contract the next writer — agent
    or human — reads before appending, so a missing file or a stripped-out
    reminder means the next discussion has nothing to follow.

    Papers added before the scaffold landed have no ``discussion.md`` at all;
    this is the check that surfaces them, and ``--fix`` backfills. Both findings
    are ``info``: nothing is broken or lost, the anchor is just absent.

    Auto-fixable and lossless — :func:`heal_discussion_scaffold` creates the
    file or re-inserts the reminder, and never touches an existing dated
    section.
    """
    out: list[Issue] = []
    for paper in papers:
        paper_id = paper.get("id")
        if not paper_id:
            continue
        disc_path = vault / "papers" / paper_id / "discussion.md"
        if not disc_path.is_file():
            out.append(
                Issue(
                    category="discussion_scaffold",
                    severity="info",
                    paper_id=paper_id,
                    message=(
                        f"papers/{paper_id}/discussion.md is missing — "
                        "the discussion log and its append-format header"
                    ),
                    hint="`lit health-check --fix` creates the empty log",
                )
            )
            continue
        try:
            text = disc_path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            # Unreadable is a real finding, not a silent skip (invariant #14).
            out.append(
                Issue(
                    category="discussion_scaffold",
                    severity="warning",
                    paper_id=paper_id,
                    message=(
                        f"papers/{paper_id}/discussion.md cannot be read "
                        "(permissions, or not UTF-8)"
                    ),
                    hint="fix the file by hand — --fix will not overwrite it",
                )
            )
            continue
        if not has_discussion_reminder(text):
            out.append(
                Issue(
                    category="discussion_scaffold",
                    severity="info",
                    paper_id=paper_id,
                    message=(
                        f"papers/{paper_id}/discussion.md has lost its "
                        "append-format header"
                    ),
                    hint="`lit health-check --fix` re-inserts it",
                )
            )
    return out


def check_duplicate_doi(vault: Path, papers: list[dict[str, Any]]) -> list[Issue]:
    """No two papers share a DOI (ledger: validity, report-only).

    ``lit add`` refuses a duplicate at ingest and ``lit modify --set doi=``
    refuses to create one, but metadata restored from a backup or written by
    an older litman can still collide. The collision is dangerous out of
    proportion to its looks: every ``--paper-doi`` lookup (``show`` / ``cite``
    / ``rm``!) resolves to the alphabetically-first match, so a destructive
    command aimed by DOI can hit the wrong paper. One finding per colliding
    DOI, naming every holder — severity ``error`` because lookups are already
    misrouting.

    Not auto-fixable: only the user knows which paper genuinely owns the DOI.
    """
    by_doi: dict[str, list[str]] = {}
    for paper in papers:
        paper_id = paper.get("id")
        doi = paper.get("doi")
        if not paper_id or not doi:
            continue
        normalized = normalize_doi(str(doi))
        if not normalized:
            continue
        by_doi.setdefault(normalized, []).append(str(paper_id))

    out: list[Issue] = []
    for normalized, ids in sorted(by_doi.items()):
        if len(ids) < 2:
            continue
        joined = ", ".join(sorted(ids))
        out.append(
            Issue(
                category="duplicate_doi",
                severity="error",
                paper_id=None,
                message=(
                    f"DOI {normalized!r} is shared by {len(ids)} papers: "
                    f"{joined} — every --paper-doi lookup (show / cite / rm) "
                    "resolves to one of them arbitrarily"
                ),
                hint=(
                    "decide which paper owns the DOI, then fix the others: "
                    "`lit modify <id> --set doi=<correct-doi>` (or --set "
                    "doi= to clear)"
                ),
            )
        )
    return out


def check_index_vs_disk(
    vault: Path, papers: list[dict[str, Any]]
) -> list[Issue]:
    """``INDEX.json`` ↔ ``papers/`` reconciliation (ledger #1; cheap, klass A).

    The most fundamental drift pair, previously checked nowhere (spec §1.2): a
    manual ``rm -rf papers/<id>/`` leaves a dead INDEX entry forever. Reads
    ONLY the INDEX id set (:func:`views.load_index_ids`) + the ``papers/``
    directory listing (invariant #15: no per-paper ``metadata.yaml``), so it is
    safe in the Tier-1 hot path.

    Two directions:

    * **vanished id** (in INDEX, dir absent) — ``error``. Repairable by the
      klass-A regen (the Tier-1 hook drops it metadata-free; ``health-check
      --fix`` rebuilds INDEX from truth). The ``paper_id`` is set so the hook
      can collect the vanished ids for targeted wikilink annotation.
    * **un-indexed dir** (dir present, not in INDEX) — ``warning``. Adding it
      to INDEX needs the paper's metadata → Tier-2; the cheap path can only
      warn. Typically a corrupt ``metadata.yaml`` (dropped by ``list_papers``
      so the last write command never indexed it) or an interrupted ``add``.

    The ``papers`` argument is ignored — using it would route detection through
    ``list_papers`` (the corrupt-paper blind spot). Directory enumeration is
    the uncorrupted truth side (spec §4).
    """
    from litman.core import views

    index_ids = views.load_index_ids(vault)
    if index_ids is None:
        # No INDEX.json yet (or unparseable): nothing to reconcile against. A
        # fresh vault has no INDEX until the first write command; a corrupt
        # INDEX is a Tier-2 concern (the next regen rewrites it).
        index_ids = set()

    papers_dir = vault / "papers"
    disk_ids: set[str] = set()
    if papers_dir.is_dir():
        for child in papers_dir.iterdir():
            if child.is_dir() and is_valid_id(child.name):
                disk_ids.add(child.name)

    out: list[Issue] = []
    for vanished in sorted(index_ids - disk_ids):
        out.append(
            Issue(
                category="index_vs_disk",
                severity="error",
                paper_id=vanished,
                message=(
                    f"INDEX.json lists {vanished!r} but papers/{vanished}/ "
                    "no longer exists (manual delete or corrupt metadata)"
                ),
                hint="run `lit health-check --fix` (or any write command) to regen INDEX",
            )
        )
    for unindexed in sorted(disk_ids - index_ids):
        out.append(
            Issue(
                category="index_vs_disk",
                severity="warning",
                paper_id=unindexed,
                message=(
                    f"papers/{unindexed}/ exists but is not in INDEX.json — "
                    "not indexed (corrupt metadata / interrupted add)"
                ),
                hint="run `lit health-check` to see the structural fault",
            )
        )
    return out


def check_inbox_staleness(
    vault: Path, papers: list[dict[str, Any]]
) -> list[Issue]:
    """``status: inbox`` papers older than :data:`INBOX_STALE_DAYS` days."""
    out: list[Issue] = []
    now = datetime.now(UTC)
    threshold = timedelta(days=INBOX_STALE_DAYS)
    for p in papers:
        if p.get("status") != "inbox":
            continue
        created = p.get("created-at")
        if not created:
            continue
        try:
            created_dt = datetime.fromisoformat(str(created))
        except ValueError:
            continue
        if created_dt.tzinfo is None:
            created_dt = created_dt.replace(tzinfo=UTC)
        age = now - created_dt
        if age > threshold:
            pid = p.get("id") or "(unknown)"
            out.append(
                Issue(
                    category="inbox_staleness",
                    severity="warning",
                    paper_id=str(pid),
                    message=(
                        f"status=inbox for {age.days} days "
                        f"(>{INBOX_STALE_DAYS} day threshold)"
                    ),
                    hint=(
                        f"promote with `lit modify {pid} --set status=skim` "
                        f"or drop with `lit modify {pid} --set status=dropped`"
                    ),
                )
            )
    return out
