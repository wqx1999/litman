// TS mirror of the server payloads. Field names match the YAML metadata schema
// and the INDEX thin projection exactly (hyphenated keys kept verbatim).

/** The INDEX.json / `lit list --format json` thin projection (12 fields). */
export interface IndexPaper {
  id: string
  title: string | null
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

/** Full metadata.yaml for one paper (superset of IndexPaper). */
export interface PaperMeta extends IndexPaper {
  authors?: string[]
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
}

/** The smart-list views the server computes (sorted by recency / read-date). */
export type SmartListView = 'reading' | 'recent-read' | 'backlog'

/** Where a search term matched. `id`/`title` are resolved client-side off the
 * loaded INDEX; `notes`/`discussion` come from /api/search. */
export type SearchScope = 'id' | 'title' | 'notes' | 'discussion'

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

export interface ProjectEntry {
  name: string
  path: string
  status: string
}

export interface VaultEntry {
  name: string
  path: string
  active: boolean
}

export interface VaultsPayload {
  active: string | null
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
}
