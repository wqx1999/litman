# litman

**Local-first, AI-augmented literature management CLI.**
One paper, one folder, one structured metadata file — governed by a
controlled vocabulary, queried by a fast index, and designed from day one
to be navigated by a Claude Code agent.

---

## What is litman

litman manages a personal literature vault — a folder on disk holding
hundreds of papers, each in its own subfolder with a YAML metadata file,
a markdown notes file, and the original PDF. The same vault can be
shared across multiple projects: one paper, many project bindings.

Everything is plain text on your filesystem. No cloud database, no
proprietary container format. You can edit any file by hand, `grep` the
whole vault, or hand the directory tree to an AI agent. The CLI exists
to make routine edits atomic and to keep cross-references consistent,
not to gate access to your data.

## What makes litman tick

Five design choices, in priority order:

1. **Local-first storage.** Everything is plain text on your
   filesystem — YAML metadata, markdown notes, original PDFs. You can
   edit any file by hand, `grep` the whole library, back it up
   anywhere. No cloud database, no proprietary container format.

2. **Controlled vocabulary with atomic operations.** Topics, methods,
   projects, and data sources are governed by a shared `TAXONOMY.md`
   file. Renames, merges, and removals cascade across every referencing
   paper in a single atomic operation — the vocabulary stays clean as
   the library grows from tens to hundreds of papers.

3. **AI-native CLI.** Two bundled Claude Code skills (`lit-library` for
   ingestion and retrieval, `lit-reading` for reading assistance) teach
   an agent how to navigate the vault and operate the CLI on your
   behalf. The agent emits structured JSON; the CLI validates and
   writes the data — your library stays correct even when the model
   isn't perfect.

4. **Paper ↔ project ↔ code triangle.** One paper can be bound to
   multiple projects without duplication; each project gets its own
   symlinked working folder and an auto-generated `REFERENCES.md`. Each
   paper can also be bound to its official code repository, cloned
   inside the vault next to the metadata.

5. **An agent-readable knowledge graph.** Metadata fields, wikilinks,
   and symlinks together form a knowledge graph that emerges
   naturally — no manual maintenance of "double-linked" notes. Because
   the vocabulary is controlled, an AI agent can do precise
   cross-paper retrieval, not just keyword search.

Together these mean: your data lives on your disk, stays consistent
over time, and an AI assistant can actually understand it.

---

## Install

litman is a Python CLI tool. Install with **pipx** so `lit` is permanently
available in every shell, isolated from your other Python environments.

**From a clone of this repo**:

```bash
git clone https://github.com/wqx1999/litman.git
cd litman
pipx install .
```

**From PyPI** (not yet shipped; planned):

```bash
pipx install litman
```

Don't have pipx? `python -m pip install --user pipx` (or `apt install pipx` /
`brew install pipx` / `conda install -c conda-forge pipx`).

Then run the one-shot onboarding wizard:

```bash
lit setup                    # interactive: shell completion → Claude Code skill → vault (name + parent dir) → (optional) cloud sync
```

Dependencies (auto-installed): `click`, `ruamel.yaml`, `httpx`, `pypdf`,
`pydantic`, `rich`, `platformdirs`.

### Platform notes

- **Linux / macOS / WSL**: install directly (the steps above). WSL counts as
  Linux — full feature support, no extra setup.
- **Windows**: use WSL (recommended). Native PowerShell / cmd also work, with
  two caveats:
  - Symlink-based browsing views and project bridges are skipped unless the
    terminal is launched "Run as administrator" (a one-line notice is printed;
    all other commands keep working).
  - Core commands (add / list / show / modify / taxonomy / export) work
    regardless.

## Upgrade

**From PyPI** (not yet shipped; planned):

```bash
pipx upgrade litman
```

**From a local clone**:

```bash
cd litman
git pull
pipx install --force .
```

**For development** (`git pull` alone takes effect):

```bash
cd litman
pipx uninstall litman
pipx install -e .
```

## Quick start

```bash
# 1. Create a vault. You pass the PARENT dir; the CLI creates the vault
#    subdir AND registers it as your active vault — no env var to set.
lit init /work/me/                  # → /work/me/literature_vault/ (registered, active)

# 2. Add your first paper (CrossRef fetch). lit already knows your active vault.
lit add ~/Downloads/attention_is_all_you_need.pdf --doi 10.48550/arXiv.1706.03762

# 3. Browse.
lit list
lit show 2017_Vaswani_Attention
lit open 2017_Vaswani_Attention      # launches your configured PDF viewer

# 4. Classify (registers a topic, then tags the paper with it).
lit taxonomy add topics transformer attention
lit modify 2017_Vaswani_Attention --add-tag topics=transformer

# 5. Bind to a project (project must be declared in lit-config.yaml first).
lit link 2017_Vaswani_Attention --project MyResearchProject
```

### Multiple vaults & overrides (advanced)

`lit init` registers your vault and makes it active, so the common case needs
no configuration. Beyond that:

- **Manage multiple vaults**: `lit vault add <name> <path>` registers an
  existing vault (e.g. a snapshot from a colleague); `lit vault list` shows
  all registered vaults and which is active; `lit vault use <name>` switches
  the active vault.
- **Override per-command / per-shell**: `--library <path>` or the
  `LIT_LIBRARY` environment variable point a single command (or shell) at a
  specific vault without touching the active selection. Useful for scripts,
  CI, or working with two vaults in parallel terminals.
- **Back up the registry**: the registry lives at your platform's user-config
  dir by default. Set `LITMAN_REGISTRY_DIR` to a cloud-synced folder to get
  backup + cross-machine sync. (Vault paths are stored absolute, so
  cross-machine sync needs each vault at the same path on every machine.)
- **Drift is surfaced automatically**: if you delete or move a registered
  vault directory by hand, the next `lit *` command offers to drop the
  stale entry (TTY: `[Y/n]` default Y; non-TTY: one-line stderr warning).
  Likewise, if a linked project's directory moves, the next command offers
  to re-point it to a new path and rebuild its `litman_reflib/` (blank to
  skip; it never auto-deletes a project). `lit health-check` reports both.

---

## Good to know

- **Moving a vault or project folder breaks its links.** Symlinks, project
  bridges, and the vault registry are path-based; relocate one outside the CLI
  and cross-references go stale. If you do move something, run
  `lit health-check` to see what broke and how to repair it.
- **Reading help is only as good as your model.** A multimodal model (e.g.
  Claude) reads the PDF directly, figures and tables included. A text-only
  model falls back to plain-text extraction (pypdf), which cannot interpret
  figures or image-based tables — don't lean on it for "what does Fig./Table N
  show?" unless you have attached a vision or OCR backend.

---

## Documentation

Full reference lives under [`docs/`](docs/):

| Topic | File |
|---|---|
| Vocabulary and concepts | [docs/concepts.md](docs/concepts.md) |
| Command reference (by scenario) | [docs/commands.md](docs/commands.md) |
| `metadata.yaml` schema | [docs/metadata-schema.md](docs/metadata-schema.md) |
| `lit-config.yaml` schema | [docs/config-schema.md](docs/config-schema.md) |
| `TAXONOMY.md` schema | [docs/taxonomy-schema.md](docs/taxonomy-schema.md) |
| Four-layer architecture | [docs/architecture.md](docs/architecture.md) |
| Design philosophy | [docs/philosophy.md](docs/philosophy.md) |

Local-preview the docs as a static site:

```bash
pip install mkdocs mkdocs-material
mkdocs serve
```

## License

MIT. See [`LICENSE`](LICENSE).
