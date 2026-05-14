---
name: lit-library
description: "Global literature vault management via the `lit` CLI (litman). Use this skill when the user wants to add a paper to their **global** literature library / vault, browse all papers across projects, manage taxonomy (projects/topics/methods), bind code repositories to papers, or recover the vault on a new machine. Triggers: 'add to lit library', '加到文献库', 'lit add', 'literature vault', 'add a paper to my vault', 'show me all my papers on X', 'link this code repo to that paper', 'restore my code repos', '跨机器恢复'. NOT for project-local References/ folders — that's the ref-manager skill."
---

# lit-library — Global Literature Vault Skill

This skill drives the `lit` CLI (the litman tool) to manage a **global, local-first literature vault**. The vault is one directory that stores every paper the user has ever read, with controlled-vocabulary tagging (TAXONOMY), bidirectional cross-references, and optional code-repo bindings.

## Architecture you must respect

1. **Two-root layout.** The vault directory (e.g. `/work/wangq/literature_vault/`) is a *different filesystem location* from the litman code repo (`/work/wangq/Project/litman_dev/`). Always discover the vault via `$LIT_LIBRARY` env var or `lit-config.yaml` walk-up — never assume a relative path.
2. **LLM never writes data files directly.** When you extract metadata from a PDF, write it as **JSON** to a temp file, then call `lit add --from-llm-json <path>`. The CLI validates the schema and writes `papers/<id>/metadata.yaml`. Never write yaml/markdown into the vault yourself.
3. **TAXONOMY is mutated only via `lit taxonomy {add,rename,merge,rm}`.** Hand-editing `TAXONOMY.md` leaves dangling references.
4. **Vault is NOT git-tracked.** Atomicity comes from `<vault>/.litman-staging/` + `os.replace`, not git.

If a user request would violate any of the above, push back and propose the CLI-mediated alternative.

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
| Add a paper to the **global** vault | **lit-library** (this skill) |
| Add a paper to a **project**'s `References/` dir | ref-manager |
| Cite in a `.bib` for a LaTeX manuscript | cite-retrieval |
| Review/critique manuscript text | paper-reviewer |
| Draft manuscript sections | academic-writing / paper-writer |

If the user has both a project `References/` and a global vault, ask which one they mean before proceeding.

## Operation Routing

- **[A] Add paper from a PDF (LLM-augmented)** — user gave you a PDF and wants it in the vault, no DOI handy or DOI fetch failed.
- **[B] Add paper from a DOI (direct CrossRef)** — user gave a DOI; no LLM extraction needed.
- **[C] Bind a code repository to a paper** — user wants the paper's `code-clones` field linked to a `codes/<name>/repo/` clone.
- **[D] Browse / search / inspect** — find a paper, list by filter, show metadata.
- **[E] Modify metadata or taxonomy** — change fields, add/rename/merge taxonomy values.
- **[F] Cross-machine recovery** — clone the vault metadata to a new machine, re-clone all code repos.

---

## [A] Add Paper from a PDF (LLM-augmented)

This is the **headline workflow** for lit-library. Use when the user has a PDF and no DOI (e.g. preprint, internal report, paywalled paper where CrossRef fails) OR explicitly says "add this paper with AI".

**Pipeline**:

1. **Read the PDF**. Use the `pdf` skill or your built-in PDF reading to extract:
   - Title (page 1 header)
   - Authors (page 1 author list — preserve "Family, Given" order)
   - Year (look for publication year; preprints may have v1 date)
   - DOI (search the first 1-2 pages — many PDFs print "DOI: 10.xxx/yyy")
   - Journal / venue
   - Abstract (the "Abstract" section, full text)

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

5. **The CLI handles**:
   - JSON schema validation (rejects unknown keys, missing required fields)
   - DOI dedup precheck (if DOI present)
   - Id derivation (`<year>_<Family>_<title-keyword>`)
   - Id-collision resolution (`--auto-suffix` for `_b` / `_c`)
   - Atomic write of `papers/<id>/{paper.pdf, metadata.yaml, notes.md}`

6. **Show the user the result**: print the panel output from `lit add`, then suggest next steps:
   - `lit modify <id> --add-tag topics=<value>` to classify
   - `lit modify <id> --set status=skim --set priority=A` to set personal evaluation
   - `lit code add <github-url> --paper <id>` if the paper has associated code

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

Unknown keys are rejected (`extra="forbid"`). If you want to suggest topics/methods, do it as a follow-up `lit modify --add-tag` call after `lit add` succeeds — the schema deliberately stops you from writing taxonomy values blindly.

### When `lit add --from-llm-json` errors

- **"DOI 'x' already registered"** → the paper is already in the vault. Show the existing id, ask the user if they want to inspect (`lit show <id-or-substring>` or `lit show --paper-doi <doi>`) or replace.
- **"Metadata from LLM JSON has no year"** → re-read the PDF for a year, or ask the user, or pass `--id 2024_Family_Keyword` explicitly.
- **"field 'title': String should have at least 1 character"** → schema rejected an empty field; re-extract.

---

## [B] Add Paper from a DOI (direct, no LLM extraction)

If the user gives a DOI (or an arxiv id you can resolve to a DOI), skip extraction entirely — CrossRef has authoritative metadata.

```bash
lit add <pdf-path> --doi 10.1093/bioinformatics/btae364
```

The pipeline is identical from id derivation onward; only the metadata source differs. This is faster, more accurate, and should be preferred whenever a DOI is available.

---

## [C] Bind a Code Repository to a Paper

When the paper's GitHub URL is on page 1 or in the abstract, offer to clone it after the paper is added:

```bash
lit code add <github-url> --paper <paper-id>
```

This creates `codes/<repo-name>/{repo/, repo-meta.yaml, notes.md}` with a shallow `--depth 1` clone (configurable via `lit-config.yaml`'s `default_clone_depth`), and bidirectionally binds the paper's `code-clones` ↔ repo-meta's `papers`.

Inspect with `lit code list --paper <paper-id>`. Pull updates with `lit code update <repo-name>`. Drop the binding with `lit code rm <repo-name> --cascade --yes`.

---

## [D] Browse / Search / Inspect

Most browsing is a single CLI call. Don't write a wrapper — call `lit list` directly with the right filters.

```bash
lit list                                     # full vault
lit list --topic transformer --year ">=2023" # filter
lit list --status deep-read --priority A     # by personal evaluation
lit show Pandi                               # fuzzy: unique substring of id
lit show 2023_Pandi_Cell-free                # exact id also works
lit show --paper-doi 10.1038/...             # DOI reverse-lookup
```

The paper-id input on every `lit` command that takes one (`show`, `open`, `modify`, `rm`, `rename`, `link`, `unlink`, `code link`, `code add --paper`, `code list --paper`) accepts (a) the full id, (b) a unique case-insensitive substring, or (c) `--paper-doi <DOI>` as a separate option mutually exclusive with the positional / `--paper` channel. Ambiguous substrings (2+ matches) print the candidate list and exit non-zero. `lit rename <old> <new>` is the one exception: it has no `--paper-doi` because two positionals make Click's parser unable to disambiguate.

For "find a paper I read last month about X", scan `lit list --format json` and grep semantically — this is where the LLM adds value.

---

## [E] Modify Metadata or Taxonomy

**Field changes** use `lit modify`:
```bash
lit modify <id> --set status=deep-read --set priority=A
lit modify <id> --add-tag topics=peptide-LM --add-tag methods=transformer
lit modify <id> --rm-tag topics=outdated-value
```

**Taxonomy changes** use `lit taxonomy` — never hand-edit `TAXONOMY.md`:
```bash
lit taxonomy add topics peptide-design               # add a new value
lit taxonomy rename topics old-name new-name         # cascade across all papers
lit taxonomy merge methods old-method into new-method
lit taxonomy rm topics unused-value                  # refuses if any paper still uses it
```

Both commands write atomically (staging dir + `os.replace`) and refresh `INDEX.json` afterward.

---

## [F] Cross-Machine Recovery

When the user moves their vault to a new machine (rclone sync, USB stick, `cp -r`):

1. Set `$LIT_LIBRARY` to the new vault path (in `~/.zshrc` or equivalent).
2. Verify: `lit hello && lit list | head` should both work.
3. Re-clone all code repos:
   ```bash
   lit code restore-all
   ```
   This scans `codes/*/repo-meta.yaml` and `git clone`s any `repo/` that's missing locally, from the `upstream` URL preserved in metadata. Single-repo failures don't abort the loop. Exit 1 if any clone failed or any orphan reference was found.
4. Run `lit health-check` to verify schema / refs / wikilinks are clean.

---

## Architecture Invariants (do not violate)

1. **Never** write `papers/<id>/metadata.yaml`, `TAXONOMY.md`, `INDEX.json`, or `codes/<name>/repo-meta.yaml` directly. Always go through `lit add` / `lit modify` / `lit taxonomy` / `lit code …`.
2. **Never** suggest hand-editing `TAXONOMY.md` to remove a value — it leaves dangling refs. Use `lit taxonomy rm` (refuses if still referenced) or `lit taxonomy rename` / `merge` (cascades).
3. **Never** assume the vault is git-tracked. It is deliberately not. Multi-file atomicity is provided by `<vault>/.litman-staging/` + `os.replace`, not git.
4. **Never** store API keys in `lit-config.yaml`. The CLI does not call any LLM API — that's your job (the agent), via the JSON-file bridge.
5. **Never** install / uninstall litman or modify its conda env. If `lit` is missing, tell the user and stop.

If unsure whether an operation respects these invariants, run `lit health-check` after — it surfaces 10 categories of vault drift and the user gets to inspect before acting.

---

## Quick Reference

| Command | What it does |
|---------|-------------|
| `lit init [--name <vault>]` | Create a new vault skeleton |
| `lit add <pdf> --doi <doi>` | Add via CrossRef |
| `lit add <pdf> --from-llm-json <json>` | Add via LLM-extracted JSON |
| `lit list [filters]` | Browse |
| `lit show <id-or-substring>` | Single-paper metadata (fuzzy substring OK; `--paper-doi` also supported) |
| `lit modify <id> --set k=v --add-tag list=v` | Edit fields |
| `lit taxonomy {list,add,rename,merge,rm} <dict> [args]` | Manage controlled vocab |
| `lit code add <url> --paper <id>` | Clone + bind a code repo |
| `lit code list [--paper <id>]` | Browse code repos |
| `lit code update <name> [--unshallow]` | git pull / promote shallow |
| `lit code restore-all` | Cross-machine recovery |
| `lit health-check [--fix]` | Vault consistency report |
| `lit rename <old> <new>` | Atomic id rename with cascade |
| `lit rm <id> [--cascade] [--purge]` | Soft-delete (trash) or purge |
| `lit trash {list,restore,empty}` | Trash bin management |
| `lit config show` | Print parsed `lit-config.yaml` |
