# Design Philosophy

A few principles are non-negotiable and shape every command's behaviour.

## LLMs never write data files directly

AI agents emit JSON candidates; the CLI validates them and performs the
actual write. This protects vault integrity from every LLM failure mode:
hallucinated values, schema drift, YAML syntax errors, prompt-injection
escapes.

The practical consequence: every "AI-augmented" command in litman has a
plain CLI path that takes the same JSON file. The `lit-library` skill
just generates the JSON for you; you can also write it by hand or
generate it with any other tool.

## Atomic multi-file ops via staging, not git rollback

Multi-file edits (rename a TAXONOMY value, rename a paper id, link a
paper to a project) stage all changes under `<vault>/.litman-staging/<op-id>/`
and then commit them via `os.replace()`. A mid-operation crash leaves an
abandoned staging dir, never a half-applied edit.

The vault is **not** git-tracked — cloud sync (`lit sync`) owns
versioning. This frees us from per-edit commit overhead and from the
"forgot to commit before crash" failure mode that hurts hand-edited
literature folders.

## CLI must work standalone

No LLM API key is required for any command. No agent runtime is required
for any command. Skills are optional sugar layered on top of an
already-complete CLI; if you delete them, nothing in `lit` stops
working.

This is enforced by the layering (see [Architecture](architecture.md))
and by every command's design: every `lit` subcommand has a
non-interactive, machine-friendly mode and never silently waits for an
agent to fill in data.

## Schema-less metadata

A missing field in `metadata.yaml` means "this dimension does not apply
to this paper", not "required-and-empty". Adding a new field costs
nothing — no migration, no schema version bump, no rebuild of existing
papers. The convention is: don't add a new field until ≥5 papers in
your vault genuinely need it; until then, capture the information in
`notes.md`.

The identity layer (`id`, `title`, `year`) is the only strictly required
set. Everything else is opt-in per paper.

## TAXONOMY changes only via `lit taxonomy`

Renames, merges, and removals of `topics` / `methods` / `projects` /
`data` values **must** go through `lit taxonomy {rename, merge, rm}`.
The command performs the dictionary edit and the cascade into every
referencing `metadata.yaml` (plus `INDEX.json`) as one atomic op.

Hand-editing `TAXONOMY.md` to delete a value is the single fastest way
to corrupt a vault, because it leaves orphan references that the system
cannot autoresolve. The CLI deliberately refuses `lit taxonomy rm` when
any paper still uses the value — you must `merge` or `rename` it
elsewhere first.

## Paper ↔ project ↔ code triangle, graph emergent

Papers, projects, and code clones bind to each other via metadata fields
(`projects`, `code-clones`, `related`, `contradicts`, `extends`) and
filesystem symlinks (`<project>/litman_reflib/<paper-id>/`, vault-resident
`codes/<repo-name>/`). The knowledge graph is **emergent** from these
bindings, not stored explicitly as a separate file.

This means: there is no master `graph.json` to keep in sync. There is no
"link two things" step beyond the natural editing commands. The graph
exists because the metadata exists, and any tool that can read YAML can
reconstruct it.

---

The full 13-rule list with rationale lives in the development repository
at `dev_docs/invariants.md`. The principles above are the user-facing
subset: what every command on the public CLI promises.
