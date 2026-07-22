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
`health` checks), so it needs litman installed in the Python that runs it. A
dedicated conda env (or a `python3 -m venv`) gives both with no pollution of your
base environment.

```bash
git clone https://github.com/wqx1999/litman.git && cd litman

conda create -n litman-bench python=3.12 -y
conda activate litman-bench         # dev-only; a `python3 -m venv` works identically
pip install -e ".[dev]"             # litman + lit CLI + pytest, all inside the env

# fetch the 10 fixture PDFs (not in git; sha256-verified against fixtures.lock)
python tests/bench/fetch_fixtures.py
```

Use a **new** env (`conda create -n litman-bench`), not `base` — that keeps the
install isolated; `conda env remove -n litman-bench` wipes it without a trace.

`fetch_fixtures.py` (no args) skips PDFs already present and matching the lock, so
it is safe to re-run. `--check` verifies cached files without the network. A wrong
URL or a silent bot-block HTML page is caught (non-`%PDF-` body → hard error;
sha256 mismatch → hard error). First-ever pinning used `--write-lock` once on a
machine with browser-grade network access; that lock is committed.

For the **live** benchmark you also need the CLI of whichever agent you are
driving (`--agent`), installed and logged in once — that is what the executor
spawns per card. The deterministic tests and `--dry-run` below do not need any of
them.

## Running

**Deterministic harness tests** (no agent spawned at all):

```bash
pytest tests/bench -q
```

**Live benchmark** — spawns a real agent per card. Two-step is recommended:
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
| `--agent` | `claude` | which agent CLI to drive: `claude`, `cursor` or `agy` |
| `--model` | the agent's own default | executor model tier (passed straight through) |
| `--rounds` | 3 | repeats per card (for mean ± std) |
| `--cards` | all | comma-separated card ids to run a subset |
| `--out` | — | write the full report JSON to this path |
| `--dry-run` | off | exercise the pipeline with a fake executor (no agent spawned) |
| `--base-url` | — | external-model proxy URL (see *External models*); **`--agent claude` only** |
| `--auth-token` | — | token for the external-model proxy (only with `--base-url`) |

## Agents

`--agent` picks the scaffold. A controlled comparison is the same `--model` run
three times, one per agent — which isolates the scaffold, though not perfectly:
the three products ship different system prompts, run their own proxies, and may
differ on thinking mode. The report therefore records the served model string
verbatim so you can judge how comparable two rows really are.

One more disclosed asymmetry: skill *delivery* differs per agent. claude gets the
skills user-level (its config dir); cursor gets them project-level (the run's cwd,
the only place a HOME-redirected cursor discovers them). Both see the identical
two repo-source skills and zero distractors, but a project-level skill may be more
salient to the agent than a user-level one. Same class of limitation as the
thinking-mode difference above: disclosed, not flattened — the claude path is
frozen (a live baseline exists against it) and cannot be moved to match.

The agents do not expose the same things about themselves, and the report says
`None` for anything a given agent cannot report — never a 0:

| | claude | cursor | agy |
|---|---|---|---|
| model default | `claude-sonnet-4-6` | `claude-sonnet-4-6` | `Claude Sonnet 4.6 (Thinking)` |
| execution evidence | event stream | event stream | generated `lit` PATH shim |
| routing (RA) | `Skill` tool | reads `SKILL.md` | **not measurable** |
| token counters | yes | yes (no turn count) | **none** |
| reports served model | yes | yes (display name) | **no — model unverified** |
| isolation | `CLAUDE_CONFIG_DIR` | `HOME` (skills via cwd) | `HOME` |

Model namespaces differ per agent, so `--model` defaults to the chosen agent's own
default rather than a shared constant.

One-time prerequisite: log in each agent you plan to drive, once, on this machine
(run `claude`, `cursor-agent`, `agy` interactively and complete each login). The
harness copies the stored credential into every run's isolated HOME/config and
never performs a login itself. With no stored login, `claude` and `cursor-agent`
runs simply fail authentication (`CURSOR_API_KEY`, if exported, still works for
cursor); `agy` aborts with instructions before the agent is spawned — driving it
logged-out would hang on a browser OAuth prompt.

## SLURM template — apply this by hand

`bench-slurm/slurm-template.sbatch` lives **outside this repo** (it is the live
scoring workspace, and it `cd`s into its own `litman-stable` clone), so this
branch cannot carry the change. Apply it when you point that clone at this branch
— not before, or a freshly copied template will pass `--agent` to a `run_bench.py`
that has never heard of it.

1. Add `AGENT` to the EDIT block:

```bash
# ================= EDIT THESE PER RUN =================
AGENT="claude"          # claude | cursor | agy
MODEL="claude-haiku-4-5-20251001"
ROUNDS=3
CARDS=""
# =====================================================
```

2. Pin the per-agent binaries next to the existing `LITMAN_BENCH_CLAUDE_BIN` line
   (only the one matching `AGENT` has to resolve):

```bash
export LITMAN_BENCH_CURSOR_BIN="$HOME/.local/bin/cursor-agent"   # AGENT=cursor
export LITMAN_BENCH_AGY_BIN="$HOME/.local/bin/agy"               # AGENT=agy
```

3. Pass the agent through at the bottom:

```bash
python tests/bench/run_bench.py --agent "$AGENT" --model "$MODEL" \
    --rounds "$ROUNDS" --run-dir "$RUN_DIR"          # + --cards "$CARDS" when set
```

4. **Delete preflight 2** (the `claude -p "reply with: ok"` block) and its
   `[ -x "$LITMAN_BENCH_CLAUDE_BIN" ]` guard. It hard-codes `claude`, so under
   `AGENT=cursor|agy` it would preflight the wrong binary against the wrong model
   and pass while the real agent is broken. Phase 0 supersedes it strictly: it
   checks the *chosen* agent's binary, headless drive, tool authorization, skill
   source, evidence chain, model pinning and token counters, and aborts non-zero
   before any card. Keep preflight 1 (the `curl` internet check) — Phase 0 assumes
   the node can reach the network.

`bench-slurm/slurm-external-template.sbatch` needs no change: `--base-url` /
`--auth-token` are `--agent claude` only and keep their existing behaviour (Phase 0
runs the probes through the proxy too).

## Phase 0 — instrument qualification

Every live run first qualifies the instrument against the chosen agent and aborts
non-zero if any check fails, before a single card runs. This exists because a
broken instrument does not produce an error — it produces numbers that look
exactly like real ones (an unauthorized tool reads as a 0% execution rate; a
leaked skill copy reads as good routing; an unpinned model reads as whatever the
agent's `auto` picked).

Checks: the binary answers `--version`; a trivial prompt comes back; the agent
really ran `lit --version` and got output; the skill it loaded is this repo's
source (proved with a sentinel planted in the isolated copy); the evidence chain
recorded the call; served model matches requested (skipped for agy, which reports
none — and the skip is written into the report as *unverified*); the token
counters are there (skipped where the agent has none).

The whole sheet lands in `report.json` under `qualification`: it is a deliverable,
not just a gate.

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
proxy (LiteLLM / claude-code-router) and pass its endpoint. This is a `--agent claude`
mode only; `cursor` and `agy` reject `--base-url` rather than silently ignore it
(an un-proxied run reported as a proxied one is a wrong data point, not a warning):

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
| `LITMAN_BENCH_CURSOR_BIN` | pin a specific `cursor-agent` binary (default: `cursor-agent` on PATH) |
| `LITMAN_BENCH_AGY_BIN` | pin a specific `agy` binary (default: `agy` on PATH) |

## Isolation

The benchmark never reads or writes your real vault. Each scoring unit runs against
a disposable `/tmp` copy of a named seed vault; the executor's env sets
`LIT_LIBRARY` to that copy (shadowing the real one), redirects `LITMAN_REGISTRY_DIR`
into the run dir, and runs from a neutral cwd outside the repo. Temp vaults live
under `/tmp` (not `/work`, to avoid quota issues; an agent's cwd on NFS also costs
minutes per spawn) and are removed after each run.

**Tool approval.** To hold the permission variable constant across scaffolds, the
harness passes each agent's own approval-bypass flag (`--permission-mode
bypassPermissions`, `--force`, `--dangerously-skip-permissions`) — the flags are
recorded in the report's `agent_flags` so every run says how it was authorized.
Mixing a full-bypass agent with a narrow-allowlist one would drag the allowlisted
agent's score down on permissions rather than capability, which is a broken
comparison. This applies to **the benchmark harness only**, against a disposable
vault with the real library shadowed. litman itself never uses or suggests
disabling an agent's safety, and nothing under `src/litman/` does.
