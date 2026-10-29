---
name: lit-library
description: "Write side of the litman vault: drives the `lit` CLI to change the library. NOT for discussing or reading a paper — that's lit-reading. Use to add a paper (PDF or DOI) or its SI files, tag it, link it to a project, bind a code repo, export a .bib (or RIS for EndNote), rename, delete or restore a paper, or tidy the topic / method / data vocabulary. Triggers: '加到文献库' / 'add this paper', '把这些 SI 加到这篇' / 'add these SI files', '导出 bib' / 'export refs', '导出到 EndNote', '给这篇打标签' / 'tag this paper', '新建项目' / 'link it to my project', '把代码 clone 下来' / 'clone this repo', '把这篇删了' / 'delete this paper', '恢复删掉的论文' / 'restore from trash', '合并这两个 topic' / 'merge these tags', '把这篇的 id 改成 X' / 'rename this paper', '库里好多 Unknown' / 'fix these bad ids'. NOT for a project's own References/ folder — that's ref-manager."
---

# lit-library — the write side of the litman vault

Changes the library through the `lit` CLI. Reading, discussing and closing out a paper is **lit-reading**.

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
7. **What you write yourself.** **Never** write `metadata.yaml`, `TAXONOMY.md`, `INDEX.json`, `lit-config.yaml` or `codes/<name>/repo-meta.yaml`, and never write, rename, copy into or delete anything in `papers/<id>/supplementary/` — every such change goes through `lit` (metadata as JSON through `lit add --from-llm-json` or `lit modify`, vocabulary through `lit taxonomy` / `lit project`, SI through `lit add --si` / `lit rm <id> --si`). The only files you write are `notes.md` (overwrite) and `discussion.md` (append); `lit add` scaffolds both with an HTML-comment line stating the format — read it before you write and never strip it. The one edit inside existing `discussion.md` text is an SI citation `lit health-check` flagged, fixed on the user's go: that citation only (lit-reading B12). When `lit` stops with a message that leaves the act to the user — `Cannot use <file>: '<field>' is not a list`, `… by hand`, `Re-save it as UTF-8`, `Rename the folder, then retry`, `Fix that file, then retry`, `move it out of supplementary/`, `could not read notes file … — skipped` — relay it verbatim and stop: no metadata edit, no folder move or rename, no re-encoding, no rewriting that notes.md. If the user asks you to edit one of these files by hand, say why not and offer the `lit` command that does it.
8. **Don't install, upgrade or uninstall litman.** If `lit` is missing, give the user the install command — `curl -LsSf https://get.litman.dev/install.sh | sh` (or `uv tool install litman` / `pipx install litman`) — and stop. If `lit hello` says a newer litman exists, pass that on and carry on with the task.
9. **Cite in-vault papers as `[[paper-id]]`**, never in backticks or plain text — those escape `lit rm`'s `(deleted)` tagging and `lit health-check`'s dangling-link check. Check that each `[[X]]` you write or keep exists (`lit show X --format json`); a missing one becomes `[[X]] (deleted)`. Cite this paper's own SI file as `si-01.pdf（<original name>）`, never a bare number; another paper's SI as `[[<other-id>]]` plus the file's original name, without its `si-NN`.
10. **Don't organise on your own.** Never tag, add an edge, link a paper to a project or bind code on your own initiative. Saying what you notice ("this touches tokenization") is fine — noticing is not tagging. Propose; the user decides; then you run the command.
11. **Write only on an explicit instruction.** Reads need no permission. A write — a status or date stamp, a tag, a grade, `type`, an edge, a project link, rewriting `notes.md`, appending to `discussion.md` — runs only when (a) the user's message asks for that write ("标成已读", "记下来", "打上 X", "加到文献库"), or (b) the user said yes to a proposal that listed it. A remark is not an instruction: "这篇差不多了", "感觉没什么要读的了", "读完了", "looks done" get one proposal that lists every write you intend, and then you wait. A yes covers the listed items only — nothing extra on the side. One exception: `lit add --si <id>` with no files — say so in one line, then run it; it only opens a window, and nothing is written until the user adds files and presses Done.

## Hand-offs with lit-reading

- **Inbound.** lit-reading hands you one write mid-discussion, with the paper id, the exact intent ("add `extends=<other-id>`") and what the user already said: do it, report, hand control back. Don't start a fresh ingest session or take over the conversation.
- **Outbound.** When the talk turns to reading — a read-finished remark ("读完了" / "这篇一般" → its B10), a question about the content or the SI ("Table S3 里…" → its B15), resume, inbox, library health — switch to lit-reading Phase 2 with the paper id, the PDF path and what the user said; most often right after a `lit add`. Its B10 owns the closing proposal; don't run it here.

## Finding the vault

`lit vault list --format json` lists the registered vaults and the active one (`$LIT_LIBRARY`, if set, overrides it); `lit hello` confirms `lit` is installed (Rule 8 if not). Never assume a relative path. A project `References/` folder and a global vault both present → ask which one the user means.

## Workflows, most used first

**[A]** add a paper from a PDF · **[B]** from a DOI (preferred whenever one exists) · **[E]** metadata, tags, edges · **[G]** export a .bib or RIS · **[D]** browse · **[L]** a paper's SI files · **[C]** code repositories · **[H]** projects · **[I]** restore a trashed paper · **[J]** vocabulary governance · **[K]** repair a legacy library.

---

## [A] Add a paper from a PDF

A local PDF with no DOI anywhere (none from the user, none printed in it), or a failed `--doi` fetch; otherwise [B]. Only a DOI or a URL → ask for the file.

1. **Read the PDF, down this ladder until a reader works:** your agent's file-read tool (in Claude Code: `Read(pdf_path, pages="1-3")`) → a PDF / document MCP tool, if you have one → `lit pdf-text <pdf> --pages 1-3`, which needs nothing beyond `lit` (exit 3 = no text layer, a scan: back to a multimodal reader or OCR). All three failed → say which rung failed and why, and give the user poppler's install command for their system, or suggest a vision-capable model. **Never run an install yourself.**

   Extract the title (page 1), the authors ("Family, Given", in order), year, DOI (first 1–2 pages), journal and abstract. **A DOI on the PDF → switch to [B] at once**: CrossRef's metadata is authoritative and carries volume / issue / pages. Note any code-repo URLs on the pages you opened (Code / Data Availability, footnotes), each with a one-line cue, for [C.1]; don't read the whole PDF for them — `lit add` scans the full text itself.
2. **Leave a field `null` rather than guess it.**
3. **Write the JSON to a fresh temp file** (e.g. `lit-llm-<random>.json` in the system temp dir). Unknown keys are rejected; tags are not part of it:
   ```json
   {
     "title": "<exact title from page 1>",
     "authors": ["Family1, Given1", "Family2, Given2"],
     "year": <int or null>,
     "doi": "<10.xxx/yyy or null>",
     "journal": "<venue or null>",
     "arxiv-id": "<2401.12345 or null>",
     "abstract": "<full abstract text or null>"
   }
   ```
4. **Run `lit add <pdf-path> --from-llm-json <json-path>`, then delete that JSON file** — never leave it in the temp dir.
5. **Confirm in two separate messages — never bundle them:**
   - **5a** — only the derived `id` and the `title` (don't judge the title yourself); then **stop and wait** for the user to confirm the source is right. No code table, no status offer.
   - **5b** — after their yes, in a new message: the [C.1] table if the CLI's `[code_candidates]` block or your URL notes found anything (a single CLI-only candidate still goes through it); both empty → stop. Don't offer tags, projects, status or grades (Rule 10). If the user then starts reading or judging the paper, hand off to lit-reading.

**`lit add --from-llm-json` stops with an error or prints a warning → read `add-errors.md` (next to this SKILL.md) before anything else.** Its two worst traps: 🔴 never `--auto-suffix` (it files another copy under an id nobody chose); 🔴 never take the year from the download, the file's date or today (a wrong year is only ever found by hand).

**Duplicate.** `lit add` refuses a DOI already in the vault, naming the id: don't retry or force a second copy — say "already in your vault as `<id>`" and hand off to lit-reading Phase 2 with that id.

**An SI file is not a paper** (`*_si_001.pdf`, `*_MOESM1_ESM.pdf`, "Supporting Information" on page 1). Say so before running anything: its paper in the vault → offer `lit add --si <id> <file>` ([L]); otherwise ask for the article's own PDF. Never switch commands silently.

**Wrong title or id after the add.** `lit modify <id> --set title=<correct>` fixes the title (notes.md's `# <title>` line follows), `lit rename <old> <new>` the id — each leaves the other alone; `lit rm` plays no part. "把这篇的 id 改成 X" / "rename this paper to X" is `lit rename`: don't ask whether they meant the title. It rewrites every in-vault reference and loses nothing — say what it will change, run it (Rule 4), and relay each `plain text <file>:<lines> still says <old>` line for the user to decide. Suggest an id as `<year>_<Family>_<Keyword>`, marked "computed — confirm or change".

## [B] Add a paper from a DOI

`lit add <pdf-path> --doi <doi>` — the user gave a DOI, or [A] found one; a local PDF is still required. There is no `--arxiv`: given only an arXiv id, read the DOI off the PDF; none printed → stay on [A] with the id in `arxiv-id`. 🔴 Never make up a DOI from an arXiv id or look one up unprompted. The id keyword is still derived locally: **report the id and stop**; if the user dislikes it, `lit rename` ([A]).

---

## [E] Metadata, tags, edges

**Every tag comes through Flow A or Flow B** (Rule 10).

- **Flow A — "tag this / classify it", no value named.** Propose **only registered values** (`lit taxonomy list <dict> --format json`, `used_by` = papers per value; never parse `TAXONOMY.md`); the user picks; you run `lit modify <id> --add-tag topics=<value>` (or `methods=` / `data=`). A near-duplicate you spot on the way → propose a merge ([J]), nothing more.
- **Flow B — the user names a value.** Registered → apply it. **Not registered → `lit modify` refuses it**: register it first (`lit taxonomy add <dict> <value>`; a project through [H]), then apply. **Before registering, look for a near-duplicate** — up to case, `-` / `_` / spaces or a plural `s`, a prefix, a near spelling, the same meaning — and report it with its `used_by` ("`peptide` is already there, on 6 papers — use it?"). `lit taxonomy add` refuses a look-alike itself: relay it, offer the existing value, `--force` only if the user, having seen the match, still wants the new one. Never hand-edit `lit-config.yaml`'s `projects:` map.
- **Stamps and fields.** Prefer the stamps to `lit modify --set`: `lit read` (read-date; `--date` backdates), `lit revisit`, `lit promote` (deep-read; it does not stamp read-date), `lit skim`, `lit drop`. A grade is `lit modify <id> --set priority-<project>=A|B|C` on a paper linked to that project; it can be changed, not cleared (`lit unlink` drops it). Authors: `lit modify <id> --set-author "Family, Given"`, one flag per author, in order — the only way to fix or reorder a name in place.
- **Edges.** Only one the user confirmed, usually handed over by lit-reading. Run the **forward** field only — `lit modify <id> --add-tag extends=<other-id>` (or `related=` / `contradicts=`); the CLI writes the reverse field (`extended-by` / `contradicted-by`) on the other paper: **never set a reverse field, never run a second command for it.**

## [G] Export a .bib or RIS

| The user says | Command |
|---|---|
| "导出 bib" / names no project | `lit export --all -o refs.bib` — just run it |
| "导出和 pepforge 有关的文献" | `lit export --project pepforge` (writes `./refs.bib`) |
| "写 thesis，把 priority A 的都导出来" | `lit export --all --priority A -o thesis.bib` (A for any project; `--project <P>` for one) |
| "导出到 EndNote" / "给 Zotero 用" | the same command plus `--format ris` (writes `./refs.ris`); EndNote imports it with the Reference Manager (RIS) option. A .bib does not import into EndNote reliably — don't offer it there |

Cite keys are paper ids (`\cite{<paper-id>}`); re-running on the same file updates it. Other filters: `lit export --help`.

1. **The CLI refuses to overwrite a file without the litman sentinel (a hand-edited bib) → never add `--force` yourself.** Relay the refusal: `--force` discards the user's edits, so it is Rule 4.
2. "here" / "current dir" → the default `./refs.bib`; a *named* directory ("thesis 目录") → ask for the path, don't guess.
3. An unregistered `--project` token → canonicalise case and whitespace only; else show the registered set.
4. No project named → infer it from the context if you can, else `--all`, without asking.

## [D] Browse and inspect

`lit list --format json` with filters (`lit list --help`) and `lit show <id> --format json`. A paper id can be the full id, a unique case-insensitive substring, or `--paper-doi <DOI>` (not for `lit rename` or `lit add --si`). An ambiguous substring lists the candidates and exits non-zero — ask which one.

## [L] A paper's SI files

SI files sit in `papers/<id>/supplementary/` as `si-01.pdf`, `si-02.xlsx`, …; `si-NN` is litman's numbering (the lowest free number), not the publisher's.

- **Add the files the user hands you** → `lit add --si <id> FILE...`. They are **moved** into the vault — the originals leave Downloads: say so before you run it. Each file is checked against the paper (stderr: `✓` / `?` / `✕` and the reason), and all are added or none. A `✕` cannot be added, even with `--force`: relay it. A `?` or a file over 100 MiB stops the run: ask the user; `--force` only on their yes.
- **No files yet** → lit-reading B15: `lit add --si <id>` opens the SI window on the user's litman page.
- **Delete one** → `lit rm <id> --si si-NN`, Rule 4. Run `--dry-run`, relay its lines verbatim (original names, any `cited 2× in discussion.md`), and say the file goes for good — no trash; the only way back is adding it again (the CLI's own warning never reaches the user here). A record whose file is already gone still reads `Would delete`: only the record goes. On the user's go, `--yes`; relay the result, a `still cited …` line included (lit-reading B12). `--si` does not combine with `--purge`.
- **Add one back** → ask the user for the file, then `lit add --si <id> FILE`. **Open one** → `lit open <id> --si si-NN`.
- 🔴 **Never download SI** — not from the publisher, PMC, Figshare or any copy; the user downloads it.

---

## [C] Code repositories

A repo under `<vault>/codes/<name>/` can serve several papers; `lit code add SOURCE` takes a URL or a local directory (copied; `--move` moves it).

**[C.1] Clone and bind a new repo.** Merge the CLI's `[code_candidates]` block with your URL notes from [A] (lowercase the scheme and host, strip a trailing `/` and sentence punctuation, keep the path's case), tag each, and present **one** table:

```
Code repo candidates for <id>:
[both]       https://github.com/foo/bar     CLI: p7 ×3 | agent: "Code Availability — we release..."
[CLI only]   https://github.com/baz/lib     p12 ×1    | agent cue: appears as dependency citation
[agent only] https://github.com/qux/model              | agent: footer p2

Pick which to bind: [1,3] / all / none
```

Give a `[CLI only]` row a one-line cue from the PDF when you can — **never drop a CLI candidate silently**; say where you saw an `[agent only]` one. Both sources empty → no table, no question. Each pick: `lit code add <url> --paper <id>`.

**[C.2] A repo already in the vault** → `lit code link <repo-name> --paper <id>`: binds, no clone. On the user's request only.

**[C.3] Unbind or retire — read the reverse list first** (`lit code list --format json`: each repo's `papers`). Other papers still use it → `lit code unlink <repo> --paper <id>`, which keeps the directory; 🔴 never `lit code rm --cascade` while other papers still cite the repo. The last one → `lit code rm <repo> --cascade --yes` deletes the directory: Rule 4. `lit unlink` without `code` is paper ↔ project ([H]).

## [H] Projects

- **Register** — `lit project add <name> --path <abs>`, or `lit project set-path <name> <abs>` after a move. **The user gives the path — never guess it.**
- **Link** — `lit link <paper-id> --project <name> [--relevance "..."] [--priority A|B|C]`, on the user's explicit request only (Rule 10); register an unknown project first. A relevance on an already-linked paper: `lit modify <paper-id> --set relevance-<project>=…`. A `kept N folder(s) that do not match the vault: <path>` line, from any command → relay the paths verbatim: those folders went to `<vault>/.trash/replaced-folders/` and come back only by hand.
- **Unlink** — `lit unlink <paper-id> --project <name>` drops `relevance-<project>` (unless `--keep-relevance`) and the grade; re-linking does not bring the grade back — say so.
- **List** — `lit project list --format json` is the source for which projects exist and where — not `lit-config.yaml`, not `lit config show`.
- **Rename** — `lit project rename <old> <new>`: say what it changes ("across the N papers using it"), then run it.
- **Delete** — `lit project rm <name>` untags every paper and deletes the project folder's `litman_reflib/` links and `REFERENCES.md`, outside the vault: no trash covers them. Rule 4 — say that, `--yes` on the user's go, relay the result.

## [I] Restore a trashed paper

Only the paper lit-reading B13 confirmed with the user — never on your own, never picking the candidate yourself. Read `lit trash list --format json` first: an entry's `orphan_repos` are what a restore would re-clone — tell the user. `lit trash restore <id> -y` restores and re-clones; without `-y` it skips the re-clone, keeps the repo's record, and names `lit code restore-all` for later. Relay the result verbatim, a `kept N folder(s)` line included ([H]). The same id trashed twice → the CLI lists the `<id>-<UTC-timestamp>` entries: ask which, don't guess. A live paper already holds `papers/<id>/` → the restore refuses: relay it; don't force. An SI file deleted with `lit rm <id> --si` was never in the trash ([L]).

## [J] Vocabulary governance

`lit taxonomy rename <dict> <old> <new>`, `lit taxonomy merge <dict> <source>... --into <keep>` and `lit taxonomy rm <dict> <value>` work on topics / methods / data; projects go through [H]. 🔴 Never hand-edit `TAXONOMY.md`.

- **Show the blast radius first** ("removing `diffusion` strips it from the N papers tagged with it"). **`rename`** of a value the user named → say what it changes, run it. **`merge` / `rm`** → Rule 4: `--yes` once the user, having seen the blast radius, says go.
- **Which values mean the same is the user's call.** You may propose a merge; never decide one.

**Tidy — "我的 topic 是不是太碎了" / "tidy my tags".** Run `lit health-check --all` and `lit taxonomy list --format json`; read the `taxonomy_similar`, `taxonomy_unused` and `taxonomy_singletons` findings (`info`, report-only, never `--fix`). An unused value may be registered ahead on purpose, or used only by a trashed paper (a restore would bring it back unregistered — say so). Add your own reading of `used_by`: same meaning without looking alike, once-used values that belong under a broader one. Propose **one** list of merges (`<drop> → <keep>`, both counts) and removals, as commands; run only what the user approves, with `--yes` — their yes to the list is the confirmation.

## [K] Repair a legacy library

`lit health-check` reports `placeholder_metadata`, `placeholder_id`, `weak_id_keyword` or `dirty_metadata` (the `rename <id>` lines `--fix` prints), often dozens at once ("库里好多 Unknown" / "clean up these bad ids") → **read `legacy-repair.md` (next to this SKILL.md) first.** In short: `lit health-check --all` for the whole list, the fields before the id, one table and one yes for the batch.

## `tip:` lines

A dim `tip:` line after a command (`no lit health-check in 14+ days…`, `no lit sync push in 7+ days…`) → relay it and offer to run the command it names; never run it unasked. A first `lit sync push` prints a size preview and stops for `--yes`: relay the preview, re-run with `--yes` on the user's go.
