"""Checks on what links papers: relation fields (dangling and one-sided
edges), wikilinks in notes, and the views/ link farm against metadata.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, cast

from litman.core.checks._base import Issue
from litman.core.document import library_paper_ids
from litman.core.id import is_valid_id
from litman.core.notes import enumerate_markdown_files, parse_wikilink_target
from litman.core.portable_link import is_portable_link, links_supported
from litman.core.relations import (
    RELATION_PAIRS,
    REVERSE_REF_FIELDS,
    dangling_relation_edges,
    relation_refs,
)
from litman.core.trash import list_trash
from litman.exceptions import VaultRegistryError

_WIKILINK_RE = re.compile(r"\[\[([^\[\]\n]+)\]\]")

# Inline deletion-status marker the CLI maintains on same-vault wikilinks
# (M24 / ADR-013). check_dangling_wikilinks peeks at the slice AFTER each
# match's ``]]`` for this suffix; _WIKILINK_RE itself is left untouched so
# rename + cross-vault logic stay agnostic to it.
_DELETED_SUFFIX = " (deleted)"

# HTML comments are scaffolding, not graph edges. The wikilink-format reminder
# seeded into every notes.md (core/notes.py WIKILINK_REMINDER) embeds a literal
# ``[[paper-id]]`` to demonstrate the syntax; left in the scan it false-positives
# as a dangling same-vault link on every paper — and since the reminder is
# self-healed back on each reading-session close (b3ecb9c), the noise is
# guaranteed library-wide. A commented-out ``[[id]]`` is "not a real reference"
# the same way commented-out code does not run, so check_dangling_wikilinks
# strips comment regions before matching. Only that check strips: rename /
# annotate rewrite text in place and must preserve comments, and the reminder's
# ``paper-id`` placeholder never resolves to a real id so it is inert there.
_HTML_COMMENT_RE = re.compile(r"<!--.*?-->", re.DOTALL)


def check_dangling_refs(
    vault: Path, papers: list[dict[str, Any]]
) -> list[Issue]:
    """Any relation field (forward or reverse) referencing missing ids.

    Covers ``related`` / ``contradicts`` / ``extends`` and their ADR-012
    reverse fields ``contradicted-by`` / ``extended-by``, so a reverse edge
    left dangling by a deletion or rename is reported the same as a forward
    one. "Missing" is ``relations.dangling_relation_edges`` over
    ``document.library_paper_ids`` — the definition ``--fix`` drops by, so a
    finding here is always one ``--fix`` clears.

    The fix is ``--fix``, not ``lit modify``: a reverse field cannot be named
    in ``--rm-tag``, and the forward edge it mirrors lived on the paper that
    is gone, so no ``lit modify`` command reaches it.

    An edge into the trash says so, and offers the restore first: it is the
    one finding ``--fix`` can cost something on. Restoring the paper rebuilds
    the edges it names itself, but not one only this side held — and only
    such an edge is left behind by ``lit rm``.
    """
    known_ids = library_paper_ids(vault, papers)
    trashed: set[str] | None = None  # read only once something dangles
    out: list[Issue] = []
    for p in papers:
        pid = p.get("id")
        if not pid:
            continue
        for field, ref in dangling_relation_edges(p, known_ids):
            if trashed is None:
                trashed = {entry.paper_id for entry in list_trash(vault)}
            if ref in trashed:
                message = f"{field!r} references {ref!r}, which is in the trash"
                hint = (
                    f"`lit trash restore {ref}` to keep the link, or "
                    "`lit health-check --fix` to drop it"
                )
            else:
                message = f"{field!r} references missing paper {ref!r}"
                hint = "`lit health-check --fix` to drop the broken edge"
            out.append(
                Issue(
                    category="dangling_refs",
                    severity="error",
                    paper_id=str(pid),
                    message=message,
                    hint=hint,
                )
            )
    return out


def check_bidirectional_refs(
    vault: Path, papers: list[dict[str, Any]]
) -> list[Issue]:
    """Every relation edge must have its ADR-012 paired reverse edge.

    Three pairs are validated via ``RELATION_PAIRS``:

    * ``related`` is self-paired — A.related:[B] implies B.related:[A].
    * ``extends`` ↔ ``extended-by`` — A.extends:[B] implies
      B.extended-by:[A].
    * ``contradicts`` ↔ ``contradicted-by`` — A.contradicts:[B] implies
      B.contradicted-by:[A].

    ADR-012 retired the old "extends is intentionally directional" stance:
    directional relations are now stored symmetrically and maintained by
    the CLI's auto double-write, so a one-directional residual means the
    pairing got out of sync (e.g. a hand-edit or an interrupted write) and
    is reported as an error. Only edges whose other endpoint is a readable
    paper are checked: an edge naming no paper is ``check_dangling_refs``'s,
    and a folder whose metadata cannot be read is
    ``check_paper_dir_validity``'s.
    """
    by_id = {str(p.get("id")): p for p in papers if p.get("id")}
    out: list[Issue] = []
    # Deduplicate symmetric (``related``) findings: A↔B and B↔A are one
    # missing pairing. Directional pairs are never collapsed — the missing
    # side is unambiguous, so each is reported on its own field.
    seen_related_pairs: set[tuple[str, str]] = set()
    for pid, paper in by_id.items():
        for field, reverse in RELATION_PAIRS.items():
            for ref in relation_refs(paper.get(field)):
                other = by_id.get(ref)
                if other is None:
                    continue
                other_vals = set(relation_refs(other.get(reverse)))
                if pid in other_vals:
                    continue
                if field == reverse:
                    pair = cast(tuple[str, str], tuple(sorted((pid, ref))))
                    if pair in seen_related_pairs:
                        continue
                    seen_related_pairs.add(pair)
                # Phrase the fix as a command the CLI accepts: reverse
                # fields are not user-settable, so re-run the *forward*
                # edge and let the auto double-write regenerate the
                # reverse. When the residual sits on a reverse field, the
                # forward edge lives on the other paper (``ref``).
                if field in REVERSE_REF_FIELDS:
                    fwd, owner, tgt = reverse, ref, pid
                else:
                    fwd, owner, tgt = field, pid, ref
                out.append(
                    Issue(
                        category="bidirectional_refs",
                        severity="error",
                        paper_id=pid,
                        message=(
                            f"{pid!r}.{field} contains {ref!r} but "
                            f"{ref!r}.{reverse} does not contain {pid!r}"
                        ),
                        hint=(
                            f"`lit modify {owner} --rm-tag {fwd}={tgt}` then "
                            f"`--add-tag {fwd}={tgt}` to re-sync the pairing"
                        ),
                    )
                )
    return out


def _load_vault_paper_ids(vault_path: Path) -> set[str] | None:
    """Read ``INDEX.json`` from any vault and return its set of paper ids.

    Used by ``check_dangling_wikilinks`` to resolve cross-vault wikilinks
    (M8.4). Returns ``None`` when the vault path no longer exists, is not
    a directory, has no INDEX.json, or whose INDEX.json is unparseable —
    every one of those cases reads as "we can't verify what's in this
    fork" and the caller surfaces it as a dangling-link diagnostic.
    """
    import json

    if not vault_path.is_dir():
        return None
    index_path = vault_path / "INDEX.json"
    if not index_path.is_file():
        return None
    try:
        payload = json.loads(index_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    papers = payload.get("papers") or []
    return {
        str(p.get("id"))
        for p in papers
        if isinstance(p, dict) and p.get("id")
    }


def check_dangling_wikilinks(
    vault: Path, papers: list[dict[str, Any]]
) -> list[Issue]:
    """``[[id]]`` / ``[[vault:id]]`` in tracked notes vs the deletion-tag truth.

    M8.4 extends the legacy check: the inner target of every ``[[...]]``
    is parsed via :func:`parse_wikilink_target`; if a vault prefix is
    present we cross-check against that vault's registry entry +
    INDEX.json instead of the current vault's paper set.

    Same-vault ``[[X]]`` (M24 deletion-tag drift, two warning cases):

    * **Missing-tag** (``warning``) — ``[[X]]`` whose ``papers/X/`` does
      NOT exist and which is NOT followed by ``" (deleted)"``. The link
      points at a paper not in the vault and carries no deletion marker, so
      an agent reading the note may hallucinate the paper is still present.
      The filesystem cannot tell "deleted" from "never existed" (ADR-013:
      ``papers/X/`` presence is the only truth), so both collapse to this
      one warning — there is no separate "genuinely never existed" error
      for the same-vault case, which avoids double-reporting one ``[[X]]``.
    * **Stale-tag** (``warning``) — ``[[X]] (deleted)`` whose ``papers/X/``
      DOES exist (the paper was restored but the tag was not cleared, e.g.
      an agent rewrote the note past the de-annotation). The marker now
      lies; health-check surfaces it for the next write to clean.
    * A correctly-tagged deleted link (``[[X]] (deleted)`` with ``papers/X/``
      absent) and a correctly-bare live link (``[[X]]`` with ``papers/X/``
      present) are both clean — no issue.

    Failure modes the check distinguishes for CROSS-vault links (errors —
    a fork prefix that cannot resolve is a genuine breakage, not drift):

    * **Unregistered vault** — the prefix points at a name not in
      ``~/.config/litman/vaults.yaml``. Surfaced with a "register or
      correct the name" hint.
    * **Vault registered but unreadable** — the registry entry exists
      but the path no longer holds an INDEX.json (directory moved /
      vault never pushed). Surfaced with a "check vault info" hint.
    * **Paper id not found in target vault** — the vault loaded
      cleanly but the id is absent from its INDEX.json.
    * **Empty vault prefix / empty paper id** — malformed link
      (``[[:id]]`` or ``[[vault:]]``); reported with a clear message.

    Local import of vault_registry helpers avoids a cycle: vault_registry
    depends only on stdlib + pydantic, but pulling it into checks at
    module load would tie health-check init time to registry-load time
    even for vaults that contain zero cross-vault wikilinks.
    """
    from litman.core.vault_registry import find_by_name, load_registry

    # Same-vault existence is reconciled against papers/<id>/ directory presence
    # (ADR-013 / review F8), not the list_papers projection, so no id set is
    # precomputed here; cross-vault refs resolve against the target's INDEX.json.
    out: list[Issue] = []

    # Lazy-load the registry once: we don't want to pay the file IO when
    # no cross-vault link exists in the entire vault. None encodes both
    # "not loaded yet" and "loaded but registry corrupt"; the boolean
    # below disambiguates so we don't retry the load on every link.
    registry = None
    registry_loaded = False
    # Cache target vault → paper-id set so repeated cross-vault refs to
    # the same vault don't re-parse its INDEX.json.
    target_ids_cache: dict[str, set[str] | None] = {}

    for md_path in enumerate_markdown_files(vault):
        rel = md_path.relative_to(vault)
        try:
            text = md_path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            # No silent-skip (M30 / invariant #14): a notes file we cannot read
            # means its wikilinks go unchecked, which is itself a finding.
            out.append(
                Issue(
                    category="dangling_wikilinks",
                    severity="warning",
                    paper_id=None,
                    message=(
                        f"could not read notes file {rel} "
                        f"({exc.__class__.__name__}) — skipped"
                    ),
                    hint="check file permissions / encoding",
                )
            )
            continue
        # Strip HTML comments (e.g. the seeded wikilink reminder) before
        # scanning — a commented-out ``[[id]]`` is not a real reference. The
        # finditer AND the deletion-tag peek below both run on this stripped
        # text so match offsets stay mutually consistent.
        scan_text = _HTML_COMMENT_RE.sub("", text)
        # Same-vault dedup must account for per-occurrence tag state: the
        # same ``[[X]]`` can appear both ``(deleted)``-tagged and bare in
        # one file, and only the bare one is a missing-tag drift. Keying on
        # ``raw`` alone would drop the bare occurrence after a tagged one was
        # seen (M24.2 regression). Cross-vault links carry no tag, so their
        # dedup collapses on ``raw`` regardless of the (always-False) flag.
        seen: set[tuple[str, bool]] = set()
        for m in _WIKILINK_RE.finditer(scan_text):
            raw = m.group(1).strip()
            if not raw:
                continue
            # Peek at the char(s) after this link's ``]]`` for the deletion
            # marker (M24). Done on the slice, not the regex, so the form
            # rename + cross-vault logic see is unchanged.
            has_deleted_tag = scan_text.startswith(_DELETED_SUFFIX, m.end())
            key = (raw, has_deleted_tag)
            if key in seen:
                continue
            seen.add(key)

            vault_prefix, paper_id = parse_wikilink_target(raw)

            if vault_prefix is None:
                # Same-vault form: reconcile against the filesystem truth —
                # ``papers/<id>/`` directory presence (ADR-013), NOT list_papers
                # membership. A paper with corrupt / empty metadata.yaml is
                # dropped by list_papers but its directory still exists, so
                # keying on known_ids would falsely flag a live [[X]] as
                # "not in the vault" (review F8). The corrupt paper itself is
                # owned by check_paper_dir_validity. Two M24 drift warnings.
                exists = is_valid_id(paper_id) and (
                    vault / "papers" / paper_id
                ).is_dir()
                if not exists and not has_deleted_tag:
                    # Missing-tag: link to an absent paper with no marker.
                    out.append(
                        Issue(
                            category="dangling_wikilinks",
                            severity="warning",
                            paper_id=None,
                            message=(
                                f"{rel}: [[{raw}]] points at a paper not in the "
                                "vault but is not tagged (deleted)"
                            ),
                            hint=(
                                "mark it `[[id]] (deleted)`, correct the id, or "
                                "remove the bracket-link"
                            ),
                        )
                    )
                elif exists and has_deleted_tag:
                    # Stale-tag: marker says deleted but the paper is back.
                    out.append(
                        Issue(
                            category="dangling_wikilinks",
                            severity="warning",
                            paper_id=None,
                            message=(
                                f"{rel}: [[{raw}]] (deleted) but the paper exists "
                                "(stale deletion tag)"
                            ),
                            hint=(
                                "drop the ` (deleted)` marker — the paper was "
                                "restored"
                            ),
                        )
                    )
                continue

            # Cross-vault form (M8.4).
            if not vault_prefix or not paper_id:
                out.append(
                    Issue(
                        category="dangling_wikilinks",
                        severity="error",
                        paper_id=None,
                        message=(
                            f"{rel}: malformed cross-vault wikilink [[{raw}]] — "
                            "both vault name and paper id must be non-empty"
                        ),
                        hint="rewrite as [[vault-name:paper-id]]",
                    )
                )
                continue

            if not registry_loaded:
                try:
                    registry = load_registry()
                except VaultRegistryError:
                    registry = None
                registry_loaded = True

            entry = (
                find_by_name(registry, vault_prefix)
                if registry is not None else None
            )
            if entry is None:
                out.append(
                    Issue(
                        category="dangling_wikilinks",
                        severity="error",
                        paper_id=None,
                        message=(
                            f"{rel}: [[{raw}]] references unregistered "
                            f"vault {vault_prefix!r}"
                        ),
                        hint=(
                            "register the vault with "
                            f"`lit vault add {vault_prefix} <path>` "
                            "or correct the vault prefix"
                        ),
                    )
                )
                continue

            if vault_prefix not in target_ids_cache:
                target_ids_cache[vault_prefix] = _load_vault_paper_ids(
                    Path(entry.path)
                )
            target_ids = target_ids_cache[vault_prefix]
            if target_ids is None:
                out.append(
                    Issue(
                        category="dangling_wikilinks",
                        severity="error",
                        paper_id=None,
                        message=(
                            f"{rel}: [[{raw}]] target vault "
                            f"{vault_prefix!r} is unreadable "
                            f"(no INDEX.json at {entry.path})"
                        ),
                        hint=(
                            f"`lit vault info {vault_prefix}` to inspect; "
                            "restore the vault directory or remove this link"
                        ),
                    )
                )
                continue
            if paper_id not in target_ids:
                out.append(
                    Issue(
                        category="dangling_wikilinks",
                        severity="error",
                        paper_id=None,
                        message=(
                            f"{rel}: [[{raw}]] but vault "
                            f"{vault_prefix!r} has no paper id {paper_id!r}"
                        ),
                        hint=(
                            f"`lit list --vault {vault_prefix}` to find the "
                            "right id, or correct the link"
                        ),
                    )
                )
    return out


def check_views_vs_metadata(
    vault: Path, papers: list[dict[str, Any]]
) -> list[Issue]:
    """``views/by-*/`` link hubs ↔ metadata (ledger #2; full, klass A).

    The ``views/by-{project,topic,method,status}/`` link hubs are a derived
    projection of each paper's list/scalar tag fields (see ``core/views.py``).
    Out-of-band edits (manual ``rm`` of a paper, hand-edited tags) leave them
    disagreeing with metadata. Full-tier because the "missing link"
    direction needs the per-paper tag values (not in the thin INDEX
    projection). Repair is the shared klass-A regen (``rebuild_views``).

    Two directions, both ``error`` (a wrong view misleads the agent's
    bucket-based retrieval):

    * **dangling / extra link** — a ``views/<view>/<bucket>/<id>`` entry
      whose owning paper no longer carries that tag value (or no longer
      exists). The link claims a membership metadata does not.
    * **missing link** — a paper's tag value implies a
      ``views/<view>/<value>/<id>`` link that is absent on disk.

    Uses the same ``LIST_VIEW_FIELDS`` / ``SCALAR_VIEW_FIELDS`` /
    ``_safe_name`` mapping as the builder so detection and repair cannot drift.
    """
    from litman.core.views import (
        LIST_VIEW_FIELDS,
        SCALAR_VIEW_FIELDS,
        _safe_name,
    )

    views_dir = vault / "views"
    if not views_dir.is_dir():
        # No views hub yet — nothing derived to disagree. A first write
        # command / regen creates it.
        return []

    can_link = links_supported(vault)

    # Expected membership per view: {view_name: {(bucket, paper_id)}}.
    expected: dict[str, set[tuple[str, str]]] = {
        v: set() for v in (*LIST_VIEW_FIELDS, *SCALAR_VIEW_FIELDS)
    }
    for p in papers:
        pid = p.get("id")
        if not pid:
            continue
        pid = str(pid)
        for view_name, field_name in LIST_VIEW_FIELDS.items():
            for value in p.get(field_name) or []:
                expected[view_name].add((_safe_name(str(value)), pid))
        for view_name, field_name in SCALAR_VIEW_FIELDS.items():
            value = p.get(field_name)
            if value:
                expected[view_name].add((_safe_name(str(value)), pid))

    out: list[Issue] = []
    for view_name in (*LIST_VIEW_FIELDS, *SCALAR_VIEW_FIELDS):
        view_dir = views_dir / view_name
        on_disk: set[tuple[str, str]] = set()
        if view_dir.is_dir():
            for bucket in view_dir.iterdir():
                if not bucket.is_dir():
                    continue
                for entry in bucket.iterdir():
                    if is_portable_link(entry):
                        on_disk.add((bucket.name, entry.name))

        for bucket_name, pid in sorted(on_disk - expected[view_name]):
            out.append(
                Issue(
                    category="views_vs_metadata",
                    severity="error",
                    paper_id=pid,
                    message=(
                        f"views/{view_name}/{bucket_name}/{pid} link has no "
                        "matching metadata tag (stale derived view)"
                    ),
                    hint="run `lit health-check --fix` to rebuild views from metadata",
                )
            )
        if not can_link:
            # This filesystem cannot hold folder links at all (FAT32 / exFAT,
            # network shares — POSIX symlinks and Windows junctions both need
            # link storage). Every expected view would report "missing" and
            # `--fix` could not repair a single one — a 50-paper library would
            # show ~300 permanently-red errors that no user action inside
            # litman can clear. That is an environment capability, not library
            # damage, and reporting it as damage is a lie that drives users
            # away. `links_unsupported` says it once, as info.
            #
            # The stale arm above is NOT suppressed: removing a link works on
            # any filesystem, so a leftover view is still a real, fixable
            # defect.
            continue

        for bucket_name, pid in sorted(expected[view_name] - on_disk):
            out.append(
                Issue(
                    category="views_vs_metadata",
                    severity="error",
                    paper_id=pid,
                    message=(
                        f"metadata implies views/{view_name}/{bucket_name}/{pid} "
                        "but the link is missing (derived view out of date)"
                    ),
                    hint="run `lit health-check --fix` to rebuild views from metadata",
                )
            )
    return out
