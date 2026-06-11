import { useMemo, useState } from 'react'
import type { IndexPaper } from '../types'

/** Sort presets: the two server-ordered smart-lists plus INDEX-order `all`. */
export type ListMode = 'all' | 'reading' | 'recent-read'

/** Every filterable dimension. `project` is deliberately excluded — it stays a
 * single-select top dropdown (a scope), not a multi-select facet group. */
export type FacetKey =
  | 'status'
  | 'priority'
  | 'type'
  | 'topics'
  | 'methods'
  | 'data'

/** One multi-select Set per facet dimension. */
export type Filters = Record<FacetKey, Set<string>>

/** A fresh, all-empty filter object (every toggle/clear builds new Sets so the
 * `visible` useMemo in App recomputes — this seeds the initial + cleared state). */
export function emptyFilters(): Filters {
  return {
    status: new Set(),
    priority: new Set(),
    type: new Set(),
    topics: new Set(),
    methods: new Set(),
    data: new Set(),
  }
}

interface Props {
  /** Papers in scope (project-filtered only) — used for facet counts. */
  scoped: IndexPaper[]
  /** Papers to render in the list (all filters applied in App). */
  visible: IndexPaper[]
  loading: boolean
  projects: string[]
  projectScope: string | null
  onProjectScope: (p: string | null) => void
  listMode: ListMode
  onListMode: (m: ListMode) => void
  filters: Filters
  onToggleFilter: (field: FacetKey, value: string) => void
  onClearFilters: () => void
  selectedId: string | null
  onSelect: (id: string) => void
  onOpenPdf: (id: string) => void
  onOpenDoc: (id: string, doc: 'notes' | 'discussion') => void
  collapsed: boolean
  onToggle: () => void
}

const LIST_MODES: Array<[ListMode, string]> = [
  ['reading', '📖 Reading'],
  ['recent-read', '✅ Read'],
  ['all', 'All'],
]

/** A facet dimension's display config. `kind` selects the counting + filter
 * shape: `single` reads `p[key]` (string | null), `array` reads `p[key]`
 * (string[]). `order` pins the value sequence for fixed-enum dimensions; values
 * absent from it (none, for fixed enums) fall through to alphabetical. */
interface Dimension {
  key: FacetKey
  label: string
  kind: 'single' | 'array'
  order?: readonly string[]
}

// Status / priority / type are fixed enums (core/checks.py:_FIXED_ENUM_VALUES);
// pin their value order to the enum sequence rather than alphabetizing. Topics /
// methods / data are free controlled-vocabulary lists → alphabetical.
const DIMENSIONS: readonly Dimension[] = [
  {
    key: 'status',
    label: 'Status',
    kind: 'single',
    order: ['deep-read', 'skim', 'inbox', 'dropped'],
  },
  { key: 'priority', label: 'Priority', kind: 'single', order: ['A', 'B', 'C'] },
  {
    key: 'type',
    label: 'Type',
    kind: 'single',
    order: [
      'research',
      'review',
      'position',
      'benchmark',
      'dataset',
      'tutorial',
      'thesis',
      'book-chapter',
    ],
  },
  { key: 'topics', label: 'Topics', kind: 'array' },
  { key: 'methods', label: 'Methods', kind: 'array' },
  { key: 'data', label: 'Data', kind: 'array' },
]

// Keyed on the authoritative status enum (core/checks.py
// _FIXED_ENUM_VALUES["status"]); `deep-read`/`skim` are the two most common
// values, so they must each get a distinct colour rather than fall through.
const STATUS_DOT: Record<string, string> = {
  'deep-read': 'bg-emerald-500',
  skim: 'bg-amber-500',
  inbox: 'bg-sky-500',
  dropped: 'bg-stone-400',
}

function statusDotClass(status: string | null): string {
  return (status && STATUS_DOT[status]) || 'bg-stone-300'
}

/** Order a dimension's `Map<value, count>` for display: pinned `order` values
 * first (in that sequence), then any leftover alphabetically. */
function orderedEntries(
  counts: Map<string, number>,
  order?: readonly string[],
): Array<[string, number]> {
  if (!order) {
    return [...counts.entries()].sort((a, b) => a[0].localeCompare(b[0]))
  }
  const rank = new Map(order.map((v, i) => [v, i]))
  return [...counts.entries()].sort((a, b) => {
    const ra = rank.get(a[0]) ?? Infinity
    const rb = rank.get(b[0]) ?? Infinity
    return ra !== rb ? ra - rb : a[0].localeCompare(b[0])
  })
}

/** Merged left panel: compact toolbar (project dropdown + smart-list chips +
 * per-dimension collapsible facet groups) on top of a one-line-per-paper list
 * whose selected row inline-expands to title + PDF/notes/discussion actions.
 * Collapses to a w-8 strip mirroring the Cockpit pattern. Read-only (write
 * controls are a later phase). */
export default function BrowsePanel({
  scoped,
  visible,
  loading,
  projects,
  projectScope,
  onProjectScope,
  listMode,
  onListMode,
  filters,
  onToggleFilter,
  onClearFilters,
  selectedId,
  onSelect,
  onOpenPdf,
  onOpenDoc,
  collapsed,
  onToggle,
}: Props) {
  // Level-1 disclosure: the whole Filter section. Collapsed by default so the
  // panel stays clean — the labelled "Filter" row is still an obvious entry.
  const [filterOpen, setFilterOpen] = useState(false)

  // Level-2 disclosure: which dimension groups are expanded. Empty = all
  // collapsed (the default).
  const [openGroups, setOpenGroups] = useState<Set<FacetKey>>(new Set())

  const toggleGroup = (key: FacetKey) =>
    setOpenGroups((prev) => {
      const next = new Set(prev)
      next.has(key) ? next.delete(key) : next.add(key)
      return next
    })

  // Live counts per dimension over the scoped set (project-scoped, pre-filter).
  // Single-value fields count `p[key]` when non-null; array fields count each
  // element. A dimension with an empty map is hidden entirely below.
  const counts = useMemo(() => {
    const out = new Map<FacetKey, Map<string, number>>()
    for (const dim of DIMENSIONS) out.set(dim.key, new Map())
    for (const p of scoped) {
      for (const dim of DIMENSIONS) {
        const m = out.get(dim.key)!
        if (dim.kind === 'single') {
          const v = p[dim.key] as string | null
          if (v != null) m.set(v, (m.get(v) || 0) + 1)
        } else {
          for (const v of (p[dim.key] as string[]) || []) {
            m.set(v, (m.get(v) || 0) + 1)
          }
        }
      }
    }
    return out
  }, [scoped])

  const activeCount = DIMENSIONS.reduce(
    (sum, dim) => sum + filters[dim.key].size,
    0,
  )

  // Dimensions that actually have values in the current scope. Empty ones are
  // hidden; if none have values (e.g. an all-untagged vault) we show a hint so
  // the Filter region never reads as a broken/empty box.
  const visibleDims = DIMENSIONS.filter((dim) => counts.get(dim.key)!.size > 0)

  // One-click fold/unfold for every level-2 field group at once.
  const anyGroupOpen = openGroups.size > 0
  const setAllGroups = (open: boolean) =>
    setOpenGroups(open ? new Set(visibleDims.map((dim) => dim.key)) : new Set())

  if (collapsed) {
    return (
      <div className="flex w-8 shrink-0 flex-col items-center border-r border-stone-200 bg-stone-100 pt-3">
        <button
          onClick={onToggle}
          title="Expand browse panel"
          className="text-stone-500 hover:text-stone-800"
        >
          ›
        </button>
      </div>
    )
  }

  return (
    <div className="flex w-80 shrink-0 flex-col overflow-hidden border-r border-stone-200 bg-stone-100">
      {/* Compact toolbar */}
      <div className="flex max-h-[60%] shrink-0 flex-col border-b border-stone-200">
        <div className="shrink-0 space-y-2 p-2">
          <div className="flex items-center gap-2">
            <select
              value={projectScope ?? ''}
              onChange={(e) => onProjectScope(e.target.value || null)}
              className="min-w-0 flex-1 rounded border border-stone-300 bg-white px-2 py-1 text-sm"
            >
              <option value="">All projects</option>
              {projects.map((p) => (
                <option key={p} value={p}>
                  {p}
                </option>
              ))}
            </select>
            <button
              onClick={onToggle}
              title="Collapse browse panel"
              className="shrink-0 rounded px-1.5 py-1 text-stone-500 hover:bg-stone-200 hover:text-stone-800"
            >
              ‹
            </button>
          </div>

          {/* Smart-list views — what subset of the library to show. Labelled to
              read as distinct from the Filter region below it. */}
          <div>
            <div className="mb-1 text-[10px] font-semibold uppercase tracking-wide text-stone-400">
              Show
            </div>
            <div className="flex flex-wrap gap-1">
              {LIST_MODES.map(([mode, label]) => (
                <button
                  key={mode}
                  onClick={() => onListMode(mode)}
                  className={`rounded px-2 py-0.5 text-xs ${
                    listMode === mode
                      ? 'bg-stone-300 font-medium text-stone-900'
                      : 'text-stone-600 hover:bg-stone-200'
                  }`}
                >
                  {label}
                </button>
              ))}
            </div>
          </div>
        </div>

        {/* Filter region — level-1 collapsible. The labelled "Filter" row is the
            first disclosure level; each dimension below is the second level.
            Both default collapsed for a clean panel. */}
        <div className="shrink-0 border-t border-stone-200 bg-stone-200/50">
          <div className="flex items-center justify-between">
            <button
              onClick={() => setFilterOpen((o) => !o)}
              className="flex flex-1 items-center gap-1.5 px-2 py-1.5 text-sm font-semibold uppercase tracking-wide text-stone-600 hover:bg-stone-200"
            >
              <span className="text-stone-400">{filterOpen ? '▾' : '▸'}</span>
              <span>Filter</span>
              {activeCount > 0 && (
                <span className="rounded-full bg-stone-600 px-1.5 py-px text-[10px] font-medium normal-case leading-none text-white">
                  {activeCount}
                </span>
              )}
            </button>
            <div className="flex shrink-0 items-center">
              {filterOpen && visibleDims.length > 0 && (
                <button
                  onClick={() => setAllGroups(!anyGroupOpen)}
                  title={anyGroupOpen ? 'Collapse all fields' : 'Expand all fields'}
                  className="px-2 py-1 text-[11px] normal-case text-stone-500 hover:text-stone-800"
                >
                  {anyGroupOpen ? 'Collapse all' : 'Expand all'}
                </button>
              )}
              {activeCount > 0 && (
                <button
                  onClick={onClearFilters}
                  className="px-2 py-1 text-[11px] normal-case text-stone-500 underline decoration-stone-400 hover:text-stone-800"
                >
                  Clear all
                </button>
              )}
            </div>
          </div>

          {/* Active-filter pills (summary across all six fields). */}
          {filterOpen && activeCount > 0 && (
            <div className="flex flex-wrap items-center gap-1 px-2 pb-1.5">
              {DIMENSIONS.flatMap((dim) =>
                [...filters[dim.key]].map((value) => (
                  <button
                    key={`${dim.key}:${value}`}
                    onClick={() => onToggleFilter(dim.key, value)}
                    title="Remove filter"
                    className="flex items-center gap-1 rounded-full bg-white px-2 py-0.5 text-xs text-stone-700 ring-1 ring-stone-300 hover:bg-stone-100"
                  >
                    {dim.key === 'status' && (
                      <span
                        className={`h-1.5 w-1.5 rounded-full ${statusDotClass(
                          value,
                        )}`}
                      />
                    )}
                    <span className="max-w-[7rem] truncate">{value}</span>
                    <span className="text-stone-400">×</span>
                  </button>
                )),
              )}
            </div>
          )}
        </div>

        {/* Per-dimension facet groups (level 2) — shown only when the Filter
            section is expanded. Each dimension is itself collapsible; one with no
            values in `scoped` is hidden entirely, and if none has any value we
            show a hint instead of an empty box. */}
        {filterOpen && (
          <div className="min-h-0 flex-1 overflow-auto px-2 pb-2">
            {visibleDims.length === 0 && (
              <div className="px-1 py-2 text-xs text-stone-400">
                No tags to filter yet.
              </div>
            )}
            {visibleDims.map((dim) => {
            const entries = orderedEntries(counts.get(dim.key)!, dim.order)
            const sel = filters[dim.key]
            const open = openGroups.has(dim.key)
            return (
              <div key={dim.key} className="border-t border-stone-200 first:border-t-0">
                <button
                  onClick={() => toggleGroup(dim.key)}
                  className="flex w-full items-center justify-between px-1 py-1 text-xs font-semibold uppercase tracking-wide text-stone-500 hover:bg-stone-200"
                >
                  <span>
                    {dim.label}
                    {sel.size > 0 && (
                      <span className="ml-1 font-normal normal-case text-stone-700">
                        ({sel.size})
                      </span>
                    )}
                  </span>
                  <span>{open ? '▾' : '▸'}</span>
                </button>
                {open && (
                  <div className="space-y-0.5 pb-1">
                    {entries.map(([value, count]) => {
                      const active = sel.has(value)
                      return (
                        <button
                          key={value}
                          onClick={() => onToggleFilter(dim.key, value)}
                          className={`flex w-full items-center gap-1.5 rounded px-2 py-0.5 text-left text-xs ${
                            active
                              ? 'bg-stone-300 font-medium text-stone-900'
                              : 'text-stone-600 hover:bg-stone-200'
                          }`}
                        >
                          {dim.key === 'status' && (
                            <span
                              className={`h-1.5 w-1.5 shrink-0 rounded-full ${statusDotClass(
                                value,
                              )}`}
                            />
                          )}
                          <span className="truncate">{value}</span>
                          <span className="ml-auto text-stone-400">{count}</span>
                        </button>
                      )
                    })}
                  </div>
                )}
              </div>
            )
            })}
          </div>
        )}
      </div>

      {/* Paper list */}
      <div className="min-h-0 flex-1 overflow-auto bg-white">
        {loading && <div className="p-3 text-sm text-stone-500">Loading…</div>}
        {!loading && visible.length === 0 && (
          <div className="p-3 text-sm text-stone-400">No papers.</div>
        )}
        {visible.map((p) => {
          const selected = p.id === selectedId
          return (
            <div
              key={p.id}
              className={`border-b border-stone-100 ${
                selected ? 'bg-stone-100' : 'hover:bg-stone-50'
              }`}
            >
              <button
                onClick={() => onSelect(p.id)}
                title={p.title ?? p.id}
                className="flex w-full items-center gap-2 px-3 py-1.5 text-left"
              >
                <span
                  className={`h-2 w-2 shrink-0 rounded-full ${statusDotClass(
                    p.status,
                  )}`}
                  title={p.status ?? 'unknown'}
                />
                <span className="truncate font-mono text-xs text-stone-700">
                  {p.id}
                </span>
                {p.year != null && (
                  <span className="ml-auto shrink-0 text-xs text-stone-400">
                    {p.year}
                  </span>
                )}
              </button>
              {selected && (
                <div className="px-3 pb-2">
                  <div className="mb-2 text-sm text-stone-800">
                    {p.title || p.id}
                  </div>
                  <div className="flex flex-wrap gap-1.5">
                    <button
                      onClick={() => onOpenPdf(p.id)}
                      className="rounded border border-stone-300 bg-white px-2 py-0.5 text-xs text-stone-700 hover:bg-stone-200"
                    >
                      📄 PDF
                    </button>
                    <button
                      onClick={() => onOpenDoc(p.id, 'notes')}
                      className="rounded border border-stone-300 bg-white px-2 py-0.5 text-xs text-stone-700 hover:bg-stone-200"
                    >
                      📝 notes
                    </button>
                    <button
                      onClick={() => onOpenDoc(p.id, 'discussion')}
                      className="rounded border border-stone-300 bg-white px-2 py-0.5 text-xs text-stone-700 hover:bg-stone-200"
                    >
                      💬 discussion
                    </button>
                  </div>
                </div>
              )}
            </div>
          )
        })}
      </div>
    </div>
  )
}
