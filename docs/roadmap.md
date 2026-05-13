# Roadmap

## Tagged releases

| Tag | Milestone | Date |
|---|---|---|
| `v0.1.0-m1` | M1 — Storage skeleton (`init`, `add`, `list`, `show`) | early 2026 |
| `v0.2.0-m2` | M2 — Governance (`modify`, `taxonomy`, `rename`, `rm`, `trash`, `health-check`, DOI/id dedup) | 2026-04-28 → 2026-05-11 |
| `v0.3.0-m3` | M3 — `lit code` (clone, list, link, update, rm, restore-all) | 2026-05-11 |
| `v0.4.0-m4` | M4 — LLM JSON importer + `lit-library` skill + `lit install-skill` | 2026-05-11 |
| `v0.5.0-m5` | M5 — Project integration (`lit link`, REFERENCES.md generator) | 2026-05-11 |
| (no tag) | M6 — Cloud sync (M6.1 + M6.2 done; M6.3 dogfood deferred) | 2026-05-12 |
| `v0.8.0-m8` | M8 — Multi-vault (registry, `lit vault`, `--vault` everywhere, cross-vault wikilinks) | 2026-05-12 |
| `v0.9.0-m9` | M9 — `lit open` + `lit-reading` skill (agent-assisted reading) | 2026-05-12 |

## In flight

- **M7** — Legacy migration of an existing reading list (waiting on
  user-provided PDFs).
- **M10** — Documentation reorganisation: split the monolithic README
  into front-page + `docs/` reference, add schema reference for
  `metadata.yaml` / `lit-config.yaml` / `TAXONOMY.md`, lay down the
  `mkdocs.yml` skeleton. Prerequisite for PyPI release.

## Next leverage layer

- **M11** — Layer-4 skill matrix: `lit-ingest`, `lit-audit`,
  `lit-writing`, `lit-synthesis`. Each is a Claude Code skill that
  composes the existing CLI for a specific workflow. The CLI is
  already feature-complete; the next leverage is teaching agents how to
  use it on knowledge-graph coherence work that no single command can
  express.

## License-and-release

- PyPI release (`pipx install litman`) is gated on M10 (docs) being
  done. M7 is independent — the migration is internal dogfood.

The full development milestone index lives in the development repository
at `dev_docs/milestones/README.md`.
