# litman

Local-first, AI-augmented literature management CLI.

> **Pre-alpha. Under active development.** Authoritative design spec lives in the
> parent directory at `../LITERATURE-SYSTEM-DESIGN.md`.

## Quick start (developer)

From the repo root:

```bash
conda activate litman                           # conda env, Python 3.12
pip install -e ".[dev]"                         # editable install with dev deps
lit --help                                      # verify CLI entry point
lit hello                                       # placeholder smoke command
pytest                                          # run smoke tests
```

## Architecture overview

Four layers; each can run without the layer above it:

| Layer | Component | Status |
|-------|-----------|--------|
| 4 | Claude Code + `lit-library` skill (optional) | M3 |
| 3 | `lit` CLI (Click) | M1.1 |
| 2 | `litman` Python package (core / importers / exporters / views / checks) | M1.1+ |
| 1 | Data on disk (yaml + markdown), source of truth | M1.2 |

LLM augmentation never writes data directly — CLI scripts validate and persist
all changes. See `feedback_architecture_invariants.md` in project memory.

## License

MIT — see `LICENSE`.
