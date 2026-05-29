"""Registry / project drift resolve-corrector machinery (M28; de-duped M30).

litman's design premise is that users are lazy and forgetful — any drift
between data sources must be surfaced at the user's next interaction, not
deferred to a manual ``lit health-check`` they have to remember to run.

**M30 Phase 2:** the *detection* of these two B-external drifts (registry ↔
vault dir, ledger #4; config project path ↔ project dir, #5) now lives in the
single tagged check core ``core/checks.py`` (``check_vault_registry_drift`` /
``check_project_path_exists``), both using the mount-safe bounded-stat below.
This module keeps the ``[Y/n]`` **prompt + repair** machinery — it is the
``resolve`` corrector for those two checks. ``LitGroup.invoke`` runs the
``tier=cheap`` check subset, and when either category fires it calls the
matching function here to prompt + repair (TTY) or report (non-TTY).

The two functions still own their own bounded-stat re-probe: the cheap check
established *that* there is drift; the corrector re-derives *which* entries /
project paths to prune / heal (the ``Issue`` records carry only messages, not
the registry entries / project map the mutation needs). The single 0.5s budget
(spec §7) caps the worst case.

Behavior contract (preserved byte-for-byte across the de-dup):

* Registry drift — TTY asks ``[Y/n]`` (default Y) and prunes dangling entries
  on Y. Non-TTY prints one stderr warning and does not block / mutate.
* Project drift — non-destructive default: TTY prompts for a NEW path (blank =
  skip), never offers removal (``lit project rm`` stays the explicit cascade).
  Non-TTY prints one stderr warning, zero mutation.

Saying N / blank keeps the drift; the next ``lit *`` invocation will prompt
again — we deliberately do NOT add an ``acknowledged_missing`` persistent
ignore flag, because that would train a lazy ``N`` reflex into permanent drift
(see ``feedback_surface_drift_eagerly.md`` for the anti-pattern list).
"""

from __future__ import annotations

import sys
import threading
from pathlib import Path
from typing import Callable

import click
from rich.console import Console

from litman.core.vault_registry import (
    VaultRegistryError,
    find_active,
    load_registry,
    remove_vault,
    save_registry,
)


def _default_tty_probe() -> bool:
    return sys.stdin.isatty() and sys.stdout.isatty()


def _exists_bounded(
    paths: list[str], budget_s: float = 0.5
) -> dict[str, bool | None]:
    """Batch existence probe with a wall-clock budget.

    Every path is pre-seeded to ``None`` (= unknown). A daemon thread walks
    the list sequentially calling ``Path(p).exists()``; the main thread waits
    at most ``budget_s`` for it to finish. Whatever the daemon resolved to a
    definite ``True``/``False`` before the timeout overwrites the ``None``;
    everything still ``None`` is "we don't know" (slow / hung mount).

    The semantics deliberately separate the three states:

    * ``True``  — directory present.
    * ``False`` — directory definitely absent (the only state callers treat
      as drift).
    * ``None``  — unknown: the stat was still running when the budget expired,
      or it raised ``OSError``. Never treated as drift, so a slow / dropped
      mount can never trigger a destructive prompt (ADR-014).

    A hung mount leaves ``stat()`` in uninterruptible (D-state) sleep that
    even SIGKILL can't break; the only safe move is to NOT interrupt it and
    instead abandon the wait. The leaked daemon thread dies with the
    short-lived CLI process — acceptable per ADR-014.
    """
    result: dict[str, bool | None] = {p: None for p in paths}

    def _probe() -> None:
        for p in paths:
            try:
                result[p] = Path(p).exists()
            except OSError:
                # Leave None — an OSError (e.g. ELOOP, stale handle) is not a
                # confident "absent" verdict.
                pass

    worker = threading.Thread(target=_probe, daemon=True)
    worker.start()
    worker.join(timeout=budget_s)
    return result


def check_and_prompt_registry_drift(
    stdin_is_tty: Callable[[], bool] | None = None,
) -> None:
    """Surface dangling vault registrations and offer one-Enter cleanup.

    Behavior:

    * Loads the registry. A corrupt registry is silently skipped (the next
      ``lit vault`` command will surface the parse error — not our job to
      duplicate that diagnostic here).
    * Computes dangling entries (path no longer exists).
    * If none, returns silently — the common path must add zero noise.
    * TTY → prints the list + ``Remove now? [Y/n]`` (default Y). On Y, drops
      each entry and saves the registry. On N, prints a one-line "kept; will
      ask again next time" note.
    * Non-TTY → emits a single stderr warning listing the names + the
      ``lit vault remove`` command to run, and returns. Never blocks
      automation on an interactive prompt.

    Args:
        stdin_is_tty: Indirection so tests can force either branch without
            faking stdin. Defaults to ``sys.stdin.isatty() and sys.stdout.isatty()``
            — both streams must be TTY before we prompt, so a piped stdout
            (``lit list | less``) does not get a drift question mid-pipe.
    """
    probe = stdin_is_tty or _default_tty_probe
    try:
        reg = load_registry()
    except VaultRegistryError:
        # Corrupt registry. Surfacing it here would double-warn the user
        # (they'll see the parse error when they hit a vault-using command);
        # prefer one clear diagnostic over two.
        return

    # Bounded-stat instead of vault_registry.find_dangling (a bare stat that
    # can hang on a dropped HPC mount). Only a definite False counts as
    # dangling; None (timeout / OSError) is "unknown" and never prompts to
    # prune (ADR-014: a slow mount must not look like a deleted vault).
    status = _exists_bounded([v.path for v in reg.vaults])
    dangling = [v for v in reg.vaults if status[v.path] is False]
    if not dangling:
        return

    names = [v.name for v in dangling]
    paths = [v.path for v in dangling]

    # Build consoles INSIDE the function (not at module level) so pytest's
    # capsys fixture, which swaps sys.stderr/sys.stdout per-test, can capture
    # them. A module-level Console(stderr=True) would bind to the import-time
    # sys.stderr and bypass capsys.
    if not probe():
        # Non-interactive (script / CI / agent). One-line stderr warning,
        # no prompt, no auto-prune (mutating registry without consent in
        # automation is a foot-gun even with default Y).
        err = Console(stderr=True)
        joined = ", ".join(f"{n} ({p})" for n, p in zip(names, paths))
        err.print(
            f"[yellow]warning:[/] vault registry has {len(dangling)} dangling "
            f"registration(s): {joined}. Run "
            f"[bold]lit vault remove <name>[/] to clean up."
        )
        return

    # TTY path.
    console = Console()
    console.print()  # blank line before, separating from prior output
    console.print(
        f"[yellow]⚠[/]  Found {len(dangling)} dangling vault "
        f"registration(s) (path no longer exists):"
    )
    for entry in dangling:
        console.print(f"    [bold]{entry.name}[/] → {entry.path}")
    if click.confirm(
        "Remove these stale entries from the registry now?",
        default=True,
    ):
        current = reg
        for entry in dangling:
            current = remove_vault(current, entry.name)
        save_registry(current)
        console.print(
            f"[green]Removed {len(dangling)} dangling registration(s).[/]\n"
        )
    else:
        console.print(
            "[dim]Kept for now. You'll be reminded again next time.[/]\n"
        )


def check_and_prompt_project_drift(
    *,
    stdin_is_tty: Callable[[], bool] | None = None,
    exists_fn: Callable[[list[str]], dict[str, bool | None]] | None = None,
    load_config_fn: Callable[[Path], object] | None = None,
) -> None:
    """Surface project directories that drifted out from under their config.

    The dual of vault-registry drift (ADR-014): a paper linked to a project
    via ``lit link`` writes derived artifacts (``<project_dir>/litman_reflib/``
    symlinks + REFERENCES.md) OUTSIDE the vault, while the project_dir path
    lives only in the vault's ``lit-config.yaml`` projects map. Move or rename
    that directory and the litman_reflib becomes an orphan. This surfaces it
    at the next command instead of waiting for a manual ``lit health-check``.

    Behavior:

    * No active vault, unresolvable config, or active vault dir not definitely
      present → return silently (the missing-vault case belongs to the
      registry-drift segment; we don't double-report).
    * Probe each project dir with a bounded stat. Only a definite ``False``
      counts as drift; ``None`` (timeout / unknown) is skipped silently.
    * Default action is NON-destructive (ADR-014): unlike registry drift (a
      lossless prune defaulting to Y), a missing project dir is more likely a
      not-yet-mounted / other-machine situation, and ``lit project rm`` is an
      irreversible cascade. So the TTY prompt only offers "enter a new path to
      fix" or "blank = skip". Removal stays with ``lit project rm``.
    * TTY → prompt per missing project sequentially. A non-empty new path runs
      the set-path mutation (config-only, via staged_write) then rebuilds ALL
      projects' links + refs so the litman_reflib at the new location is
      recreated. Blank = skip (no persistent ignore flag, per the M28
      surface-eagerly principle).
    * Non-TTY → one stderr warning listing the missing projects + the
      ``lit project set-path`` hint, zero mutation.

    Args:
        stdin_is_tty: TTY probe indirection (tests force either branch).
        exists_fn: Bounded existence probe indirection (default
            :func:`_exists_bounded`); lets tests avoid real FS / threads.
        load_config_fn: Config loader indirection (default
            ``core.config.load_config``); lets tests inject a projects map
            without materializing a vault.
    """
    probe = stdin_is_tty or _default_tty_probe
    exists = exists_fn or _exists_bounded

    try:
        reg = load_registry()
    except VaultRegistryError:
        return

    active = find_active(reg)
    if active is None:
        return

    # The active vault's own directory must be definitely present before we
    # try to read its config. If it's missing (False) or unknown (None), the
    # registry-drift segment owns that case — don't double-report here.
    if exists([active.path]).get(active.path) is not True:
        return

    if load_config_fn is None:
        from litman.core.config import load_config as load_config_fn  # type: ignore[assignment]

    vault = Path(active.path)
    try:
        config = load_config_fn(vault)
        projects: dict[str, str] = dict(config.projects)
    except Exception:
        # A broken config surfaces its own diagnostic on the next config-using
        # command; don't crash the user's actual command from inside the hook.
        return

    if not projects:
        return

    status = exists(list(projects.values()))
    missing = [
        name
        for name, path in projects.items()
        if status.get(path) is False
    ]
    if not missing:
        return

    # Build consoles INSIDE the function so pytest's capsys (which swaps
    # sys.stderr/stdout per-test) can capture them.
    if not probe():
        err = Console(stderr=True)
        joined = ", ".join(f"{n} ({projects[n]})" for n in missing)
        err.print(
            f"[yellow]warning:[/] {len(missing)} project director"
            f"{'y' if len(missing) == 1 else 'ies'} not found: {joined}. "
            f"Run [bold]lit project set-path <name> <new-path>[/] to fix "
            f"(or [bold]lit project rm <name>[/] to drop)."
        )
        return

    console = Console()
    healed: dict[str, str] = {}
    for name in missing:
        old = projects[name]
        console.print()
        console.print(
            f"[yellow]⚠[/]  Project [bold]{name}[/] directory not found "
            f"(was {old})."
        )
        new_path = click.prompt(
            "    Moved? Enter the new path to fix, or leave blank to skip",
            default="",
            show_default=False,
        ).strip()
        if not new_path:
            console.print("[dim]Skipped. You'll be reminded again next time.[/]")
            continue
        healed[name] = str(Path(new_path).expanduser())

    if not healed:
        return

    # Apply all heals in one config mutation, then rebuild from the updated
    # map. set-path is config-only (papers store project NAMES, not paths), so
    # we reuse project.py's _config_with_projects + staged_write, then rebuild
    # ALL projects' links + refs so the litman_reflib at the new location is
    # recreated (the set-path command itself only prints a "remember to
    # rebuild" hint; the heal must do it automatically).
    from litman.commands.project import _config_with_projects
    from litman.core.atomic import staged_write
    from litman.core.project_link import rebuild_all_project_links
    from litman.core.project_refs import rebuild_all_project_refs

    new_projects = dict(projects)
    new_projects.update(healed)
    new_config_text = _config_with_projects(vault, new_projects)
    with staged_write(vault, op_id="project-set-path-drift") as stage:
        stage.write_text("lit-config.yaml", new_config_text)

    link_status = rebuild_all_project_links(vault, new_projects)
    # Double-writes REFERENCES.md (links-rebuild already wrote it) — kept for
    # parity with project.py rename/set-path, which calls both in this order.
    rebuild_all_project_refs(vault, new_projects)

    for name in healed:
        # Only claim a rebuild if it actually happened. A new path that is not
        # a directory here (typo, or a legitimately not-yet-mounted / other-
        # machine location per ADR-014) is "skipped" by rebuild_all_*; the
        # config is updated but litman_reflib is NOT recreated, so the message
        # must not lie about it.
        if link_status.get(name, {}).get("status") == "rebuilt":
            console.print(
                f"[green]✓ Updated[/] {name} → {healed[name]} "
                f"and rebuilt its litman_reflib."
            )
        else:
            console.print(
                f"[green]✓ Updated[/] {name} → {healed[name]}[dim] (config only "
                f"— directory not reachable here yet; run [bold]lit refresh-views"
                f"[/bold] there to rebuild litman_reflib).[/dim]"
            )
    console.print()
