"""litman CLI entry point.

Defines the root Click group ``cli`` and the entry-point function ``main``
referenced by ``[project.scripts]`` in ``pyproject.toml``.

Subcommands are registered onto ``cli`` via ``cli.add_command`` below.
``main`` wraps ``cli`` so that ``LitmanError`` subclasses become friendly
single-line error messages with exit code 1; other exceptions propagate as
normal Python tracebacks (they indicate bugs, not user errors).
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import click
from rich.console import Console

from litman import __version__
from litman.commands.add import add_cmd
from litman.commands.cite import cite_cmd
from litman.commands.code import code_group
from litman.commands.config import config_group
from litman.commands.drop import drop_cmd
from litman.commands.export import export_cmd
from litman.commands.gui import gui_cmd
from litman.commands.health import health_check_cmd
from litman.commands.init import init_cmd
from litman.commands.install_completion import install_completion_cmd
from litman.commands.install_skill import install_skill_cmd
from litman.commands.link import link_cmd, unlink_cmd
from litman.commands.list import list_cmd
from litman.commands.modify import modify_cmd
from litman.commands.open import open_cmd
from litman.commands.pdf_text import pdf_text_cmd
from litman.commands.project import project_group
from litman.commands.promote import promote_cmd
from litman.commands.read import read_cmd
from litman.commands.refresh import refresh_views_cmd
from litman.commands.related import related_cmd
from litman.commands.rename import rename_cmd
from litman.commands.revisit import revisit_cmd
from litman.commands.rm import rm_cmd
from litman.commands.search import search_cmd
from litman.commands.setup import setup_cmd
from litman.commands.show import show_cmd
from litman.commands.skim import skim_cmd
from litman.commands.sync import sync_group
from litman.commands.taxonomy import taxonomy_group
from litman.commands.trash import trash_group
from litman.commands.vault import vault_group
from litman.exceptions import LitmanError

console = Console()


def _timestamp_is_stale(ts: str | None, *, now: Any, stale_days: int) -> bool:
    """Return True if ``ts`` is older than ``stale_days`` days before ``now``.

    Generic staleness test shared by the post-dispatch nudges (health-check
    ``last_health_check_at`` and sync ``last_push``).

    Defensive parse (M30 §5 / human decision 3): a None / missing / unparseable
    timestamp counts as STALE so the nudge fires. A legacy *naive* ISO string
    must not crash — we assume UTC for it rather than raising. ``now`` is passed
    in (tz-aware) so callers / tests control the clock.
    """
    from datetime import datetime, timezone

    if not ts:
        return True
    try:
        parsed = datetime.fromisoformat(ts)
    except (ValueError, TypeError):
        return True
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return (now - parsed).total_seconds() > stale_days * 86400


# Workflow-ordered command groups for `lit --help` / `lit help`. Any command
# not listed here still appears under "Other" (so a newly added command is
# never silently hidden) — add it to the right section when you add it.
_COMMAND_SECTIONS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("Setup & vaults",
     ("setup", "init", "vault", "install-completion", "install-skill", "help")),
    ("Papers",
     ("add", "list", "show", "search", "related", "open", "pdf-text",
      "cite", "modify", "rename", "rm")),
    ("Reading status",
     ("read", "skim", "promote", "revisit", "drop")),
    ("Linking & organization",
     ("link", "unlink", "project", "code", "taxonomy")),
    ("Maintenance",
     ("health-check", "refresh-views", "trash", "sync", "export", "config", "gui")),
)


class LitGroup(click.Group):
    """Root group that (1) renders the command list in workflow sections
    instead of one flat alphabetical block (M27), and (2) surfaces vault
    registry drift before dispatching to most subcommands (M28). Falls back
    to an 'Other' section for any command missing from _COMMAND_SECTIONS so
    nothing is silently hidden."""

    # Subcommands that must NOT trigger the drift prompt:
    # - help / hello: trivial, registry-irrelevant; would feel out of place
    # - None: the user typed `lit` with no subcommand and is about to see
    #   the help message; don't ambush them with a registry prompt
    _DRIFT_SKIP: frozenset[str | None] = frozenset({"help", "hello", None})

    def invoke(self, ctx: click.Context) -> Any:
        # ``ctx.invoked_subcommand`` is only populated inside ``Group.invoke``
        # AFTER our override runs, so we peek at the raw protected args set
        # by ``Group.parse_args`` to know which subcommand is about to run.
        # An empty protected-args list means ``lit`` with no subcommand
        # (e.g. about to render help) — represented as ``None`` in the skip
        # set, matching the convention used by ``ctx.invoked_subcommand``.
        #
        # ``ctx._protected_args`` is a Click-8.x internal carrying the same
        # value, populated by ``parse_args``. Click 9 plans to remove this
        # attribute (deprecation flagged in 8.2), so ``pyproject.toml`` pins
        # ``click<9``. Bumping past 9 requires either moving this hook into
        # the group callback (after ``parse_args`` has populated
        # ``invoked_subcommand``) or switching to a public-API replacement.
        protected = getattr(ctx, "_protected_args", None) or []
        cmd_name: str | None = protected[0] if protected else None
        if cmd_name not in self._DRIFT_SKIP:
            self._run_drift_hook()
        result = super().invoke(ctx)
        # Post-dispatch staleness nudge (M30 Phase 5), dual to the pre-dispatch
        # drift hook. Fires on the normal return path; same skip gate as the
        # drift hook (bare `lit` / `lit --help` / `lit help` / `lit hello` do
        # not nudge). A command that raises SystemExit (Click's normal exit
        # path, e.g. `health-check` exit 1) bypasses this — accepted limitation;
        # the nudge is a passive reminder, not a guarantee on every exit path.
        if cmd_name not in self._DRIFT_SKIP:
            self._emit_staleness_nudge()
        return result

    def _run_drift_hook(self) -> None:
        """Tier-1 pre-dispatch drift hook (M28; unified detection M30 Phase 2).

        Runs the ``tier=cheap`` check subset (``cheap_checks()`` — registry /
        config / bounded-stat only, invariant #15) as the single detection
        core, then dispatches each fired category to its corrector:

        * klass-B-ext cheap issues (registry #4, project #5) → the ``resolve``
          corrector: the bespoke ``[Y/n]`` prompt + repair in ``_drift.py``,
          which preserves the M28 UX (registry prune default Y; project
          non-destructive default; non-TTY → stderr report, no mutate).
        * klass-A cheap issues → ``regen``. None are registered yet in Phase 2;
          the dispatch below is wired so Phase 3's ``index_vs_disk`` lands
          without re-touching the hook.

        ``cheap_checks()`` is the same detection core that feeds ``health-check``
        (Tier 2) — registry drift uses the same bounded-stat in both paths,
        closing the §1.1 divergence. We resolve the active vault *cheaply* (read
        the registry active entry + bounded-stat it — never ``find_vault``,
        which bare-``stat()``s the active vault dir and would re-introduce the
        hung-mount risk on the hot path). ``check_vault_registry_drift`` ignores
        the vault entirely, so it runs even when no vault resolves. Once a
        category fires, the matching ``resolve`` corrector self-detects which
        entries / paths to repair (``Issue`` records carry only messages, not
        the registry entries / config map the mutation needs); the cheap check
        having fired is the gate.

        The whole hook is wrapped so a failure (hung mount, FS error inside a
        heal) never crashes the user's actual command — it degrades to a silent
        skip and the cold-path ``lit health-check`` catches the drift later.
        """
        try:
            from litman.core.checks import cheap_checks

            vault = self._cheap_active_vault()

            # Single shared bounded-stat budget (spec §7 / verification task 3):
            # gather every path the cheap bounded-stat checks need (registry
            # vault paths + active-vault project map paths) and probe them with
            # ONE _exists_bounded call. The two checks that would otherwise each
            # probe (vault_registry_drift, project_path_exists) get the result
            # threaded in via exists_status. Worst-case hung-mount cost is one
            # 0.5s budget, not two.
            shared_status = self._cheap_shared_exists(vault)

            # Detection: run every cheap check. Vault-scoped checks need a vault
            # dir; the registry check ignores it. Collect the fired categories.
            issues: list[Any] = []
            for spec in cheap_checks():
                if vault is None and spec.category != "vault_registry_drift":
                    continue
                if spec.category in ("vault_registry_drift", "project_path_exists"):
                    issues.extend(
                        spec.fn(
                            vault or Path("/nonexistent"),
                            [],
                            exists_status=shared_status,
                        )
                    )
                else:
                    issues.extend(spec.fn(vault or Path("/nonexistent"), []))
            categories = {i.category for i in issues}

            # Correction dispatch, keyed on the unified detection categories.
            # config_unreadable first: a present-but-unparseable lit-config.yaml
            # blinds the config-keyed drift checks below, so surface it once to
            # stderr (invariant #14 / review F27) — consistent with the corrupt-
            # registry line. Not a prompt: nothing here can auto-repair a
            # hand-broken config; the user must fix the YAML.
            if "config_unreadable" in categories:
                Console(stderr=True).print(
                    "[red]error:[/] lit-config.yaml is unreadable: "
                    "config-dependent drift checks cannot run. Inspect / "
                    "repair it (`lit config show`)."
                )

            # Registry next: it owns the missing-active-vault case, so project
            # drift must run after it (M28 ordering preserved).
            if "vault_registry_drift" in categories:
                from litman.commands._drift import check_and_prompt_registry_drift

                check_and_prompt_registry_drift()

            if "project_path_exists" in categories:
                # Project-path drift heals via staged_write + rebuild, which
                # touch the filesystem from inside the pre-dispatch hook. A
                # failure there must never crash the user's actual command —
                # degrade to silent skip and let `lit health-check` catch it.
                from litman.commands._drift import check_and_prompt_project_drift

                try:
                    check_and_prompt_project_drift()
                except Exception:
                    pass

            # klass-A cheap repair: INDEX.json ↔ papers/ vanished ids (#1).
            # MUST stay metadata-free (invariant #15) — never call regen() /
            # list_papers() here. Spec §6: TTY → metadata-free INDEX regen +
            # targeted (or bulk-deferred) wikilink annotate; non-TTY →
            # report-only, no mutate.
            if vault is not None and "index_vs_disk" in categories:
                self._repair_index_vs_disk(vault, issues)
        except Exception:
            pass

    @staticmethod
    def _repair_index_vs_disk(vault: "Path", issues: list[Any]) -> None:
        """Tier-1 vanished-id repair (spec §6). Metadata-free (invariant #15).

        Collects the vanished ids (``index_vs_disk`` errors — present in INDEX,
        absent on disk; the un-indexed-dir warnings carry no INDEX entry to drop
        and need Tier-2). Then:

        * **non-TTY** (agent / automation): report-only to stderr, NO mutation
          (spec §6 — even lossless A-class regen does not auto-mutate without
          consent in automation). The user/agent runs an explicit fix.
        * **TTY, > 5 vanished** (bulk manual delete / restored backup): drop all
          dead ids from INDEX once (metadata-free) and print ONE line deferring
          all wikilink annotation to ``lit health-check``; do NOT per-id
          annotate.
        * **TTY, ≤ 5 vanished**: metadata-free INDEX drop + per-id targeted
          wikilink annotate (``[[id]] (deleted)``, grep-narrowed).
        """
        from litman.commands._drift import _default_tty_probe
        from litman.core.correctors import annotate, regen_index_drop_ids

        vanished = sorted(
            {
                i.paper_id
                for i in issues
                if i.category == "index_vs_disk"
                and i.severity == "error"
                and i.paper_id
            }
        )
        if not vanished:
            return

        if not _default_tty_probe():
            # Automation: report-only, never mutate without consent.
            err = Console(stderr=True)
            joined = ", ".join(vanished)
            err.print(
                f"[yellow]warning:[/] INDEX.json lists {len(vanished)} paper(s) "
                f"no longer on disk: {joined}. Run "
                f"[bold]lit health-check --fix[/] to reconcile."
            )
            return

        # TTY: metadata-free INDEX repair (lossless klass-A regen).
        regen_index_drop_ids(vault, vanished)

        if len(vanished) > 5:
            # Bulk: defer all wikilink annotation to health-check's single full
            # pass instead of per-id targeted annotate.
            console.print(
                f"[dim]library changed substantially ({len(vanished)} papers "
                f"vanished), run `lit health-check`[/]"
            )
            return

        # ≤ 5: per-id targeted wikilink annotate.
        annotate(vault, vanished, targeted=True)

    @staticmethod
    def _cheap_shared_exists(vault: "Path | None") -> dict[str, "bool | None"]:
        """Single bounded-stat over every path the cheap checks need (task 3).

        Gathers the registry vault paths plus (when an active vault resolves)
        its ``lit-config.yaml`` project map paths — expanded with the SAME
        ``Path(...).expanduser()`` normalization that
        ``check_project_path_exists`` uses for its lookups — then probes them in
        ONE :func:`_exists_bounded` call sharing a single 0.5s budget. Returns
        the ``{path: bool|None}`` map; an empty dict if nothing to probe. Best-
        effort: a registry / config read failure resolves to whatever paths it
        could gather (the individual checks fall back to None = unknown for the
        rest).
        """
        from litman.commands._drift import _exists_bounded
        from litman.core.vault_registry import (
            VaultRegistryError,
            load_registry,
        )

        paths: set[str] = set()
        try:
            reg = load_registry()
            paths.update(v.path for v in reg.vaults)
        except VaultRegistryError:
            pass

        if vault is not None:
            try:
                from litman.core.config import load_config

                config = load_config(vault)
                paths.update(
                    str(Path(p).expanduser()) for p in config.projects.values()
                )
            except Exception:
                pass

        if not paths:
            return {}
        return _exists_bounded(sorted(paths))

    @staticmethod
    def _cheap_active_vault() -> "Path | None":
        """Resolve the active vault path without ``find_vault``'s bare stat.

        Reads the registry active entry and bounded-stats it (invariant #15:
        no per-paper metadata; ADR-014: a dropped mount must not hang). Returns
        the path only when it is definitely present AND holds a
        ``lit-config.yaml``; otherwise ``None`` (the registry-drift check owns
        the missing-vault case, and a ``None``/unknown stat is never actioned).
        """
        from litman.commands._drift import _exists_bounded
        from litman.core.vault_registry import (
            VaultRegistryError,
            find_active,
            load_registry,
        )

        try:
            reg = load_registry()
        except VaultRegistryError:
            return None
        active = find_active(reg)
        if active is None:
            return None
        if _exists_bounded([active.path]).get(active.path) is not True:
            return None
        candidate = Path(active.path)
        if not (candidate / "lit-config.yaml").is_file():
            return None
        return candidate

    @staticmethod
    def _emit_staleness_nudge() -> None:
        """Post-dispatch staleness nudges (M30 §5).

        Two independent, condition-driven reminders for the **active
        registered** vault (an unregistered ``--library`` override has no entry
        → no nudge). Each fires when its tracked timestamp is older than its
        threshold, or None / missing / unparseable (treated as stale):

        * health-check: ``last_health_check_at`` (registry) >
          :data:`HEALTH_CHECK_STALE_DAYS` → "run ``lit health-check``".
        * sync push: ``.litman-sync-state.yaml`` ``last_push`` >
          :data:`SYNC_STALE_DAYS`, AND only when a remote is configured
          (``lit-config.yaml`` ``sync`` set) → "run ``lit sync push``".

        Both are stateless (recomputed every command, no "already shown" flag),
        so an ignored tip re-appears on the next command and only clears once
        the user runs the action that refreshes the timestamp.

        Always emits (invariant #5 / ADR-007: the agent is the primary consumer
        and routinely drives ``lit`` non-interactively, so suppressing in
        non-TTY would silence the reminder for the primary consumer). Only the
        *destination* is TTY-gated: TTY → stdout tail; non-TTY → stderr (keeps
        stdout clean for pipes). Non-disableable: no env var / config read can
        suppress it.

        Reads only the registry plus, for the sync arm, two bounded vault-root
        files (``lit-config.yaml`` + ``.litman-sync-state.yaml``) gated behind
        the hung-mount-safe :meth:`_cheap_active_vault` resolver — never
        per-paper metadata (invariant #15).

        Wrapped so a failure never crashes the user's command (dual to the
        pre-dispatch drift hook's defensive wrapper).
        """
        try:
            from datetime import datetime, timezone

            from litman.commands._drift import _default_tty_probe
            from litman.core.checks import (
                HEALTH_CHECK_STALE_DAYS,
                SYNC_STALE_DAYS,
            )
            from litman.core.vault_registry import (
                VaultRegistryError,
                find_active,
                load_registry,
            )

            try:
                reg = load_registry()
            except VaultRegistryError:
                return
            active = find_active(reg)
            if active is None:
                return

            now = datetime.now(timezone.utc)
            tty = _default_tty_probe()

            def _emit(line: str) -> None:
                if tty:
                    console.print(line)
                else:
                    Console(stderr=True).print(line)

            # 1. Health-check staleness — registry-only.
            if _timestamp_is_stale(
                active.last_health_check_at,
                now=now,
                stale_days=HEALTH_CHECK_STALE_DAYS,
            ):
                _emit(
                    f"[dim]tip: no `lit health-check` in "
                    f"{HEALTH_CHECK_STALE_DAYS}+ days. "
                    "Run it to catch silent drift.[/dim]"
                )

            # 2. Sync-push staleness — only when a remote is configured. The
            # _cheap_active_vault resolver has already bounded-stat'd the vault
            # root and confirmed lit-config.yaml exists, so the two small
            # root-file reads below share that hung-mount-safe envelope. A
            # read failure degrades to no nudge (the explicit `lit sync status`
            # / health-check own the real config-corruption finding).
            vault = LitGroup._cheap_active_vault()
            if vault is not None:
                try:
                    from litman.core.config import load_config
                    from litman.core.sync import read_sync_state

                    if load_config(vault).sync is not None and _timestamp_is_stale(
                        read_sync_state(vault).last_push,
                        now=now,
                        stale_days=SYNC_STALE_DAYS,
                    ):
                        _emit(
                            f"[dim]tip: no `lit sync push` in "
                            f"{SYNC_STALE_DAYS}+ days. "
                            "Run it to back up your vault to the configured "
                            "remote.[/dim]"
                        )
                except Exception:
                    pass
        except Exception:
            pass

    def format_commands(
        self, ctx: click.Context, formatter: click.HelpFormatter
    ) -> None:
        visible: dict[str, click.Command] = {}
        for name in self.list_commands(ctx):
            cmd = self.get_command(ctx, name)
            if cmd is None or cmd.hidden:
                continue
            visible[name] = cmd
        if not visible:
            return
        limit = formatter.width - 6 - max(len(n) for n in visible)

        def rows(names: list[str]) -> list[tuple[str, str]]:
            return [(n, visible[n].get_short_help_str(limit)) for n in names]

        placed: set[str] = set()
        for title, names in _COMMAND_SECTIONS:
            present = [n for n in names if n in visible]
            if not present:
                continue
            with formatter.section(title):
                formatter.write_dl(rows(present))
            placed.update(present)

        leftover = [n for n in visible if n not in placed]
        if leftover:
            with formatter.section("Other"):
                formatter.write_dl(rows(leftover))


@click.group(
    cls=LitGroup,
    context_settings={"help_option_names": ["-h", "--help"]},
)
@click.version_option(version=__version__, prog_name="lit")
def cli() -> None:
    """litman — local-first, AI-augmented literature management CLI.

    Run lit help COMMAND (or lit COMMAND --help) for command-specific
    help, e.g. lit help code add.
    """


cli.add_command(setup_cmd)
cli.add_command(init_cmd)
cli.add_command(add_cmd)
cli.add_command(list_cmd)
cli.add_command(show_cmd)
cli.add_command(cite_cmd)
cli.add_command(search_cmd)
cli.add_command(related_cmd)
cli.add_command(open_cmd)
cli.add_command(pdf_text_cmd)
cli.add_command(refresh_views_cmd)
cli.add_command(modify_cmd)
cli.add_command(read_cmd)
cli.add_command(revisit_cmd)
cli.add_command(drop_cmd)
cli.add_command(promote_cmd)
cli.add_command(skim_cmd)
cli.add_command(taxonomy_group)
cli.add_command(project_group)
cli.add_command(rename_cmd)
cli.add_command(rm_cmd)
cli.add_command(trash_group)
cli.add_command(health_check_cmd)
cli.add_command(code_group)
cli.add_command(config_group)
cli.add_command(install_skill_cmd)
cli.add_command(install_completion_cmd)
cli.add_command(link_cmd)
cli.add_command(unlink_cmd)
cli.add_command(sync_group)
cli.add_command(vault_group)
cli.add_command(export_cmd)
cli.add_command(gui_cmd)


@cli.command(hidden=True)
def hello() -> None:
    """Sanity-check command. Confirms lit is installed and importable."""
    console.print(
        f"[bold green]litman[/] v{__version__} is installed and importable."
    )


@cli.command("help")
@click.argument("command_path", nargs=-1)
@click.pass_context
def help_cmd(ctx: click.Context, command_path: tuple[str, ...]) -> None:
    """Show help for lit or a specific command.

    "lit help" prints the top-level command list (same as "lit --help").
    "lit help COMMAND [SUBCOMMAND ...]" prints that command's help (same as
    "lit COMMAND ... --help"), e.g. "lit help code add".
    """
    # ctx.parent is the root `cli` group's context (info_name == "lit").
    root_ctx = ctx.parent
    if not command_path:
        click.echo(cli.get_help(root_ctx))
        return

    current_cmd: click.Command = cli
    current_ctx = root_ctx
    walked: list[str] = []
    for name in command_path:
        if not isinstance(current_cmd, click.Group):
            raise click.UsageError(
                f"'lit {' '.join(walked)}' takes no subcommands, "
                f"so '{name}' has no help."
            )
        sub = current_cmd.get_command(current_ctx, name)
        if sub is None:
            raise click.UsageError(f"No such command: {' '.join(command_path)!r}")
        current_ctx = click.Context(sub, info_name=name, parent=current_ctx)
        current_cmd = sub
        walked.append(name)
    click.echo(current_cmd.get_help(current_ctx))


def _force_utf8_output() -> None:
    """Make stdout/stderr encode UTF-8 so Rich glyphs survive everywhere.

    litman prints non-ASCII output (``✓``/``→``/``—``/``•`` and Rich table
    box-drawing characters). On a legacy-codepage console (most commonly a
    Chinese Windows host, where the default is GBK/cp936) the encoder cannot
    represent these and Python raises ``UnicodeEncodeError`` *after* the data
    has already been committed, leaving a scary traceback on every command.
    The failure also surfaces when stdout is piped (e.g. captured by an
    agent), because Python then falls back to the locale encoding instead of
    the terminal's. Reconfiguring both streams to UTF-8 here fixes both paths;
    the already-constructed ``Console`` objects read ``sys.stdout.encoding``
    lazily at print time, so doing this once before ``cli()`` is enough.
    POSIX hosts are UTF-8 by default and unaffected.
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            try:
                reconfigure(encoding="utf-8")
            except (ValueError, OSError):
                pass


def main() -> None:
    """Entry point invoked by the ``lit`` console script."""
    _force_utf8_output()
    try:
        cli()
    except LitmanError as e:
        console.print(f"[bold red]error:[/] {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
