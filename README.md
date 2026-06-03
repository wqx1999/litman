<h1>LITerature MANager <img src="assets/logo1.png" width="120" align="right"/></h1>

<br clear="all"/>

<div align="center">

<img src="assets/logo2.svg" width="58%" alt="LITMAN"/>

<p>
<img src="https://img.shields.io/badge/python-3.12%2B-3776AB?logo=python&logoColor=white" alt="Python 3.12+"/>
<img src="https://img.shields.io/badge/license-MIT-green" alt="License: MIT"/>
<img src="https://img.shields.io/badge/AI--native-Claude%20Code-D97757?logo=anthropic&logoColor=white" alt="AI-native: Claude Code"/>
</p>

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
Create a new vault at /work/me/.
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

## Acknowledgments

This tool was developed in the [Süssmuth Lab](https://www.tu.berlin/en/biochemie/research/research-in-suessmuth-group), Technische Universität Berlin. Computations and infrastructure were supported by the [TU Berlin HPC cluster](https://www.tu.berlin/en/hpc-cluster/introduction-slurm-version).

This project was built with the help of AI-powered development tools:

[![Claude Code](https://img.shields.io/badge/Claude_Code-Anthropic-d4a574?logo=anthropic&logoColor=white)](https://claude.ai/code)
[![Cursor](https://img.shields.io/badge/Cursor-AI_Editor-000000?logo=cursor&logoColor=white)](https://cursor.sh)

Core dependencies that make litman possible:

[![Click](https://img.shields.io/badge/Click-CLI_Framework-4B8BBE?logoColor=white)](https://click.palletsprojects.com/)
[![ruamel.yaml](https://img.shields.io/badge/ruamel.yaml-YAML_Parser-FFDD54?logoColor=black)](https://pypi.org/project/ruamel.yaml/)
[![pypdf](https://img.shields.io/badge/pypdf-PDF_Extraction-EE4C2C?logoColor=white)](https://pypdf.readthedocs.io/)
[![Pydantic](https://img.shields.io/badge/Pydantic-Data_Validation-E92063?logo=pydantic&logoColor=white)](https://docs.pydantic.dev/)
[![Rich](https://img.shields.io/badge/Rich-Terminal_UI-FAD000?logoColor=black)](https://rich.readthedocs.io/)
[![httpx](https://img.shields.io/badge/httpx-HTTP_Client-2D9CDB?logoColor=white)](https://www.python-httpx.org/)

## License

MIT. See [`LICENSE`](LICENSE).
