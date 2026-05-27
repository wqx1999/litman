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

litman lives in its own environment, so it never pollutes `base` or another
project. Two ways in — both isolate the install, then share the same two
follow-up steps.

**From a clone of this repo** — `install.sh` creates (or reuses) a `litman`
conda env and installs into it (requires conda):

```bash
./install.sh            # editable dev install into conda env 'litman'
# ./install.sh --prod   # regular (non-editable) install
# ./install.sh --env X  # use a different env name
```

**From PyPI** (not yet shipped; planned) — `pipx` isolates automatically:

```bash
pipx install litman
```

Both install *only*. Finish with the same two manual steps:

```bash
lit install-skill            # deploy the Claude Code skills into ~/.claude/skills/
lit init /path/to/parent     # create your vault — see Quick start
```

Dependencies (auto-installed): `click`, `ruamel.yaml`, `httpx`, `pypdf`,
`pydantic`, `rich`, `platformdirs`.

## Quick start

```bash
# 1. Create a vault. You pass the PARENT dir; the CLI creates the vault subdir.
lit init /work/me/                  # → /work/me/literature_vault/

# 2. Point your shell at it (drop into ~/.bashrc / ~/.zshrc).
export LIT_LIBRARY=/work/me/literature_vault

# 3. Add your first paper (CrossRef fetch).
lit add ~/Downloads/attention_is_all_you_need.pdf --doi 10.48550/arXiv.1706.03762

# 4. Browse.
lit list
lit show 2017_Vaswani_Attention
lit open 2017_Vaswani_Attention      # launches your configured PDF viewer

# 5. Classify (registers a topic, then tags the paper with it).
lit taxonomy add topics transformer attention
lit modify 2017_Vaswani_Attention --add-tag topics=transformer

# 6. Bind to a project (project must be declared in lit-config.yaml first).
lit link 2017_Vaswani_Attention --project MyResearchProject

# 7. (Skills are wired during Install; re-run after upgrades to refresh them.)
lit install-skill
```

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
