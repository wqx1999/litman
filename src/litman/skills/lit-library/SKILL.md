---
name: lit-library
description: "Write side of the litman vault — drives the `lit` CLI to change the library. Use when the user wants to add a paper to their **global** vault, browse/filter all papers, tag with controlled vocabulary, bind code repositories to a paper, export a BibTeX file for writing, manage the project registry, govern the TAXONOMY, or restore a deleted paper. Triggers: 'add to lit library', '加到文献库', 'lit add', 'show me all my papers on X', 'tag/classify this paper', 'link this code repo to that paper'; export bib ('导出 bib', 'export refs', 'thesis bib', '准备引用', 'refs.bib'); project lifecycle ('新建项目', 'register a project', 'rename/delete the project', 'the project moved', 'list my projects'); TAXONOMY governance ('合并这两个 topic', 'merge/rename a tag value', '删掉这个 topic/method'); restore a trashed paper ('恢复删掉的论文', 'restore from trash'). NOT for discussing/comparing/summarizing a paper you are reading — that's the lit-reading skill. NOT for project-local References/ folders — that's the ref-manager skill."
---

# lit-library — Global Literature Vault Skill

This skill drives the `lit` CLI (the litman tool) to manage a **global, local-first literature vault**. The vault is one directory that stores every paper the user has ever read, with controlled-vocabulary tagging (TAXONOMY), bidirectional cross-references, and optional code-repo bindings. It is the **write side** of litman; its read-side companion is **lit-reading** (discussion, comparison, navigation).

## Architecture you must respect

1. **Two-root layout.** The vault directory (e.g. `/work/wangq/literature_vault/`) is a *different filesystem location* from the litman code repo (`/work/wangq/Project/litman_dev/`). Always discover the vault via `$LIT_LIBRARY` env var or `lit-config.yaml` walk-up — never assume a relative path.
2. **LLM never writes data files directly.** When you extract metadata from a PDF, write it as **JSON** to a temp file, then call `lit add --from-llm-json <path>`. The CLI validates the schema and writes `papers/<id>/metadata.yaml`. Never write yaml/markdown into the vault yourself.
3. **TAXONOMY is mutated only via `lit taxonomy {add,rename,merge,rm}`** (and `lit project` for the `projects` dict). Hand-editing `TAXONOMY.md` leaves dangling references.
4. **Vault is NOT git-tracked.** Atomicity comes from `<vault>/.litman-staging/` + `os.replace`, not git.
5. **Agent-writable free-form = `notes.md` (overwrite) + `discussion.md` (append) only.** Every other file in `papers/<id>/` and `codes/<name>/` is structured and mediated by the CLI. The three paper-to-paper relation fields you may drive are the **forward** ones (`related` / `extends` / `contradicts`); the CLI auto-maintains the paired reverse fields (`extended-by` / `contradicted-by`) — never set a reverse field by hand.

If a user request would violate any of the above, push back and propose the CLI-mediated alternative.

---

# PART A — Autonomy, chaining, scope (read this first)

## A1. Autonomy ladder

Classify every action before you take it. The tier is about *behavior*, not which skill executes.

| Tier | Operation class | Behavior | Examples |
|---|---|---|---|
| 1 | **Read** | Just do it, don't ask | `lit list`, `lit show`, scan a PDF, query INDEX via `lit list --format json`, `lit project list`, `lit code list`, `lit trash list`, `lit health-check` |
| 2 | **Write, reversible, single-paper** | Do it, then report | `lit modify --set priority=A`, `lit modify --add-tag topics=X` *(only after the value passed Flow A/B — [E])*, `lit read`/`skim`/`promote`/`drop`/`revisit`, `lit link`/`unlink` *(paper↔project, only on explicit request — [H])*, `lit code link`, `lit modify --set relevance-<P>=` |
| 3 | **Write, multi-paper / structural / remote-IO** | Ask once before acting | `lit add` (confirm title before commit — [A]/[B]), `lit code add` (git clone), `lit taxonomy add` / `lit project add` (register a new controlled value — user types it), `lit taxonomy merge`/`rename`/`rm` + `lit project rename`/`rm` (governance — cascades to every referencing paper, see [J]/[H]), `lit export --force` (overwrite a hand-edited bib — [G]) |

**M15 already physically split tier 2 from tier 3.** Registering a controlled value (`lit taxonomy add` / `lit project add`, tier 3, *the user types it*) is a separate hard step from applying it (`lit modify --add-tag`, tier 2, *you run it*). The CLI hard-rejects unregistered values, so your only job is to **not invent values** (curation boundary) — the CLI polices registration.

**Execution ownership.** **lit-library runs every Tier-2 write INLINE — it owns the write surface.** lit-reading owns only the single-paper *evaluation stamps* (`lit read`/`promote`/`skim`/`drop`/`revisit`, `lit modify --set priority=`) inline, and chains to lit-library for everything else (vocab tagging, edges, project binding, ingest, restore, governance). When lit-reading hands one of those off to you, just run it (see A2 inbound-defense).

## A2. Chain hand-off contract

Switching skills is a real, explicit `Skill` tool call — there is no call stack, no auto-return. A target skill's `description` can never trigger a mid-task chain (descriptions match only on a *new user message*); the chain works **only because a body literally tells the agent to chain**.

**Direction is asymmetric: the main chain is `lit-reading → lit-library`** (a reading discussion discovers a needed write). The reverse rarely needs a real switch — once you finish the write, the result (e.g. a new id) is already in context and the reading SOP continues. Do **NOT** add any "can be summoned by lit-reading" cue to this skill's `description`; it pollutes the activation surface for zero benefit (the chain is body-level only).

**Inbound-defense (the rule that matters most on the write side):** *if you are summoned mid-discussion from lit-reading, do the one write you were handed, report the result concisely, and hand control back.* Do **NOT** start a fresh interactive `lit add` session or a multi-step ingest flow that swallows the conversation — the user is reading, not re-cataloguing. The hand-off carries: the paper id, the exact intent (e.g. "add `extends=<other-id>`"), and any user instruction already given; resume by returning the result to the still-resident lit-reading SOP.

## A3. Scope discipline — SOP vs maintenance, reversibility, active-vault confinement

**(1) SOP = high-frequency literature actions; maintenance falls through to `lit <cmd> --help`.** The Operation Routing branches below make you fluent at the high-frequency write actions this skill is for — ingest, tag, link, bind code, export, govern the vocabulary, restore. **Low-frequency vault-maintenance** — cloud sync (`lit sync`), switching the active vault (`lit vault use`), config edits (`lit config`), view rebuilds (`lit refresh-views`), and any command with no named branch below (`lit init`, `lit install-skill`, `lit vault add`/`info`/`remove`, `lit code update`/`restore-all`) — gets **no SOP**. This is not a refusal: the SOP is the expert shortcut, `lit <cmd> --help` is the universal fallback, the CLI is complete on its own.

- **Read vs write within the no-SOP set.** A no-SOP command that is a pure *read* — `lit code list`, `lit vault info`, `lit vault list`, `lit config show` — stays **Tier 1: just run it and report**. Lacking a narrative SOP lowers your *fluency at composing the call*, not a read's autonomy tier.
- **"Teach, don't do" applies only to no-SOP *writes / maintenance*.** When the user asks for a maintenance action with no SOP, understand the intent → run `lit <cmd> --help` to find the real command/flags → **surface the command for the user to run**, don't run it. Execute it yourself **only when the user explicitly asks** ("you run it" / "just do it"). Either way, **never guess a flag or invent a command from memory** — a wrong maintenance command corrupts the library.

**Destructive operations split by reversibility:**

- **Soft-delete `lit rm`** (default → moves `papers/<id>/` into `.trash/`, recoverable via `lit trash restore`; recovery SOP at [I]). You **never initiate** it (trigger is the user's), but **may execute it on an explicit, confirmed request**, and must **relay `lit rm`'s cascade report verbatim** ("This paper is linked with N entries…") — never silently delete, never summarize the link count away. (The report is CLI-computed; relaying it is transparency. The real backstops are default-deny + reversibility: `lit rm` without `-y` aborts on a non-TTY stdin, and soft-delete is reversible.)
- **Irreversible removal** (`lit rm --purge`, `lit trash empty`): you **NEVER execute these, even on explicit request.** Your deletions are always the recoverable soft-delete; permanent destruction is user-sovereign. Surface the exact command and let the user run it. *The user holds absolute sovereignty over their own knowledge base; the agent and litman help manage the vault, they never irreversibly destroy its contents.*
- **`lit code rm`** stays governed by [C] (confirm before execute; the clone is re-cloneable from upstream, so it is not in the never-execute tier).

**(2) Write only in the active (primary) vault.** Every write — ingest, modify, link, code add, taxonomy, and even appending to `notes.md` / `discussion.md` — targets the **currently-active vault only**. Cross-vault *reading* is fine; cross-vault *writing* is forbidden. If the user's intent requires writing into a different vault, do **not** switch silently — **tell the user to switch first** (`lit vault use <name>`, a user-owned maintenance action) and only then operate in what is by then the active vault.

---

## How to detect the vault

Run from any directory:
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

- **[A] Add paper from a PDF (LLM-augmented)** — user gave you a PDF and wants it in the vault, no DOI handy or DOI fetch failed.
- **[B] Add paper from a DOI (direct CrossRef)** — user gave a DOI; no LLM extraction needed.
- **[C] Code repositories** — bind a new clone, bind an existing repo (1:N), or retire/unbind.
- **[D] Browse / search / inspect** — find a paper, list by filter, show metadata.
- **[E] Modify metadata, tag, or apply an edge** — change fields, Flow A/B tagging, apply a confirmed knowledge-graph edge.
- **[G] Export bib for writing** — project the vault out to a `.bib`.
- **[H] Project operations** — register / link / unlink / list / rename / delete / set-path; the one place for anything project-related.
- **[I] Restore a trashed paper** — execute the restore lit-reading B13 confirmed.
- **[J] TAXONOMY governance** — merge / rename / remove a controlled value.

**SOP-1 (hard rule, applies to every branch):** **the agent does not proactively tag a paper after reading it.** Listing observations ("this touches tokenization, evaluation") is fine — *listing ≠ tagging*. Tagging always goes through Flow A or Flow B ([E]). The same posture extends to edges, project links, and code binds: you **propose**, the user decides, then you run the CLI. Never self-initiate a structural write.

---

## [A] Add Paper from a PDF (LLM-augmented)

This is the **headline workflow** for lit-library. Use when the user has a PDF and no DOI (e.g. preprint, internal report, paywalled paper where CrossRef fails) OR explicitly says "add this paper with AI".

**PDF-required precondition.** `lit add` takes the **local PDF path as a required positional argument** (`lit add [OPTIONS] PDF_PATH`); a DOI / arXiv link alone cannot ingest. This is intentional (ADR-006: curation means you have *read* the PDF — litman is not a fetch-by-DOI discovery tool). If the user asks to add a paper but supplies only a DOI / URL with **no local file**, do **not** let `lit add` error out on the missing argument — explain the precondition ("litman ingests from a local PDF you've read; point me at the file") and ask for the path. (B9 mirrors this on the read side.)

**Pipeline**:

1. **Read the PDF** — a *must-achieve* goal, not best-effort. Walk this ladder until one rung works; **never stop at rung 1's failure** and tell the user "I can't read PDFs":

   1. **Claude Code `Read` tool** (default — ~99% of CC users with any vision-capable Claude model): `Read(pdf_path, pages="1-3")`. PDFs are natively handled via CC's multimodal pipeline; do NOT try external CLIs first.
   2. **PDF-related MCP tool** (for non-multimodal backends — DeepSeek / GLM / Qwen / etc.): scan your current `available tools` list for any tool whose description mentions PDF / document / extract, and use that.
   3. **System CLI fallback** (probe with `command -v` first, then run the first available): `pdftotext -layout <pdf> -`, `mutool draw -F txt <pdf> -`, or `python -c "from pypdf import PdfReader; print(PdfReader('<pdf>').pages[N].extract_text())"`.
   4. **Tell the user what's missing — with concrete install commands for their OS** (`brew install poppler` / `apt install poppler-utils` / `dnf install poppler-utils` / `scoop install poppler`), or suggest switching to a vision-capable model. Never report a vague "can't read PDF" — name the rung that failed and the exact gap.

   Extract: title (page 1 header), authors (page 1 list — preserve "Family, Given" order), year, DOI (search first 1-2 pages — many PDFs print "DOI: 10.xxx/yyy"), journal / venue, abstract.

2. **Verify**, do NOT hallucinate. If you cannot find a field after reading the relevant pages, leave it as `null` rather than guessing. A wrong DOI corrupts the vault's dedup index for that paper forever.

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

4. **Call the CLI**:
   ```bash
   lit add <pdf-path> --from-llm-json $TMP_META
   ```

5. **The CLI handles**: JSON schema validation (rejects unknown keys / missing required fields), DOI dedup precheck, id derivation (`<year>_<Family>_<title-keyword>`), id-collision resolution (`--auto-suffix` for `_b` / `_c`), atomic write of `papers/<id>/{paper.pdf, metadata.yaml, notes.md}`.

6. **Confirmation gate (mandatory — human in the loop).** `lit add` prints a success panel and runs the M20 code-URL scan. **Read out the derived `id` and the `title`, then stop and wait for the user to confirm the source metadata is right.** You do **not** self-judge title correctness — the human confirms. **Surface only id + title.** After the user confirms:
   - if the scan found candidates → present them ([C] scan present-and-pick);
   - otherwise stop. Do **NOT** proactively enumerate tag / project / status / priority offers — those are separate intents the user will trigger when they decide to read / curate the paper. SOP-1 governs: list observations only when asked, never pre-stage menus the user did not request.

**Duplicate-add path.** `lit add` prechecks the DOI and **refuses** with a `DuplicateDOIError` naming the existing id when the paper is already in the vault. Do **not** retry or force a second copy — **relay "already in your vault as `<id>`" and route to *reading* it** (chain to lit-reading Phase 2 with that id). Re-adding a paper you already have is almost always intent to open it, not duplicate it (invariant #13 — one paper, one folder). This is the ingest-side counterpart to B9's in-vault check.

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

Unknown keys are rejected (`extra="forbid"`). Topics/methods/data are deliberately NOT in this schema — classification is a follow-up `lit modify --add-tag` through Flow A ([E]), so the schema cannot let you write taxonomy values blindly.

### When `lit add --from-llm-json` errors

- **`DuplicateDOIError` / "DOI 'x' already registered"** → see the Duplicate-add path above (relay + route to reading; never force a second copy).
- **"Metadata from LLM JSON has no year"** → re-read the PDF for a year, or ask the user, or pass `--id 2024_Family_Keyword` explicitly.
- **"field 'title': String should have at least 1 character"** → schema rejected an empty field; re-extract.

### Title / id rollback (when the confirm gate fails)

`id` and `title` are **decoupled**: `lit modify --set title=` changes the field without touching the id; `lit rename` changes the id handle (atomically rippling every back-reference) without touching the title; the id is derived locally by `derive_id` as `<year>_<Family>_<Keyword>`. Three branches:

1. **Title field wrong** (almost only on the `--from-llm-json` path; `--doi`/CrossRef is authoritative) → `lit modify <id> --set title=<correct>`.
2. **Door-plate keyword unsatisfying** (decoupled from title-correctness; caused by local keyword truncation, possible even on the CrossRef path) → `lit rename <old> <new>`. **Suggest** a new id following `<year>_<Family>_<Keyword>`, label it "computed, confirm or change", and let the user decide (the id is a deterministic derivation, not a controlled vocabulary — suggesting it does not violate "don't guess controlled values").
3. **`lit rm` does NOT participate** — it is deletion semantics; as a rollback it would wrongly discard accumulated state and hit the reference safety net.

---

## [B] Add Paper from a DOI (direct, no LLM extraction)

If the user gives a DOI (or an arxiv id you can resolve to a DOI), skip extraction entirely — CrossRef has authoritative metadata.

```bash
lit add <pdf-path> --doi 10.1093/bioinformatics/btae364
```

The pipeline is identical from id derivation onward; only the metadata source differs. This is faster, more accurate, and preferred whenever a DOI is available. The PDF-required precondition still holds — a DOI alone, with no local file, cannot ingest (see [A]).

**Confirm gate (narrower than [A], still required).** On the CrossRef path the *title* is authoritative, so you do not re-confirm it — but the door-plate keyword is still derived **locally** and can be unsatisfying even when the title is right. So **read out the derived `id` and stop**; if the user is unhappy with the door-plate, that is C3 branch 2 (`lit rename`). Net: both ingest paths end with a confirm gate — [A] confirms title + id, [B] confirms the id door-plate.

---

## [C] Code repositories — bind, link, retire

A paper's `code-clones` field is a **1:N** relationship: one repo under `<vault>/codes/<name>/` can be cited by several papers (invariant #12). Three operations:

### [C.1] Bind a NEW clone — present-and-pick from the full-text scan

`lit add` already runs the M20 `scan_code_urls` full-text recall and prints the result in a structurally-stable block you parse: fenced by the literal markers `[code_candidates]` / `[/code_candidates]`, **one candidate per line as `<url> (p<page>, ×<count>)`**, ranked by hit-count descending. The empty case prints a single `no code repo URL found in full text` line.

- **Non-empty** → present the deduped candidate list (url + page + hit-count; there is **no "original line" field** — the scan returns only `{url, page, count}`) and let the user **multi-select 0+**. Do **zero judgment / zero silent dropping / zero pre-filtering** — M20 over-captures (reference and dependency URLs are listed too); precision comes from the user's eyes, not your guess (invariant #5). Each selected item runs:
  ```bash
  lit code add <url> --paper <current-id>      # clone (--depth 1) + bind, atomic
  ```
  This creates `codes/<repo-name>/{repo/, repo-meta.yaml, notes.md}` and bidirectionally binds the paper's `code-clones` ↔ repo-meta's `papers`. **Tier 3** (git clone = remote IO). Binding one paper to multiple repos does not violate invariant #13 — ingest is still one paper.
- **Empty scan → do not prompt at all.**

Inspect with `lit code list --paper <paper-id>` (tier-1 read). Pull updates with `lit code update <name>`.

### [C.2] Bind an EXISTING vault repo (the 2nd+ paper in a 1:N)

When the repo is **already cloned** in `<vault>/codes/` and the user wants another paper bound to it (a follow-up paper, a shared utility lib):

```bash
lit code link <repo-name> --paper <id>       # bind only, no clone
```

**Tier 2** (pure bind, no remote IO, single-paper reversible — contrast `lit code add`'s clone = tier 3). **Trigger = explicit user request** — never auto-bind or self-initiate (reuse the [H] `lit link` posture). *(Auto URL routing — agent decides add-vs-link automatically — needs `lit code list --format json`, which does not exist yet; deferred, not in this milestone.)*

### [C.3] Retire / unbind — read the reverse list FIRST

Before unbinding, do a **deterministic table lookup, not a model guess**: `cat <vault>/codes/<repo>/repo-meta.yaml`, read the `papers:` reverse list (the M14 truth source), remove the current paper, then branch:

- **Reverse list now empty** (current paper is the last citer) → unbinding the field alone would orphan the directory into a dangling clone. Retire the whole repo:
  ```bash
  lit code rm <repo> --cascade        # strips every binding, then removes the directory
  ```
  Directory removal is destructive → deletion class: **ask the user before running** (A3 principle 1).
- **Reverse list still non-empty** (other papers still cite it) → unbind this paper only:
  ```bash
  lit modify <id> --rm-tag code-clones=<repo>     # drops the field, keeps the directory
  ```
  This is *correct* under invariant #12's 1:N semantics (keeping the directory for the others is the point, not a violation). **Tier 2** (single-paper field edit, reversible).
- **Never** `lit code rm --cascade` while other papers still cite the repo, and **never** leave a `--rm-tag` orphan directory when unbinding the last citer (both produce a state `lit health-check` flags).
- `lit unlink` is paper↔project ([H]), **not** for code — do not use it to unbind a repo.

---

## [D] Browse / Search / Inspect

Most browsing is a single Tier-1 CLI call. Don't write a wrapper — call `lit list` directly with the right filters.

```bash
lit list                                     # full vault
lit list --topic transformer --year ">=2023" # filter
lit list --status deep-read --priority A     # by personal evaluation
lit list --project pepforge --format json    # papers bound to a project (bounded retrieval)
lit show Pandi                               # fuzzy: unique substring of id
lit show 2023_Pandi_Cell-free                # exact id also works
lit show --paper-doi 10.1038/...             # DOI reverse-lookup
```

The paper-id input on every `lit` command that takes one (`show`, `open`, `modify`, `rm`, `rename`, `link`, `unlink`, `code link`, `code add --paper`, `code list --paper`) accepts (a) the full id, (b) a unique case-insensitive substring, or (c) `--paper-doi <DOI>` (a separate option, mutually exclusive with the positional / `--paper` channel). Ambiguous substrings (2+ matches) print the candidate list and exit non-zero. `lit rename <old> <new>` is the one exception: no `--paper-doi`, because two positionals make Click's parser unable to disambiguate.

For "find a paper I read last month about X", scan `lit list --format json` and grep semantically — this is where the LLM adds value. (For large vaults prefer a `--topic` / `--project` filter so the CLI prunes file-side rather than loading the whole INDEX.) An **author** cue uses `lit list --author <cue>` — the JSON projection rows do not echo `authors`, so grepping the rows for an author name finds nothing; the `--author` filter matches file-side.

---

## [E] Modify Metadata, Tag, or Apply an Edge

### SOP-1 — never proactively tag (restated; the hard rule)

The agent does **not** proactively tag a paper after reading it. Listing observations is fine; *listing ≠ tagging*. Every tag goes through Flow A or Flow B below.

### Flow A — user says "tag this / classify it" (no specific value)

Propose candidates **only from registered values** → user picks → `lit modify --add-tag`. **Where the registered set comes from (source asymmetry — state it once):**

- `topics` / `methods` / `data` → read the **in-context `TAXONOMY.md`** (it was loaded into context for the session; `lit taxonomy list <dict>` is the equivalent CLI, but reading the already-resident file is the deliberate cheaper path).
- `projects` → the canonical set is **`lit project list`, NOT the file** — it JOINs `TAXONOMY.md`'s `projects` section with the config map (whose on-disk-path half does not live in `TAXONOMY.md`), so a file read would be incomplete (see [H]).

```bash
lit modify <id> --add-tag topics=peptide-LM --add-tag methods=transformer
lit modify <id> --rm-tag topics=outdated-value
```

While enumerating candidates you MAY **propose** a TAXONOMY merge if you spot near-duplicate registered values ("your `topics` dict has both `tokenization` and `tokenisation` — merge them?"), but only propose — governance runs through [J].

### Flow B — user names a value ("add tokenization")

Check if it is registered. **Registered → apply.** **Not registered → the CLI HARD-REJECTS** (M15, no escape hatch); say so and route registration by dict:

```bash
# topics / methods / data — register via lit taxonomy:
lit taxonomy add topics peptide-LM
lit modify <id> --add-tag topics=peptide-LM            # now allowed

# projects — register via lit project (NOT lit taxonomy):
lit project add pepforge --path /abs/path/to/pepforge
lit modify <id> --add-tag projects=pepforge            # now allowed
```

After the user registers, **re-read `TAXONOMY.md`** (cache invalidation) and then apply.

### Register-first (MANDATORY for the four controlled dicts)

`projects` / `topics` / `methods` / `data` are **controlled vocabularies**. `lit modify --add-tag <dict>=<value>` HARD-REJECTS a value not already registered in `TAXONOMY.md`. There is deliberately no `--register` escape hatch — registration is a separate, explicit step (Flow B above). `projects` is special: it carries an on-disk path binding, so it has its own group `lit project {add,list,rename,set-path,rm}` ([H]) that keeps `TAXONOMY.md` and `lit-config.yaml`'s `projects:` map atomically in sync. **`lit taxonomy {add,rename,rm} projects` is hard-deprecated** — it errors and redirects to `lit project`. (`lit taxonomy list projects` still works — read-only.) Never hand-edit `lit-config.yaml`'s `projects:` map.

What is NOT register-first checked (do not try to "register" these): schemaless scalar fields (`read-date`, `doi`, `year`, any custom scalar — invariant #7), reference fields (`authors`, `related`, `contradicts`, `extends`, `code-clones` — validated by dangling-ref health checks), and fixed enums (`type`, `status`, `priority` — hard-coded). `--rm-tag` is never register-checked (clearing a stale value is legitimate).

### Sugar commands — prefer over `lit modify --set` for known semantic fields

Five one-shot commands compress the most common `lit modify --set` patterns; prefer the sugar whenever it applies (removes recall-field-name + compute-date + assemble-syntax steps, and kills a class of typos):

```bash
lit read <id> [--date YYYY-MM-DD]   # stamp read-date (defaults to today; --date backdates)
lit revisit <id>                    # stamp last-revisited = today (distinct field, invariant #11)
lit drop <id>                       # status = dropped
lit promote <id>                    # status = deep-read  (does NOT also stamp read-date)
lit skim <id>                       # status = skim
```

Same-day / same-value repeats are no-ops (`lit read X` twice the same day does not bump `updated-at`). For `priority` or an arbitrary scalar, fall back to `lit modify <id> --set priority=A`.

### Apply a knowledge-graph edge (inbound from lit-reading B7)

When lit-reading hands off a **user-confirmed** edge, run the **forward** field only:

```bash
lit modify <id> --add-tag extends=<other-id>      # or related=<other-id> / contradicts=<other-id>
```

After M23.0 the CLI mirrors the paired reverse field on the opposite paper automatically (`extends` → `other.extended-by`, `contradicts` → `other.contradicted-by`, `related` self-paired) inside the same `staged_write` — **never run a second command for the reverse, never set `extended-by` / `contradicted-by` directly** (the CLI does not expose them as tag targets). Edges are reference fields (not register-first checked). You apply **only** an edge the user already confirmed in the reading discussion — you do not originate edges here.

---

## [G] Export bib for writing

Project the vault out to a `.bib`. The user expresses *intent*; you translate it to `lit export` flags.

| User says | Command |
|---|---|
| "导出和 pepforge 有关的文献到这里" | `lit export --project pepforge` (defaults to `./refs.bib`) |
| "写 thesis，把 priority A 的都导出来" | `lit export --all --priority A -o thesis.bib` |
| "给 PepCodec 准备 bib" | `lit export --project pepcodec` (canonicalize the project token first — Flow B / [H]) |
| "更新一下 refs.bib" | infer current project → `lit export --project <inferred>`; if not inferrable, ask |

Real flags: `--project` XOR `--all` (exactly one required), `-o/--output` (default `./refs.bib`), `--priority` / `--status` / `--year` / `--type` (comma-separated; values within one flag OR-combine, across flags AND-combine), `--force`, `--vault`. Cite keys equal paper ids, so output drops into `\cite{<paper-id>}` directly; re-running on the same file is the supported update path.

Three hard rules:

1. **Sentinel rejection → NEVER auto-add `--force`.** When the CLI refuses to overwrite a target lacking the litman sentinel (typically a hand-edited `references.bib`), relay that verbatim and let the user decide — `--force` discards their hand edits.
2. **Path inference**: "current dir" / "here" → default `./refs.bib`; a *named* directory ("thesis dir") → ask for the path, do not guess.
3. **Project token**: an unregistered `--project` gets deterministic canonicalization (case/whitespace) only, else present the registered set — same no-fuzzy-guess rule as [H].

Tier: the projection is tier 2 (local, reversible file write); the `--force`-over-sentinel decision is a **tier-3 ask** (never silent). This is read-vault → write-disk projection, no ingest, so it does not touch ADR-006 curation.

---

## [H] Project operations

The single place to reach for anything project-related: register / link / unlink / list projects / list a project's literature / rename / delete / set-path. Each operation carries its own autonomy tier.

- **Register** — `lit project add <name> --path <abs>`. **Tier 3** (registers a new controlled value; the **user supplies the path — never guess it**). This is the same register-first instance Flow B routes to. (`lit taxonomy add projects` is hard-deprecated; projects only via `lit project`.)
- **Link** — `lit link <paper-id> --project <name> [--relevance "..."]`. Atomically adds the project to the paper's `projects:`, builds the vault-side symlink, builds `<project>/literature/<paper-id>/` → symlink back to vault, and regenerates `<project>/REFERENCES.md`. **Trigger ownership = user** (only on explicit request; never self-initiate — see B6). **Tier 2** (local, single-paper, highly reversible via `lit unlink` / `lit link --rebuild-all`; writing into the user's project dir does NOT escalate the tier — the project was authorized at `lit project add --path`, and what's written is litman's own rebuildable `literature/` symlink hub + `REFERENCES.md`, not user content). Unregistered `--project` → Flow B routing (`lit project add`, NOT `lit taxonomy add`).
- **Unlink** — `lit unlink <paper-id> --project <name>`. **Tier 2**, reversible. (Not for code — that's [C].)
- **Update relevance after linking** — `lit modify <paper-id> --set relevance-<project>=…` sets/edits the per-project relevance note **without re-linking**. **Tier 2.** This is the after-the-fact / update path (at link time, prefer the inline `lit link --relevance "..."`); it is where the B10 product-4 self-check routes when a paper is linked but `relevance-<project>` is blank.
- **List projects** — `lit project list`. **Tier 1** read, the **canonical source for both the registered set AND each project's path**: three columns — `name` / `path` / `status` (the drift marker `✓` / `⚠ path-missing` / `⚠ config-only` / `⚠ taxonomy-only`) — as a JOIN of `TAXONOMY.md`'s `projects` section + the config map. Whenever a consumer needs the registered set OR to resolve a project name → its on-disk dev_docs directory (lit-reading Phase 4 / B5), **both come from this one command.** Do NOT hand-parse `lit-config.yaml`, do NOT route the path through `lit config show` (it collapses `projects` into one `{}` cell). *(It is a Rich table, not JSON; `lit project list --format json` is deferred, not in this milestone.)*
- **List a project's literature** — `lit list --project <name>` (**Tier 1** browse; supports `--format json`). The per-project view of *papers*, distinct from `lit project list` which lists the *projects* themselves. This is the command behind "what literature does `<project>` cite"; the CLI-generated `<project>/REFERENCES.md` is an equivalent human-readable view.
- **Rename** — `lit project rename <old> <new>`. **Tier 3 governance** (one transaction cascades: TAXONOMY + config key + every referencing paper's `projects:` + INDEX). Reuse the [J] governance discipline: never hand-edit, **show the impact** ("renames `<old>`→`<new>` across the N papers using it"), **ask once**, never decide the rename yourself; re-read TAXONOMY afterward.
- **Delete** — `lit project rm <name>`. **Tier 3 + destructive** (cascade-untags every paper, drops the project from both truth sources). Treat like deletion (A3): **never initiate it**, and **show the impact + confirm before executing** even on explicit request. This is project-registry removal (re-registerable), NOT paper deletion — it trashes no paper.
- **Set path** — `lit project set-path <name> <abs>`. **Tier 2** (config-only; papers store no absolute path, so this is a localized reversible edit). For when the project dir moved; the **user supplies the new path** (never guessed); no cascade.

Governance discipline (rename / rm) is shared with [J] — cross-reference, do not duplicate. The whole branch reuses present-and-user-picks: you surface candidates / impact, the user decides.

---

## [I] Restore a trashed paper

After lit-reading B13 hands off a **user-confirmed** paper id (find + confirm happen on the read side), execute the restore and relay the outcome:

```bash
lit trash restore <id>          # or the full entry name <id>-<UTC-timestamp>
```

What the command does (one atomic `staged_write`, M23.2 — do NOT re-implement or second-guess it):

- Accepts the **paper id** (must be unambiguous) or the **full entry name** `<id>-<UTC-timestamp>`. If the same id was deleted more than once, the CLI raises with the list of entry names — **relay it and ask which timestamp**, do not guess.
- **Step 0 — id-slot collision**: if `papers/<id>/` already holds a LIVE paper, restore **REFUSES** (never clobbers). Relay the error; tell the user to rename / remove the active paper first. Do not force.
- **Steps 1-2 (atomic)**: moves `papers/<id>/` back; rebuilds every opposite paper's paired reverse edge from A's own sealed fields (M23.0 symmetry); re-binds surviving repos' `repo-meta.papers`; **silently drops** edges whose opposite is no longer in the library; de-annotates `[[A]] (deleted)` → `[[A]]` across notes (M24); refreshes INDEX/views.
- **Step 3 — re-clone is built INTO restore, NOT a separate `lit code add`.** A repo that was A's *sole* binder (1:1) got hard-deleted at rm time; its upstream URL was preserved in the trash sidecar (`orphan_repos`). Restore re-clones it: **prompted per repo (default Yes) interactively, or auto with `-y`**. On refuse / clone failure the binding `A.code-clones:[X]` is **KEPT** + a warning emitted (health-check backstops); re-clone is **never a precondition** for restore success.

Agent behavior around the re-clone sub-decision (where the autonomy ladder bites):

- Before running, you MAY read the sidecar `<vault>/.trash/<entry>.meta.yaml` `orphan_repos` map to tell the user up front "restoring also re-clones `<repo>` from `<url>`" (a read; tier 1).
- **TTY caveat** (same as deletion): driving `lit trash restore <id>` without `-y` hangs/aborts at the interactive re-clone `click.confirm` on a non-TTY stdin. So the agent path is: surface the re-clone target(s), get the user's nod, then run `lit trash restore <id> -y` (auto re-clone). If the user wants to restore but **skip** re-clone, note there is no `--no-reclone` flag today (interactive-only) — relay that and let the user run it themselves, or defer.
- **Tier 2** (local, reversible, single-paper; the re-clone sub-step is remote-IO but non-fatal). **Trigger = the user-confirmed identity from B13** — never restore on your own initiative, never pick the candidate for the user.
- **Relay the result summary verbatim**: reverse edges rebuilt in N papers, re-bound to N repos, re-linked into N projects (the CLI prints these; pass them through rather than paraphrasing).

---

## [J] TAXONOMY governance

Maintain the controlled vocabulary so it does not drift (near-duplicate values, typos, dead tags split and degrade bounded retrieval — the very thing TAXONOMY exists to make precise). 0 CLI — all three verbs shipped.

- **Trigger keywords**: "合并这两个 topic" / "merge X into Y", "把 X 改名" / "rename", "删掉这个 topic/method/值" / "remove this value".
- **Commands** (verify exact flag spelling with `lit taxonomy <verb> --help` before running — they exist, the precise syntax is not memorized here):
  - `lit taxonomy rename <dict> <old> <new>` — rename a value, ripples to every referencing paper.
  - `lit taxonomy merge` — fold one or more near-duplicate sources into one destination, re-tags every referencing paper, drops the sources.
  - `lit taxonomy rm <dict> <value>` — remove a value, strips it from every referencing paper.
- **Safety (invariant #2, hard line)**: NEVER hand-edit `TAXONOMY.md`; always the atomic CLI. Each verb cascades across the vault in one transaction — a hand-edit would leave dangling references.
- **Tier 3 (cascades to N papers)**: before running, **show the impact** ("merge folds `tokenisation` into `tokenization` and re-tags the N papers using it") and **ask once**. `merge` / `rm` are cascade-with-confirm (M15) and prompt `Continue? [y/N]`; in a non-interactive run you MUST pass `--yes` / `-y` or the command aborts. On confirm, run + relay, then **re-read `TAXONOMY.md`** (cache invalidation).
- **The agent never decides a consolidation** — which values "mean the same thing" is a vocabulary judgment the user owns (present-and-user-picks). You MAY **propose** a merge when you spot near-duplicate registered values (e.g. while enumerating candidates in Flow A), but propose only; never run governance on your own initiative.
- **dict routing**: these verbs operate on `topics` / `methods` / `data`. The **`projects` dict is governed through `lit project` ([H]), not `lit taxonomy`** (mirror the Flow B add-routing split; verify the project-lifecycle surface with `lit project --help`).

---

## Architecture Invariants (do not violate)

1. **Never** write `papers/<id>/metadata.yaml`, `TAXONOMY.md`, `INDEX.json`, or `codes/<name>/repo-meta.yaml` directly. Always go through `lit add` / `lit modify` / `lit taxonomy` / `lit project` / `lit code …`.
2. **Never** suggest hand-editing `TAXONOMY.md` or `lit-config.yaml`'s `projects:` map. Use `lit taxonomy {rm,rename,merge}` for topics/methods/data ([J]) and `lit project {add,rename,set-path,rm}` for projects ([H]) — both keep the truth sources atomic. Tagging requires the value registered first (register-first; no escape hatch).
3. **Never** assume the vault is git-tracked. It is deliberately not. Multi-file atomicity is `<vault>/.litman-staging/` + `os.replace`, not git.
4. **Never** store API keys in `lit-config.yaml`. The CLI calls no LLM API — that's your job (the agent), via the JSON-file bridge.
5. **Never** install / uninstall litman or modify its conda env. If `lit` is missing, tell the user and stop.
6. **Verify every `[[X]]` wikilink against the filesystem before you write or keep it.** When you rewrite a `notes.md` / `discussion.md`, for each `[[X]]` you emit or preserve, check that `papers/X/` exists (`lit show X` resolves, or it appears in `lit list`). If not, write it as `[[X]] (deleted)` — **never emit a bare `[[X]]` for a paper not in the vault.** The CLI maintains this `(deleted)` marker on `lit rm` / `lit trash restore` (ADR-013), but a full-note rewrite can wipe it; you are the second line of defence (this pairs with lit-reading B3's `notes.md` overwrite discipline — losing it reopens M24's hallucination hole, with `lit health-check` the only remaining backstop).
7. **Never** set a reverse relation field (`extended-by` / `contradicted-by`) by hand — drive only the forward field and let the CLI's atomic double-write maintain the pair ([E]).

If unsure whether an operation respects these, run `lit health-check` after — it surfaces vault drift (including missing / stale `(deleted)` tags and broken bidirectional pairs) and the user inspects before acting.

---

## Quick Reference

| Command | What it does |
|---------|-------------|
| `lit init [--name <vault>]` | Create a new vault skeleton |
| `lit add <pdf> --doi <doi>` | Add via CrossRef ([B]) |
| `lit add <pdf> --from-llm-json <json>` | Add via LLM-extracted JSON ([A]) |
| `lit list [filters] [--format json]` | Browse / bounded retrieval ([D]) |
| `lit show <id-or-substring>` | Single-paper metadata (fuzzy substring OK; `--paper-doi` supported) |
| `lit modify <id> --set k=v --add-tag list=v` | Edit fields / tag ([E]) |
| `lit read / revisit / drop / promote / skim <id>` | Status & date sugar ([E]) |
| `lit taxonomy {list,add,rename,merge,rm} <dict> [args]` | Topics/methods/data vocab; merge/rm prompt — pass `--yes` non-interactively ([J]) |
| `lit project {add,list,rename,set-path,rm} [args]` | Project registry (path-bound; dual-writes TAXONOMY + config — [H]) |
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
