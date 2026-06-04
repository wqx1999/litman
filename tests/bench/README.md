# litman-bench (v0)

Agent-driven, real-scenario regression suite for litman. A live agent is handed
a card's natural-language intent + fixture PDFs, decides which `lit` commands to
run, and a deterministic checker scores the resulting vault state. Fixes the
model + harness and measures litman (the inverse of SWE-bench).

This is a **dev-side tier**, separate from `tests/{commands,core,...}/` (the
per-commit unit tests). It is run on demand at milestone boundaries, not on every
commit, and it never touches your real vault (see *Isolation* below).

> The benchmark is not part of the shipped package. End users install litman via
> `pipx install litman` and never see this directory. The setup below is only for
> running the benchmark from a clone.

## Layout

| Path | Tracked | What |
|---|---|---|
| `manifest.yaml` | yes | the 10 fixture papers (id, source, url, golden metadata, test role) |
| `fixtures.lock` | yes | sha256 pins for the fixture PDFs (sha256sum format) |
| `fixtures/golden/*.json` | yes | extraction ground truth, derived from reading the real PDFs |
| `fixtures/pdfs/` | **no** (gitignored) | the downloaded PDFs — copyrighted preprints, never committed |
| `scenarios/*.yaml` | yes | the 33 scenario cards (intent + expected end-state) |
| `harness/` | yes | seeds, run-vault isolation, executor, checker, routing, batch |
| `run_bench.py` | yes | CLI entry: run all cards × N rounds, score, print a report |

## Setup from a fresh clone

The harness drives the `lit` CLI **and** imports `litman` as a library (the
`health` checks), so it needs litman installed in the Python that runs it. A venv
gives both with zero pollution of the system Python.

```bash
git clone https://github.com/wqx1999/litman.git && cd litman

python3 -m venv .venv
source .venv/bin/activate           # the one activation step; dev-only
pip install -e ".[dev]"             # litman + lit CLI + pytest, all inside .venv

# fetch the 10 fixture PDFs (not in git; sha256-verified against fixtures.lock)
python tests/bench/fetch_fixtures.py
```

`fetch_fixtures.py` (no args) skips PDFs already present and matching the lock, so
it is safe to re-run. `--check` verifies cached files without the network. A wrong
URL or a silent bot-block HTML page is caught (non-`%PDF-` body → hard error;
sha256 mismatch → hard error). First-ever pinning used `--write-lock` once on a
machine with browser-grade network access; that lock is committed.

For the **live** benchmark you also need the `claude` CLI installed and logged in
once (`claude` is what the executor spawns per card). The deterministic tests and
`--dry-run` below do not need it.

## Running

**Deterministic harness tests** (no agent, no `claude`):

```bash
pytest tests/bench -q
```

**Live benchmark** — spawns a real `claude -p` per card. Two-step is recommended:
validate the pipeline on a single round first, then measure with `--rounds 3`.

```bash
# step 1: single round, confirm the live pipeline holds (real seed/cp/agent/cleanup)
python tests/bench/run_bench.py --model claude-sonnet-4-6 --rounds 1 --out step1.json

# step 2: full measurement once step 1 is clean
python tests/bench/run_bench.py --model claude-sonnet-4-6 --rounds 3 --out full.json
```

Flags:

| Flag | Default | What |
|---|---|---|
| `--model` | `claude-sonnet-4-6` | executor model tier (passed straight through) |
| `--rounds` | 3 | repeats per card (for mean ± std) |
| `--cards` | all | comma-separated card ids to run a subset |
| `--out` | — | write the full report JSON to this path |
| `--dry-run` | off | exercise the pipeline with a fake executor (no `claude -p`) |
| `--base-url` | — | external-model proxy URL (see *External models*); unset → Anthropic OAuth |
| `--auth-token` | — | token for the external-model proxy (only with `--base-url`) |

The report prints an honest coverage breakdown (auto-scored / prose-blocked /
routing / skipped / multi-turn). Only auto-scored execution cards contribute to
TRR; routing accuracy is reported on its own axis; prose-blocked, skipped, and
multi-turn cards are surfaced, never folded silently into a passing number.
`skipped` = the sandbox physically cannot run the card (network / pty);
`multi-turn` = it runs but encodes an intrinsically multi-turn interaction that
cannot be fairly scored from one cold-start utterance (kept distinct so the
exclusion reason — methodology vs sandbox limits — stays legible).

## External models

To benchmark a non-Anthropic model, point the `claude` CLI at an Anthropic-compatible
proxy (LiteLLM / claude-code-router) and pass its endpoint:

```bash
python tests/bench/run_bench.py --model <proxy-model-name> \
    --base-url http://localhost:4000 --auth-token <token> --rounds 3
```

With `--base-url` set the harness skips the OAuth credential copy and authenticates
through the proxy. Without it, the default is your Claude subscription via OAuth.

## Env overrides

| Var | Effect |
|---|---|
| `LITMAN_BENCH_LIT_BIN` | pin a specific `lit` binary (default: `lit` discovered on PATH) |
| `LITMAN_BENCH_CLAUDE_BIN` | pin a specific `claude` binary (default: `claude` on PATH) |

## Isolation

The benchmark never reads or writes your real vault. Each scoring unit runs against
a disposable `/tmp` copy of a named seed vault; the executor's env sets
`LIT_LIBRARY` to that copy (shadowing the real one), redirects `LITMAN_REGISTRY_DIR`
into the run dir, and runs from a neutral cwd outside the repo. Temp vaults live
under `/tmp` (not `/work`, to avoid quota issues) and are removed after each run.
