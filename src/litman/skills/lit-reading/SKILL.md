---
name: lit-reading
description: "Read side of the litman vault: grounds a discussion of a paper, or its SI, in the user's own library. NOT for adding or changing papers — that's lit-library. Use when the user talks about a paper they are reading, asks about its supplementary information ('Table S3', '补充材料'), looks for related work in the vault, connects a paper to their project, or wants the discussion logged. Triggers: '这篇文章…' / 'this paper…', '继续读' / 'continue reading', '读完了' / '这篇一般' / 'done with this paper', '跟这篇相关的还有哪些' / 'related work', '我在 notes 里写过 X 吗' / 'search my notes', '最近读了啥' / 'what did I read recently', '记下这次讨论' / 'log this discussion', '误删了那篇' / 'restore a paper I deleted', '清一下 inbox' / 'triage my inbox', '我的库还干净吗' / 'is my library clean'. Reads the vault and the PDF; writes notes.md and discussion.md only when asked."
---

# lit-reading — vault-aware reading companion

Grounds a reading discussion in the user's own litman vault, not in general knowledge. Changing the library is **lit-library**.

## Rules — they hold for every workflow below

1. **Read with `--format json`, one `lit` command per shell call.** Every read of the vault (`lit list`, `lit show`, `lit project list`, `lit taxonomy list`, …) takes `--format json` (`lit search` and `lit related` print JSON already): the table form is cut to the terminal's width. The command rule litman installs for your agent allows a command that *is* `lit …` and nothing else, so `which lit && lit …`, `cd <dir> && lit …`, `lit … ; echo $?`, `lit … | head` and redirections (`2>&1`, `> file`) are refused. Read the exit code and the output from the tool result, and filter the output yourself.
2. **Not sure what the user means, or which command or flag does it → `lit help` / `lit <cmd> --help` first**; if it is still unclear, ask the user. Never guess a command or a flag from memory.
3. **A request no workflow here covers → look it up in `lit help`.** If a command does it, run it — Rules 4 and 11 still apply. Don't answer "I can't" before you have looked.
4. **Sensitive operations come in two levels.**
   - **Never run these, even when asked — show the exact command and let the user type it:** `lit trash empty` and `lit rm --purge`. Nothing brings their files back. You may run `lit trash empty --dry-run` or `lit rm <id> --dry-run` first to show what would go; `trash empty` also deletes the folders `lit health-check --fix` moved to `.trash/replaced-folders/`, so say that too.
   - **Run these only after the user has seen the consequences and said go:** `lit rm <id>` (moves the paper to the trash), `lit rm <id> --si si-NN` (deletes an SI file for good — there is no trash for SI), `lit code rm`, `lit taxonomy merge`, `lit taxonomy rm`, `lit project rm`, `lit export --force` (overwrites a hand-edited .bib), and rewriting a `notes.md` that holds more than the `lit add` scaffold. Never start one yourself — relaying a `lit` hint that names one is fine, and lit-reading B10 may offer a `notes.md` rewrite in its closing proposal. When the user asks for one, show the command and what it will do — run its `--dry-run` where it has one and relay the lines verbatim, never summarising a link count away — and wait. Their request and their go after seeing the consequences are the two confirmations: then run it (with `--yes`: the CLI's own `y/N` prompt never reaches you, and a non-interactive run without `--yes` aborts) and relay the result. Don't ask a third time.
   - **Renames lose nothing** (`lit rename`, `lit taxonomy rename`, `lit project rename`): say what will change, then run them, with no separate question.
5. **Write only in the active vault.** Reading another registered vault is fine. To write into another one, the user switches first (`lit vault use <name>`).
6. **Windows PowerShell 5.1** shows whatever `lit` writes to stderr, warnings included, as a red error and may report exit code 1. Judge by what the command printed, not by the colour.
7. **What you write yourself.** **Never** write `metadata.yaml`, `TAXONOMY.md`, `INDEX.json`, `lit-config.yaml` or `codes/<name>/repo-meta.yaml`, and never write, rename, copy into or delete anything in `papers/<id>/supplementary/` — every such change goes through `lit` (metadata as JSON through `lit add --from-llm-json` or `lit modify`, vocabulary through `lit taxonomy` / `lit project`, SI through `lit add --si` / `lit rm <id> --si`). The only files you write are `notes.md` (overwrite) and `discussion.md` (append); `lit add` scaffolds both with an HTML-comment line stating the format — read it before you write and never strip it. The one edit inside existing `discussion.md` text is an SI citation `lit health-check` flagged, fixed on the user's go: that citation only (B12). When `lit` stops with a message that leaves the act to the user — `Cannot use <file>: '<field>' is not a list`, `… by hand`, `Re-save it as UTF-8`, `Rename the folder, then retry`, `Fix that file, then retry`, `move it out of supplementary/`, `could not read notes file … — skipped` — relay it verbatim and stop: no metadata edit, no folder move or rename, no re-encoding, no rewriting that notes.md. If the user asks you to edit one of these files by hand, say why not and offer the `lit` command that does it.
8. **Don't install, upgrade or uninstall litman.** If `lit` is missing, give the user the install command — `curl -LsSf https://get.litman.dev/install.sh | sh` (or `uv tool install litman` / `pipx install litman`) — and stop. If `lit hello` says a newer litman exists, pass that on and carry on with the task.
9. **Cite in-vault papers as `[[paper-id]]`**, never in backticks or plain text — those escape `lit rm`'s `(deleted)` tagging and `lit health-check`'s dangling-link check. Check that each `[[X]]` you write or keep exists (`lit show X --format json`); a missing one becomes `[[X]] (deleted)`. Cite this paper's own SI file as `si-01.pdf（<original name>）`, never a bare number; another paper's SI as `[[<other-id>]]` plus the file's original name, without its `si-NN`.
10. **Don't organise on your own.** Never tag, add an edge, link a paper to a project or bind code on your own initiative. Saying what you notice ("this touches tokenization") is fine — noticing is not tagging. Propose; the user decides; then you run the command.
11. **Write only on an explicit instruction.** Reads need no permission. A write — a status or date stamp, a tag, a grade, `type`, an edge, a project link, rewriting `notes.md`, appending to `discussion.md` — runs only when (a) the user's message asks for that write ("标成已读", "记下来", "打上 X", "加到文献库"), or (b) the user said yes to a proposal that listed it. A remark is not an instruction: "这篇差不多了", "感觉没什么要读的了", "读完了", "looks done" get one proposal that lists every write you intend, and then you wait. A yes covers the listed items only — nothing extra on the side. One exception: `lit add --si <id>` with no files — say so in one line, then run it; it only opens a window, and nothing is written until the user adds files and presses Done.
12. **Speak only from the vault or the user.** Every claim about a paper comes from something in the vault — `metadata.yaml`, `notes.md`, `discussion.md`, `paper.pdf`, the SI files in `supplementary/` — or from the user. A paper not in the vault: say so; never summarise it from its title or from memory. Keep what the notes and discussion record (the user's endorsed view) apart from your own fresh synthesis, and mark the latter as yours.
13. **There is no "current paper" on record.** The user names it, or you infer it from their words. `lit open` / `lit show` with no id fall back to the most recently touched paper — a keystroke saver for a human at a terminal, not a way for you to find out which paper is meant. Never run them bare: ask, or use the resume branch (Phase 1). Always pass the id.
14. **The vault is discovered, never hard-coded:** `$LIT_LIBRARY`, the active vault (`lit vault list --format json`), or `lit-config.yaml` walking up from the cwd — never a path pasted from an earlier session. Several vaults may be registered; a `[[<vault>:<id>]]` link points into another one, which you may read (Rule 5). No vault found → say so and stop; never make up paper content.

## What lit-reading runs itself, and what goes to lit-library

On the user's instruction (Rule 11) lit-reading runs the evaluation stamps (`lit read` / `promote` / `skim` / `drop` / `revisit`, `lit modify --set priority-<P>=` / `--set type=`), `lit add --si` for the paper being read (B15) and the writes to `notes.md` / `discussion.md`; reads, freely. **Every other write goes to lit-library**, named explicitly, with the paper id, the PDF path, the exact intent ("add `extends=<other-id>`"), what the user said and where to resume here — tags and edges [E], project links and `relevance-<P>` [H] (a grade the user gave rides along as `--priority`), new papers and code [A] / [B] / [C], SI deletion [L], restores [I], governance [J] / [H]. Then carry on here.

## Workflows, most used first

**B10** closing out · **Phase 5** logging a discussion · **Phase 2** loading a paper · **B15** SI · **B9** a dropped PDF · **Phase 1** locating, resuming · **Phases 3–4** comparing, connecting to a project · **B12** health-check · **B11** inbox · **B13** mis-deletion · **B14** `lit open`.

---

## B10 — Closing out a paper

When the reading reaches a natural close — "读完了", "这篇一般", "没价值", "这篇差不多了", "done with this paper" — **send one proposal and wait** (Rule 11). It lists every write you intend:

1. **Status and read-date.** The depth is the user's judgment, never yours. Pre-fill what they said — "读得挺细的" / "精读了" / "read it closely" → `deep-read` (`lit promote`); "扫了一下" / "skimmed" → `skim` (`lit skim`); "一般" / "没价值" → `dropped` (`lit drop`) — else ask which, with your lean. Add `lit read` either way: it stamps the read-date, which marks the paper finished for resume and `--unread` (`lit promote` does not). A paper dropped by its title at inbox triage, never read, gets no `lit read`.
2. **notes.md.** Only the `lit add` scaffold and the user asked for notes → you may write it; it has content → ask in the proposal whether to rewrite it (Rule 4). Draw only on what is in context — this session's discussion, the abstract, the existing note; nothing new discussed → propose keeping it or adding one line. Even a drop gets a one-line "why dropped". Check every `[[X]]` you keep (Rule 9), and every SI citation against `lit show <id>`'s `SI:` lines — a number can come to name another file.
3. **Empty curation fields.** Scan the metadata Phase 2 already loaded (no new CLI call) and add each empty field with 2–5 candidates, a one-line why for each, and your lean:

   | Empty field | What to offer | Runs |
   |---|---|---|
   | `projects` | 1–2 registered projects | lit-library [H] |
   | `topics` / `methods` / `data` | the top 3–5 **registered** values ("`amp` — the focus throughout §2"); never an unregistered one | lit-library [E] Flow A |
   | `related` / `extends` / `contradicts` | vault paper ids ("extends `[[2022_X_AMPGen]]` — same CFB + AMP combo") | lit-library [E], forward only |
   | `relevance-<project>` (linked, blank) | a one-sentence draft | lit-library [H] |
   | `priority-<project>` (linked, ungraded) | A / B / C, **once per linked project**; none when the paper is in no project | here |
   | `type` | 2–3 enum values ("`review` — surveys 80+ models") | here |

   An empty field is a legitimate "not applicable" — never press for it. A grade or a tag the user mentioned while reading ("这篇对 pepforge 是 C") goes in pre-filled; don't write it on the spot. Rationale only from the discussion, the abstract and the notes in context, never from memory of the paper. Never a blank question ("What's the priority for pepforge?"), never a yes/no on a single guess ("Is this a review?").

Put it all in **one** message, grouped ("收尾前：状态？type？对 pepforge 有多重要？可以一起回"), so the user answers once. Then run exactly what they approved — the stamps, grade and `type` here, the rest handed to lit-library — and nothing else.

- **An explicit instruction is not a remark.** "标成已读，精读" / "mark it read, deep-read" → run `lit read` and `lit promote` at once, without asking again; offer the notes and the empty fields afterwards, in one message.
- **A drop is short**: a one-line drop reason in `notes.md`, `lit drop` and `lit read` — no field offers (a grade the user volunteered still goes in).
- **No memory of declined fields.** A later close may offer them again; don't invent a "dismissed" marker.
- **Re-opening a finished paper** (it has a `read-date`, or is `deep-read` / `skim`): "又把 X 翻出来想了想", "重新看了下 X", "looking at X again" → propose `lit revisit <id>` (stamps `last-revisited`, not `read-date`), carry on, and run it on their yes. Never infer a revisit they did not state.

## Phase 5 — Write down a discussion (only when asked)

When the conversation produces a conclusion, an idea or a follow-up worth keeping, offer: "Should I append this to `papers/<id>/discussion.md`?" When the user asks ("记下这次讨论"), just do it. Read the top of `discussion.md` first — its HTML-comment line is the format contract — then append, never overwrite:

```markdown
## YYYY-MM-DD HH:MM

**Question:** <one-line restatement of what the user asked>

<3–10 lines of the discussion's distilled conclusion + the user's questions — user's perspective first, not the full back-and-forth>

[optional] **Follow-up:** <action item / open question>
```

- The distilled conclusion and the user's questions — their perspective first, not the whole back-and-forth.
- Mark **your own inference**, or a number or section you did not check against the PDF, with `⚠` or "not verified", so it is not later taken for the user's settled view. One marker — no tag system.
- Cite an SI file with its original name and the place — `si-01.pdf（ci9b00237_si_001.pdf）, p. 12, Table S3`, never a bare `si-01`: numbers are given again after a file is deleted, and `lit health-check` checks each citation against the name in brackets. Another paper's SI is `[[<other-id>]]` plus the file's original name, without its `si-NN`.
- Append only; the one exception is a flagged SI citation the user asked you to fix (Rule 7). Keep the `# Discussion log for <id>` heading and the format comment under it. A pre-scaffold paper with no file: create it with that heading, add your section, and tell the user `lit health-check --fix` backfills the rest of the library. Afterwards, say which file you appended to.

## Phase 2 — Load the paper (lazily)

At the start of a reading session load the vocabulary once: `lit taxonomy list --format json` (never `cat TAXONOMY.md`); re-run it after a `lit taxonomy` or `lit project add` change. Then read only what you need, in this order: `lit show <id> --format json` (always: the full metadata) → `papers/<id>/notes.md` (usually: the current-understanding snapshot) → `discussion.md` (on drill-down: the dated log, only the relevant section) → `paper.pdf` (sections, figures, numbers: only the pages you need) → `supplementary/` (B15).

**PDF reading ladder — keep going until one rung works; never stop at the first failure:** your agent's file-read tool (in Claude Code: `Read(pdf_path, pages="1-10")`) → a PDF / document MCP tool, if you have one → `lit pdf-text <pdf> --pages 1-10`, which needs nothing beyond `lit` (exit 3 = no text layer, a scan: back up to a multimodal reader or OCR). All three failed → say which rung failed and why, and give the user poppler's install command for their system, or suggest a vision-capable model. **Never run an install yourself.** Never summarise from the title or from memory instead of reading; never read all 40 pages when the user asked about §3.

**Text-only backend asked about a figure / table image ("what does Fig./Table N show?").** When the active model has no native vision (rung 1 unavailable) AND no vision-capable PDF MCP (rung 2), `lit pdf-text` is the only reader and it returns the *text layer only* — it cannot see images. Do this, in order: (1) pull the figure/table **caption** and the **body sentences that reference it** via `lit pdf-text`, and answer strictly from that text; (2) state plainly that you **cannot see the figure/table image itself**; (3) tell the user that reading the actual visual (curve shapes, bar heights, numbers inside an image-table) needs a multimodal model or an explicitly attached vision MCP. **Never infer or fabricate the visual content from the caption or surrounding prose** — caption text is not the figure. This is the highest-risk hallucination point in the whole skill.

`notes.md` is the STATE (one snapshot, rewritten at B10); `discussion.md` is the LOG (appended at Phase 5). A `⚠` / "not verified" item there is a past inference nobody confirmed — ask about it in passing. An empty or missing `notes.md` (an `inbox` paper): say so and offer to read the PDF; never pretend the user has notes they don't.

## B15 — SI on demand (supplementary / supporting information)

**When.** The user names the SI or an S-numbered item ("Table S3", "Figure S2", "SI 里…", "补充材料"), or asks for a detail the main text leaves to the SI ("see Table S3"). Not for the gist, a skim, or anything the main text answers.

**Red lines.** Never download SI yourself — not from the publisher, PMC, Figshare or any copy. Never write, rename, copy into or delete anything in `supplementary/`. A refused file stays refused. Deleting an SI file belongs to lit-library [L].

1. **Already in the library?** `lit show <id>` prints one `SI:` line per file: its absolute path, original name and size (`--format json` has no paths). Never probe with `lit add --si <id>`: on a paper with no SI it opens a window on the user's screen.
2. **To run `lit add --si`, or to find the answer inside an SI file → read `si.md` (next to this SKILL.md) first**: the exit codes and what to do on each, the per-file checks, how to find a page in a long SI PDF.

**Answer = the file + where the user can jump to**: `si-01.pdf (ci9b00237_si_001.pdf), p. 12, Table S3`, or an xlsx sheet + rows. `si-NN` is litman's numbering, not the publisher's: never infer "Supplementary Data 2" from `si-02`. A file you couldn't search → say which and where you stopped. Never guess, and never pass off the main text or your memory of the paper as SI content.

---

## B9 — A PDF dropped into the chat

1. **In the vault?** `lit list --title <a distinctive word or two> --format json` — let the CLI filter, don't pull the whole list and search it yourself; a DOI on the PDF → `lit show --paper-doi <doi> --format json`, the exact check.
2. **Not in the vault → propose** "this isn't in your vault — want me to add it?". On yes, hand off to lit-library [A] / [B] with the PDF path and any DOI; resume at Phase 2 with the new id. A bare DOI or URL with no file is lit-library [B]'s.
3. **An SI file is not a new paper** (`*_si_001.pdf`, `*_MOESM1_ESM.pdf`, "Supporting Information" on page 1): its paper in the vault → offer `lit add --si <id> <file>` (B15) instead of `lit add`.

## Phase 1 — Locate the paper

**Resume — "继续读" / "上次读到哪了" / "continue reading", no paper named.** `lit list --unread --sort recent --format json` (active vault only). Keep the order returned — **don't re-sort or re-filter**, except drop `status: dropped` rows (`--unread` keeps a paper dropped by title; keep `skim`). Offer the top 3–5 (id, title, `status`) as "your most-recently-touched unfinished papers, probably where you left off" — **never** "you were reading X". On a pick, suggest `lit open <id>` (B14) and go to Phase 2. `[]` → say whether nothing is unfinished or there are no papers yet (a plain `lit list --format json` tells). A `deep-read` row with no `read-date` → offer the B10 close.

**The user names a paper.** Every cue has a CLI filter — never `grep` or `cat` the vault: an id → `lit show <id> --format json` (missing → ask; never substitute another paper); an author ("Pandi 那篇") → `lit list --author <cue> --format json`; a title word → `--title`; a topic or method ("the GAT one") → `--topic` / `--method`; something they wrote ("我在 notes 里写过 X 吗") → `lit search <query>`, which searches notes.md + discussion.md only, never the PDF or the SI. One match → use it and say which ("I'm reading `2023_Pandi_Cell-free`, Pandi et al. 2023") so they can correct you; 2–5 → list them and ask; more than 5 → ask them to narrow the cue. They went back to a paper they had finished → B10, "Re-opening".

**Related and retrospective questions — bounded retrieval.** **Never `cat INDEX.json`**, and never pull every row to compare topics by hand. A focus paper ("跟这篇相关的还有哪些", "what builds on this") → `lit related <id>`: edges first, then shared topics / methods, each row's reason in `via`. "What's in my library about X" / "汇总我库里关于 X 的" → `lit list --topic X --format json` (or `--method`, `--data`, `--project`). "最近读了啥" → `lit list --sort recent --format json`, as returned: "your most-recently-touched papers, probably what you read lately", never "you read these this week". Load a paper's own files only when the user drills into one.

**The index looks stale** — an id missing from `lit list` though its folder exists, or `lit show <id> --format json` (it reads `metadata.yaml`) disagreeing with the list → say so and propose `lit refresh-views`; run it on the user's yes. Don't route around it silently.

## Phase 3 — Cross-paper (on demand)

Only when the user asks for a comparison or related work: `lit related <id>`, then **one** signpost line — **at most 3** papers in the order returned, each with the reason its row gives (the `edge` field or the `shared` values, never an invented one), then "N more related". Load a candidate's notes only after the user picks it. A `[[<vault>:<id>]]` link → resolve it with `lit vault list --format json` and name the source vault.

## Phase 4 — Connect to the user's project

`lit project list --format json` gives the registered projects and their paths — not `lit-config.yaml`, not `lit config show`. Match the user's project name by case, whitespace and separators only; anything looser → show the registered set and let them pick. Never guess; the cwd is at most a hint. Read, stopping when you have enough, `<project>/dev_docs/identity.md`, `<project>/dev_docs/todo/active/*.md`, `<project>/dev_docs/proposals/*.md`, and tie the paper's claim / method / data to the project's current question concretely ("paper §3 shows X; the active todo <name> needs Y"). You may say "this looks related to your `<project>`"; a link only when the user asks → lit-library [H].

## Edges — propose only

When the discussion implies one ("this is Pandi's follow-up" → `extends`; "its conclusion contradicts X" → `contradicts`; "these two read well together" → `related`), **propose** it; once the user confirms, hand it to lit-library [E]. Name the **forward** direction only; the CLI writes the reverse field itself.

## B12 — Vault health-check

At a natural moment, or when the user asks ("我的库还干净吗"), **run `lit health-check`** — a read: just do it — translate the report's categories and propose a remedy per finding. A bare `lit health-check` never writes, and no fix runs without the user's go. **Before you propose `lit health-check --fix`, or act on any finding, read `health-check.md` (next to this SKILL.md).** `--fix` loses three things; name them before proposing it: the grade of a paper linked to no project (`retired_priority`), an edge to a paper in the trash (`dangling_refs`), and project folder copies that don't match the vault (moved to `.trash/replaced-folders/`). The `env` class (`links_unsupported`) has no remedy: relay the CLI's hint, and never suggest Developer Mode, an administrator shell, WSL or any Windows setting.

## B11 — Inbox triage

`inbox` is the default `status` — "not judged yet", not a folder. At a natural moment offer "you have N papers in inbox, sitting X days" and go one paper at a time, from its row in `lit list --status inbox --format json`: the **user** decides skim / deep-read / drop, then you run that stamp. B10's field offers ride along, limited to what the row shows — no extra CLI call. Each paper judged by the user — not batch ingest.

## B13 — Mis-deletion recovery: find → confirm → restore

"误删了那篇关于…的文献" / "把删掉的 X 找回来" / "I deleted that paper by mistake":

1. **Find.** `lit trash list --format json` (newest first). A trashed folder is readable at `<vault>/.trash/<entry-name>/`: open its files when the title alone is ambiguous.
2. **Confirm — never restore on a guess.** Show the likeliest candidate (id, title, deleted_at, a content cue) and ask "is this the one?"; several plausible → at most 3, newest first; the user picks.
3. **Restore** → lit-library [I] with the id (or the entry name `<id>-<UTC-timestamp>` when the id is in the trash twice) and what they said about re-cloning code. There is **no** top-level `lit restore`.

**A deleted SI file is not in the trash** (`lit rm <id> --si` deletes for good): ask the user for the file and add it back (B15). A trashed *paper* keeps its `supplementary/`.

## B14 — `lit open`

Suggest `lit open <id>` by default; run it when the user asks ("打开这篇" / "open it for me"). It changes no state. `lit open <id> --si si-NN` opens one SI file the same way, always with the id (Rule 13); exit 2 with an `SI path:` line (no desktop here, or no app for the type) → relay the path, or point to the file's chip in the litman page's Supplementary row.

## `tip:` lines

A dim `tip:` line after a command (`no lit health-check in 14+ days…`, `no lit sync push in 7+ days…`) → relay it and offer to run the command it names; never run it unasked. A first `lit sync push` prints a size preview and stops for `--yes`: relay the preview, re-run with `--yes` on the user's go.
