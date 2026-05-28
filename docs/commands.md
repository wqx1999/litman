# Commands by Scenario

This page walks through `lit`'s commands grouped by what you are
*trying to do*, not alphabetically. Every command also accepts the
global `--library` / `--vault` flags; see the
[Global flags](#global-flags) section at the end.

For one-line summaries of every command, run `lit --help` or
`lit <cmd> --help`.

## Scenario 1 — Set up a new vault

| Command | What it does |
|---|---|
| `lit setup` | Interactive first-run wizard: shell completion → Claude Code skill → first vault (prompts for parent dir AND vault name) → optional cloud sync. Chains the four commands below behind simple prompts. TTY-only. |
| `lit init <parent_dir>` | Create a vault under `<parent_dir>/literature_vault/`. |
| `lit init <parent_dir> --name <subdir>` | Override the default subdir name (e.g. `--name pepforge_lib`). |
| `lit init <parent_dir> --no-register` | Skip auto-registration in the vault registry. |
| `lit init <parent_dir> --register-as <name>` | Override the registered name (default: subdir name). |
| `lit vault add <name> <path>` | Register an *existing* vault directory in `~/.config/litman/vaults.yaml`. |
| `lit vault use <name>` | Set it as the active vault. |
| `lit config show` | Print the parsed `lit-config.yaml`. Use to verify defaults. |

`lit init` registers the new vault and (for your first vault) makes it the
active vault automatically, so subsequent commands find it with no further
setup. Use `lit vault use <name>` to switch the active vault, or
`$LIT_LIBRARY` / `--library` to override per shell or per command.

For automated / scripted onboarding (CI, dotfiles, install scripts), call the
individual commands directly instead of `lit setup` — the wizard refuses to
run outside a TTY and lists the equivalent commands.

## Scenario 2 — Add a paper

The metadata source is either CrossRef (a DOI you trust) or an LLM-prepared
JSON file. The CLI refuses both at once.

| Command | What it does |
|---|---|
| `lit add <pdf> --doi <doi>` | Fetch metadata from CrossRef, derive an id, drop into `papers/<id>/`. |
| `lit add <pdf> --from-llm-json <path>` | Same, but read metadata from a JSON file (used by the `lit-library` skill). |
| `lit add <pdf> --doi <doi> --id <id>` | Override the auto-derived id. |
| `lit add <pdf> --doi <doi> --auto-suffix` | On id collision, auto-append `_b` / `_c`. Required for batch (non-TTY) mode. |

`lit add` refuses on duplicate DOI (checked against `unique_keys` in
`lit-config.yaml`) and auto-creates `paper.pdf`, `metadata.yaml`,
`notes.md` under `papers/<id>/`.

## Scenario 3 — Browse and find papers

| Command | What it does |
|---|---|
| `lit list` | List every paper, sorted by id. |
| `lit list --year 2024 --status deep-read` | AND-combined filters. |
| `lit list --topic transformer` | Match papers whose `topics` list contains exactly this value. |
| `lit list --author wang` | Case-insensitive substring against any author entry. |
| `lit list --unread --sort recent` | Unread papers (empty `read-date`), most-recently-engaged first — the "continue reading" primitive. |
| `lit show <id>` | Print one paper's full metadata + file paths. |

Available `lit list` filters: `--year`, `--type`, `--status`,
`--priority`, `--topic`, `--method`, `--project`, `--data`, `--author`,
`--unread`. Multi-valued fields use exact list-membership; `--author` uses
substring; all other filters use exact equality. `--unread` keeps only
papers whose `read-date` is empty (None/missing/empty string).

`--sort [id|recent]` controls order (default `id`, ascending, matches
`INDEX.json`). `--sort recent` orders most-recently-engaged first, by
`max(paper.pdf mtime, updated-at)`. Both options apply to `--format json`
as well as the default table.

## Scenario 4 — Edit a paper's metadata

| Command | What it does |
|---|---|
| `lit modify <id> --set FIELD=VALUE` | Set a scalar field. Empty value (`--set field=`) unsets (writes `null`). |
| `lit modify <id> --add-tag FIELD=VALUE` | Append to a list field (deduped). |
| `lit modify <id> --rm-tag FIELD=VALUE` | Remove from a list field (silent if absent). |
| `lit rename <old-id> <new-id>` | Change a paper id; ripple through every back-reference and wikilink. |

All `lit modify` writes are atomic and bump `updated-at`. Tag-style
operations refuse values not registered in the corresponding
[TAXONOMY](taxonomy-schema.md) dict.

`lit rename` is the **only safe way** to change a paper id. Plain
`mv papers/<old> papers/<new>` leaves dangling references in other
papers' relations fields and in notes wikilinks.

## Scenario 5 — Manage the controlled vocabulary

All TAXONOMY changes are atomic (TAXONOMY.md + every referencing
`metadata.yaml` + INDEX.json in one staged write).

| Command | What it does |
|---|---|
| `lit taxonomy list` | Show every dict. |
| `lit taxonomy list <dict>` | Show one dict (e.g. `lit taxonomy list topics`). |
| `lit taxonomy add <dict> <value>...` | Register one or more new values in a user dict. |
| `lit taxonomy rename <dict> <old> <new>` | Rename a value; cascade into every paper. |
| `lit taxonomy merge <dict> <src>... --into <dest>` | Fold values into one; cascade. |
| `lit taxonomy rm <dict> <value>` | Remove a value (refused if any paper still uses it). |

**Never hand-edit TAXONOMY.md** to remove or rename a value — see
[philosophy: TAXONOMY changes only via lit taxonomy](philosophy.md#taxonomy-changes-only-via-lit-taxonomy).

The three fixed-enum dicts (`type`, `status`, `priority`) are read-only
through `lit taxonomy` and require a code release to extend.

## Scenario 6 — Delete and recover

| Command | What it does |
|---|---|
| `lit rm <id>` | Move `papers/<id>/` to `<vault>/.trash/`, atomically tearing down all external links to it (other papers' relation fields, repo bindings, project symlinks). Reports the relationship count + a `lit show` pointer, then confirms (default N). |
| `lit rm <id> --purge` | Permanently delete instead of moving to `.trash/`. |
| `lit rm <id> -y` | Non-interactive force-delete: skip the prompt and tear down in one step (script / agent path). |
| `lit trash list` | Show trash entries, newest first. |
| `lit trash restore <id>` | Restore a trashed paper to `papers/<id>/`. |
| `lit trash empty` | Permanently delete every trash entry. |

`lit rm` is a single unified flow: it tears down the *external→A* half of
the relationship network (drops the paper from opposite papers' paired
relation fields, unbinds it from each repo's `repo-meta.yaml`, removes its
project symlinks and re-renders `REFERENCES.md`) in one atomic transaction.
A repo whose last binder was this paper (the 1:1 case) is hard-deleted and
its upstream URL recorded in the trash sidecar for re-clone on restore; a
repo still bound by another paper (1:N) only loses the binding. The paper's
*own* fields ride into trash unchanged so `lit trash restore` can rebuild
them. `[[id]]` wikilinks in other papers' `notes.md` / `discussion.md` are
**annotated** with a trailing ` (deleted)` marker so an agent reading the
note can tell the target is gone; `lit trash restore` removes the marker
atomically. The wikilink text itself is never stripped, so manual recovery
stays possible.

Trash is capped at 100 entries. When `lit rm` would push it past 100,
the oldest entry is permanently removed (the evicted id is printed).
`lit trash empty` still clears everything manually.

## Scenario 7 — Bind papers to projects

A project is a name + filesystem path declared in
[`lit-config.yaml`](config-schema.md#projects). Register projects there
before linking.

| Command | What it does |
|---|---|
| `lit link <id> --project <name>` | Bind: add tag, write symlink under `<project>/literature/<id>/`, regenerate `<project>/REFERENCES.md`. |
| `lit link <id> --project <name> --relevance "..."` | Same plus write `relevance-<project>` field on the paper. |
| `lit link --rebuild-all` | Cross-machine recovery: rebuild every project's symlinks + REFERENCES.md from scratch, scanning each paper's metadata. |
| `lit unlink <id> --project <name>` | Reverse a link. Drops the tag, the symlink, the REFERENCES.md entry, and (by default) the `relevance-<project>` field. |
| `lit unlink <id> --project <name> --keep-relevance` | Preserve the `relevance-<project>` field across unlink. |

When unlinking, code symlinks under the project are only removed if no
**other** linked paper in the project still references the same repo
(shared-utility-library case). The atomic guarantee covers all four
side effects.

## Scenario 8 — Bind papers to code

Code clones live under `<vault>/codes/<repo-name>/`. The binding is
bidirectional: the paper's `code-clones` field references the repo,
and the repo's `repo-meta.yaml` has a `papers` field listing all bound
papers.

| Command | What it does |
|---|---|
| `lit code add <url>` | Clone into `codes/<name>/repo/` (name auto-derived from URL), seed `repo-meta.yaml` and `notes.md`. |
| `lit code add <url> --name <override>` | Override the auto-derived repo name. |
| `lit code add <url> --paper <id>` | Same plus bind to a paper atomically. |
| `lit code add <url> --depth N` | `git clone --depth N`. `0` = full history. Default from `lit-config.yaml`'s `default_clone_depth`. |
| `lit code list` | Show every repo and its paper bindings. |
| `lit code link <repo-name> --paper <id>` | Bind an already-cloned repo to a paper (idempotent if already bound). |
| `lit code update <repo-name>` | `git pull --ff-only` inside the repo. |
| `lit code update <repo-name> --unshallow` | Promote a shallow clone to full history (`git fetch --unshallow`). |
| `lit code rm <repo-name>` | Permanently delete `codes/<repo-name>/` and clean up references. |
| `lit code restore-all` | Re-clone every repo whose `codes/<name>/repo/` is missing. Used for cross-machine recovery. |
| `lit code restore-all --dry-run` | Preview only. |

`lit code restore-all` exits with code 1 if any clone failed or any
orphan reference was found, so cron / CI can gate on it.

## Scenario 9 — Work with multiple vaults

Vaults are forks, not overlays: once linked, a vault is yours to read
and write, and the two vaults then evolve independently.

| Command | What it does |
|---|---|
| `lit vault add <name> <path>` | Register an existing vault directory. |
| `lit vault add <name> <path> --import-from "<provenance>"` | Same, with a free-text origin note. |
| `lit vault use <name>` | Switch active vault. |
| `lit vault list` | Show every registered vault; the active one is marked. |
| `lit vault info <name>` | Show one vault's path, paper count, on-disk size, provenance. |
| `lit vault remove <name>` | Unregister (does **not** delete the directory). |
| any `lit <cmd> --vault <name>` | One-shot override on any command. |

Cross-vault wikilinks (`[[<vault-name>:<paper-id>]]`) resolve against
the registry. `lit health-check` validates the prefix is a registered
vault and that the id exists in it.

## Scenario 10 — Cloud sync and cross-machine moves

litman uses rclone for cloud sync. The vault root and the
`.litman-staging/` transient directory are excluded by default and
never travel between machines.

| Command | What it does |
|---|---|
| `lit sync setup` | Hand the TTY to `rclone config`, then record the chosen remote in `lit-config.yaml`. |
| `lit sync push` | Upload the vault to the configured remote (`rclone sync` — one-way, deletes orphans on the remote). |
| `lit sync push --dry-run` | Preview only. |
| `lit sync push -y` | Skip the first-push size confirmation prompt. |
| `lit sync push --exclude-repos` | Skip `codes/*/repo/` checkouts on this push. |
| `lit sync pull` | Download the configured remote into the vault. **One-way with deletion** — local files absent on remote are removed. |
| `lit sync pull --dry-run` | Preview only. |
| `lit sync status` | Show last-push / last-pull timestamps and any pending diff. |

A full cross-machine restore looks like:

```bash
# On the new machine:
lit init /work/me/                   # empty vault skeleton
rclone config                        # register the remote (if not already)
lit sync setup                       # point litman at the remote
lit sync pull                        # materialise the vault
lit vault add my-main /work/me/literature_vault
lit vault use my-main
lit code restore-all                 # re-clone code repos (skipped by push if --exclude-repos)
lit link --rebuild-all               # rebuild every project's symlinks + REFERENCES.md
```

## Scenario 11 — Health-check and rebuild derived views

| Command | What it does |
|---|---|
| `lit health-check` | Scan the vault for dangling refs, schema gaps, stale staging dirs, missing PDFs, dangling wikilinks. Exits 1 if any issue is found. |
| `lit health-check --fix` | Auto-clean fixable categories (orphan trash sidecars, stale staging). Exits 0 if every remaining issue is fixable. |
| `lit refresh-views` | Rebuild `INDEX.json`, `views/by-*/` symlink hubs, and every project's `REFERENCES.md` from each paper's `metadata.yaml`. |

`refresh-views` is the safety net: every output it produces is derived
from `papers/*/metadata.yaml` and can be regenerated wholesale.
Per-project failures (e.g. the project's directory doesn't exist on
this machine) are skipped, not aborted.

## Scenario 12 — Read a paper

| Command | What it does |
|---|---|
| `lit open <id>` | Open `papers/<id>/paper.pdf` in the configured viewer (or platform default). |
| `lit open <substring>` | Fuzzy id match — case-insensitive substring. Multiple matches print the candidate list and exit. |

Viewer is set via `default_pdf_viewer` in
[`lit-config.yaml`](config-schema.md#default_pdf_viewer); `null`
falls back to `xdg-open` / `open` / `os.startfile` / `wslview`
depending on platform.

## Scenario 13 — Export references for LaTeX writing

When you are writing a paper, project the vault out to a ``.bib`` file
that LaTeX can consume. Cite keys equal paper ids, so the same
``\cite{2024_Liu_HELM-encoding}`` works across machines and over time.
Re-running the command on the same file is the supported update path.

| Command | What it does |
|---|---|
| `lit export --project <name>` | Write every paper linked to the project into `./refs.bib`. |
| `lit export --all` | Export every paper in the vault. |
| `lit export --project <name> -o path/to/refs.bib` | Override the output path. |
| `lit export --project <name> --priority A,B` | OR within a field: include priorities A *or* B. |
| `lit export --project <name> --status deep-read --year 2024` | AND across fields. |
| `lit export --project <name> --force` | Overwrite even a hand-edited target file (default refusal protects unsentinel'd files). |
| `lit export --vault <name> --project <name>` | Run against a non-active vault. |

Every generated file's first line is a sentinel comment of the form

```
% Generated by litman vX.Y.Z on <timestamp>. Do not hand-edit — re-run `lit export`.
```

`lit export` refuses to overwrite a target file whose first line is
not a litman sentinel, so a hand-curated ``references.bib`` that
happens to share the path is safe; pass ``--force`` to override.

The exporter relies on the 6 bib-oriented fields filled in by
``lit add`` (volume, issue, pages, publisher, venue-type, booktitle —
see [metadata-schema.md](metadata-schema.md)). For older papers
missing these fields, fill them in with
``lit modify <id> --set venue-type=journal-article`` etc.; the schema
is forgiving (missing fields become empty bibtex keys, not errors).

## Scenario 14 — Install shell completion and Claude Code skills

Standalone onboarding steps. `lit setup` (see [Scenario 1](#scenario-1--set-up-a-new-vault))
runs both interactively; call them directly for scripted setup.

| Command | What it does |
|---|---|
| `lit install-completion` | Install shell tab-completion. Auto-detects `bash` / `zsh` / `fish` from `$SHELL`; for bash/zsh, appends an eval line to `~/.bashrc` / `~/.zshrc`; for fish, drops a self-sourcing snippet under `~/.config/fish/completions/lit.fish`. Idempotent via a sentinel comment. Restart the shell (or `source`) to activate. |
| `lit install-completion <shell>` | Force a specific shell (`bash` / `zsh` / `fish`) instead of `$SHELL` auto-detection. |
| `lit install-skill` | Install every bundled skill (`lit-library` + `lit-reading`) to `~/.claude/skills/`. |
| `lit install-skill --skill lit-reading` | Install just one skill. |
| `lit install-skill --parent-dir <path>` | Override the install directory. |
| `lit install-skill --force` | Overwrite files inside an existing target directory. Files in the target that are NOT part of the bundled skill are left in place. |

Both are **optional**. The CLI is fully usable without completion or skills;
installing them just makes day-to-day use nicer (faster typing; Claude
Code's agent-mediated workflows). See
[philosophy: CLI must work standalone](philosophy.md#cli-must-work-standalone).

---

## Global flags

Every command accepts these:

| Flag | What it does |
|---|---|
| `--library <path>` | Use the vault at this filesystem path. |
| `--vault <name>` | Use the vault registered under this name in `~/.config/litman/vaults.yaml`. Mutually exclusive with `--library`. |
| `-h` / `--help` | Show that command's help and exit. |

### Vault discovery chain

The everyday default is the **active registered vault**: `lit init` registers
your vault and activates it, so you normally set nothing. `$LIT_LIBRARY` and
`--library` are explicit overrides for scripts, CI, or working with several
vaults at once. Full precedence (highest to lowest):

1. `--vault <name>` flag
2. `--library <path>` flag
3. `$LIT_LIBRARY` environment variable
4. The active vault in `~/.config/litman/vaults.yaml` (set by `lit init` /
   `lit vault use`)
5. cwd-walk: `lit` walks up from the current directory looking for the
   vault marker (`lit-config.yaml`)

The chain stops at the first hit. If none match, the command exits
with an error explaining which step failed.

### Registry override

The vault registry path can be redirected with `$LITMAN_REGISTRY_DIR`
(useful for putting the registry in a cloud-synced folder). Defaults
to `~/.config/litman/` on Linux / WSL,
`~/Library/Application Support/litman/` on macOS,
`%APPDATA%\litman\` on Windows.
