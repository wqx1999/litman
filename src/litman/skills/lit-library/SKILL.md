---
name: lit-library
description: "Write side of the litman vault: drives the `lit` CLI to change the library. NOT for discussing or reading a paper — that's lit-reading. Use to add a paper (PDF or DOI) or its SI files, tag it, link it to a project, bind a code repo, export a .bib, rename, delete or restore a paper, or tidy the topic / method / data vocabulary. Triggers: '加到文献库' / 'add this paper', '把这些 SI 加到这篇' / 'add these SI files', '导出 bib' / 'export refs', '给这篇打标签' / 'tag this paper', '新建项目' / 'link it to my project', '把代码 clone 下来' / 'clone this repo', '把这篇删了' / 'delete this paper', '恢复删掉的论文' / 'restore from trash', '合并这两个 topic' / 'merge these tags', '把这篇的 id 改成 X' / 'rename this paper', '库里好多 Unknown' / 'fix these bad ids'. NOT for a project's own References/ folder — that's ref-manager."
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

- **Inbound.** When lit-reading hands you a write mid-discussion, do that one write, report the result, and hand control back — don't start a fresh ingest session or take over the conversation. The hand-off carries the paper id, the exact intent ("add `extends=<other-id>`") and anything the user already said.
- **Outbound.** The moment the conversation turns from changing the library to reading it, switch to lit-reading at its Phase 2, passing the paper id, the PDF path and what the user just said: a read-finished remark ("读完了" / "这篇一般" / "没价值" / "done with this paper" → its B10), a question about the content ("它讲了啥" / "compare it to <other>" / "有没有类似的"), a question the SI answers ("Table S3 里…" / "SI 里写了啥" → its B15), resume reading, inbox triage, library health. This happens most often right after a `lit add`. The closing proposal — status, read-date, notes, empty fields — belongs to lit-reading B10; don't run it here.

## Finding the vault

`lit vault list --format json` lists the registered vaults and which one is active (`$LIT_LIBRARY`, if set, overrides it); `lit hello` confirms `lit` is installed (Rule 8 if it is not). Never assume a relative path. If the user has both a project `References/` folder and a global vault, ask which one they mean.

## Workflows

- **[A] Add a paper from a PDF** — a PDF and no DOI anywhere (none from the user, none printed in the PDF), or the `--doi` fetch failed.
- **[B] Add a paper from a DOI** — a DOI from any source, the user's or the PDF's. Preferred whenever one exists.
- **[C] Code repositories** — clone and bind, bind a repo already in the vault, unbind or retire.
- **[D] Browse and inspect.**
- **[E] Metadata, tags, edges.**
- **[G] Export a .bib.**
- **[H] Projects** — register, link, unlink, list, rename, delete, set-path.
- **[I] Restore a trashed paper** — the one lit-reading B13 confirmed.
- **[J] Vocabulary governance** — rename, merge or remove a value; tidy a fragmented vocabulary.
- **[K] Repair a legacy library** — `lit health-check` reports filler metadata or filler ids in bulk.
- **[L] A paper's SI files** — add the files the user hands you, delete one, add one back. Asking the GUI for SI while reading is lit-reading B15.

---

## [A] Add a paper from a PDF

`lit add` needs a local PDF. Given only a DOI or a URL, ask for the file ("litman ingests from a local PDF you've read").

1. **Read the PDF — keep going until a reader works:**
   1. your agent's file-read tool (in Claude Code: `Read(pdf_path, pages="1-3")`);
   2. a PDF / document MCP tool, if you have one;
   3. `lit pdf-text <pdf> --pages 1-3` — needs nothing beyond `lit` (no poppler); exit 3 means no text layer (a scan), so go back to a multimodal reader or OCR;
   4. only when all three failed: say which rung failed and why, then show install commands for the user to run (`brew install poppler` / `apt install poppler-utils` / `dnf install poppler-utils` / `scoop install poppler`) or suggest a vision-capable model. **Never run an install yourself.**

   Extract the title (page 1), the authors ("Family, Given", in order), year, DOI (first 1–2 pages), journal and abstract. **A DOI on the PDF → switch to [B] at once** (`lit add <pdf> --doi <doi>`): CrossRef's metadata is authoritative and carries volume / issue / pages, which the JSON cannot. Stay here only with no DOI anywhere, or when the `--doi` fetch fails.

   While you read, note any code-repo URLs on the pages you opened (Code / Data Availability, footnotes, "we release at https://…"), each with a one-line cue, for [C.1]. Don't read the whole PDF for them: `lit add` scans the full text itself.
2. **Leave a field `null` rather than guess it.**
3. **Write the JSON to a fresh temp file** (e.g. `lit-llm-<random>.json` in the system temp dir):
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
   Unknown keys are rejected. Topics / methods / data are not part of it — tagging is [E], on request.
4. **Run `lit add <pdf-path> --from-llm-json <json-path>`, then delete that JSON file.** It was a one-shot bridge; never leave it in the temp dir.
5. **Confirm in two separate messages — never bundle them:**
   - **5a** — report only the derived `id` and the `title` (don't judge the title yourself), then **stop and wait** for the user to confirm the source is right. Nothing else goes in this message: no code table, no status offer.
   - **5b** — after their yes, in a new message: if the CLI's `[code_candidates]` block or your URL notes found anything, present the [C.1] table (a single CLI-only candidate still goes through it); if both are empty, stop. Don't offer tags, projects, status or grades (Rule 10).

   If the user then starts reading or judging the paper, hand off to lit-reading (Hand-offs above).

**Duplicate.** `lit add` refuses a DOI already in the vault (`DuplicateDOIError`, naming the id). Don't retry or force a second copy: say "already in your vault as `<id>`" and hand off to lit-reading Phase 2 with that id. Without a DOI, the id check catches it (next list).

**An SI file is not a paper.** A PDF named like `*_si_001.pdf` / `*_MOESM1_ESM.pdf`, or headed "Supporting Information", is a paper's SI. Say so before running anything: if its paper is in the vault, offer `lit add --si <id> <file>` ([L]); otherwise ask for the article's own PDF. Never switch commands silently.

### When `lit add --from-llm-json` errors

- **"Paper id 'X' already exists"** → `lit show X --format json`. The same paper → the duplicate path above; a different one → propose an `--id` to the user and stop. 🔴 Never `--auto-suffix`: it files another copy under an id nobody chose.
- **"Metadata from LLM JSON has no year"** → re-read the PDF (page 1, header / footer, the DOI landing page), ask the user, or pass `--id <year>_<Family>_<Keyword>`. 🔴 **Never use the download year, the file's date or the current year.** The year is baked into the id and into every exported citation; a wrong one passes every later check and is only ever found by hand.
- **"field 'title': String should have at least 1 character"** → re-extract.
- **"This title cannot produce a paper id"** → the title is in a script an id cannot carry (Chinese, Japanese, Korean): ids are ASCII folder names. Pass `--id <year>_<Family>_<Keyword>`, using the candidate the error names if it gives one; otherwise the keyword is yours to invent — from the paper's English title if it prints one, else a transliteration. 🔴 **Say it is invented and stop** ("这个关键词是我起的，不是从标题算出来的 —— 用 `2018_Zhang_Green-synthesis` 可以吗？"), and run the add only once the user accepts or replaces it: short of `lit rename` the id is permanent. 🔴 **Never translate the `title` field** to make the id easier — the stored title keeps its own script; only the id is ASCII. Write the first author's family name romanised (`Zhang, Tianshun`), as CrossRef and .bib files expect.
- **"is `…`, a placeholder"** (on the title or the first author: `Unknown`, `N/A`, `untitled`) → re-extract it from page 1. 🔴 **Never fill a placeholder to get the add through** — the title keyword and the first author's family name both go into the id. A work with no personal author (patent, editorial, standard) names its issuing body — assignee, journal, organisation; a genuinely unattributed one is `Anonymous`.
- **`Warning: author N is 'Unknown', not a real name`** (the add succeeded: a filler after the first author never reaches the id) → re-read that name off the PDF and rewrite the whole list with `lit modify <id> --set-author "…"`, one flag per author, **in order** (🔴 not `--rm-tag` + `--add-tag`, which appends the correction to the end). If it is unreadable, say which position is unresolved when you report the add.

### Wrong title or id after the add

The id and the title are separate. `lit modify <id> --set title=<correct>` fixes the title (and the `# <title>` line of `notes.md`) without touching the id; `lit rename <old> <new>` changes the id without touching the title. For an unsatisfying id, suggest one in `<year>_<Family>_<Keyword>` form, marked "computed — confirm or change", and let the user decide. `lit rm` plays no part in either.

**Renaming an id.** "把这篇的 id 改成 X" / "rename this paper to X" means `lit rename <old> <new>` — don't ask whether they meant the title. It rewrites every in-vault reference (other papers' edges, project links, every `[[id]]` in notes, its discussion.md heading, INDEX) and loses nothing: say what it will change, then run it (Rule 4). Relay each `plain text <file>:<lines> still says <old>, left as is` line — the old id written outside `[[ ]]`, which rename never rewrites — and let the user decide.

---

## [B] Add a paper from a DOI

`lit add <pdf-path> --doi <doi>` — the user gave a DOI, or [A] found one on the PDF. A local PDF is still required.

There is no `--arxiv`. Given only an arXiv id, read the DOI off the PDF; with none printed, stay on [A] and put the id in `arxiv-id`. 🔴 Never make up a DOI from an arXiv id or look one up unprompted.

The title is authoritative on this path, but the id keyword is still derived locally: **report the id and stop**; if the user dislikes it, `lit rename` ([A]).

---

## [C] Code repositories

A paper's `code-clones` is 1:N: one repo under `<vault>/codes/<name>/` can serve several papers. `lit code add SOURCE` takes a URL (a shallow clone) or a local directory (copied, or moved with `--move`; a non-git directory becomes a fresh repo) — so "the repo I already have on disk" is [C.1] with a path.

### [C.1] Clone and bind a new repo

Two sources: the CLI's `[code_candidates]` block after `lit add` (one `<url> (p<page>, ×<count>)` per line, or `no code repo URL found in full text`), and your URL notes from [A]. Merge them (lowercase the scheme and host, strip a trailing `/` and sentence punctuation, keep the path's case), tag each `[both]` / `[CLI only]` / `[agent only]`, and present **one** table:

```
Code repo candidates for <id>:
[both]       https://github.com/foo/bar     CLI: p7 ×3 | agent: "Code Availability — we release..."
[CLI only]   https://github.com/baz/lib     p12 ×1    | agent cue: appears as dependency citation
[agent only] https://github.com/qux/model              | agent: footer p2

Pick which to bind: [1,3] / all / none
```

- `[both]` — high confidence.
- `[CLI only]` — add a one-line cue from the PDF when you can ("deliverable in Methods"). **Never drop a CLI candidate silently.**
- `[agent only]` — say where in the PDF you saw it.

Both sources empty → no table, no question. For each item the user picks: `lit code add <url> --paper <id>`. `lit code list --paper <id> --format json` shows what is bound; `lit code update <name>` pulls.

### [C.2] Bind a repo already in the vault

`lit code link <repo-name> --paper <id>` — binds, no clone. On the user's request only.

### [C.3] Unbind or retire — read the reverse list first

`lit code list --format json` gives each repo's `papers` list. Then:

- **Other papers still use it** → `lit code unlink <repo> --paper <id>`: drops both sides of the binding, keeps the directory. 🔴 Never `lit code rm --cascade` while other papers still cite the repo.
- **This paper is the last one** → retire the whole repo with `lit code rm <repo> --cascade --yes`, which deletes the directory: Rule 4, on the user's go.
- `lit unlink` (no `code`) is paper ↔ project ([H]), not code.

---

## [D] Browse and inspect

`lit list --format json` with filters (`--topic`, `--year`, `--status`, `--project`, `--priority`, `--author`, `--title`, `--read-since`, …: see `lit list --help`) and `lit show <id> --format json`. Any command that takes a paper id accepts the full id, a unique case-insensitive substring, or `--paper-doi <DOI>` (not `lit rename`; `lit add --si` takes no `--paper-doi`). An ambiguous substring lists the candidates and exits non-zero — ask which one.

---

## [E] Metadata, tags, edges

**Every tag comes through Flow A or Flow B** (Rule 10).

### Flow A — "tag this / classify it" (no value named)

Propose candidates **only from registered values**; the user picks; you run `lit modify <id> --add-tag topics=<value>` (or `methods=` / `data=`). The registered set is `lit taxonomy list <dict> --format json` (`used_by` = papers per value; never parse `TAXONOMY.md` by hand); for `projects`, `lit project list --format json` ([H]). Spotting near-duplicates while you list ("your topics have both `tokenization` and `tokenisation` — merge them?") → propose only; merging is [J].

### Flow B — the user names a value

Registered → apply it. **Not registered → `lit modify` refuses it**, with no way round. Register it first — `lit taxonomy add <dict> <value>` for topics / methods / data, `lit project add <name> --path <abs>` for projects ([H]; `lit taxonomy add projects` is refused) — then re-run `lit taxonomy list <dict> --format json` and apply.

**Before registering, look for a near-duplicate**: the same words up to case, `-` / `_` / spaces and a plural `s` (`Peptide Design` / `peptide-design`), a prefix (`peptidedrug` / `peptide`), a near spelling (`tokenisation` / `tokenization`), or the same meaning. Report it with its `used_by` ("`peptide` is already there, on 6 papers — use it?"). `lit taxonomy add` itself refuses a look-alike (`'peptidedrug' looks like 'peptide' (prefix).`): relay it, offer the existing value, and add `--force` only when the user, having seen the match, still wants the new one.

Never hand-edit `lit-config.yaml`'s `projects:` map.

### Stamps and fields

Prefer the stamps to `lit modify --set`: `lit read` (read-date; `--date` backdates), `lit revisit` (last-revisited), `lit promote` (deep-read — it does not stamp read-date), `lit skim`, `lit drop`. A per-project grade is `lit modify <id> --set priority-<project>=A|B|C` on a paper linked to that project (`lit link <id> --project <P> --priority A` does both at once); it can be changed but not cleared — `lit unlink` drops it. Rewrite the author list with `lit modify <id> --set-author "Family, Given"`, one flag per author in order: the only way to fix or reorder a name in place.

### Apply an edge

Only an edge the user confirmed, usually handed over by lit-reading. Run the **forward** field only: `lit modify <id> --add-tag extends=<other-id>` (or `related=` / `contradicts=`). The CLI writes the reverse field (`extended-by` / `contradicted-by`) on the other paper: **never set a reverse field, never run a second command for it.**

---

## [G] Export a .bib

| The user says | Command |
|---|---|
| "导出 bib" / names no project | `lit export --all -o refs.bib` — just run it |
| "导出和 pepforge 有关的文献" | `lit export --project pepforge` (writes `./refs.bib`) |
| "写 thesis，把 priority A 的都导出来" | `lit export --all --priority A -o thesis.bib` (A for any project; add `--project <P>` for one) |

Filters and the rest: `lit export --help`. Cite keys are paper ids, so the output drops into `\cite{<paper-id>}`; re-running on the same file is how to update it.

1. **The CLI refuses to overwrite a file without the litman sentinel (a hand-edited bib) → never add `--force` yourself.** Relay the refusal: `--force` discards the user's edits, so it is Rule 4.
2. "here" / "current dir" → the default `./refs.bib`; a *named* directory ("thesis 目录") → ask for the path, don't guess.
3. An unregistered `--project` token → canonicalise case and whitespace only; else show the registered set.
4. No project named → infer it from the context if you can, else `--all` — no question. Ask only about an unresolvable project name (3) or `--force` (1).

---

## [H] Projects

- **Register** — `lit project add <name> --path <abs>`. **The user gives the path — never guess it.**
- **Link** — `lit link <paper-id> --project <name> [--relevance "..."] [--priority A|B|C]`, on the user's explicit request only (Rule 10). An unregistered project → register it first. If the output says `kept N folder(s) that do not match the vault: <path>`, relay the paths verbatim: those folders went to `<vault>/.trash/replaced-folders/` and come back only by hand (the same line can come from `--rebuild-all`, `trash restore`, `health-check --fix`, and the drift prompt after a project or the library moved).
- **Unlink** — `lit unlink <paper-id> --project <name>` drops that project's `relevance-<project>` (unless `--keep-relevance`) and its grade; re-linking does not bring the grade back — say so.
- **Relevance or grade after linking** — `lit modify <paper-id> --set relevance-<project>=…` / `--set priority-<project>=A|B|C`.
- **List** — `lit project list --format json` (`{name, path, status}`; `status` is `ok` / `path-missing` / `config-only` / `taxonomy-only`) is the source for which projects exist and where each one lives — not `lit-config.yaml`, not `lit config show`. A project's papers: `lit list --project <name> --format json`.
- **Rename** — `lit project rename <old> <new>`: say what it changes ("renames it across the N papers using it"), run it, then re-run `lit project list --format json`.
- **Delete** — `lit project rm <name>` untags every paper and deletes the project folder's `litman_reflib/` links and `REFERENCES.md`, which live outside the vault: no trash covers them. Rule 4 — say that, run it with `--yes` on the user's go, relay the result.
- **Set path** — `lit project set-path <name> <abs>` when the project moved; the user gives the path.

---

## [I] Restore a trashed paper

Only the paper lit-reading B13 confirmed with the user — never on your own, never picking the candidate yourself.

1. Read `lit trash list --format json` first: an entry's `orphan_repos` (`{repo: upstream}`) are the repos the restore would re-clone. Tell the user.
2. `lit trash restore <id> -y` restores and re-clones. Without `-y` (non-interactive) it restores, skips the re-clone and prints the `lit code add <url> --paper <id> --name <repo>` that does it — use that when the user doesn't want the clone now. A failed clone keeps the binding and warns; the paper is restored either way.
3. Relay the result verbatim (reverse edges rebuilt, repos re-bound, projects re-linked), including any `kept N folder(s)` line ([H]).

- The same id deleted more than once → the CLI lists the entry names (`<id>-<UTC-timestamp>`): relay them and ask which, don't guess.
- A live paper already holds `papers/<id>/` → the restore refuses. Relay it; the user renames or removes that paper first. Don't force.
- Edges to papers no longer in the vault are dropped. A restored paper brings back its `supplementary/`; an SI file deleted with `lit rm <id> --si` was never in the trash — add it again ([L]).

---

## [K] Repair a legacy library (filler metadata / filler ids)

Trigger: `lit health-check` reports `placeholder_metadata`, `placeholder_id`, `weak_id_keyword` or `dirty_metadata` (the `rename <id>` lines `--fix` prints) — typically dozens or hundreds at once, on a vault filled before those guards existed. The user's phrasing is usually "库里好多 Unknown" / "clean up these bad ids".

**Get the whole list first.** The report caps each category at its first few findings; **`lit health-check --all`** prints every one. Working from the capped view silently repairs 5 papers and reports the category done.

**Fix the fields before the id, always.** The two are separate commands and the order is not cosmetic:

```bash
lit modify <id> --set title="…" --set-author "Family, Given" --set-author "…"
lit rename <old-id> <new-id>
```

Repairing the fields is the half that needs the PDF reopened. Once they are right, **re-run `lit health-check`** — `placeholder_id`'s hint is now a complete, filled-in `lit rename <old> <new>` computed from the corrected metadata, so the second half costs no thinking at all.

🔴 **`--set-author`, one flag per author, the whole list in order.** `--add-tag` appends, which puts a corrected first author last — and the first author's family name is what the id is built from.

**Batch the confirmation, don't batch the decision.** These are permanent handles you are proposing, so they wait for the user's yes (Rule 11) — but asking 300 times is not asking. Present **one table** and take **one yes**:

| old id | proposed | from |
|---|---|---|
| `2018_Unknown_Untitled` | `2018_Zhang_Green-synthesis` | 标题:关于化合物的绿色合成方法 |
| `2019_Unknown_A` | `2019_Li_CRISPR-Cas9` | 标题里的 CRISPR-Cas9 |

Then run the whole batch and report. The user scans, corrects the few they care about, and approves the rest in one message.

**Mark the invented ones.** When a title is in a non-Latin script the keyword is yours to invent, not derive ([A]'s rule) — say which rows are guesses so the user knows where to look. 🔴 Never translate the `title` field to make an id easier.

**What `lit rename` cannot reach.** It rewrites every in-vault link — other papers' `related` / `extends` / `contradicts`, every `[[id]]` in notes, project links, INDEX — and lists, without rewriting, the places notes spell the old id as plain text. It cannot rewrite what left the vault: a `\cite{2024_Unknown_Untitled}` already sitting in the user's manuscript, a link they pasted somewhere. On an old library that is a real risk — **say it once** when proposing the batch, so "一次起对" is a choice and not a surprise.

**Reporting is not fixing.** All three checks are report-only by design: only the reader knows what a paper should be called, so `--fix` never picks a handle. A paper the user chooses to leave alone stays reported — that is correct, not a bug to work around.

---

## [J] Vocabulary governance

`lit taxonomy rename <dict> <old> <new>`, `lit taxonomy merge <dict> <source>... --into <keep>` and `lit taxonomy rm <dict> <value>` work on topics / methods / data; projects go through `lit project` ([H]). 🔴 Never hand-edit `TAXONOMY.md`.

- **Show the blast radius first**: "removing `diffusion` strips it from the N papers tagged with it" / "merge folds `tokenisation` into `tokenization` and re-tags the N papers".
- **`rename`** of a value the user named → say what it changes, run it, report.
- **`merge` and `rm`** → Rule 4: run with `--yes` once the user, having seen the blast radius, says go.
- **Which values mean the same is the user's call.** You may propose a merge; never decide one.
- Afterwards re-run `lit taxonomy list <dict> --format json`.

### Tidy the vocabulary — "我的 topic 是不是太碎了" / "tidy my tags"

1. Run `lit health-check --all` (without `--all` each category stops after 5) and `lit taxonomy list --format json`. Read three `info` findings — report-only, never `--fix`, they don't change the exit code: `taxonomy_similar` (two values that look alike; the hint is the merge, the less-used into the more-used), `taxonomy_unused` (used by no paper — maybe registered ahead on purpose; a value only a trashed paper uses shows here too, so if `lit trash list` is not empty, say a restore could bring it back unregistered), `taxonomy_singletons` (a dict where many values tag one paper each). Add your own reading of `used_by`: values that mean the same without looking alike, once-used values that belong under a broader one.
2. Propose **one** list: each merge (`<drop> → <keep>`, with both counts) and each removal, as the commands you would run.
3. Run only the items the user approves — `lit taxonomy merge <dict> <drop> --into <keep> --yes` / `lit taxonomy rm <dict> <value> --yes`; their yes to the list is the confirmation. Then re-run `lit taxonomy list --format json`.

---

## [L] A paper's SI files

SI files live in `papers/<id>/supplementary/` as `si-01.pdf`, `si-02.xlsx`, …, each recorded with the name it was added under; `si-NN` is litman's numbering (the lowest free number), not the publisher's.

- **Add the files the user hands you** → `lit add --si <id> FILE...`. Each file is checked against the paper first; stderr shows `✓` / `?` / `✕` per file with its original name, size and the reason, and **all are added or none**:
  - `✕` (`empty file`, `this is the article itself`, `looks like the SI of <other-id>`, `already in the library`, `same file as <name>`) cannot be added, even with `--force`. Relay the reason.
  - `?` (`can't confirm it belongs to this paper`), or a file over 100 MiB, stops the run: `… nothing was added. Pass --force to add it anyway.` Ask the user; `--force` only on their yes.
  - The files are **moved** into the vault — the originals leave Downloads. Say so before you run it.
- **No files yet** → lit-reading B15: `lit add --si <id>` opens the SI window on the user's litman page.
- **Delete one** → `lit rm <id> --si si-NN`, Rule 4. Run it with `--dry-run` and relay its lines verbatim — each file with its original name, any `<file> is cited 2× in discussion.md` — and say the file goes for good: no trash, the only way back is adding it again (on this path the CLI's own `This can't be undone` never reaches the user). A record whose file is already gone still reads `Would delete`: say only the record goes. On the user's go, run it with `--yes` and relay the result, including a `still cited … — lit health-check lists the lines` line (those citations are now stale; lit-reading B12). `--si` does not combine with `--purge`; deleting the whole paper takes its `supplementary/` to the trash with it.
- **Add one back** → ask the user for the file, then `lit add --si <id> FILE`; it takes the lowest free number, often its old one.
- **Open one** → `lit open <id> --si si-NN` (lit-reading B14).
- 🔴 **Never download SI** — not from the publisher, PMC, Figshare or any copy; the user downloads it. `lit health-check` reports SI drift (lit-reading B12).

---

## `tip:` lines

`lit` may print a dim `tip:` line after a command — `no lit health-check in 14+ days…`, or `no lit sync push in 7+ days…` (only when a sync remote is configured). Relay it and offer to run the command it names; never run it unasked. A first `lit sync push` prints a size preview and stops for `--yes`: relay the preview, re-run with `--yes` on the user's go.
