<h1>LITerature MANager <img src="https://raw.githubusercontent.com/wqx1999/litman/main/assets/logo1.png" width="120" align="right"/></h1>

<br clear="all"/>

<div align="center">

<img src="https://raw.githubusercontent.com/wqx1999/litman/main/assets/logo2.png" width="58%" alt="LITMAN"/>

<p>
<a href="https://pypi.org/project/litman/"><img src="https://img.shields.io/pypi/v/litman?logo=pypi&logoColor=white" alt="PyPI version"/></a>
<img src="https://img.shields.io/badge/python-3.12%2B-3776AB?logo=python&logoColor=white" alt="Python 3.12+"/>
<img src="https://img.shields.io/badge/license-MIT-green" alt="License: MIT"/>
<img src="https://img.shields.io/badge/AI--native-Claude%20Code-D97757?logo=anthropic&logoColor=white" alt="AI-native: Claude Code"/>
</p>

<p><b>English</b> | <a href="README-CN.md">中文</a></p>

</div>

**Local-first, AI-augmented literature manager.**

A local knowledge base for research papers, stored as plain files on your
disk. Papers link explicitly to projects, code repositories, and each other
through structured metadata and symlinks. Use it through a web UI for everyday
browsing, reading, and annotation — and, for anything the UI doesn't cover,
just ask an AI agent, which drives the full `lit` CLI for you via the bundled
Claude Code skills.

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
3. **Don't edit metadata files by hand.** Change papers, taxonomy, and config
   through the web UI or by asking your AI agent — both go through validated
   `lit` commands that keep cross-references intact.
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

4. **AI drives the CLI; you drive the agent.** The `lit` CLI is litman's
   complete capability surface, but it's built to be operated by an AI agent,
   not typed by hand. Two bundled Claude Code skills (`lit-library` for
   ingestion and retrieval, `lit-reading` for reading assistance) teach the
   agent to run it from plain-English requests; the agent emits structured
   JSON and the CLI validates every write, so your library stays correct even
   when the model isn't perfect. A web UI (`lit gui`) covers everyday
   browsing, reading, and annotation — a friendly subset of what the agent
   can do.

---

## Install

litman is a Python CLI tool. Install with **pipx** so `lit` is permanently
available in every shell, isolated from your other Python environments.
Don't have pipx? See [pipx.pypa.io](https://pipx.pypa.io).

**From PyPI** (recommended):

```bash
pipx install 'litman[web]'   # first install (includes the web UI)
pipx upgrade litman          # update
```

**From a local clone** (development):

```bash
# first install
git clone https://github.com/wqx1999/litman.git
cd litman
pipx install '.[web]'

# update (pull latest code first)
git pull
pipx install --force '.[web]'
```

Then run the one-shot setup wizard:

```bash
lit setup   # interactive wizard: shell completion → Claude Code skill → vault setup → (optional) cloud sync
```

## Uninstall

Run two steps, in order — `lit uninstall` first (while the `lit` command still
exists), then pipx:

```bash
lit uninstall          # removes bundled skills, shell completion, and the vault registry
pipx uninstall litman  # removes the lit CLI itself
```

`lit uninstall` lists exactly what it will delete and asks first — pass
`--dry-run` to preview or `-y` to skip the prompt.

If you installed from a local clone, also delete the cloned repo folder once the
CLI is gone:

```bash
rm -rf path/to/litman   # the directory you git-cloned into
```

Your vault (papers, PDFs, notes, annotations) is never touched by any of this;
delete that directory by hand if you also want the data gone.

## Quick start

```bash
lit init /work/me/    # create a vault (pass the parent dir; lit creates + registers the subdir)
lit gui               # open the web UI — browse, read, annotate, tag, and link papers
```

The web UI covers everyday browsing, reading, and annotation — a friendly subset
of everything litman can do. For anything it doesn't cover (adding papers,
taxonomy edits, project links, health checks, ...), just ask your Claude Code
agent in plain English ("add this paper and tag it transformer") and the bundled
skills drive the full CLI for you. The complete command set the agent operates
is in the [command reference](docs/4-commands.md).

---

## Agent model benchmark

litman's agent layer (the bundled `lit-library` and `lit-reading` skills) is
meant to work with whatever model you point Claude Code at, not only Anthropic's.
To see how well different models drive it, we ran each one as the Claude Code
backend and had it operate litman through the skills, over **22 everyday-workflow
tasks** (add, read, tag, modify, link, export, taxonomy edits, health checks,
...), 3 rounds each, on the **litman 1.0.0** codebase ([commit 876d11c](https://github.com/wqx1999/litman/commit/876d11c), June 2026).

**What the score is.** Each task is a **single-turn prompt in a clean context**:
a fresh agent gets one natural-language instruction and must complete it in that
one turn, with no prior conversation and no follow-up. **TRR** (task-completion
rate) is the fraction of tasks the resulting vault state passed; **RA** (routing
accuracy) is how often the agent picked the correct skill for a request.

**A low score does not mean the model cannot operate litman.** It means the model
less often *one-shots* the task from a cold start. With more guidance (a more
detailed request, or a few follow-up turns) a lower-scoring model can still do the
same work. This is a deliberately hard zero-shot floor, not a ceiling.

| Model | Task completion (TRR) | Routing (RA) |
|:---|---:|---:|
| [Claude Sonnet 4.6](https://www.anthropic.com) | 97% | 100% |
| [Claude Haiku 4.5](https://www.anthropic.com) | 97% | 79% |
| [DeepSeek-V4 Flash](https://www.deepseek.com) | 80% | 71% |
| [DeepSeek-V4 Pro](https://www.deepseek.com) | 76% | 57% |
| [MiniMax-M3](https://www.minimax.io) | 71% | 75% |
| [GLM-5.1](https://z.ai/model-api) | 58% | 64% |
| [MiMo-V2.5 Pro](https://mimo.mi.com/) | 26% | 0% |
| [MiMo-V2.5](https://mimo.mi.com/) | 21% | 0% |

TRR is the mean over the 22 auto-scored tasks across 3 rounds; network-dependent
and multi-turn scenarios (code cloning, cloud sync, a multi-turn recovery case)
are excluded from this single-turn score. Whatever the model scores, the data
layer validates every write — a wrong command fails loudly rather than writing
bad data into the vault, so a lower-scoring model needs more turns but never
corrupts the library.

---

## Documentation

Full documentation lives under [`docs/`](docs/). New to litman? The
[tutorial](docs/5-tutorial.md) covers about 80% of everyday use; for anything
else, ask the agent or check the command reference. [docs/0-readme.md](docs/0-readme.md)
maps out the whole set.

| Topic | File |
|---|---|
| Start here — docs map | [docs/0-readme.md](docs/0-readme.md) |
| Design philosophy | [docs/1-philosophy.md](docs/1-philosophy.md) |
| Four-layer architecture | [docs/2-architecture.md](docs/2-architecture.md) |
| Concepts and field reference (`metadata.yaml`, `lit-config.yaml`, `TAXONOMY.md`) | [docs/3-concepts.md](docs/3-concepts.md) |
| Command reference | [docs/4-commands.md](docs/4-commands.md) |
| Tutorial | [docs/5-tutorial.md](docs/5-tutorial.md) |

Local-preview the docs as a static site:

```bash
pip install mkdocs mkdocs-material
mkdocs serve
```

## Acknowledgments

This tool was developed in the [Süssmuth Lab](https://www.tu.berlin/en/biochemie/research/research-in-suessmuth-group), Technische Universität Berlin. Development was carried out with access to the [TU Berlin HPC cluster](https://www.tu.berlin/en/hpc-cluster/introduction-slurm-version).

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

Cloud sync (`lit sync`) is powered by [rclone](https://rclone.org/), the external
CLI that mirrors the vault to any cloud backend it supports — the backbone of how
a vault gets backed up and moved between machines:

[![rclone](https://img.shields.io/badge/rclone-Cloud_Sync_Engine-3F87E5?logo=rclone&logoColor=white)](https://rclone.org/)

Octopus mascot generated with [Doubao](https://www.doubao.com/) (AI image generation).

## License

MIT. See [`LICENSE`](LICENSE).

---

<sub>AI agents: a condensed, link-dense map of this project lives in [README-Agent.md](README-Agent.md).</sub>
