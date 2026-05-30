---
name: lit-library
description: "Write side of the litman vault — drives the `lit` CLI to change the library. Use when the user wants to add a paper to their **global** vault, browse/filter all papers, tag with controlled vocabulary, bind code repositories to a paper, export a BibTeX file for writing, manage the project registry, govern the TAXONOMY, or restore a deleted paper. Triggers: 'add to lit library', '加到文献库', 'lit add', 'show me all my papers on X', 'tag/classify this paper', 'link this code repo to that paper'; export bib ('导出 bib', 'export refs', 'thesis bib', '准备引用', 'refs.bib'); project lifecycle ('新建项目', 'register a project', 'rename/delete the project', 'the project moved', 'list my projects'); TAXONOMY governance ('合并这两个 topic', 'merge/rename a tag value', '删掉这个 topic/method'); restore a trashed paper ('恢复删掉的论文', 'restore from trash'). NOT for discussing/comparing/summarizing a paper you are reading — that's the lit-reading skill. NOT for project-local References/ folders — that's the ref-manager skill."
---

# lit-library — Global Literature Vault Skill

Write side of litman. Read-side companion: **lit-reading**.

## Architecture you must respect

1. **Two-root layout.** Vault and code repo live in different filesystem locations. Discover the vault via `$LIT_LIBRARY` env var or `lit-config.yaml` walk-up — never assume a relative path.
2. **LLM never writes data files directly.** Extract metadata as JSON to a temp file, then call `lit add --from-llm-json <path>`. The CLI writes `papers/<id>/metadata.yaml`. Never write yaml/markdown into the vault yourself.
3. **TAXONOMY is mutated only via `lit taxonomy {add,rename,merge,rm}`** (and `lit project` for the `projects` dict). Never hand-edit `TAXONOMY.md`.
4. **Vault is NOT git-tracked.** The CLI handles atomicity — don't try to use git to roll back.
5. **Agent-writable free-form = `notes.md` (overwrite) + `discussion.md` (append) only.** Every other file in `papers/<id>/` and `codes/<name>/` is CLI-mediated. Drive only the **forward** paper-to-paper relation fields (`related` / `extends` / `contradicts`); the CLI auto-maintains the reverse pair (`extended-by` / `contradicted-by`) — never set a reverse field by hand.

If a user request violates any of the above, push back and propose the CLI-mediated alternative.

---

# PART A — Autonomy, chaining, scope (read this first)

## A1. Autonomy ladder

Classify every action before you take it.

| Tier | Operation class | Behavior | Examples |
|---|---|---|---|
| 1 | **Read** | Just do it, don't ask | `lit list`, `lit show`, scan a PDF, query INDEX via `lit list --format json`, `lit project list`, `lit code list`, `lit trash list`, `lit health-check` |
| 2 | **Write, reversible, single-paper** | Do it, then report | `lit modify --set priority=A`, `lit modify --add-tag topics=X` *(only after Flow A/B — [E])*, `lit read`/`skim`/`promote`/`drop`/`revisit`, `lit link`/`unlink` *(paper↔project, explicit request only — [H])*, `lit code link`, `lit modify --set relevance-<P>=` |
| 3 | **Write, multi-paper / structural / remote-IO** | Ask once before acting | `lit add` (confirm — [A]/[B]), `lit code add` (git clone), `lit taxonomy add` / `lit project add` (user types the new value), `lit taxonomy merge`/`rename`/`rm` + `lit project rename`/`rm` ([J]/[H]), `lit export --force` ([G]) |

The CLI hard-rejects unregistered controlled-vocabulary values, so your job is to **not invent values**.

**Execution ownership.** lit-library runs every Tier-2 write inline — it owns the write surface. lit-reading runs single-paper *evaluation stamps* inline (`lit read`/`promote`/`skim`/`drop`/`revisit`, `lit modify --set priority=`) and chains to lit-library for everything else.

## A2. Chain hand-off contract

**Inbound from lit-reading:** when summoned mid-discussion, do the **one** write you were handed, report the result, and hand control back. Do **NOT** start a fresh interactive `lit add` session or swallow the conversation with a multi-step ingest. The hand-off carries: paper id, exact intent (e.g. "add `extends=<other-id>`"), any user instruction already given.

## A2-out. Outbound hand-off to lit-reading

The chain is **bidirectional**: lit-reading hands writes *in* (A2), and lit-library hands *reading/evaluation* back *out*. lit-library owns the write surface but NOT the reading verdict — the moment the conversation shifts from "change the library" to "read / discuss / evaluate this paper", **switch to lit-reading** and resume at its Phase 2 (with the paper id already in context).

**Outbound triggers** (most common right after an `lit add`, but any time mid-session):

- read-finished verdict — "读完了" / "这篇一般" / "没价值" / "done with this paper"
- discussion / comprehension — "它讲了啥" / "这篇关于 X 怎么说" / "compare it to <other>" / "有没有类似的"
- resume reading / inbox triage / library health — all lit-reading triggers

**What to pass:** paper id, PDF path, and whatever the user just said. **Resume point:** lit-reading Phase 2 (loaded context) → B10 verdict ritual if the cue was read-finished. Do **NOT** try to run the status verdict, `read-date` stamp, or metadata self-check inside lit-library — those are lit-reading B10's, by design (SOP-1). The failure mode this prevents: staying parked in lit-library after add, so a "读完了，一般" never reaches the verdict ritual and the paper rots at `status: inbox`.

## A3. Scope discipline — SOP vs maintenance, reversibility, active-vault confinement

**(1) SOP vs maintenance.** Operation Routing below covers high-frequency literature actions — ingest, tag, link, bind code, export, govern vocab, restore. Low-frequency vault-maintenance (`lit sync`, `lit vault use`, `lit config`, `lit refresh-views`, `lit init`, `lit install-skill`, `lit vault {add,info,remove}`, `lit code update`/`restore-all`) has **no SOP** — fall through to `lit <cmd> --help`.

- **Read vs write within the no-SOP set.** Pure-read no-SOP commands (`lit code list`, `lit vault info`, `lit vault list`, `lit config show`) stay **Tier 1: just run and report**.
- **"Teach, don't do" applies only to no-SOP writes / maintenance.** Run `lit <cmd> --help`, surface the command, let the user run it. Execute it yourself only when the user explicitly asks. **Never guess a flag or invent a command from memory.**

**Destructive operations by reversibility:**

- **Soft-delete `lit rm`** (default → moves `papers/<id>/` into `.trash/`, recoverable via `lit trash restore`; [I]). **Never initiate.** May execute on explicit, confirmed request, and must **relay `lit rm`'s cascade report verbatim** ("This paper is linked with N entries…") — never silently delete, never summarize the link count away.
- **Irreversible removal** (`lit rm --purge`, `lit trash empty`): **NEVER execute these, even on explicit request.** Surface the exact command and let the user run it.
- **`lit code rm`** stays governed by [C.3] (confirm before execute; clone is re-cloneable).

**(2) Write only in the active (primary) vault.** Every write — ingest, modify, link, code add, taxonomy, appends to `notes.md` / `discussion.md` — targets the **currently-active vault only**. Cross-vault reading is fine; cross-vault writing is forbidden. If the user's intent requires writing into a different vault, **tell the user to switch first** (`lit vault use <name>`) and only then operate.

---

## How to detect the vault

```bash
lit hello                                   # confirms `lit` is installed
echo $LIT_LIBRARY                           # if set, that's the vault
lit list --format json | head -1            # confirms vault is reachable
```

If `lit` is missing: tell the user `pipx install -e /path/to/litman_dev/litman/` or activate the `litman` conda env. Do NOT try to install it yourself.

## When to use lit-library vs other skills

| User intent | Skill |
|-------------|-------|
| Add / tag / link / export / govern in the **global** vault | **lit-library** (this skill) |
| Discuss / compare / summarize a paper you are **reading** | lit-reading |
| Add a paper to a **project**'s `References/` dir | ref-manager |
| Cite in a `.bib` for a LaTeX manuscript | cite-retrieval |
| Review/critique manuscript text | paper-reviewer |
| Draft manuscript sections | academic-writing / paper-writer |

If the user has both a project `References/` and a global vault, ask which one they mean before proceeding.

## Operation Routing

- **[A] Add paper from a PDF (LLM-augmented)** — PDF in hand and **no DOI anywhere** (not user-supplied AND none printed in the PDF), or the `--doi` CrossRef fetch failed.
- **[B] Add paper from a DOI (direct CrossRef)** — a DOI is available **from any source** (user-supplied OR read off the PDF); no LLM extraction. Preferred whenever a DOI exists.
- **[C] Code repositories** — bind new clone, bind existing repo (1:N), retire/unbind.
- **[D] Browse / search / inspect** — find a paper, list by filter, show metadata.
- **[E] Modify metadata, tag, or apply an edge** — fields, Flow A/B tagging, confirmed edge.
- **[G] Export bib for writing** — project vault → `.bib`.
- **[H] Project operations** — register / link / unlink / list / rename / delete / set-path.
- **[I] Restore a trashed paper** — execute the restore lit-reading B13 confirmed.
- **[J] TAXONOMY governance** — merge / rename / remove a controlled value.

**SOP-1 (hard rule for every branch):** the agent does not proactively tag, edge-link, project-link, or code-bind a paper. Listing observations ("this touches tokenization, evaluation") is fine — *listing ≠ tagging*. Always **propose → user decides → you run the CLI.** Never self-initiate a structural write.

---

## [A] Add Paper from a PDF (LLM-augmented)

Use when the user has a PDF and no DOI, or explicitly says "add this paper with AI".

**PDF-required precondition.** `lit add` requires `PDF_PATH` as a positional argument. If the user gives only a DOI / URL with no local file, ask for the path ("litman ingests from a local PDF you've read").

**Pipeline**:

1. **Read the PDF** — a *must-achieve* goal. Walk this ladder until one rung works:

   1. **Claude Code `Read` tool** (default): `Read(pdf_path, pages="1-3")`. PDFs are natively handled via CC's multimodal pipeline.
   2. **PDF-related MCP tool** (for non-multimodal backends): scan your `available tools` for any whose description mentions PDF / document / extract.
   3. **`lit pdf-text` — deterministic fallback, no model / network / system tool**: `lit pdf-text <pdf> --pages 1-3` (omit `--pages` for the whole doc). litman ships pypdf as a hard dependency, so this works wherever `lit` runs — it does NOT need poppler / pdftoppm. Exit code 3 means "no extractable text layer" (scanned / image-only PDF): go back up to a multimodal reader or OCR, don't retry here.
   4. **Only if every rung above failed** (no multimodal read, no PDF MCP, and `lit pdf-text` returned no text): name the rung that failed and the exact gap, then **surface** OS-appropriate install commands for the user to run (`brew install poppler` / `apt install poppler-utils` / `dnf install poppler-utils` / `scoop install poppler`) or suggest a vision-capable model. **Show these commands — never execute an install yourself.**

   Extract: title (page 1 header), authors (page 1 list — preserve "Family, Given" order), year, DOI (search first 1-2 pages), journal / venue, abstract.

   **DOI-found reroute (do this the moment a DOI surfaces).** If you read a DOI off the PDF, **abort the LLM-extraction path and switch to [B]** — run `lit add <pdf> --doi <doi>` instead. CrossRef returns authoritative metadata plus `volume` / `issue` / `pages` / `publisher` / `venue-type`, which the LLM-json schema cannot carry. Stay on [A] only when (a) no DOI appears anywhere in the PDF, or (b) the `--doi` CrossRef fetch fails — then fall back here with the fields you already extracted. The up-front [A]/[B] split keys off "did the user hand a DOI"; this rule covers the other case ("a DOI turned up while reading the PDF").

   **Also harvest code-repo URLs into a side-buffer** (NOT part of the metadata JSON — feeds [C.1]). This is **best-effort over the pages you already read** — do NOT read the whole PDF just to collect URLs. Full-text code-URL discovery is the CLI's job: `lit add` runs a full-text scan and emits the `[code_candidates]` block ([C.1] source 1). So harvest opportunistically from whatever pages you opened for metadata (`Code Availability` / `Data Availability` blocks, Acknowledgments, footnotes, first-page footer, inline "we release at https://…" / "available at https://github.com/…"); for each URL note one short context cue ("we present X" / "we use X" / "see also"). An empty side-buffer is expected and fine — the CLI scan is the safety net.

2. **Verify, do NOT hallucinate.** If you cannot find a field, leave it as `null` rather than guessing.

3. **Write metadata to a temp JSON file**:
   ```bash
   TMP_META=$(mktemp --suffix=.json /tmp/lit-llm-XXXX.json)
   cat > $TMP_META <<'EOF'
   {
     "title": "<exact title from page 1>",
     "authors": ["Family1, Given1", "Family2, Given2"],
     "year": <int or null>,
     "doi": "<10.xxx/yyy or null>",
     "journal": "<venue or null>",
     "arxiv-id": "<2401.12345 or null>",
     "abstract": "<full abstract text or null>"
   }
   EOF
   ```

4. **Call the CLI, then delete the temp file**:
   ```bash
   lit add <pdf-path> --from-llm-json "$TMP_META"
   rm -f "$TMP_META"                 # clean up — the JSON was a one-shot bridge, not a kept artifact
   ```
   On a platform without `mktemp` (e.g. Windows), you still wrote a JSON somewhere — delete that exact path afterward. Never leave the extracted-metadata JSON lying in a temp dir.

5. **Confirmation gate (mandatory — human in the loop).** `lit add` prints a success panel and runs a full-text code-URL scan. Run this as **two separate messages — never bundle them**:

   - **5a — STOP and confirm the source.** Report ONLY the derived `id` and the `title`, then **stop and wait** for the user to confirm the source metadata is right. Surface only id + title — do not self-judge title correctness. Do **NOT** attach the code-candidate table, a status offer, or anything else to this message. This is a hard gate: the next move waits on the user's word.
   - **5b — after the user confirms, in a fresh message:** if **either** the CLI scan **or** your step-1 side-buffer surfaced a candidate → present the merged table ([C.1]). A single CLI-only candidate **still goes through the [C.1] three-state table** — no shortcut. If both are empty, stop here. Do **NOT** proactively enumerate tag / project / status / priority offers (SOP-1).

   **Post-ingest curation is lit-reading's, not yours.** Status verdict (`deep-read` / `skim` / `dropped`), `read-date`, and the metadata completeness self-check all live in **lit-reading B10** — lit-library deliberately does not run them after add (SOP-1). If, right after confirming, the user starts reading or evaluating the paper ("读完了" / "这篇一般" / "done with this" / "what does it say about X"), **hand off to lit-reading** (see A2-out) — do not stop dead and do not absorb the reading verdict here.

**Duplicate-add path.** `lit add` prechecks the DOI and **refuses** with a `DuplicateDOIError` naming the existing id. Do **not** retry or force a second copy — **relay "already in your vault as `<id>`" and route to *reading* it** (chain to lit-reading Phase 2 with that id).

### JSON Schema Contract (LLMCandidateMeta)

| Field | Type | Required | Notes |
|-------|------|----------|-------|
| `title` | string (non-empty) | ✓ | Exactly as printed; no normalization |
| `authors` | list[string] | ✓ (>=1) | "Family, Given" each; preserve order |
| `year` | int \| null | – | Publication year |
| `doi` | string \| null | – | Canonical DOI, no URL prefix |
| `journal` | string \| null | – | Venue / journal / preprint server |
| `arxiv-id` | string \| null | – | e.g. "2401.12345" |
| `abstract` | string \| null | – | Currently informational |

Unknown keys are rejected. Topics/methods/data are **NOT** in this schema — classification goes through Flow A ([E]) as a follow-up.

### When `lit add --from-llm-json` errors

- **`DuplicateDOIError` / "DOI 'x' already registered"** → see Duplicate-add path above.
- **"Metadata from LLM JSON has no year"** → re-read the PDF for a year, ask the user, or pass `--id 2024_Family_Keyword` explicitly.
- **"field 'title': String should have at least 1 character"** → re-extract.

### Title / id rollback (when the confirm gate fails)

`id` and `title` are **decoupled**: `lit modify --set title=` changes the field without touching the id; `lit rename` changes the id handle (cascades every back-reference) without touching the title. Three branches:

1. **Title field wrong** (almost only on `--from-llm-json`; `--doi` is authoritative) → `lit modify <id> --set title=<correct>`.
2. **Door-plate keyword unsatisfying** (decoupled from title-correctness; possible on either path) → `lit rename <old> <new>`. **Suggest** a new id following `<year>_<Family>_<Keyword>`, label it "computed, confirm or change", let the user decide.
3. **`lit rm` does NOT participate** — it is deletion semantics, would discard accumulated state and hit the reference safety net.

---

## [B] Add Paper from a DOI (direct, no LLM extraction)

Entered two ways: (1) the user hands a DOI up front, or (2) the [A] DOI-found reroute fired because a DOI surfaced while reading the PDF. Either way, skip LLM extraction:

```bash
lit add <pdf-path> --doi 10.1093/bioinformatics/btae364
```

Pipeline identical from id derivation onward; only the metadata source differs. Preferred whenever a DOI is available. PDF-required precondition still holds ([A]).

**No `--arxiv` flag — `lit add` accepts only `--doi` / `--from-llm-json`.** If the user gives only an arxiv id, do NOT fabricate a DOI from it. A published/preprint PDF almost always prints its DOI on page 1 — read it off the PDF and use `--doi`. If no DOI is printed anywhere, stay on [A] (LLM extraction) and leave `doi: null`; the `arxiv-id` goes into the [A] JSON's `arxiv-id` field. Never resolve an arxiv id to a DOI by guessing or by an unprompted external lookup.

**Confirm gate.** Title is authoritative on this path — no re-confirm. But the door-plate keyword is still derived locally and can be unsatisfying. **Read out the derived `id` and stop**; if the user is unhappy with it, branch 2 of [A]'s rollback (`lit rename`).

---

## [C] Code repositories — bind, link, retire

A paper's `code-clones` field is a **1:N** relationship: one repo under `<vault>/codes/<name>/` can be cited by several papers.

### [C.1] Bind a NEW clone — reconcile CLI scan with agent re-read

Two sources:

1. **CLI scan** — `lit add` prints candidates fenced by `[code_candidates]` / `[/code_candidates]`, one per line as `<url> (p<page>, ×<count>)`. Empty: `no code repo URL found in full text`.
2. **Agent side-buffer** — the URLs you harvested in [A] step 1.

**Merge** (lowercase scheme + host, strip trailing `/` and sentence punctuation, preserve path case), tag each `[both]` / `[CLI only]` / `[agent only]`, present ONE table:

```
Code repo candidates for <id>:
[both]       https://github.com/foo/bar     CLI: p7 ×3 | agent: "Code Availability — we release..."
[CLI only]   https://github.com/baz/lib     p12 ×1    | agent cue: appears as dependency citation
[agent only] https://github.com/qux/model              | agent: footer p2

Pick which to bind: [1,3] / all / none
```

Tag rules:

- **`[both]`** — high confidence.
- **`[CLI only]`** — agent adds a one-line cue from PDF context when it can ("appears as dependency citation" / "deliverable in Methods"). **Advisory; never silently drop a CLI candidate.**
- **`[agent only]`** — note where in the PDF the agent saw it.

**Both empty** → do not prompt.

Each selected item runs:
```bash
lit code add <url> --paper <current-id>      # clone (--depth 1) + bind, atomic
```
**Tier 3** (git clone = remote IO). Binding one paper to multiple repos is not batch ingest — ingest is still one paper.

Inspect with `lit code list --paper <paper-id>` (tier-1 read). Pull updates with `lit code update <name>`.

### [C.2] Bind an EXISTING vault repo (the 2nd+ paper in a 1:N)

When the repo is already cloned in `<vault>/codes/` and the user wants another paper bound:

```bash
lit code link <repo-name> --paper <id>       # bind only, no clone
```

**Tier 2** (pure bind, single-paper reversible). **Trigger = explicit user request.**

### [C.3] Retire / unbind — read the reverse list FIRST

Before unbinding, `cat <vault>/codes/<repo>/repo-meta.yaml` and read the `papers:` reverse list. Then branch:

- **Reverse list now empty** (current paper is the last citer) → retire the whole repo to avoid an orphan clone:
  ```bash
  lit code rm <repo> --cascade        # strips every binding, then removes the directory
  ```
  Directory removal is destructive → **ask the user before running.**
- **Reverse list still non-empty** → unbind this paper only, keep the directory:
  ```bash
  lit modify <id> --rm-tag code-clones=<repo>     # drops the field, keeps the directory
  ```
  **Tier 2** (single-paper field edit, reversible).
- **Never** `lit code rm --cascade` while other papers still cite the repo; **never** leave a `--rm-tag` orphan directory when unbinding the last citer.
- `lit unlink` is paper↔project ([H]), not for code.

---

## [D] Browse / Search / Inspect

```bash
lit list                                     # full vault
lit list --topic transformer --year ">=2023" # filter
lit list --status deep-read --priority A     # by personal evaluation
lit list --project pepforge --format json    # papers bound to a project
lit show Pandi                               # fuzzy: unique substring of id
lit show 2023_Pandi_Cell-free                # exact id also works
lit show --paper-doi 10.1038/...             # DOI reverse-lookup
```

Every `lit` command that takes a paper id (`show`, `open`, `modify`, `rm`, `rename`, `link`, `unlink`, `code link`, `code add --paper`, `code list --paper`) accepts (a) the full id, (b) a unique case-insensitive substring, or (c) `--paper-doi <DOI>` (mutually exclusive with positional / `--paper`). Ambiguous substrings (2+ matches) print the candidate list and exit non-zero. `lit rename <old> <new>` is the one exception: no `--paper-doi` (two positionals would be ambiguous).

For "find a paper I read last month about X", scan `lit list --format json` and grep semantically. For large vaults filter via `--topic` / `--project` first. For **author** cues use `lit list --author <cue>` — the JSON rows don't include `authors`.

---

## [E] Modify Metadata, Tag, or Apply an Edge

### SOP-1 — never proactively tag (restated)

Every tag goes through Flow A or Flow B.

### Flow A — user says "tag this / classify it" (no specific value)

Propose candidates **only from registered values** → user picks → `lit modify --add-tag`. Sources:

- `topics` / `methods` / `data` → read the in-context `TAXONOMY.md` (or `lit taxonomy list <dict>`).
- `projects` → `lit project list` is canonical. A file read alone is incomplete; see [H].

```bash
lit modify <id> --add-tag topics=peptide-LM --add-tag methods=transformer
lit modify <id> --rm-tag topics=outdated-value
```

While enumerating candidates you MAY **propose** a TAXONOMY merge if you spot near-duplicates ("your `topics` dict has both `tokenization` and `tokenisation` — merge them?") — propose only; governance runs through [J].

### Flow B — user names a value ("add tokenization")

Check if it is registered. **Registered → apply.** **Not registered → the CLI HARD-REJECTS** (no escape hatch); say so and route registration by dict:

```bash
# topics / methods / data — register via lit taxonomy:
lit taxonomy add topics peptide-LM
lit modify <id> --add-tag topics=peptide-LM            # now allowed

# projects — register via lit project (NOT lit taxonomy):
lit project add pepforge --path /abs/path/to/pepforge
lit modify <id> --add-tag projects=pepforge            # now allowed
```

After the user registers, **re-read `TAXONOMY.md`** and then apply.

### Register-first (MANDATORY for the four controlled dicts)

`projects` / `topics` / `methods` / `data` are **controlled vocabularies**. `lit modify --add-tag <dict>=<value>` HARD-REJECTS an unregistered value (no `--register` escape hatch). `projects` has its own group `lit project {add,list,rename,set-path,rm}` ([H]); **`lit taxonomy {add,rename,rm} projects` is hard-deprecated** — it errors and redirects. (`lit taxonomy list projects` still works.) Never hand-edit `lit-config.yaml`'s `projects:` map.

**Not register-first checked**: schemaless scalar fields (`read-date`, `doi`, `year`, custom scalars), reference fields (`authors`, `related`, `contradicts`, `extends`, `code-clones`), fixed enums (`type`, `status`, `priority`). `--rm-tag` is never register-checked.

### Sugar commands — prefer over `lit modify --set` for known semantic fields

```bash
lit read <id> [--date YYYY-MM-DD]   # stamp read-date (defaults to today; --date backdates)
lit revisit <id>                    # stamp last-revisited = today (distinct from read-date)
lit drop <id>                       # status = dropped
lit promote <id>                    # status = deep-read  (does NOT also stamp read-date)
lit skim <id>                       # status = skim
```

Same-day repeats are no-ops. For `priority` or an arbitrary scalar, fall back to `lit modify <id> --set priority=A`.

### Apply a knowledge-graph edge (inbound from lit-reading B7)

When lit-reading hands off a **user-confirmed** edge, run the **forward** field only:

```bash
lit modify <id> --add-tag extends=<other-id>      # or related=<other-id> / contradicts=<other-id>
```

The CLI mirrors the paired reverse field on the opposite paper automatically — **never run a second command for the reverse, never set `extended-by` / `contradicted-by` directly.** You apply **only** an edge the user already confirmed; never originate edges here.

---

## [G] Export bib for writing

The user expresses *intent*; you translate it to `lit export` flags.

| User says | Command |
|---|---|
| "导出和 pepforge 有关的文献到这里" | `lit export --project pepforge` (defaults to `./refs.bib`) |
| "写 thesis，把 priority A 的都导出来" | `lit export --all --priority A -o thesis.bib` |
| "给 PepCodec 准备 bib" | `lit export --project pepcodec` (canonicalize the project token first — Flow B / [H]) |
| "更新一下 refs.bib" | infer current project → `lit export --project <inferred>`; if not inferrable, ask |

Flags: `--project` XOR `--all` (exactly one required), `-o/--output` (default `./refs.bib`), `--priority` / `--status` / `--year` / `--type` (comma-separated; within one flag OR, across flags AND), `--force`, `--vault`. Cite keys equal paper ids — output drops into `\cite{<paper-id>}` directly. Re-running on the same file is the supported update path.

Three hard rules:

1. **Sentinel rejection → NEVER auto-add `--force`.** When the CLI refuses to overwrite a target lacking the litman sentinel (typically a hand-edited `references.bib`), relay verbatim and let the user decide — `--force` discards their hand edits.
2. **Path inference**: "current dir" / "here" → default `./refs.bib`; a *named* directory ("thesis dir") → ask for the path, do not guess.
3. **Project token**: unregistered `--project` gets deterministic canonicalization (case/whitespace) only, else present the registered set.

Tier: projection is **Tier 2**; `--force`-over-sentinel is a **Tier-3 ask**.

---

## [H] Project operations

- **Register** — `lit project add <name> --path <abs>`. **Tier 3.** **User supplies the path — never guess it.** Same register-first instance Flow B routes to.
- **Link** — `lit link <paper-id> --project <name> [--relevance "..."]`. Atomically adds the project to the paper's `projects:`, builds symlinks, regenerates `<project>/REFERENCES.md`. **Trigger ownership = user** (only on explicit request; never self-initiate). **Tier 2.** Unregistered `--project` → Flow B routing (`lit project add`).
- **Unlink** — `lit unlink <paper-id> --project <name>`. **Tier 2**, reversible. (Not for code — that's [C].)
- **Update relevance after linking** — `lit modify <paper-id> --set relevance-<project>=…` sets/edits the per-project relevance note without re-linking. **Tier 2.** At link time prefer the inline `lit link --relevance "..."`.
- **List projects** — `lit project list`. **Tier 1** read, **canonical source** for the registered set AND each project's path. Three columns: `name` / `path` / `status` (drift marker `✓` / `⚠ path-missing` / `⚠ config-only` / `⚠ taxonomy-only`). Use this for both "what projects exist" and "where is `<project>` on disk". Do NOT hand-parse `lit-config.yaml`, do NOT use `lit config show` for project paths.
- **List a project's literature** — `lit list --project <name>` (**Tier 1**; supports `--format json`). Per-project view of *papers*, distinct from `lit project list` which lists the *projects* themselves.
- **Rename** — `lit project rename <old> <new>`. **Tier 3 governance** (cascades: TAXONOMY + config key + every referencing paper's `projects:` + INDEX). Reuse [J] discipline: never hand-edit, **show the impact** ("renames `<old>`→`<new>` across the N papers using it"), **ask once**; re-read TAXONOMY afterward.
- **Delete** — `lit project rm <name>`. **Tier 3 + destructive** (cascade-untags every paper). Treat like deletion: **never initiate**, **show the impact + confirm before executing** even on explicit request. Project-registry removal only — trashes no paper.
- **Set path** — `lit project set-path <name> <abs>`. **Tier 2.** For when the project dir moved; user supplies the new path; no cascade.

Governance discipline (rename / rm) is shared with [J] — cross-reference, don't duplicate.

---

## [I] Restore a trashed paper

After lit-reading B13 hands off a **user-confirmed** paper id, execute the restore and relay the outcome:

```bash
lit trash restore <id>          # or the full entry name <id>-<UTC-timestamp>
```

Behavior:

- Accepts the **paper id** (must be unambiguous) or the **full entry name** `<id>-<UTC-timestamp>`. If the same id was deleted more than once, the CLI raises with the list of entry names — **relay it and ask which timestamp**, do not guess.
- **id-slot collision**: if `papers/<id>/` already holds a LIVE paper, restore **REFUSES**. Relay the error; tell the user to rename / remove the active paper first. Do not force.
- **Edges with deleted opposites are silently dropped**; surviving relations rebuild atomically.
- **Re-clone is built INTO restore.** If a repo was the paper's sole binder, it was hard-deleted at rm time with its upstream URL preserved. Restore re-clones it: **prompted per repo (default Yes) interactively, or auto with `-y`.** On refuse / clone failure the binding `code-clones:[X]` is **KEPT** + a warning emitted; re-clone is **never a precondition** for restore success.

Agent behavior:

- Before running, you MAY `cat <vault>/.trash/<entry>.meta.yaml` to surface the re-clone target(s) to the user up front (tier 1 read).
- **TTY caveat**: driving `lit trash restore <id>` without `-y` hangs/aborts at the interactive re-clone confirm on a non-TTY stdin. Agent path: surface the re-clone target(s) → get the user's nod → run `lit trash restore <id> -y`. No `--no-reclone` flag today — if the user wants restore but skip re-clone, relay that and let them run it themselves.
- **Tier 2** (local, reversible, single-paper). **Trigger = the user-confirmed identity from B13** — never restore on your own initiative, never pick the candidate for the user.
- **Relay the CLI's result summary verbatim** (reverse edges rebuilt in N papers, re-bound to N repos, re-linked into N projects).

---

## [J] TAXONOMY governance

Maintain the controlled vocabulary. Trigger keywords: "合并这两个 topic" / "merge X into Y", "把 X 改名" / "rename", "删掉这个 topic/method/值" / "remove this value".

Commands (verify exact flag spelling with `lit taxonomy <verb> --help`):

- `lit taxonomy rename <dict> <old> <new>` — rename a value, ripples to every referencing paper.
- `lit taxonomy merge` — fold one or more near-duplicate sources into one destination, re-tags every referencing paper, drops the sources.
- `lit taxonomy rm <dict> <value>` — remove a value, strips it from every referencing paper.

Rules:

- **NEVER hand-edit `TAXONOMY.md`** — always the atomic CLI.
- **Tier 3 (cascades to N papers)**: before running, **show the impact** ("merge folds `tokenisation` into `tokenization` and re-tags the N papers using it") and **ask once**. `merge` / `rm` prompt `Continue? [y/N]`; in a non-interactive run you MUST pass `--yes` / `-y` or the command aborts. On confirm, run + relay, then **re-read `TAXONOMY.md`**.
- **Never decide a consolidation yourself** — which values "mean the same thing" is a vocabulary judgment the user owns. You MAY propose a merge when you spot near-duplicates (e.g. in Flow A), but propose only.
- **dict routing**: these verbs operate on `topics` / `methods` / `data`. The `projects` dict is governed through `lit project` ([H]), not `lit taxonomy`.

---

## Architecture Invariants (do not violate)

1. **Never** write `papers/<id>/metadata.yaml`, `TAXONOMY.md`, `INDEX.json`, or `codes/<name>/repo-meta.yaml` directly. Always go through `lit add` / `lit modify` / `lit taxonomy` / `lit project` / `lit code …`.
2. **Never** suggest hand-editing `TAXONOMY.md` or `lit-config.yaml`'s `projects:` map. Use `lit taxonomy {rm,rename,merge}` for topics/methods/data ([J]) and `lit project {add,rename,set-path,rm}` for projects ([H]). Tagging requires the value registered first.
3. **Never** assume the vault is git-tracked. It is deliberately not.
4. **Never** store API keys in `lit-config.yaml`. The CLI calls no LLM API — that's the agent's job, via the JSON-file bridge.
5. **Never** install / uninstall litman or modify its conda env. If `lit` is missing, tell the user and stop.
6. **Verify every `[[X]]` wikilink against the filesystem before you write or keep it.** When you rewrite a `notes.md` / `discussion.md`, for each `[[X]]` you emit or preserve, check that `papers/X/` exists (`lit show X` resolves, or it appears in `lit list`). If not, write it as `[[X]] (deleted)` — **never emit a bare `[[X]]` for a paper not in the vault.**
7. **Never** set a reverse relation field (`extended-by` / `contradicted-by`) by hand — drive only the forward field and let the CLI maintain the pair ([E]).

If unsure whether an operation respects these, run `lit health-check` after — it surfaces vault drift and the user inspects before acting.

**Staleness nudge.** When `lit` output ends with the line `tip: no lit health-check in 14+ days. Run it to catch silent drift.`, relay it to the user and offer to run `lit health-check` — never auto-run it.

---

## Quick Reference

| Command | What it does |
|---------|-------------|
| `lit init [--name <vault>]` | Create a new vault skeleton |
| `lit add <pdf> --doi <doi>` | Add via CrossRef ([B]) |
| `lit add <pdf> --from-llm-json <json>` | Add via LLM-extracted JSON ([A]) |
| `lit pdf-text <pdf> [--pages 1-3]` | Dump PDF text layer (deterministic read fallback, [A] step 1) |
| `lit list [filters] [--format json]` | Browse ([D]) |
| `lit show <id-or-substring>` | Single-paper metadata (fuzzy substring OK; `--paper-doi` supported) |
| `lit modify <id> --set k=v --add-tag list=v` | Edit fields / tag ([E]) |
| `lit read / revisit / drop / promote / skim <id>` | Status & date sugar ([E]) |
| `lit taxonomy {list,add,rename,merge,rm} <dict> [args]` | Topics/methods/data vocab; merge/rm prompt — pass `--yes` non-interactively ([J]) |
| `lit project {add,list,rename,set-path,rm} [args]` | Project registry ([H]) |
| `lit link / unlink <id> --project <name>` | Bind / unbind paper↔project ([H]) |
| `lit export (--project <p> \| --all) [filters] [-o file]` | Project vault → `.bib` ([G]) |
| `lit code add <url> --paper <id>` | Clone + bind a code repo ([C.1]) |
| `lit code link <repo> --paper <id>` | Bind an existing vault repo (1:N — [C.2]) |
| `lit code list [--paper <id>]` | Browse code repos |
| `lit code rm <repo> --cascade` | Retire a repo (last citer only — [C.3]) |
| `lit health-check` | Vault consistency report |
| `lit rename <old> <new>` | Atomic id rename with cascade |
| `lit rm <id> [--cascade] [--purge]` | Soft-delete (trash) or purge |
| `lit trash {list,restore,empty}` | Trash bin; `restore` rebuilds relations + re-clones ([I]) |
| `lit config show` | Print parsed `lit-config.yaml` |
