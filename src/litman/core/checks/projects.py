"""Checks on lit-config.yaml and the projects it lists: the config parses
and agrees with TAXONOMY.md, project paths exist, REFERENCES.md and the bridge
links are current, the drive can hold links at all, and relevance- /
priority- grades still match membership.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from litman.core.checks._base import Issue
from litman.core.portable_link import (
    _os_error_reason,
    is_portable_link,
    links_supported,
    links_unsupported_hint,
)
from litman.core.schema import PROJECT_PRIORITY_PREFIX
from litman.core.taxonomy import parse_taxonomy, read_taxonomy_text
from litman.exceptions import ConfigError, UnreadableTaxonomyError


def check_config_readable(
    vault: Path, papers: list[dict[str, Any]]
) -> list[Issue]:
    """``lit-config.yaml`` parses (invariant #14 no-silent-skip; review F6/F27).

    Owns the "config is present but unparseable" finding so the config-dependent
    checks (project consistency / path-exists / references / pdf-viewer) can
    narrow their guard to ``except ConfigError: return []`` and defer here
    instead of each reporting a clean vault while config-keyed governance is
    actually blind. Emitting it once, from a dedicated cheap-tier check, mirrors
    ``check_vault_registry_drift``'s corrupt-registry finding and keeps the
    Tier-1 per-command hook and Tier-2 ``health-check`` consistent.

    Cheap-tier safe (invariant #15): reads only the single ``lit-config.yaml``
    file (the same bounded read ``check_project_path_exists`` already does),
    never per-paper ``metadata.yaml``.
    """
    from litman.core.config import load_config

    try:
        load_config(vault)
    except ConfigError as exc:
        return [
            Issue(
                category="config_unreadable",
                severity="error",
                paper_id=None,
                message=(
                    f"lit-config.yaml is unreadable ({exc.__class__.__name__}): "
                    "config-dependent checks cannot run"
                ),
                hint="inspect / repair lit-config.yaml (see `lit config show`)",
            )
        ]
    return []


def check_project_config_consistency(
    vault: Path, papers: list[dict[str, Any]]
) -> list[Issue]:
    """TAXONOMY.md ``## projects`` ↔ lit-config.yaml ``projects:`` same set.

    M15 added ``lit project`` which writes both truth sources atomically.
    Old hand-edited vaults (or a half-applied legacy edit) may have a name
    in one but not the other. Either direction is a warning, not an error:
    nothing is actively broken, but `lit link` / register-first validation
    will behave inconsistently until reconciled. NOT auto-fixable — the
    user decides which side is authoritative.
    """
    taxonomy_file = vault / "TAXONOMY.md"
    if not taxonomy_file.is_file():
        return []
    try:
        registered = parse_taxonomy(read_taxonomy_text(vault))
    except UnreadableTaxonomyError:
        # Unreadable TAXONOMY.md is OWNED by check_taxonomy_drift (it emits the
        # error); deferring here keeps it a single report (invariant #14 is
        # satisfied by the other check, not violated by this return []).
        return []

    from litman.core.config import load_config

    try:
        config = load_config(vault)
    except ConfigError:
        # Unparseable config is OWNED by check_config_readable (it emits the
        # error); deferring keeps it a single report. Narrowed from
        # `except Exception` (review F6) so a genuine bug in this check now
        # propagates instead of masquerading as a clean vault.
        return []

    taxonomy_names = set(registered.get("projects", []))
    config_names = set(config.projects)

    out: list[Issue] = []
    for name in sorted(taxonomy_names - config_names):
        out.append(
            Issue(
                category="project_config_consistency",
                severity="warning",
                paper_id=None,
                message=(
                    f"project {name!r} is in TAXONOMY.md but not in "
                    "lit-config.yaml's projects: map"
                ),
                hint=(
                    f"`lit project add {name} --path <path>` to register "
                    "its path, or remove it from TAXONOMY.md via "
                    f"`lit project rm {name}`"
                ),
            )
        )
    for name in sorted(config_names - taxonomy_names):
        out.append(
            Issue(
                category="project_config_consistency",
                severity="warning",
                paper_id=None,
                message=(
                    f"project {name!r} is in lit-config.yaml but not in "
                    "TAXONOMY.md's ## projects section"
                ),
                hint=(
                    f"`lit project add {name} --path <existing-path>` to "
                    "complete the registration (or fix the yaml by hand)"
                ),
            )
        )
    return out


def check_project_path_exists(
    vault: Path,
    papers: list[dict[str, Any]],
    *,
    exists_status: dict[str, bool | None] | None = None,
) -> list[Issue]:
    """Every lit-config.yaml ``projects:`` path exists and is a directory.

    Common after cross-machine sync (rclone / USB) where the registry
    travels but the project working directories live at different absolute
    paths per machine. Warning (not error) + NOT auto-fixable: only the
    user knows the correct path on this machine.

    Mount-safe (ADR-014): each configured path is probed with the bounded-stat
    (:func:`_drift._exists_bounded`) so a dropped HPC mount yields ``None``
    (unknown, never flagged) instead of hanging on a bare ``stat()``. This
    keeps the divergence-free guarantee in the ``health-check`` path too, not
    just the hook. A definite ``False`` is "does not exist"; a ``True`` that is
    not a directory is still surfaced via a direct ``is_dir`` check (the path
    is reachable, so the extra stat is cheap and cannot hang).

    ``exists_status`` (M30 Phase 5 / verification task 3): the Tier-1 cheap hook
    threads in its single shared bounded-stat result so this check does not
    re-probe. Tier-2 passes ``None`` → falls back to its own probe, unchanged.
    """
    from litman.commands._drift import _exists_bounded
    from litman.core.config import load_config

    try:
        config = load_config(vault)
    except ConfigError:
        # Owned by check_config_readable (also cheap-tier, so it fires in the
        # same Tier-1 hook). Narrowed from `except Exception` (review F6/F27)
        # so a real bug propagates instead of reporting a clean vault.
        return []

    if not config.projects:
        return []

    paths = {
        name: str(Path(path_str).expanduser())
        for name, path_str in config.projects.items()
    }
    if exists_status is None:
        status = _exists_bounded(list(paths.values()))
    else:
        status = {p: exists_status.get(p) for p in paths.values()}

    out: list[Issue] = []
    for name, path_str in sorted(config.projects.items()):
        project_dir = Path(path_str).expanduser()
        present = status.get(str(project_dir))
        if present is None:
            # Unknown (slow / dropped mount) — never flag (ADR-014).
            continue
        if present is False:
            out.append(
                Issue(
                    category="project_path_exists",
                    severity="warning",
                    paper_id=None,
                    message=(
                        f"project {name!r} path does not exist: {path_str}"
                    ),
                    hint=(
                        f"`lit project set-path {name} <correct-path>` "
                        "(likely cross-machine path drift)"
                    ),
                )
            )
        elif not project_dir.is_dir():
            out.append(
                Issue(
                    category="project_path_exists",
                    severity="warning",
                    paper_id=None,
                    message=(
                        f"project {name!r} path is not a directory: "
                        f"{path_str}"
                    ),
                    hint=(
                        f"`lit project set-path {name} <correct-path>`"
                    ),
                )
            )
    return out


_FOLDER_COPY_HINT = (
    "`lit health-check --fix` replaces it with a link; a copy that does not "
    "match the vault is kept in .trash/"
)


def _folder_copy_issue(path: Path, paper_id: str | None, *, known: bool) -> Issue:
    """A hub position holding a real folder where a litman link belongs.

    Copy tools expand links, so this is what a project directory looks like
    after it travels between machines. Reported as its own finding rather than
    as "the link is missing": the position is occupied, and the old wording
    sent the user looking for something that was never lost.
    """
    return Issue(
        category="project_references",
        severity="error",
        paper_id=paper_id,
        message=(
            f"{path} is a folder copy, not a litman link "
            "(copied from another machine?)"
            if known
            else f"{path} is a stale folder copy, not a litman link"
        ),
        hint=_FOLDER_COPY_HINT,
    )


def check_project_references(
    vault: Path, papers: list[dict[str, Any]]
) -> list[Issue]:
    """Project ``REFERENCES.md`` / ``litman_reflib/`` / ``litman_code/`` ↔ membership (ledger #3).

    For each configured project whose directory is reachable (bounded-stat),
    compare the derived project-side artifacts against the project's membership
    set (papers whose ``projects`` field contains the name):

    * the generated ``REFERENCES.md`` content vs the freshly rendered content;
    * the ``litman_reflib/<id>`` link set vs the membership ids;
    * the ``litman_code/<repo>`` link set vs the repos bound (via
      ``code-clones``) by member papers whose clone is present locally.

    Any mismatch is a klass-A drift (the derived artifact is a pure function of
    metadata). The code-link arm specifically catches the
    ``lit code add``/``link``/``unlink`` staleness window: those commands mutate
    ``code-clones`` and refresh the links via ``refresh_project_code_links``,
    so a leftover mismatch means the refresh was bypassed (hand-edit, partial
    write, an older binary). Full-tier: needs per-paper ``projects`` /
    ``relevance`` / ``code-clones`` (REFERENCES.md embeds relevance). Repair is
    the shared regen (``rebuild_all_project_refs`` + ``rebuild_all_project_links``,
    which rebuilds BOTH link hubs). Only checked when the project dir is
    definitely present — an unreachable / not-yet-mounted dir is left to
    ``project_path_exists`` (ledger #5), not flagged as content drift here.
    """
    from litman.commands._drift import _exists_bounded
    from litman.core.config import load_config

    try:
        config = load_config(vault)
    except ConfigError:
        # Owned by check_config_readable; narrowed from `except Exception`
        # (review F6) so real bugs propagate.
        return []
    if not config.projects:
        return []

    paths = {
        name: str(Path(path_str).expanduser())
        for name, path_str in config.projects.items()
    }
    status = _exists_bounded(list(paths.values()))

    out: list[Issue] = []
    for name in sorted(config.projects):
        project_dir = Path(paths[name])
        if status.get(str(project_dir)) is not True:
            # Unreachable / unknown — project_path_exists owns that case.
            continue
        if not project_dir.is_dir():
            continue

        try:
            out.extend(_project_reference_issues(vault, name, project_dir, papers))
        except OSError as err:
            out.append(_unreadable_project_issue(name, project_dir, err))
    return out


def _project_reference_issues(
    vault: Path, name: str, project_dir: Path, papers: list[dict[str, Any]]
) -> list[Issue]:
    """:func:`check_project_references` for one project whose directory is
    there. Raises ``OSError`` when a hub (or REFERENCES.md) cannot be read; the
    caller turns that into one finding for the project."""
    from litman.core.project_link import CODE_SUBDIR
    from litman.core.project_refs import (
        LITERATURE_SUBDIR,
        REFERENCES_FILENAME,
        _papers_for_project,
        render_references_md,
    )

    out: list[Issue] = []
    reflib = project_dir / LITERATURE_SUBDIR
    member_papers = _papers_for_project(papers, name)
    member_ids = {
        str(p.get("id")) for p in member_papers if p.get("id")
    }

    # 1) REFERENCES.md content vs freshly rendered (banner timestamp is the
    #    only volatile line; compare with the timestamp pinned so a stale
    #    body — not just a clock difference — is what we report).
    refs_file = reflib / REFERENCES_FILENAME
    existing = None
    if refs_file.is_file():
        existing = refs_file.read_text(encoding="utf-8")
    if existing is not None:
        pinned_now = _references_banner_timestamp(existing)
        expected = render_references_md(
            vault, name, now=pinned_now, papers=papers
        )
        if _strip_references_banner(existing) != _strip_references_banner(
            expected
        ):
            out.append(
                Issue(
                    category="project_references",
                    severity="error",
                    paper_id=None,
                    message=(
                        f"project {name!r} REFERENCES.md is out of date "
                        "with the membership set (derived content drift)"
                    ),
                    hint="run `lit health-check --fix` to regenerate it",
                )
            )
    elif member_ids:
        out.append(
            Issue(
                category="project_references",
                severity="error",
                paper_id=None,
                message=(
                    f"project {name!r} has {len(member_ids)} member paper(s) "
                    "but no litman_reflib/REFERENCES.md"
                ),
                hint="run `lit health-check --fix` to generate it",
            )
        )

    # 2) litman_reflib/<id> link set vs membership.
    link_ids: set[str] = set()
    foreign_dirs: set[str] = set()
    if reflib.is_dir():
        for entry in reflib.iterdir():
            # is_portable_link first: a junction answers is_dir() True, so
            # the other order files every Windows link as a folder copy.
            if is_portable_link(entry):
                link_ids.add(entry.name)
            elif entry.is_dir():
                foreign_dirs.add(entry.name)
    for extra in sorted(link_ids - member_ids):
        out.append(
            Issue(
                category="project_references",
                severity="error",
                paper_id=extra,
                message=(
                    f"{project_dir / LITERATURE_SUBDIR / extra} link has "
                    f"no matching membership in project {name!r}"
                ),
                hint="run `lit health-check --fix` to rebuild project links",
            )
        )
    # Probed on the PROJECT dir, not the vault: a vault on an internal
    # drive and a project folder on an exFAT stick give different answers,
    # so one project can be reportable while another is not. When the
    # filesystem cannot hold links the bridges were never made, `--fix`
    # cannot make them, and reporting one error per member paper is noise
    # no user can clear — `links_unsupported` states it once instead. The
    # `extra` arms above stay live: removing a link works anywhere.
    project_can_link = links_supported(project_dir)

    if project_can_link:
        for missing in sorted(member_ids - link_ids - foreign_dirs):
            out.append(
                Issue(
                    category="project_references",
                    severity="error",
                    paper_id=missing,
                    message=(
                        f"project {name!r} membership implies a litman_reflib "
                        f"link for {missing!r} but it is missing"
                    ),
                    hint="run `lit health-check --fix` to rebuild project links",
                )
            )
        for occupied in sorted(foreign_dirs):
            known = occupied in member_ids
            out.append(
                _folder_copy_issue(
                    reflib / occupied,
                    occupied if known else None,
                    known=known,
                )
            )

    # 3) litman_code/<repo> link set vs derived code-clone membership.
    #    Parallel to the litman_reflib block: expected = every repo bound
    #    (code-clones) by a member paper whose codes/<repo>/repo clone is
    #    present locally (mirrors rebuild_all_project_links, which skips
    #    absent clones — a missing clone is owned by
    #    check_code_clone_integrity / `lit code restore-all`).
    expected_repos: set[str] = set()
    for p in member_papers:
        for repo_name in p.get("code-clones") or []:
            if (vault / "codes" / str(repo_name) / "repo").exists():
                expected_repos.add(str(repo_name))
    code_dir = project_dir / CODE_SUBDIR
    code_link_names: set[str] = set()
    code_foreign_dirs: set[str] = set()
    if code_dir.is_dir():
        for entry in code_dir.iterdir():
            if is_portable_link(entry):
                code_link_names.add(entry.name)
            elif entry.is_dir():
                code_foreign_dirs.add(entry.name)
    for extra in sorted(code_link_names - expected_repos):
        out.append(
            Issue(
                category="project_references",
                severity="error",
                paper_id=None,
                message=(
                    f"{code_dir / extra} link has no matching code-clone "
                    f"membership in project {name!r} (stale project link)"
                ),
                hint="run `lit health-check --fix` to rebuild project links",
            )
        )
    if project_can_link:
        for missing in sorted(
            expected_repos - code_link_names - code_foreign_dirs
        ):
            out.append(
                Issue(
                    category="project_references",
                    severity="error",
                    paper_id=None,
                    message=(
                        f"project {name!r} membership implies a litman_code "
                        f"link for {missing!r} but it is missing"
                    ),
                    hint="run `lit health-check --fix` to rebuild project links",
                )
            )
        for occupied in sorted(code_foreign_dirs):
            out.append(
                _folder_copy_issue(
                    code_dir / occupied,
                    None,
                    known=occupied in expected_repos,
                )
            )
    return out


def _unreadable_project_issue(name: str, project_dir: Path, err: OSError) -> Issue:
    """A project folder the check could not read through. One finding for the
    project, in place of the comparisons it could not make (an unreadable
    ``litman_reflib/`` would otherwise report every member as missing)."""
    where = err.filename if err.filename else project_dir
    return Issue(
        category="project_references",
        severity="warning",
        paper_id=None,
        message=(
            f"could not read {where} ({_os_error_reason(err)}) — project "
            f"{name!r} not checked"
        ),
        hint="fix that, then run `lit health-check --fix`",
    )


def check_link_support(
    vault: Path,
    papers: list[dict[str, Any]],
) -> list[Issue]:
    """Report — once — that this filesystem cannot hold folder links.

    Not a drift check (klass ``env``): nothing is out of date and nothing is
    broken. It states a capability of the drive, so that the *absence* of
    views/ and of the litman_reflib / litman_code shortcuts is explained rather
    than silent, while ``views_vs_metadata`` and ``project_references`` stay
    quiet about links they know cannot exist.

    This fires only where NO link mechanism works: POSIX symlinks and Windows
    junctions both need a filesystem that stores links, which FAT32 / exFAT and
    most network shares do not. It is a property of the drive, not of the
    user's privileges — no setting, elevation or mode change would alter the
    verdict, so the hint recommends none.

    Severity is ``info`` on purpose, and ``health-check`` does not gate its
    exit code on info: a library on such a drive is perfectly healthy, and must
    not be told otherwise.

    Scopes probed: the vault (which owns ``views/``) and every configured
    project directory that exists (each owns its own bridges). They are probed
    separately because they can live on different filesystems — a vault on an
    internal drive and a project on an exFAT stick disagree. All failing scopes
    collapse into a single finding: a vault on such a drive fails all of them
    at once and deserves one line, not N.
    """
    from litman.core.config import load_config

    scopes: list[str] = []

    if vault.is_dir() and not links_supported(vault):
        scopes.append("the library (views/)")

    try:
        config = load_config(vault)
    except ConfigError:
        config = None
    if config is not None:
        for name in sorted(config.projects):
            project_dir = Path(config.projects[name]).expanduser()
            if not project_dir.is_dir():
                # Unreachable / missing — project_path_exists owns that case.
                continue
            if not links_supported(project_dir):
                scopes.append(f"project {name!r} (litman_reflib / litman_code)")

    if not scopes:
        return []

    return [
        Issue(
            category="links_unsupported",
            severity="info",
            paper_id=None,
            message=(
                "folder links cannot be created here — "
                + ", ".join(scopes)
                + ". Those shortcuts are skipped; your library itself is fine: "
                "metadata.yaml and INDEX.json are authoritative, and every "
                "command, the web UI and the agent workflow work normally."
            ),
            hint=links_unsupported_hint(),
        )
    ]


def _references_banner_timestamp(text: str) -> str:
    """Extract the ``<!-- Last updated: ... -->`` timestamp from a REFERENCES.md.

    Returns the embedded timestamp so the freshly rendered comparison body uses
    the same banner line (the timestamp is the only legitimately-volatile part;
    we want to flag a stale *body*, not a clock difference). Falls back to an
    empty string when no banner is present.
    """
    m = re.search(r"<!-- Last updated: (.*?) -->", text)
    return m.group(1) if m else ""


def _strip_references_banner(text: str) -> str:
    """Drop the auto-generated banner comment lines for body-only comparison."""
    return "\n".join(
        line
        for line in text.splitlines()
        if not line.startswith("<!-- ")
    )


def check_project_bridge_dangling(
    vault: Path,
    papers: list[dict[str, Any]],
    *,
    exists_status: dict[str, bool | None] | None = None,
) -> list[Issue]:
    """Project bridge links still resolve (ledger #3's cheap arm; klass A).

    The ``litman_reflib/<id>`` / ``litman_code/<repo>`` links bridge a
    project working directory into the vault ACROSS trees: their stored
    target (relative on POSIX, absolute for Windows junctions) points at
    wherever the vault was when the link was written. Moving the vault (or deleting a
    linked target) dangles them — and the name-set comparison in
    ``check_project_references`` cannot see it, because the names still match
    membership. This check probes what names cannot say: does each bridge
    still resolve?

    Metadata-free (invariant #15): config projects map + hub directory
    listings + a bounded-stat of the link targets — never a per-paper
    ``metadata.yaml``. That earns it tier=cheap, so the Tier-1 hook surfaces
    a moved library at the user's NEXT command (surface-drift-eagerly, M28)
    instead of waiting for a manual ``health-check`` nobody remembers to run.

    Mount-safe (ADR-014): targets are probed with the bounded stat; ``None``
    (slow / dropped mount) is never flagged — only a definite ``False`` is
    drift. Projects whose own directory is not definitely present are skipped
    (``project_path_exists`` owns that finding).

    One issue per project, not per link: a moved vault dangles every bridge
    at once and the repair is a wholesale rebuild, so per-link issues would
    repeat the same fact N times.

    ``exists_status``: the Tier-1 hook threads its shared bounded-stat of the
    project DIR paths in; the per-link target probe is always this check's
    own second bounded call (the shared probe never stats link targets).
    """
    from litman.commands._drift import _exists_bounded
    from litman.core.config import load_config
    from litman.core.project_link import find_dangling_bridges

    try:
        config = load_config(vault)
    except ConfigError:
        # Owned by check_config_readable (same tier, fires in the same hook).
        return []
    if not config.projects:
        return []

    paths = {
        name: str(Path(path_str).expanduser())
        for name, path_str in config.projects.items()
    }
    if exists_status is None:
        dir_status = _exists_bounded(list(paths.values()))
    else:
        dir_status = {p: exists_status.get(p) for p in paths.values()}

    out: list[Issue] = []
    for name, dangling in find_dangling_bridges(
        dict(config.projects), dir_status, _exists_bounded
    ).items():
        out.append(
            Issue(
                category="project_bridge_dangling",
                severity="error",
                paper_id=None,
                message=(
                    f"project {name!r}: {len(dangling)} bridge "
                    f"link{'s point' if len(dangling) != 1 else ' points'} "
                    f"at nothing (e.g. {dangling[0]}) — the library, or what "
                    "a link targeted, has moved"
                ),
                hint=(
                    "run `lit health-check --fix` (or `lit refresh-views`) "
                    "to rebuild project links"
                ),
            )
        )
    return out


def _prefix_key_orphans(
    papers: list[dict[str, Any]],
    *,
    prefix: str,
    category: str,
    message: str,
    hint: str,
) -> list[Issue]:
    """``<prefix><project>`` keys on a paper that is not in ``<project>``.

    Shared body of :func:`check_relevance_orphan` (ledger #11) and
    :func:`check_priority_orphan` (its ``priority-<project>`` twin, ADR-025).
    Both keys are per-project annotations written by the link path, so both
    strand the same way — the paper was unlinked without purging the key, or a
    ``project rm`` / ``rename`` did not cascade.

    The project name is the key with ``prefix`` stripped, matched against the
    ``projects`` list. Never a split on ``-``: real project names carry hyphens
    (``binder-design``), so ``priority-binder-design`` names ONE project.

    ``message`` and ``hint`` are per-caller, not built from a shared noun: the
    relevance wording is the one this check has always emitted and must stay
    byte-identical, and the repair differs anyway (authored prose that must
    never be auto-deleted vs one letter ``lit unlink`` drops). Both are
    templates over ``{pid}`` / ``{key}`` / ``{project}``.
    """
    out: list[Issue] = []
    for p in papers:
        pid = p.get("id")
        if not pid:
            continue
        member_projects = set(p.get("projects") or [])
        for key in p:
            if not isinstance(key, str) or not key.startswith(prefix):
                continue
            project = key[len(prefix):]
            if not project:
                continue
            if project not in member_projects:
                out.append(
                    Issue(
                        category=category,
                        severity="warning",
                        paper_id=str(pid),
                        message=message.format(
                            pid=pid, key=key, project=project
                        ),
                        hint=hint.format(pid=pid, key=key, project=project),
                    )
                )
    return out


def check_relevance_orphan(
    vault: Path, papers: list[dict[str, Any]]
) -> list[Issue]:
    """``relevance-<project>`` field whose paper is not in that project (ledger #11).

    klass B-auth: the relevance text is hand-authored, so this is
    **report-only** — never auto-deleted. Full-tier (the ``relevance-*`` fields
    are not in the INDEX projection).
    """
    return _prefix_key_orphans(
        papers,
        prefix="relevance-",
        category="relevance_orphan",
        message=(
            "{pid!r} has a {key!r} annotation but its projects "
            "list does not contain {project!r} (orphan relevance)"
        ),
        hint=(
            "`lit modify {pid} --add-tag projects={project}` to "
            "re-link, or remove the {key} field by hand "
            "(authored text — never auto-deleted)"
        ),
    )


def check_priority_orphan(
    vault: Path, papers: list[dict[str, Any]]
) -> list[Issue]:
    """``priority-<project>`` grade whose paper is not in that project (ADR-025).

    Same klass/tier as its relevance twin above: the grade is authored, so this
    reports rather than deleting. It stays even though ``lit unlink`` drops the
    grade — this is the check for the unlink that did not clean up (hand-edit,
    interrupted cascade), which is exactly what the relevance row covers too.
    """
    return _prefix_key_orphans(
        papers,
        prefix=PROJECT_PRIORITY_PREFIX,
        category="priority_orphan",
        message=(
            "{pid!r} has a {key!r} grade but its projects "
            "list does not contain {project!r} (orphan priority)"
        ),
        hint=(
            "`lit modify {pid} --add-tag projects={project}` to re-link, or "
            "`lit unlink {pid} --project {project}` to drop the grade"
        ),
    )
