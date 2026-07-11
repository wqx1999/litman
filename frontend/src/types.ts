// TS mirror of the server payloads. Field names match the YAML metadata schema
// and the INDEX thin projection exactly (hyphenated keys kept verbatim).

import type { ToastVariant } from './ui/Toast'

/** The INDEX.json / `lit list --format json` thin projection (13 fields). */
export interface IndexPaper {
  id: string
  title: string | null
  /** "Family, Given" strings; `[]` when absent (list-field projection rule). */
  authors: string[]
  year: number | null
  type: string | null
  priority: string | null
  status: string | null
  topics: string[]
  projects: string[]
  methods: string[]
  data: string[]
  doi: string | null
  'read-date': string | null
}

/** GET /api/doc-mtimes payload: per-paper notes/discussion file mtimes (epoch
 * seconds, null when absent). Used ONLY by the resync diff to detect free-form
 * edits made outside the GUI; never rendered. */
export type DocMtimes = Record<string, { notes: number | null; discussion: number | null }>

/** Full metadata.yaml for one paper (superset of IndexPaper). */
export interface PaperMeta extends IndexPaper {
  journal?: string | null
  'arxiv-id'?: string | null
  github?: string | null
  'created-at'?: string | null
  'updated-at'?: string | null
  'last-revisited'?: string | null
  related?: string[]
  extends?: string[]
  'extended-by'?: string[]
  contradicts?: string[]
  'contradicted-by'?: string[]
  'code-clones'?: string[]
  /** Derived by the server (get_paper): the subset of `code-clones` whose
   * codes/<name>/ is gone on disk, so the cockpit can mark them as missing.
   * Absent on the trash-inspector path (that endpoint does not annotate it). */
  'code-clones-missing'?: string[]
}

/** The smart-list views the server computes (sorted by recency / read-date). */
export type SmartListView = 'reading' | 'recent-read' | 'backlog'

/** Where a search term matched. `id`/`title`/`author`/`doi`/`year` are
 * resolved client-side off the loaded INDEX; `notes`/`discussion` come from
 * /api/search. */
export type SearchScope =
  | 'id'
  | 'title'
  | 'author'
  | 'doi'
  | 'year'
  | 'notes'
  | 'discussion'

/** One notes/discussion hit from /api/search (one per paper, notes preferred). */
export interface SearchHit {
  id: string
  scope: 'notes' | 'discussion'
  /** Match-centered slice of the matched line (already trimmed server-side). */
  snippet: string
  line: number
}

export interface SearchPayload {
  query: string
  hits: SearchHit[]
}

/** TAXONOMY controlled vocabulary, one list per key. */
export interface Taxonomy {
  projects: string[]
  topics: string[]
  methods: string[]
  data: string[]
  type: string[]
  status: string[]
  priority: string[]
}

/** One fixed-enum field's dropdown options. `allowsNone` is true for the
 * optional enums (priority/type), which then offer an "— (unset)" choice;
 * status is required so it has none. */
export interface FixedEnumField {
  values: string[]
  allowsNone: boolean
}

/** The /api/fixed-enums payload: status/priority/type whitelists for the
 * cockpit dropdowns, sourced from core/checks (not hard-coded in the frontend). */
export interface FixedEnums {
  status: FixedEnumField
  priority: FixedEnumField
  type: FixedEnumField
}

/** How a registered project stands, as `GET /api/projects` joins TAXONOMY.md's
 * projects section against lit-config.yaml's projects map — the same join that
 * backs `lit project list`. `path-missing` means the folder is no longer there;
 * the two `-only` states mean the project is registered in one truth source but
 * not the other. See `projectHealth` in projects.ts for what each means to the
 * user. */
export type ProjectStatus = 'ok' | 'path-missing' | 'config-only' | 'taxonomy-only'

export interface ProjectEntry {
  name: string
  /** Empty string for a `taxonomy-only` project: it has no path to show. */
  path: string
  status: ProjectStatus
}

export interface VaultEntry {
  name: string
  path: string
  active: boolean
  /** Whether `path` still holds a library (its lit-config.yaml), re-probed by
   * the server on every `GET /api/vaults`. A vault whose folder was moved,
   * deleted, or replaced by an unrelated folder comes back false, and switching
   * to it would be refused — the UI marks it missing. */
  exists: boolean
}

export interface VaultsPayload {
  active: string | null
  /** The vault this server is actually bound to, or null when it started with no
   * vault (welcome-page mode). Distinct from `active` (the registry's active
   * name): the frontend keys the welcome page off this, not `active`. */
  served: string | null
  vaults: VaultEntry[]
}

/** A center-pane tab: a paper PDF or one of its markdown docs. */
export type TabKind = 'pdf' | 'notes' | 'discussion'

export interface Tab {
  /** Stable key = `${kind}:${paperId}`. */
  key: string
  kind: TabKind
  paperId: string
  /** Display label (paper id + a kind suffix). */
  label: string
  /** True for a tab opened from the trash view: PDF/md render read-only and the
   * tab carries a "Trash" badge. Absent on normal library tabs. */
  trash?: boolean
}

/** One row of the GET /api/trash projection (the read-only trash library). */
export interface TrashEntry {
  paperId: string
  title: string | null
  /** ISO 8601 deletion time, or "(unknown)" when the sidecar was lost. */
  deletedAt: string
  /** Unique on-disk dir name (`<id>-<UTC-timestamp>`); the restore/read key. */
  entryName: string
  /** Number of 1:1 hard-deleted repos a restore would surface for re-clone. */
  orphanRepoCount: number
}

/** One finding from GET /api/health (a serialized core.checks.Issue). `category`
 * groups; `severity` colors; `paper_id`/`hint` are shown when present. Matches the
 * dataclass field names verbatim (snake_case is fine — this payload is consumed,
 * not authored). */
export interface HealthIssue {
  category: string
  severity: 'error' | 'warning' | 'info'
  paper_id: string | null
  message: string
  hint: string | null
}

/** One entry in the in-memory session activity log (the PyMOL-style history that
 * `notify` feeds). `ts` is `Date.now()` ms (rendered as local HH:MM:SS); `variant`
 * picks the glyph. Never persisted — a page refresh clears the buffer (AC4). */
export interface ActivityLogEntry {
  ts: number
  variant: ToastVariant
  message: string
}

/** The POST /api/trash/{entry}/restore summary — what the rebuild touched.
 * Mirrors core.trash.RestoreResult minus the on-disk path. `missingRepos` maps
 * repo name → upstream url for 1:1 repos the GUI never re-clones (CLI / health-
 * check does); when non-empty the restore toast points the user at the CLI. */
export interface RestoreResult {
  paperId: string
  title: string | null
  reverseEdgesRebuilt: string[]
  reposRebound: string[]
  projectsRebuilt: string[]
  missingRepos: Record<string, string>
  deadEdgesDropped: string[]
}
