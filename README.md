<div align="center">
<img src="assets/logo1.png" width="90"/>&nbsp;&nbsp;<img src="assets/logo2.svg" width="55%"/>
<br/><br/>
<b>LITerature MANager</b>
</div>

**Local-first, AI-augmented literature management CLI.**

A local knowledge base for research papers, stored as plain files on your
disk. Papers link explicitly to projects, code repositories, and each other
through structured metadata and symlinks. Bundled Claude Code skills let an
AI agent operate the CLI on your behalf; every command works equally well
typed by hand.

---

## Know before you use

A few things worth knowing up front:

1. **Don't move a vault or project folder by hand.** Links (symlinks, project
   bridges, the registry) are path-based and will break. If you must move one,
   run `lit health-check` afterwards to find and repair what broke.
2. **Figure/table reading needs a multimodal model.** A text-only model falls
   back to plain-text extraction (pypdf) and cannot see figures or
   image-based tables — don't ask it "what does Fig./Table N show?" without a
   vision or OCR backend attached.
3. **Don't edit metadata by hand.** Use `lit` commands to modify papers,
   taxonomy, and config — or ask an AI agent if you're unsure which command
   to use.
4. **Windows users.** Symlink-based features (browsing views, project bridges)
   require administrator privileges; all other commands work regardless.
   [WSL](https://learn.microsoft.com/en-us/windows/wsl/) is recommended.

## Key Features

1. **Long-term reliable local knowledge vault.** Everything is plain
   text on your filesystem — YAML metadata, markdown notes, original
   PDFs. No cloud database, no proprietary container format. Back it up
   anywhere, read every file as plain text, `grep` the whole library.

2. **Consistent by design.** Topics, methods, projects, and data
   sources are governed by a shared `TAXONOMY.md` controlled vocabulary.
   Atomic operations keep cross-references clean as the library grows,
   and `lit health-check` catches any drift before it accumulates.

3. **Paper ↔ project ↔ code triangle.** One paper can be bound to
   multiple projects without duplication; each project gets its own
   symlinked working folder and an auto-generated `REFERENCES.md`. Each
   paper can also be bound to its official code repository, cloned
   inside the vault. Metadata fields and symlinks together form an
   explicit, navigable knowledge graph — no manual upkeep required.

4. **AI-native CLI.** Two bundled Claude Code skills (`lit-library` for
   ingestion and retrieval, `lit-reading` for reading assistance) teach
   an agent how to navigate the vault and operate the CLI on your
   behalf. The agent emits structured JSON; the CLI validates and
   writes the data — your library stays correct even when the model
   isn't perfect.

---

## Install

litman is a Python CLI tool. Install with **pipx** so `lit` is permanently
available in every shell, isolated from your other Python environments.
Don't have pipx? See [pipx.pypa.io](https://pipx.pypa.io).

**From PyPI** (stable release; not yet shipped, planned):

```bash
pipx install litman   # first install
pipx upgrade litman   # update
```

**From a local clone** (development):

```bash
# first install
git clone https://github.com/wqx1999/litman.git
cd litman
pipx install .

# update (pull latest code first)
git pull
pipx install --force .
```

Then run the one-shot setup wizard:

```bash
lit setup   # interactive wizard: shell completion → Claude Code skill → vault setup → (optional) cloud sync
```

## Quick start

### With an AI agent

```
Add ~/Downloads/attention_is_all_you_need.pdf to my vault.
Show me all papers tagged with topic: transformer.
Tag 2017_Vaswani_Attention with topic: attention.
Link 2017_Vaswani_Attention to project MyResearchProject.
Remove 2017_Vaswani_Attention from my vault.
```

### CLI

```bash
# create a vault (pass the parent dir; CLI creates the subdir and registers it)
lit init /work/me/

# add a paper
lit add ~/Downloads/attention_is_all_you_need.pdf --doi 10.48550/arXiv.1706.03762

# browse
lit list
lit show 2017_Vaswani_Attention

# tag
lit taxonomy add topics transformer
lit modify 2017_Vaswani_Attention --add-tag topics=transformer

# link to a project
lit link 2017_Vaswani_Attention --project MyResearchProject

# remove
lit remove 2017_Vaswani_Attention
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
