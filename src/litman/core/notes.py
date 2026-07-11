"""Shared helpers for scanning markdown notes that may contain ``[[id]]`` wikilinks.

Used by ``lit rename`` (M2.6) to rewrite wikilinks, ``lit rm`` (M23/M24) to
annotate deleted targets, ``lit trash restore`` (M24) to de-annotate, and
``lit health-check`` (M2.8 + M8.4 + M24.2) to flag dangling / drifted
wikilink targets. Centralising the scope here keeps every caller honest
about which files participate in the wikilink graph.

Wikilink scope (per design doc §5.1):
    * ``papers/<id>/notes.md``      — per-paper notes (always scaffolded)
    * ``papers/<id>/discussion.md`` — per-paper discussion log, also scaffolded
      by ``lit add``. Papers added before the scaffold landed have none until
      ``lit health-check --fix`` backfills them, so it is still guarded with
      ``.is_file()``.

Anything outside these locations is the user's own thing and is left
untouched by rename / rm / restore.

Wikilink syntax (M8.4 extends the original one-form syntax):
    * ``[[paper-id]]``           — same-vault reference (legacy form)
    * ``[[vault-name:paper-id]]``— cross-vault reference, where
      ``vault-name`` is the handle registered in
      ``~/.config/litman/vaults.yaml``.

The same-vault form is preserved exactly as before, so existing notes
and tests stay valid. The cross-vault form lets agents and humans
write references to papers living in linked fork vaults without
having to first copy them across.

Deletion-status tags (M24): the CLI maintains an inline ``(deleted)``
suffix on same-vault ``[[A]]`` links whenever ``papers/A/`` is absent.
:func:`annotate_deleted_wikilinks` adds it on ``lit rm``,
:func:`deannotate_deleted_wikilinks` strips it on ``lit trash restore``.
Both key off the RESOLVED target id (never the literal prior string), so
a note an agent rewrote still self-heals. The filesystem (``papers/A/``)
is the single source of truth; the tag is only its surfaced projection
(ADR-013).
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from pathlib import Path

# Same ``[[...]]`` form health-check uses (core/checks.py): no ``|alias``
# support, no nested brackets, single line. Kept identical so annotate /
# de-annotate / dangling-detection all agree on what a wikilink is.
_WIKILINK_RE = re.compile(r"\[\[([^\[\]\n]+)\]\]")

# The status suffix appended right after a ``[[A]]`` whose target is gone.
_DELETED_SUFFIX = " (deleted)"


# ---------------------------------------------------------------------------
# Wikilink-format reminder (self-healing scaffold)
# ---------------------------------------------------------------------------

# Seeded into every fresh notes.md by ``lit add`` and re-inserted on every
# reading-session close (see :func:`heal_wikilink_reminder`). The agent
# regenerates notes.md wholesale (skills: notes = overwrite-style STATE
# snapshot), so a reminder living inside the file is wiped on the first
# regeneration and the agent stops seeing the ``[[id]]`` convention. Healing it
# back keeps the nudge in front of the agent every round with no skill change.
WIKILINK_REMINDER = (
    "<!-- Link another paper in this vault as [[paper-id]] (a wikilink, "
    "not backticks or plain text) so lit rm and lit health-check keep "
    "it tracked. -->"
)

# Distinctive phrase used to detect the reminder regardless of exact wording or
# position. Real notes never contain it, so a false "already present" is
# implausible; the historical scaffold carries the identical phrase, so old
# vaults are recognised and never get a duplicate.
_WIKILINK_REMINDER_MARKER = "not backticks or plain text"


# ---------------------------------------------------------------------------
# Discussion scaffold (format anchor for the append-only log)
# ---------------------------------------------------------------------------

# Seeded into every discussion.md by ``lit add``, repaired by
# ``lit health-check --fix`` and by the GUI's discussion PUT. Where the notes
# reminder has to be healed back after every agent regeneration (notes.md is an
# overwrite-style STATE snapshot), this one survives on its own: discussion.md
# is the append-only LOG, so the header stays at the top of the file as the
# format contract each writer reads before appending.
#
# The example ``[[paper-id]]`` sits INSIDE the HTML comment on purpose. Both
# scanners that would otherwise mistake it for a real graph edge ignore comment
# regions: ``check_dangling_wikilinks`` (core/checks.py) strips them before
# matching, and ``search_notes`` (core/search.py) masks them before comparing.
# The rewrite paths (``lit rm`` / ``lit rename``) deliberately preserve comments
# and stay inert only because ``paper-id`` never resolves to a real paper — keep
# the placeholder literal, never a real-looking id.
DISCUSSION_FORMAT_REMINDER = (
    "<!-- Append-only log: one `## YYYY-MM-DD HH:MM` section per discussion, "
    "opening with **Question:**. Link another paper in this vault as "
    "[[paper-id]] (a wikilink, not backticks or plain text) so lit rm and "
    "lit health-check keep it tracked. -->"
)

# Distinctive phrase used to detect the discussion reminder. Deliberately NOT
# :data:`_WIKILINK_REMINDER_MARKER` (which this reminder also happens to
# contain): the two heal paths are file-scoped and must never take one file's
# anchor for the other's.
_DISCUSSION_REMINDER_MARKER = "Append-only log"


def enumerate_markdown_files(vault: Path) -> Iterable[Path]:
    """Yield .md files in the wikilink scope (see module docstring).

    Both ``papers/<id>/notes.md`` and ``papers/<id>/discussion.md`` are
    yielded when present. A paper added before the discussion scaffold landed
    has no ``discussion.md`` until ``lit health-check --fix`` backfills it, so
    it simply contributes only its ``notes.md``.
    """
    papers_dir = vault / "papers"
    if papers_dir.is_dir():
        # sorted() makes the yield order deterministic (paper id ascending,
        # then notes.md before discussion.md). Every caller — search,
        # rename, rm, trash, health-check — relies on a stable order for
        # reproducible output; iterdir() alone yields arbitrary OS order.
        for child in sorted(papers_dir.iterdir()):
            if child.is_dir():
                for name in ("notes.md", "discussion.md"):
                    md = child / name
                    if md.is_file():
                        yield md


def parse_wikilink_target(raw: str) -> tuple[str | None, str]:
    """Split the inner text of a ``[[...]]`` wikilink into (vault, paper_id).

    Examples:
        ``"2024_Wang_AMP"``           → ``(None, "2024_Wang_AMP")``
        ``"zhang-shared:2024_Wang_AMP"`` → ``("zhang-shared", "2024_Wang_AMP")``
        ``"  zhang : id  "``         → ``("zhang", "id")``  (whitespace stripped)

    Splits on the FIRST ``:`` only. Paper ids never contain ``:`` per
    :func:`litman.core.id.is_valid_id`, so any colon in the target text
    must be the vault separator.

    Returns:
        ``(None, paper_id)`` for same-vault links; ``(vault_name, paper_id)``
        for cross-vault links. Either ``vault_name`` or ``paper_id`` may
        come back empty when the input is malformed (e.g. ``"vault:"`` or
        ``":id"``); the caller decides how to surface those.
    """
    raw = raw.strip()
    if ":" not in raw:
        return (None, raw)
    vault_name, _, paper_id = raw.partition(":")
    return (vault_name.strip(), paper_id.strip())


def annotate_deleted_wikilinks(text: str, deleted_id: str) -> str:
    """Append ``" (deleted)"`` after each same-vault ``[[deleted_id]]``.

    Only same-vault links whose RESOLVED target equals ``deleted_id`` are
    touched (cross-vault ``[[v:id]]`` is out of scope, ADR-013 / M24). The
    match keys on the parsed target, never on the literal bracket string, so
    an agent-rewritten link still gets tagged. Idempotent: a link already
    followed by ``" (deleted)"`` is left as-is — no ``(deleted) (deleted)``.

    Returns the text unchanged when nothing matched, so callers can stage
    only files whose content actually changed.
    """
    return _retarget(text, deleted_id, annotate=True)


def deannotate_deleted_wikilinks(text: str, restored_id: str) -> str:
    """Strip a trailing ``" (deleted)"`` after each ``[[restored_id]]``.

    Inverse of :func:`annotate_deleted_wikilinks`: same-vault links resolving
    to ``restored_id`` lose the suffix; everything else is untouched.
    Idempotent — a link with no suffix is a no-op.
    """
    return _retarget(text, restored_id, annotate=False)


def _retarget(text: str, target_id: str, *, annotate: bool) -> str:
    """Rewrite the deletion suffix on every ``[[...]]`` resolving to target_id.

    Walks matches left-to-right and rebuilds the string so suffix
    insertion / removal never shifts an index we still need (a plain
    ``re.sub`` cannot peek at the char *after* ``]]`` to enforce
    idempotency). Untouched links and all non-link text are copied verbatim,
    so a file with no matching target comes back byte-identical.
    """
    out: list[str] = []
    pos = 0
    for m in _WIKILINK_RE.finditer(text):
        vault_prefix, paper_id = parse_wikilink_target(m.group(1))
        if vault_prefix is not None or paper_id != target_id:
            continue
        # Copy everything up to and including this link's closing ``]]``.
        out.append(text[pos : m.end()])
        pos = m.end()
        has_suffix = text.startswith(_DELETED_SUFFIX, pos)
        if annotate and not has_suffix:
            out.append(_DELETED_SUFFIX)
        elif not annotate and has_suffix:
            pos += len(_DELETED_SUFFIX)
    out.append(text[pos:])
    return "".join(out)


def ensure_wikilink_reminder(text: str) -> str:
    """Return ``text`` with the wikilink-format reminder guaranteed present.

    Idempotent: when the reminder (detected via the distinctive
    :data:`_WIKILINK_REMINDER_MARKER` phrase) is already there, ``text`` comes
    back unchanged so the caller can skip the write. When absent, the reminder
    is inserted just after the first ``# `` H1 heading — matching the ``lit add``
    scaffold layout (heading, blank, reminder, blank, body) — or prepended when
    the note has no heading.
    """
    if _WIKILINK_REMINDER_MARKER in text:
        return text

    lines = text.split("\n")
    heading_idx = next(
        (i for i, line in enumerate(lines) if line.startswith("# ")), None
    )
    if heading_idx is None:
        if not text:
            return WIKILINK_REMINDER + "\n"
        return "\n".join([WIKILINK_REMINDER, "", *lines])

    head = lines[: heading_idx + 1]
    tail = lines[heading_idx + 1 :]
    # Drop a single existing blank right after the heading so the inserted
    # block does not stack double blank lines.
    if tail and tail[0] == "":
        tail = tail[1:]
    return "\n".join([*head, "", WIKILINK_REMINDER, "", *tail])


def heal_wikilink_reminder(vault: Path, paper_id: str) -> bool:
    """Re-insert the wikilink reminder into ``papers/<id>/notes.md`` if missing.

    Wired into every reading-session close (``lit read`` / ``promote`` /
    ``drop`` / ``revisit``) so an agent overwrite that stripped the reminder is
    repaired before the next session reads the note. Returns ``True`` when the
    file was rewritten, ``False`` on a no-op (reminder already present, or the
    note / paper dir absent). The single-file write goes through ``staged_write``
    so a crash never leaves a half-written note; notes.md is not truth-lockable,
    so it stays agent-writable afterwards.
    """
    from litman.core.atomic import staged_write

    notes_path = vault / "papers" / paper_id / "notes.md"
    if not notes_path.is_file():
        return False
    text = notes_path.read_text(encoding="utf-8")
    healed = ensure_wikilink_reminder(text)
    if healed == text:
        return False
    with staged_write(vault, op_id=f"notes-reminder-{paper_id}") as stage:
        stage.write_text(f"papers/{paper_id}/notes.md", healed)
    return True


def has_discussion_reminder(text: str) -> bool:
    """True when a discussion log still carries its append-format header.

    The detection any caller outside this module should use (health-check's
    ``discussion_scaffold`` check) — the marker phrase itself stays private so
    its wording can change without breaking them.
    """
    return _DISCUSSION_REMINDER_MARKER in text


def discussion_scaffold(paper_id: str) -> str:
    """The body ``lit add`` seeds into a fresh ``papers/<id>/discussion.md``.

    An H1 the GUI and the reader see, plus the format reminder every writer
    (agent or human) reads before appending its dated section.
    """
    return f"# Discussion log for {paper_id}\n\n{DISCUSSION_FORMAT_REMINDER}\n"


def ensure_discussion_scaffold(text: str, paper_id: str) -> str:
    """Return ``text`` with the discussion format reminder guaranteed present.

    Idempotent: when the reminder (detected via the distinctive
    :data:`_DISCUSSION_REMINDER_MARKER` phrase) is already there, ``text`` comes
    back unchanged so the caller can skip the write. An empty file becomes the
    full :func:`discussion_scaffold`; otherwise the reminder is inserted right
    after the first ``# `` H1, or an H1 + reminder are prepended when the log
    has no heading (a hand-written or GUI-truncated file).
    """
    if _DISCUSSION_REMINDER_MARKER in text:
        return text
    if not text.strip():
        return discussion_scaffold(paper_id)

    lines = text.split("\n")
    heading_idx = next(
        (i for i, line in enumerate(lines) if line.startswith("# ")), None
    )
    if heading_idx is None:
        return "\n".join(
            [
                f"# Discussion log for {paper_id}",
                "",
                DISCUSSION_FORMAT_REMINDER,
                "",
                *lines,
            ]
        )

    head = lines[: heading_idx + 1]
    tail = lines[heading_idx + 1 :]
    # Drop a single existing blank right after the heading so the inserted
    # block does not stack double blank lines.
    if tail and tail[0] == "":
        tail = tail[1:]
    return "\n".join([*head, "", DISCUSSION_FORMAT_REMINDER, "", *tail])


def heal_discussion_scaffold(vault: Path, paper_id: str) -> bool:
    """Create ``papers/<id>/discussion.md``, or repair its format reminder.

    The backfill path for vaults whose papers predate the scaffold (``lit add``
    seeds it for every new paper) and the repair path for a log whose header was
    edited away. Driven by ``lit health-check --fix`` via the
    ``discussion_scaffold`` check. Returns ``True`` when the file was written,
    ``False`` on a no-op (already scaffolded, or the paper dir is absent).
    Appends nothing and rewrites nothing else: an existing log keeps its dated
    sections verbatim.
    """
    from litman.core.atomic import staged_write

    paper_dir = vault / "papers" / paper_id
    if not paper_dir.is_dir():
        return False
    disc_path = paper_dir / "discussion.md"
    if disc_path.is_file():
        text = disc_path.read_text(encoding="utf-8")
        healed = ensure_discussion_scaffold(text, paper_id)
        if healed == text:
            return False
    else:
        healed = discussion_scaffold(paper_id)
    with staged_write(vault, op_id=f"discussion-scaffold-{paper_id}") as stage:
        stage.write_text(f"papers/{paper_id}/discussion.md", healed)
    return True
