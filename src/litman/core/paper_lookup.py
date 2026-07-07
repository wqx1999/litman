"""Paper-id input ergonomics for ``--paper`` / paper-id arguments (M11).

Three resolution strategies, layered on top of the strict-id model:

1. **Fuzzy substring match** (``resolve_paper_id``): exact id wins; otherwise
   case-fold substring match enumerates ``<vault>/papers/*/``. Unique hit
   wins; zero hits or 2+ hits raise ``PaperNotFoundError``.

2. **DOI reverse-lookup** (``find_paper_id_by_doi``): thin wrapper around
   ``core.dedup.find_paper_by_doi`` that surfaces just the id and raises
   ``PaperNotFoundError`` on miss.

3. **Shell completion** (``complete_paper_id``): Click ``shell_complete``
   callback. Failure-safe: any exception (vault not discoverable, broken
   metadata, OS error) returns ``[]`` so a misconfigured environment cannot
   break a user's shell session.

The ``resolve_paper_input`` helper dispatches between the fuzzy and DOI
channels for command sites that accept either ``--paper`` (or a positional
id) or ``--paper-doi``, enforcing the mutual-exclusion contract.

Layered above the strict ``core.document.find_paper``: callers fuzzy-resolve
first, then pass the canonical id into the existing ``find_paper`` /
metadata read paths. No existing call sites need to learn about query
resolution — the ergonomics shim lives entirely in commands/.
"""

from __future__ import annotations

import os
from difflib import get_close_matches
from pathlib import Path

import click

from litman.core.dedup import find_paper_by_doi
from litman.core.library import find_vault
from litman.exceptions import LitmanError, PaperNotFoundError


def resolve_paper_id(vault: Path, query: str) -> str:
    """Resolve a fuzzy paper-id query to an exact id.

    Order:
        1. Exact match against ``<vault>/papers/<query>/`` → return it.
        2. Case-fold substring match against every paper id:
            - 1 hit → return it.
            - 0 hits → ``PaperNotFoundError``.
            - 2+ hits → ``PaperNotFoundError`` listing candidates so the
              user can pick a more specific substring.

    Title / author search is intentionally NOT performed here either:
    natural-language disambiguation belongs to the ``lit-reading`` skill,
    which has context the CLI lacks. Mirrors ``core.viewer.resolve_paper_id``
    in spirit, but lifts the ambiguous case to ``PaperNotFoundError`` so
    every M11 call site can use a single exception type.

    Args:
        vault: Vault root (must contain a ``papers/`` subdir).
        query: User-supplied id (exact or partial).

    Returns:
        Canonical paper id.

    Raises:
        PaperNotFoundError: zero matches OR two-plus matches.
    """
    papers_dir = vault / "papers"
    all_ids: list[str] = []
    if papers_dir.is_dir():
        for child in sorted(papers_dir.iterdir()):
            if child.is_dir() and (child / "metadata.yaml").is_file():
                all_ids.append(child.name)

    if query in all_ids:
        return query

    q_lower = query.lower()
    matches = [pid for pid in all_ids if q_lower in pid.lower()]

    if not matches:
        # A non-substring typo (e.g. "2020_Vaswni") lands here. Offer the
        # closest ids as a did-you-mean before falling back to `lit list`.
        # Message-only: the exception type / exit code are unchanged, so an
        # agent parsing failures is unaffected.
        msg = f"No paper matching {query!r} in vault {vault.name!r}. "
        suggestions = get_close_matches(query, all_ids, n=3)
        if suggestions:
            msg += f"Did you mean: {', '.join(suggestions)}? "
        msg += "Run `lit list` to see available ids."
        raise PaperNotFoundError(msg)
    if len(matches) == 1:
        return matches[0]
    raise PaperNotFoundError(
        f"Ambiguous query {query!r} matched {len(matches)} papers: "
        f"{', '.join(matches)}. Pass a more specific substring."
    )


def find_paper_id_by_doi(vault: Path, doi: str) -> str:
    """Reverse-lookup a paper id from a DOI.

    Thin wrapper around ``core.dedup.find_paper_by_doi`` so call sites can
    treat ``--paper`` and ``--paper-doi`` symmetrically: both branches end
    with a canonical paper id and a single ``PaperNotFoundError`` on miss.

    Args:
        vault: Vault root.
        doi: DOI string. Empty / whitespace is treated as a miss.

    Returns:
        Canonical paper id whose ``metadata.yaml`` has a matching DOI
        (case-insensitive per the DOI Handbook).

    Raises:
        PaperNotFoundError: no paper in the vault carries this DOI.
    """
    hit = find_paper_by_doi(vault, doi)
    if hit is None:
        raise PaperNotFoundError(
            f"No paper with DOI {doi!r} in vault {vault.name!r}. "
            "Run `lit list` to see available papers, or `lit add` to "
            "import this one."
        )
    return hit[0]


def resolve_paper_input(
    vault: Path,
    paper_id: str | None,
    paper_doi: str | None,
) -> str:
    """Collapse the ``--paper`` / ``--paper-doi`` XOR pair into one id.

    Every M11 command site that accepts either input channel routes through
    this helper:

    * Exactly one of ``paper_id`` / ``paper_doi`` must be non-empty.
    * ``paper_doi`` → reverse-lookup via ``find_paper_id_by_doi``.
    * ``paper_id`` → fuzzy-resolve via ``resolve_paper_id``.

    Args:
        vault: Vault root.
        paper_id: Value of the ``--paper`` option / positional argument, or
            ``None`` when not supplied.
        paper_doi: Value of the ``--paper-doi`` option, or ``None``.

    Returns:
        Canonical paper id.

    Raises:
        LitmanError: both channels supplied, or neither was.
        PaperNotFoundError: resolution failed downstream.
    """
    has_id = paper_id is not None and paper_id != ""
    has_doi = paper_doi is not None and paper_doi != ""
    if has_id and has_doi:
        raise LitmanError(
            "--paper-doi and the paper-id input are mutually exclusive. "
            "Pass one or the other, not both."
        )
    if not has_id and not has_doi:
        raise LitmanError(
            "No paper specified. Pass a paper id (full or unique substring) "
            "or --paper-doi <DOI>."
        )
    if has_doi:
        assert paper_doi is not None
        return find_paper_id_by_doi(vault, paper_doi)
    assert paper_id is not None
    return resolve_paper_id(vault, paper_id)


def complete_paper_id(
    ctx: click.Context, param: click.Parameter, incomplete: str
) -> list[str]:
    """Click ``shell_complete`` callback for paper-id inputs.

    Returns the list of paper ids in the active vault that start with
    ``incomplete`` (case-sensitive — shells expect prefix completion to
    preserve case so the buffer can be replaced in-place).

    Vault discovery checks the ``LIT_LIBRARY`` environment variable
    explicitly first because Click's ``envvar=`` plumbing only surfaces
    options the user actually typed; during tab completion the user has
    not yet entered ``--library``, so the standard ``find_vault()``
    discovery chain (registry → cwd-walk) would miss the env-var case.

    Failure-safe by contract: shell completion runs inside the user's
    interactive shell, and an uncaught exception here would break tab
    completion across the entire ``lit`` command tree until the user
    cleared the shell session. We catch ``Exception`` (not just the
    expected ``LitmanError``) because OS errors during ``iterdir`` are
    plausible in misconfigured environments.

    Args:
        ctx: Click context (unused; required by the callback protocol).
        param: Click parameter object (unused).
        incomplete: Partial string typed so far.

    Returns:
        Sorted list of paper-id candidates whose names start with
        ``incomplete``; empty list on any error.
    """
    del ctx, param
    try:
        env_vault = os.environ.get("LIT_LIBRARY")
        if env_vault:
            vault = find_vault(Path(env_vault))
        else:
            vault = find_vault()
        papers_dir = vault / "papers"
        if not papers_dir.is_dir():
            return []
        return sorted(
            child.name
            for child in papers_dir.iterdir()
            if child.is_dir()
            and child.name.startswith(incomplete)
            and (child / "metadata.yaml").is_file()
        )
    except Exception:
        return []
