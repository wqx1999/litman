"""Per-project ``REFERENCES.md`` generator (M5.1).

For each project registered in ``lit-config.yaml``'s ``projects`` map,
this module emits ``<project_dir>/litman_reflib/REFERENCES.md`` listing
every paper whose metadata ``projects`` field contains the project name.
The file is grouped by priority (A → B → C → unprioritized) and within
each group sorted by year descending then by id alphabetically.

Single-truth source: each paper's ``metadata.yaml`` holds the authoritative
``projects`` list AND the per-project relevance annotation
(``relevance-<project>:``). REFERENCES.md is derived; never edit it by
hand (the AUTO-GENERATED banner says so).

Wiki-links (``[[<id>]]``) are used instead of markdown filesystem paths
so the file resolves in Obsidian/Foam and stays valid even if the
litman_reflib/ symlinks (created by ``lit link``, M5.2) are absent — e.g.
on a fresh machine before the user runs ``lit link --rebuild-all``.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from litman.core.document import list_papers

REFERENCES_FILENAME = "REFERENCES.md"
LITERATURE_SUBDIR = "litman_reflib"

# Priority order, with ``None`` (unprioritized) coming last.
_PRIORITY_ORDER = ("A", "B", "C")


def _now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def _format_authors(authors: list[str]) -> str:
    """Format the author list for the bullet header.

    First 2 authors then 'et al.' for longer lists. Each author is the raw
    'Family, Given' string from metadata; we just keep the family portion
    here to stay terse — the full list is one ``lit show <id>`` away.
    """
    if not authors:
        return "(unknown)"
    families = [a.split(",", 1)[0].strip() for a in authors if a]
    if len(families) == 1:
        return families[0]
    if len(families) == 2:
        return f"{families[0]}, {families[1]}"
    return f"{families[0]}, {families[1]} et al."


def _format_year(year: Any) -> str:
    if year in (None, "", 0):
        return "n.d."
    return str(year)


def _papers_for_project(papers: list[dict[str, Any]], project: str) -> list[dict[str, Any]]:
    """Filter papers whose ``projects`` list contains ``project``.

    Comparison is exact string match (case-sensitive) — the TAXONOMY
    convention is lowercase hyphenated names, so case mismatches indicate
    a typo and should NOT silently match.
    """
    matched = []
    for p in papers:
        projs = p.get("projects") or []
        if not isinstance(projs, list):
            continue
        if project in projs:
            matched.append(p)
    return matched


def _group_by_priority(
    papers: list[dict[str, Any]],
) -> dict[str | None, list[dict[str, Any]]]:
    """Bucket papers by ``priority``. Unknown / missing → ``None`` bucket.

    Within each bucket, sort by year descending then by id ascending.
    """
    buckets: dict[str | None, list[dict[str, Any]]] = {}
    for p in papers:
        pr = p.get("priority")
        key = pr if pr in _PRIORITY_ORDER else None
        buckets.setdefault(key, []).append(p)
    for key, group in buckets.items():
        group.sort(
            key=lambda x: (
                -(int(x["year"]) if x.get("year") not in (None, "") else 0),
                str(x.get("id", "")),
            )
        )
    return buckets


def _render_bullet(paper: dict[str, Any], project: str) -> str:
    """Render one paper entry as a markdown bullet (multi-line).

    Format::

        - **<title>** (<authors>, <year>). `[[<id>]]`

          *<relevance-<project> text if present>*

    The relevance line is suppressed when the field is missing or empty
    so the file stays clean for newly-linked papers awaiting annotation.
    """
    paper_id = paper.get("id", "?")
    title = paper.get("title", "(untitled)")
    authors = _format_authors(paper.get("authors") or [])
    year = _format_year(paper.get("year"))
    relevance_key = f"relevance-{project}"
    relevance = paper.get(relevance_key)
    head = f"- **{title}** ({authors}, {year}). `[[{paper_id}]]`"
    if relevance:
        return f"{head}\n\n  *{relevance}*"
    return head


def render_references_md(
    vault: Path,
    project: str,
    *,
    now: str | None = None,
    papers: list[dict[str, Any]] | None = None,
) -> str:
    """Render the full ``REFERENCES.md`` content for ``project``.

    Args:
        vault: Vault root.
        project: Project name (matches a value in some paper's
            ``projects`` list).
        now: Override timestamp for banner — testing only.
        papers: Override paper list — testing only; if absent, scans
            ``<vault>/papers/`` via ``list_papers``.

    Returns:
        Multi-line markdown string ready to write.
    """
    if papers is None:
        papers = list_papers(vault)
    matched = _papers_for_project(papers, project)
    buckets = _group_by_priority(matched)

    lines: list[str] = []
    lines.append(
        "<!-- AUTO-GENERATED by `lit refresh-views` — DO NOT EDIT -->"
    )
    lines.append(f"<!-- Last updated: {now or _now_iso()} -->")
    lines.append("")
    lines.append(f"# {project} — Literature")
    lines.append("")
    n = len(matched)
    if n == 0:
        lines.append(
            f"*No papers tagged with `projects: [{project}]` yet. "
            f"Add one with `lit modify <id> --add-tag projects={project}` "
            "and rerun `lit refresh-views`.*"
        )
        lines.append("")
        return "\n".join(lines)

    lines.append(
        f"**{n} paper{'s' if n != 1 else ''}** "
        "(sorted by priority, then by year descending)."
    )
    lines.append("")

    for key in (*_PRIORITY_ORDER, None):
        group = buckets.get(key, [])
        if not group:
            continue
        label = f"Priority {key}" if key else "Unprioritized"
        lines.append(f"## {label} ({len(group)} paper{'s' if len(group) != 1 else ''})")
        lines.append("")
        for p in group:
            lines.append(_render_bullet(p, project))
            lines.append("")

    return "\n".join(lines)


def write_references_md(
    vault: Path,
    project: str,
    project_dir: Path,
    *,
    now: str | None = None,
    papers: list[dict[str, Any]] | None = None,
) -> Path:
    """Write ``<project_dir>/litman_reflib/REFERENCES.md``.

    Auto-creates ``<project_dir>/litman_reflib/`` if missing. The project
    directory itself must exist on disk (this is the user's working
    project root; auto-creating it would surprise the user).

    Returns:
        Path to the written file.

    Raises:
        FileNotFoundError: ``project_dir`` does not exist. Caller is
            expected to surface this as a friendly skip rather than a
            hard failure — see ``rebuild_all_project_refs``.
    """
    if not project_dir.is_dir():
        raise FileNotFoundError(
            f"Project directory does not exist: {project_dir}"
        )
    literature_dir = project_dir / LITERATURE_SUBDIR
    literature_dir.mkdir(exist_ok=True)
    target = literature_dir / REFERENCES_FILENAME
    target.write_text(
        render_references_md(vault, project, now=now, papers=papers),
        encoding="utf-8",
    )
    return target


def rebuild_all_project_refs(
    vault: Path,
    project_registry: dict[str, str],
    *,
    now: str | None = None,
) -> dict[str, dict[str, Any]]:
    """Regenerate ``REFERENCES.md`` for every project in the registry.

    Per-project failures (e.g. project_dir missing on this machine) do
    NOT abort the loop — each project gets a status entry. Common on a
    fresh machine where some projects have not been cloned yet.

    Args:
        vault: Vault root.
        project_registry: ``lit-config.yaml``'s ``projects`` mapping.
        now: Override timestamp — testing only.

    Returns:
        ``{project_name: {"status": "written" | "skipped" | "error",
                           "path": Path | None,
                           "n_papers": int,
                           "detail": str}}``
    """
    papers = list_papers(vault)
    out: dict[str, dict[str, Any]] = {}
    for project, project_dir_str in sorted(project_registry.items()):
        project_dir = Path(project_dir_str).expanduser()
        n_papers = len(_papers_for_project(papers, project))
        if not project_dir.is_dir():
            out[project] = {
                "status": "skipped",
                "path": None,
                "n_papers": n_papers,
                "detail": f"project dir not found: {project_dir}",
            }
            continue
        try:
            target = write_references_md(
                vault, project, project_dir, now=now, papers=papers
            )
        except Exception as e:  # defensive — write_references_md raises only FileNotFoundError currently
            out[project] = {
                "status": "error",
                "path": None,
                "n_papers": n_papers,
                "detail": str(e),
            }
            continue
        out[project] = {
            "status": "written",
            "path": target,
            "n_papers": n_papers,
            "detail": "",
        }
    return out
