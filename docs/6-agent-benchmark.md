# Agent model benchmark

litman's agent layer — the bundled `lit-library` and `lit-reading` skills — is
meant to work with whatever model you point your agent at, not only Anthropic's.
This page reports how well eight models drive it, and how the numbers were
produced.

## Which model to use

- **An [Anthropic](https://www.anthropic.com) subscription is the recommendation.**
  Claude Sonnet 4.6 and Claude Haiku 4.5 both complete 97% of the tasks.
- **Without one, the [DeepSeek-V4](https://www.deepseek.com) family scores highest
  of the rest** — 80% for Flash, 76% for Pro.

## Method

Each model was run as the Claude Code backend and told to operate litman through
the skills, over the suite's **22 auto-scored everyday-workflow tasks** (add,
read, tag, modify, link,
export, taxonomy edits, health checks, ...), 3 rounds each, on the **litman 1.0.0**
codebase ([commit 876d11c](https://github.com/wqx1999/litman/commit/876d11c),
June 2026).

Every task is a **single-turn prompt in a clean context**: a fresh agent gets one
natural-language instruction and must complete it in that one turn, with no prior
conversation and no follow-up.

- **TRR** (task-completion rate) is the fraction of tasks whose resulting vault
  state passed.
- **RA** (routing accuracy) is how often the agent picked the correct skill for a
  request.

## Scores

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

TRR is the mean over the 22 auto-scored tasks across 3 rounds. Network-dependent
and multi-turn scenarios (code cloning, cloud sync, a multi-turn recovery case)
are excluded from this single-turn score.

## Reading the numbers

**A low score does not mean the model cannot operate litman.** It means the model
less often *one-shots* the task from a cold start. With more guidance — a more
detailed request, or a few follow-up turns — a lower-scoring model can still do
the same work. This is a deliberately hard zero-shot floor, not a ceiling.

Whatever the model scores, the data layer validates every write. A wrong command
fails loudly rather than writing bad data into the vault, so a lower-scoring model
needs more turns but never corrupts the library.
