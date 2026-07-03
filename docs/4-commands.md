# Command Reference

Every operation on the library is a `lit` subcommand. This page documents each
one: its purpose, the shapes you call it in, and every flag it accepts. The
commands are grouped exactly as `lit --help` lists them, so the order here
matches what you see in the terminal.

`lit <cmd> --help` is the always-current authority for any single command. This
page mirrors it but adds the cross-command context the inline help cannot.

## Conventions

**Each entry** gives a one-line purpose, a synopsis of the common call shapes,
and a table of that command's own flags. Flags shared by most commands
(`--library`, `--vault`, `-h`) are documented once below and omitted from the
per-command tables.

**Global flags.** Commands that operate *inside* a vault accept:

| Flag | What it does |
|---|---|
| `--library <path>` | Use the vault at this filesystem path. |
| `--vault <name>` | Use the vault registered under this name. Mutually exclusive with `--library`. |
| `-h`, `--help` | Show that command's help and exit. |

The root command also takes `lit --version`, and `lit help <cmd>` prints the
same help as `lit <cmd> --help`.

A few commands do **not** take `--library` / `--vault`, because they create a
vault, target the registry, or touch no vault at all: `init`, `setup`,
`install-completion`, `install-skill`, `uninstall`, `pdf-text`, `help`, and
every `lit vault` subcommand.

**Vault discovery chain.** When a command needs a vault and you give no explicit
override, `lit` resolves one in this order (first hit wins):

1. `--vault <name>` flag
2. `--library <path>` flag
3. `$LIT_LIBRARY` environment variable
4. the active vault in the registry (set by `lit init` / `lit vault use`)
5. cwd-walk: walk up from the current directory looking for a `lit-config.yaml`

`lit init` registers and activates your vault, so step 4 normally covers you and
you set nothing. Steps 1–3 are explicit overrides for scripts, CI, or juggling
several vaults.

**Registry location.** The registry lives at `$LITMAN_REGISTRY_DIR/vaults.yaml`
when that variable is set (use it to put the registry in a cloud-synced
directory), otherwise the platform config dir: `~/.config/litman/` on
Linux / WSL, `~/Library/Application Support/litman/` on macOS, `%APPDATA%\litman\`
on Windows.

---

## 1. Setup & vaults

### `lit setup`

Interactive first-run wizard. Chains four optional steps behind simple prompts:
(1) shell tab-completion, (2) agent skill install, (3) create your first vault,
(4) cloud sync. Each step just runs the matching standalone command, so anything
the wizard does you can also do or redo directly. TTY-only; for scripted
onboarding call the individual commands.

```
lit setup
```

No flags beyond `-h`.

### `lit init`

Create a new vault under `<parent_dir>/<name>/` with the standard skeleton
(`papers/`, `codes/`, the four `views/by-*` hubs, a seeded `TAXONOMY.md`, an
empty `INDEX.json`, and `lit-config.yaml`), then register it. The first vault you
create becomes active automatically. `PARENT_DIR` defaults to the current
directory.

```
lit init <parent_dir>
lit init <parent_dir> --name pepforge_lib
lit init <parent_dir> --no-register
```

| Flag | What it does |
|---|---|
| `--name <subdir>` | Vault subdirectory name to create. Default `literature_vault`. |
| `--register-as <name>` | Registry name for the new vault. Default: the `--name` value. |
| `--no-register` | Create the vault but skip registration (CI / scripts / throwaway). Point `lit` at it later via `--library` / `$LIT_LIBRARY` / `lit vault add`. |

### `lit vault`

Manage the registry of vaults known to litman. Exactly one vault is active at a
time. Subcommands operate on the registry, not on a vault's contents, so they
take no `--library` / `--vault`.

```
lit vault add <name> <path> [--import-from "..."] [--use]
lit vault use <name>
lit vault list
lit vault info <name>
lit vault remove <name> [-y]
```

| Subcommand | What it does |
|---|---|
| `add <name> <path>` | Register an *existing* vault directory (must already contain `lit-config.yaml`). Does not create a vault — use `lit init` for that. |
| `use <name>` | Switch the active vault. |
| `list` | Show every registered vault; the active one is marked `✓`, with path, paper count, and provenance. |
| `info <name>` | Show one vault's path, paper count, on-disk size, provenance, and active flag. |
| `remove <name>` | Unregister `<name>`. The directory itself is **not** deleted. |

`lit vault add` flags: `--import-from <text>` (free-form provenance note for a
vault received from elsewhere; auto-fills today's date), `--import-at <date>`
(override that date), `--use` (activate the new entry immediately).
`lit vault remove` takes `-y` / `--yes` to skip the confirmation.

### `lit install-completion`

Install shell tab-completion for the current user. `SHELL` is optional and
auto-detected from `$SHELL`; supported shells are `bash` / `zsh` / `fish`. For
bash/zsh an eval line is appended to `~/.bashrc` / `~/.zshrc`; for fish a
self-sourcing snippet lands under `~/.config/fish/completions/`. Idempotent via a
sentinel comment. Restart the shell (or `source`) to activate.

```
lit install-completion
lit install-completion zsh
```

No flags beyond `-h`.

### `lit install-skill`

Install the bundled Claude Code skills (`lit-library` for the write side,
`lit-reading` for the read side). Both are optional — the CLI is fully usable
without them. Copies files only; does not install Claude Code or configure any
keys.

```
lit install-skill
lit install-skill --skill lit-reading
```

| Flag | What it does |
|---|---|
| `--skill <name>` | Install only this skill. Default: install all bundled skills. |
| `--parent-dir <path>` | Install directory. Default: where Claude Code auto-discovers skills. |
| `--force` | Overwrite files inside an existing target. Files not part of the bundled skill are left in place. |

### `lit uninstall`

Reverse of `lit setup`: remove the bundled skills, the shell-completion block,
and the vault registry (the list of vault names/paths). It does not remove the
`lit` CLI itself — a running command can't delete its own pipx environment — so
it prints the final `pipx uninstall litman` step for you to run. Your vault
directories (papers, PDFs, notes, annotations) are never touched; only the
registry pointers to them are dropped. Skill directories are removed file by
file, so any file you added next to `SKILL.md` is left in place.

```
lit uninstall
lit uninstall --dry-run
```

| Flag | What it does |
|---|---|
| `--dry-run` | Show what would be removed; change nothing. |
| `-y` / `--yes` | Skip the confirmation prompt. |

---

## 2. Papers

### `lit add`

Import a paper PDF into the vault. The metadata source is either `--doi`
(CrossRef fetch) or `--from-llm-json` (an LLM-prepared JSON file); exactly one is
required, and the CLI refuses both at once. Derives a canonical id
(`<year>_<Family>_<Keyword>`), refuses on duplicate DOI, and creates
`papers/<id>/` with `paper.pdf`, `metadata.yaml`, and an empty `notes.md`.

```
lit add <pdf> --doi <doi>
lit add <pdf> --from-llm-json <path>
lit add <pdf> --doi <doi> --id <id>
lit add <pdf> --doi <doi> --auto-suffix
```

| Flag | What it does |
|---|---|
| `--doi <doi>` | Fetch metadata from CrossRef. Mutually exclusive with `--from-llm-json`. |
| `--from-llm-json <path>` | Read metadata from a JSON file, or `-` for stdin. Used by the `lit-library` skill. Mutually exclusive with `--doi`. |
| `--id <id>` | Override the auto-derived id. |
| `--auto-suffix` | On id collision, auto-append `_b` / `_c` without prompting. Required for non-interactive (non-TTY) batch use. |

`lit add` writes a complete metadata skeleton (all fields, defaults filled); see
[3-concepts.md](3-concepts.md) §1.1 for the schema.

### `lit list`

List papers, optionally filtered. Filters are AND-combined across flags; within
one flag, comma-separated values are OR-combined. Multi-valued fields
(`topics` / `methods` / `projects` / `data`) match by list intersection;
`--author` / `--title` use case-insensitive substring; `--year` / `--type` /
`--status` / `--priority` match exact values.

```
lit list
lit list --year 2024 --status deep-read
lit list --status deep-read,skim --limit 5
lit list --unread --sort recent
lit list --topic transformer --format json
```

| Flag | What it does |
|---|---|
| `--year <v>` | Publication year. |
| `--type <v>` | Paper type (research / review / position / ...). |
| `--status <v>` | Status (deep-read / skim / inbox / dropped). |
| `--priority <v>` | Priority (A / B / C). |
| `--topic <v>` | Match papers whose `topics` list contains the value. |
| `--method <v>` | Match against the `methods` list. |
| `--project <v>` | Match against the `projects` list. |
| `--data <v>` | Match against the `data` list. |
| `--author <v>` | Case-insensitive substring against any author. |
| `--title <v>` | Case-insensitive substring against the title. |
| `--read-since <YYYY-MM-DD>` | Papers with `read-date` on or after the date. |
| `--added-since <YYYY-MM-DD>` | Papers with `created-at` on or after the date. |
| `--unread` | Only papers with an empty `read-date`. |
| `--sort [id\|recent]` | Order. `id` (default) ascending, matches `INDEX.json`. `recent` = most-recently-engaged first. |
| `--limit <N>` | Keep only the first N after filtering + sorting. |
| `--format [table\|json]` | Output format. `json` emits the same per-paper projection as `INDEX.json`. |

With `--sort recent` the table view shows the top 10 by default; raise it with
`--limit`, or use `--format json` for the full ranked list.

### `lit show`

Print one paper's full metadata plus its PDF / notes paths. Accepts a full id, a
unique case-insensitive id substring, or `--paper-doi`.

```
lit show <id>
lit show <id> --format json
lit show --paper-doi 10.1038/...
```

| Flag | What it does |
|---|---|
| `--paper-doi <doi>` | Look the paper up by DOI instead of id. Mutually exclusive with the positional id. |
| `--format [table\|json]` | `table` (default) renders metadata + file paths; `json` emits the **full** metadata dict (every field, not the `INDEX.json` projection). |

### `lit search`

Case-insensitive substring search over your `notes.md` / `discussion.md` only —
not the PDF full text, not trashed papers, not the `views/` symlinks. Each hit is
one matched line. Defaults to JSON output (`{id, file, line, snippet}`).

```
lit search <query>
lit search <query> --in notes
lit search <query> --format table --limit 20
```

| Flag | What it does |
|---|---|
| `--in <notes,discussion>` | Which files to search (comma-separated). Default: both. |
| `--format [json\|table]` | `json` (default, agent-facing) or a human-readable `table`. |
| `--limit <N>` | Keep only the first N hits. Default: unbounded. |

### `lit related`

Find papers related to `<id>`: author-asserted relation edges (`related` /
`extends` / `extended-by` / `contradicts` / `contradicted-by`) first, then papers
sharing `topics` / `methods` keys, ranked by shared-key count. Each neighbour
carries a `via` annotation. Defaults to JSON output.

```
lit related <id>
lit related <id> --by edges
lit related <id> --min-shared 2 --limit 10
```

| Flag | What it does |
|---|---|
| `--paper-doi <doi>` | Look the paper up by DOI instead of id. |
| `--by [edges\|taxonomy]` | Narrow to one neighbour kind. Default: both, edges first. |
| `--min-shared <N>` | Minimum shared `topics` / `methods` keys for a taxonomy neighbour. Default 1. Does not affect edge neighbours. |
| `--limit <K>` | Top-K cap on the merged list. Default 20. |
| `--format [json\|table]` | `json` (default) or human-readable `table`. |

### `lit open`

Open a paper's PDF in the configured viewer (or the platform default). Accepts a
full id, a unique substring, or `--paper-doi`. Multiple substring matches print
the candidate list and exit.

```
lit open <id>
lit open <substring>
```

| Flag | What it does |
|---|---|
| `--paper-doi <doi>` | Look the paper up by DOI instead of id. |

The viewer comes from `default_pdf_viewer` in `lit-config.yaml`; `null` falls
back to the platform opener. See [3-concepts.md](3-concepts.md) §1.4.

### `lit pdf-text`

Print a PDF's embedded text layer to stdout (pages joined by form feed).
Deterministic pypdf extraction — no model, no network, no system tool. A scanned
/ image-only PDF yields empty output and exit code 3. Operates on a file path, so
it takes no vault flags.

```
lit pdf-text <pdf>
lit pdf-text <pdf> --pages 1-3,5
```

| Flag | What it does |
|---|---|
| `--pages <spec>` | 1-based pages to extract, e.g. `1-3`, `1`, `1-3,5`. Omit for the whole document. |

### `lit cite`

Print a compact, presentation-ready citation for one paper to stdout as a single
clean line, so `lit cite <id> | pbcopy` (or `| xclip`) copies a paste-ready
string. The form is `<journal abbrev.> <year>, <volume>, <pages>.` — ACS-style,
with no author list or title, the version you drop on a slide. Accepts a full id,
a unique substring, or `--paper-doi`.

```
lit cite <id>
lit cite <id> | pbcopy
lit cite --paper-doi 10.1038/...
```

| Flag | What it does |
|---|---|
| `--paper-doi <doi>` | Look the paper up by DOI instead of id. Mutually exclusive with the positional id. |

The journal abbreviation comes from a shipped ISO4 table; an unknown journal is
printed verbatim with a warning on **stderr** (never mixed into the piped
citation). Other caveats (missing volume/pages, preprint venue) go to stderr too.

### `lit modify`

Edit fields on a paper's `metadata.yaml`. Writes `metadata.yaml` (refreshing
`updated-at`) and `INDEX.json` atomically; `views/by-*` are rebuilt afterwards.
Accepts a full id, a unique substring, or `--paper-doi`.

```
lit modify <id> --set FIELD=VALUE
lit modify <id> --set field=            # unset (writes null)
lit modify <id> --add-tag topics=transformer
lit modify <id> --rm-tag topics=transformer
```

| Flag | What it does |
|---|---|
| `--paper-doi <doi>` | Look the paper up by DOI instead of id. |
| `--set KEY=VALUE` | Set a scalar field. Repeatable. Empty value unsets (writes `null`). |
| `--add-tag FIELD=VALUE` | Append to a list field (deduped). Repeatable. |
| `--rm-tag FIELD=VALUE` | Remove from a list field (silent if absent). Repeatable. |

Tag operations refuse values not registered in the corresponding TAXONOMY dict
(register-first). See [3-concepts.md](3-concepts.md) §1.3 for the two-step
register-then-tag model and which fields are controlled.

### `lit rename`

Change a paper id, rippling the change everywhere: the renamed paper's metadata
and directory, every other paper's metadata that references it, every `notes.md`
with a `[[<old>]]` wikilink, `INDEX.json`, and `views/`. `<old>` accepts a unique
substring; `<new>` must be the exact target id.

```
lit rename <old-id> <new-id>
```

No flags beyond the global ones. This is the only safe way to change a paper id —
a plain `mv papers/<old> papers/<new>` leaves dangling references in other
papers' relation fields and in notes wikilinks.

### `lit rm`

Remove a paper. By default moves `papers/<id>/` to `<vault>/.trash/` (recoverable
via `lit trash restore`); `--purge` deletes permanently. All external links to
the paper (other papers' relation fields, repo bindings, project symlinks) are
torn down atomically; the paper's own fields ride into trash so a later restore
can rebuild them. A `y/N` prompt guards the delete (default N).

```
lit rm <id>
lit rm <id> --dry-run
lit rm <id> -y
lit rm <id> --purge
```

| Flag | What it does |
|---|---|
| `--paper-doi <doi>` | Look the paper up by DOI instead of id. |
| `--purge` | Permanently delete instead of moving to `.trash/`. |
| `-y`, `--yes` | Non-interactive force-delete: skip the prompt and tear down in one step. |
| `--dry-run` | Preview the full impact set (the paper plus every link that would be cleared / unbound / orphaned), then exit without deleting. |

---

## 3. Reading status

Five one-keystroke shorthands for the equivalent `lit modify --set` on the
reading-workflow fields. Each accepts a full id, a unique case-insensitive id
substring, or `--paper-doi <DOI>`.

```
lit read <id> [--date YYYY-MM-DD]
lit revisit <id> [--date YYYY-MM-DD]
lit skim <id>
lit promote <id>
lit drop <id>
```

| Command | Effect | Equivalent |
|---|---|---|
| `lit read` | Stamp `read-date` (the first read). | `--set read-date=<date>` |
| `lit revisit` | Stamp `last-revisited` (a re-read). | `--set last-revisited=<date>` |
| `lit skim` | Set `status=skim`. | `--set status=skim` |
| `lit promote` | Set `status=deep-read`. | `--set status=deep-read` |
| `lit drop` | Set `status=dropped`. | `--set status=dropped` |

`read` and `revisit` default to today (local timezone) and accept `--date` to
backdate. `read-date` and `last-revisited` are kept semantically separate;
`promote` does not touch `read-date`. To reverse a status, use
`lit modify <id> --set status=<value>`. See [3-concepts.md](3-concepts.md) §1.1
for what each field means.

---

## 4. Linking & organization

### `lit link`

Link a paper to a project: add the `projects` tag, write a symlink under
`<project>/litman_reflib/<id>/`, and regenerate `<project>/REFERENCES.md`. The
project must be registered in `lit-config.yaml` (via `lit project add`) and its
directory must exist on disk **before** linking.

```
lit link <id> --project <name>
lit link <id> --project <name> --relevance "Direct baseline"
lit link --rebuild-all
```

| Flag | What it does |
|---|---|
| `--paper-doi <doi>` | Look the paper up by DOI. Mutually exclusive with the positional id and `--rebuild-all`. |
| `--project <name>` | Project name (must be registered in `lit-config.yaml`). |
| `--relevance <text>` | Set the `relevance-<project>` field in one shot. Otherwise left untouched. |
| `--rebuild-all` | Cross-machine recovery: rebuild every project's symlinks + `REFERENCES.md` from each paper's `projects` field. Skips `<id>` / `--project`. |

### `lit unlink`

Reverse a link: drop the `projects` tag, the symlink, the `REFERENCES.md` entry,
and (by default) the `relevance-<project>` field. Code symlinks under the project
are removed only if no other linked paper there still references the same repo.

```
lit unlink <id> --project <name>
lit unlink <id> --project <name> --keep-relevance
```

| Flag | What it does |
|---|---|
| `--paper-doi <doi>` | Look the paper up by DOI instead of id. |
| `--project <name>` | Project to unlink from. **Required.** |
| `--keep-relevance` | Preserve the `relevance-<project>` field. Default drops it (the value is echoed in the summary). |

### `lit project`

Manage the project registry. A project is a controlled `projects` value bound to
an on-disk path. Both truth sources — `TAXONOMY.md`'s `## projects` section and
`lit-config.yaml`'s `projects:` map — are kept in sync by every subcommand. Do
not hand-edit either side.

```
lit project add <name> --path <abs-path>
lit project list
lit project rename <old> <new>
lit project set-path <name> <new-path>
lit project rm <name> [-y]
```

| Subcommand | What it does |
|---|---|
| `add <name> --path <dir>` | Register a project (dual-write TAXONOMY + config) in one atomic write. `--path` is **required** and must already exist (no placeholder registration). |
| `list` | List every project, each row tagged with a drift marker (`✓` / `⚠ path-missing` / `⚠ config-only` / `⚠ taxonomy-only`). |
| `rename <old> <new>` | Rename the project across TAXONOMY, the config key, every paper, and `INDEX.json`. The path carries over. No prompt (semantics-preserving). |
| `set-path <name> <path>` | Change the on-disk path (config only — papers store names). Prints the rebuild hint, since it does not move the directory. |
| `rm <name>` | Cascade-untag papers and drop from both truth sources. Lists referencing papers and prompts `y/N`; `-y` skips. |

`lit project` is the project counterpart to `lit taxonomy`; `projects` is **not**
managed through `lit taxonomy` (only `lit taxonomy list projects` works
read-only). See [3-concepts.md](3-concepts.md) §1.3.

### `lit code`

Manage code repositories bound to papers. Repos live under
`<vault>/codes/<repo-name>/` with `repo/` (the git checkout), `repo-meta.yaml`,
and `notes.md`. The binding is bidirectional: a paper's `code-clones` field
lists the repo, and the repo's `papers` field lists every bound paper (one repo
can bind multiple papers).

```
lit code add <url> [--name <n>] [--paper <id>] [--depth N]
lit code add <local-dir> --move
lit code list [--paper <id> | --orphan]
lit code link <repo-name> --paper <id>
lit code unlink <repo-name> --paper <id>
lit code update <repo-name> [--unshallow]
lit code rm <repo-name> [--cascade] [-y]
lit code restore-all [--dry-run]
```

| Subcommand | What it does |
|---|---|
| `add <source>` | Clone (URL source) or copy/move (local-path source) into `codes/<name>/repo/`, seeding `repo-meta.yaml` and `notes.md`. |
| `link <repo-name> --paper <id>` | Bind an already-present repo to a paper (idempotent if already bound). |
| `unlink <repo-name> --paper <id>` | Unbind a repo from a paper without deleting the clone. Drops only the named paper's edge; tolerant of an already-deleted clone. |
| `list` | List repos and their paper bindings. |
| `update <repo-name>` | `git pull --ff-only` inside the repo. |
| `rm <repo-name>` | Permanently delete `codes/<repo-name>/`. Hard delete (re-clonable from the recorded upstream). |
| `restore-all` | Re-clone every repo whose `repo/` checkout is missing (cross-machine recovery). |

Per-subcommand flags:

- `add`: `--name <override>`, `--paper <id>` / `--paper-doi <doi>` (bind on add),
  `--depth N` (URL only; `0` = full history; default from `lit-config.yaml`'s
  `default_clone_depth`), `--move` (local-import only: move instead of copy).
- `list`: `--paper <id>` / `--paper-doi <doi>` / `--orphan` (repos with no
  bindings) — mutually exclusive.
- `update`: `--unshallow` (promote a shallow clone to full history).
- `rm`: `--cascade` (strip the repo from every paper's `code-clones` first;
  without it, `rm` refuses when any paper still references the repo), `-y` / `--yes`.
- `restore-all`: `--depth N`, `--dry-run`. Exit code 1 if any clone failed or any
  orphan reference was found (CI / cron-gateable).

### `lit taxonomy`

Manage `TAXONOMY.md`, the controlled vocabulary. Governs three user dictionaries:
`topics`, `methods`, `data`. Tagging a paper with a value requires the value to be
registered here first (register-first; no escape hatch on `lit modify`). All
changes are atomic (TAXONOMY + every referencing `metadata.yaml` + `INDEX.json`
in one staged write).

```
lit taxonomy list [<dict>]
lit taxonomy add <dict> <value>...
lit taxonomy rename <dict> <old> <new>
lit taxonomy merge <dict> <src>... --into <dest> [-y]
lit taxonomy rm <dict> <value> [-y]
```

| Subcommand | What it does |
|---|---|
| `list [<dict>]` | Show one dict, or all dicts when no name is given. |
| `add <dict> <value>...` | Register one or more values in a user dict. Already-present values are silent no-ops; the dict is kept sorted. |
| `rename <dict> <old> <new>` | Rename a value and ripple to every referencing paper. No prompt (semantics-preserving). |
| `merge <dict> <src>... --into <dest>` | Fold sources into a destination value (existing or new), cascading. `--into` **required**; `-y` skips the prompt. |
| `rm <dict> <value>` | Remove a value, cascading the removal to every referencing paper. Lists them and prompts `y/N`; `-y` skips. |

`projects` is not managed here — use `lit project` (it carries an on-disk path).
The three fixed-enum dicts (`type`, `status`, `priority`) are read-only through
`lit taxonomy` and require a code release to extend. Never hand-edit
`TAXONOMY.md` to rename or remove a value. See [3-concepts.md](3-concepts.md) §1.3.

---

## 5. Maintenance

### `lit health-check`

Scan the whole vault for inconsistencies: dangling references, schema gaps, stale
staging dirs, missing PDFs, dangling wikilinks, dangling vault-registry entries,
missing project directories. Exits 0 on a clean vault, 1 if any issue is found
(so it can gate cron / CI).

```
lit health-check
lit health-check --fix
```

| Flag | What it does |
|---|---|
| `--fix` | Auto-regenerate all derived artifacts (lossless recompute from metadata) and clean stale staging dirs / orphan trash sidecars. Registry / project / taxonomy / code-clone drift stays report-only (it needs a per-case decision). With `--fix`, the exit code reflects post-fix state. |

### `lit refresh-views`

Rebuild every derived artifact from `papers/*/metadata.yaml`, in order: (1)
`INDEX.json` (paper summary + by-doi reverse map), (2) `views/by-*` symlink hubs
(wiped and rebuilt, so stale tag buckets disappear), (3) each project's
`litman_reflib/` symlinks and `REFERENCES.md`. Per-project failures (missing
project dir on this machine) are skipped, not aborted.

```
lit refresh-views
```

No flags beyond the global ones. Everything it produces is derived and safe to
regenerate wholesale.

### `lit trash`

Manage the recoverable-delete bin under `<vault>/.trash/`, capped at 100 entries
(`lit rm` evicts the oldest when full).

```
lit trash list
lit trash restore <id-or-entry> [-y]
lit trash empty [--dry-run] [-y]
```

| Subcommand | What it does |
|---|---|
| `list` | Show trash entries, newest first. |
| `restore <id-or-entry>` | Restore a trashed paper to `papers/<id>/` and rebuild its relations (opposite papers' reverse edges, surviving repo bindings, project symlinks + `REFERENCES.md`). A 1:1 repo hard-deleted at `rm` time is re-cloned (`-y` to auto-attempt without prompting). |
| `empty` | Permanently delete every trash entry. `--dry-run` lists what would be removed; `-y` skips the prompt. |

### `lit sync`

rclone-backed one-way cloud sync. `push` mirrors the vault to your remote;
`pull` reverses it for cross-machine restore. The vault's per-machine sync-state
file and the transient `.litman-staging/` directory are always excluded.

```
lit sync setup [--remote NAME] [--path PATH]
lit sync push [--dry-run] [-y] [-f] [--exclude-repos]
lit sync pull [--dry-run] [--exclude-repos]
lit sync status
```

| Subcommand | What it does |
|---|---|
| `setup` | Hand the TTY to `rclone config`, then record the chosen remote + path in `lit-config.yaml`. `--remote <name>` skips the interactive step (point litman at an existing remote); `--path <path>` sets the mirror path. |
| `push` | Upload the vault (`rclone sync` — deletes orphans on the remote). |
| `pull` | Download the remote into the vault. **One-way with deletion** — local files absent on the remote are removed. |
| `status` | Show last-push / last-pull timestamps and local vs. remote file counts. No network mutation. |

`push` runs a full health-check first as an integrity gate: any error-severity
finding aborts the push, so a corrupted local state never overwrites the cloud
backup. `-f` / `--force` bypasses the gate; `-y` / `--yes` only skips the
first-push size confirmation and does **not** bypass the gate. Both `push` and
`pull` accept `--dry-run` and the paired `--exclude-repos` / `--include-repos`
(apply `codes_ignore_patterns` so `codes/*/repo/` checkouts are skipped; re-clone
them later with `lit code restore-all`).

### `lit export`

Project the vault out to a `.bib` file for LaTeX. Cite keys equal paper ids, so
`\cite{<paper-id>}` works across machines. Re-running on the same file is the
supported update path. One of `--project` / `--all` is required.

```
lit export --project <name>
lit export --all
lit export --project <name> -o path/to/refs.bib
lit export --all --topic transformer --author wang
```

| Flag | What it does |
|---|---|
| `--project <name>` | Export every paper linked to the project. Mutually exclusive with `--all`. |
| `--all` | Export every paper in the vault. |
| `-o`, `--output <file>` | Output path. Default `./refs.bib`. |
| `--priority` / `--status` / `--year` / `--type` / `--topic` / `--method` / `--data` / `--author` | The same filter set as `lit list` (within a flag OR, across flags AND). |
| `--force` | Overwrite a target file even without the litman sentinel (typically a hand-edited `.bib`). |
| `--format [bibtex]` | Output format. Only `bibtex` is implemented. |

Every generated file's first line is a litman sentinel comment; `lit export`
refuses to overwrite a target whose first line is not that sentinel, so a
hand-curated `.bib` at the same path is safe. The exporter uses the bib-oriented
fields filled in by `lit add`; fill them on older papers with
`lit modify <id> --set venue-type=journal-article` etc.

### `lit config`

Inspect the active vault's `lit-config.yaml`.

```
lit config show
lit config show --format yaml
```

| Subcommand | What it does |
|---|---|
| `show` | Print the parsed, validated config, reflecting the *effective* values after schema defaults fill in any omitted fields. `--format [table\|yaml]` chooses a Rich table (default) or the canonical YAML form. |

See [3-concepts.md](3-concepts.md) §1.4 for what each config field controls.

### `lit gui`

Launch the litman Web UI — a localhost browser app for browsing, reading PDFs,
annotating, and everyday curation. It serves the active vault and binds
`127.0.0.1` only; on HPC it prints a ready-to-paste `ssh -L` tunnel line so you
can open the printed URL in your local browser. If the default port is busy it
walks upward to the next free one (Jupyter-style) and prints the port it landed
on.

```
lit gui
lit gui --port 9000
```

| Flag | What it does |
|---|---|
| `--port <n>` | Port to bind. Default `8765`; auto-increments if busy. |

The Web UI drives a growing subset of the commands on this page through the same
code paths — this page (the CLI) stays the complete surface. The web server
(fastapi + uvicorn) ships as a core dependency; a corrupted install missing it
prints a `pipx install --force litman` hint.
