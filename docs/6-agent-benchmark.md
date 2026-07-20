# Agent model benchmark

litman's agent layer — the bundled `lit-library` and `lit-reading` skills — is
meant to work with whatever model you point your agent at, and with whatever agent
CLI you drive it from, not only Anthropic's. This page reports how well a range of
models drive it across five agent CLIs, and how the numbers were produced.

## Which model to use

- **The cheapest capable tier already tops the chart — you do not need a frontier
  model.** Cursor's [Composer 2.5](https://cursor.com) and GLM 5.2, and opencode's
  [MiniMax-M3](https://www.minimax.io), all complete 100% of the tasks;
  [DeepSeek-V4](https://www.deepseek.com) (96–97%), Codex GPT-5.4-mini (94%), and
  Gemini 3.1 Pro (97%) are right behind. No frontier model scored higher than the
  best cheap one, and several scored lower.
- **On Claude Code, use Sonnet, not Haiku.** Claude Sonnet 5 completes 96%; Claude
  Haiku 4.5 is inconsistent (76% this run) — it sometimes answers conversationally
  without calling `lit` at all.
- **Frontier models are better interactively than headless.** GPT-5.6 and Sonnet 5
  pause to ask for confirmation before structural changes (adding a taxonomy value,
  creating a project). At the keyboard that is a feature; in an unattended
  single-turn run it scores as an incomplete task.
- **Cursor Pro cannot run frontier models** — its usage cap blocks GPT-5.6 and
  Sonnet 5 outright, so Composer or GLM are the practical picks there anyway.

## Scores

Each row is one **agent CLI running one model** — the combination is what you
install and drive litman with, so that combination is what we measure.

| Agent CLI | Model | Task completion (TRR) | Routing (RA) |
|:---|:---|---:|---:|
| Claude Code | [Claude Sonnet 5](https://www.anthropic.com) | 96% | 93% |
| Claude Code | [Claude Haiku 4.5](https://www.anthropic.com) | 76% \* | 50% |
| Codex | [GPT-5.4-mini](https://openai.com) | 94% | 93% |
| Codex | GPT-5.5 | 88% | 93% |
| Codex | GPT-5.6 (luna) | 83% | 93% |
| Codex | GPT-5.6 (sol) | 82% | 93% |
| Codex | GPT-5.6 (terra) | 77% | 93% |
| Cursor | [Composer 2.5](https://cursor.com) | 100% | 100% |
| Cursor | [GLM 5.2](https://z.ai/model-api) | 100% | 64% |
| Cursor | [Grok 4.5](https://x.ai) | 96% | 100% |
| Antigravity | [Gemini 3.1 Pro (High)](https://deepmind.google/models/gemini/) | 97% | N/M |
| Antigravity | Gemini 3.1 Pro (Low) | 92% | N/M |
| Antigravity | Gemini 3.5 Flash | 88% | N/M |
| opencode | [MiniMax-M3](https://www.minimax.io) | 100% | 100% |
| opencode | [DeepSeek-V4 Pro](https://www.deepseek.com) | 97% | 100% |
| opencode | DeepSeek-V4 Flash | 96% | 100% |
| opencode | [Kimi K2.6](https://www.moonshot.ai) | 94% | 86% |
| opencode | [Qwen3.7-Max](https://chat.qwen.ai) | 89% | 100% |

TRR is the mean over the 22 auto-scored tasks across 3 rounds. Network-dependent
and multi-turn scenarios (code cloning, cloud sync, a multi-turn recovery case)
are excluded from this single-turn score.

- **\* Haiku is high-variance.** Across runs its TRR swings roughly 76–97%
  depending on how many colloquial prompts it answers without invoking `lit`. Treat
  it as unreliable for unattended use, not as a fixed 76%.
- **RA = N/M for Antigravity.** It emits only plain text — no event stream — so
  routing cannot be measured (reported as N/M, never as 0). Its task completion is
  reconstructed from a `lit` shim that records each call.

## Reading the numbers

**A low score does not mean the model cannot operate litman.** Almost every
combination lands high; the ones that do not fail in one of two ways, neither of
which is a failure to understand the skill:

- **Confirmation-halt (caution).** Frontier models — GPT-5.6, Sonnet 5 — stop and
  ask before a structural change rather than doing it unprompted. One more turn
  ("yes, go ahead") and they finish. Interactively you never notice; only a
  single-turn headless run scores it as incomplete.
- **Under-invocation.** Haiku occasionally replies conversationally without running
  `lit`. A more detailed request, or a follow-up turn, gets it back on track.

Whatever the model scores, the data layer validates every write. A wrong command
fails loudly rather than writing bad data into the vault, so a lower-scoring model
needs more turns but never corrupts the library.

## Method

Five agent CLIs — [Claude Code](https://claude.ai/code),
[Codex](https://openai.com/codex), [Cursor](https://cursor.com),
[opencode](https://opencode.ai), and [Antigravity](https://antigravity.google) —
each drove litman through the bundled skills over the suite's **22 auto-scored
everyday-workflow tasks** (add, read, tag, modify, link, export, taxonomy edits,
health checks, ...), 3 rounds each, on **litman 1.2.0** (July 2026).

Every task is a **single-turn prompt in a clean context**: a fresh agent gets one
natural-language instruction and must complete it in that one turn, with no prior
conversation and no follow-up.

- **TRR** (task-completion rate) is the fraction of tasks whose resulting vault
  state passed, scored against the actual end-state on disk.
- **RA** (routing accuracy) is how often the agent picked the correct skill for a
  request.

This is a deliberately hard zero-shot floor, not a ceiling: with a more detailed
request or a few follow-up turns, a lower-scoring combination does the same work.
