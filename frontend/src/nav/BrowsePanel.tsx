import { useEffect, useMemo, useRef, useState } from 'react'
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
  /** True vault-empty (full INDEX fetched, zero papers) — renders the
   * getting-started card instead of the plain no-match empty state. */
  vaultEmpty: boolean
  /** The list fetch failed (server unreachable) — renders an explicit failure
   * line instead of an empty state that would read as "no papers". */
  loadFailed: boolean
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
  /** Soft-delete the paper (the expanded card's trash action). Hands off to App,
   * which runs the default-No confirm + `lit rm` DELETE. */
  onRemovePaper: (id: string) => void
  /** Number of entries in this vault's trash (drives the footer count). */
  trashCount: number
  /** Enter the full-screen trash (recovery) view. */
  onOpenTrash: () => void
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
// _FIXED_ENUM_VALUES["status"]); each value gets a macOS system-colour dot
// (green / orange / teal / grey) defined as a `--color-status-*` theme token.
const STATUS_DOT: Record<string, string> = {
  'deep-read': 'bg-status-read',
  skim: 'bg-status-skim',
  inbox: 'bg-status-inbox',
  dropped: 'bg-status-dropped',
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

/** Merged left panel: compact toolbar (project dropdown + smart-list segmented
 * control + per-dimension collapsible facet groups) on top of a one-line-per-
 * paper list whose selected row inline-expands to title + PDF/notes/discussion
 * actions. Collapses to a narrow strip via an animated width. Every disclosure
 * (Filter level-1, each dimension level-2, the selected row) grows fluidly with
 * the shared `ease-fluid` curve. Read-only (write controls are a later phase). */
export default function BrowsePanel({
  scoped,
  visible,
  vaultEmpty,
  loadFailed,
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
  onRemovePaper,
  trashCount,
  onOpenTrash,
  collapsed,
  onToggle,
}: Props) {
  // Level-1 disclosure: the whole Filter section. Collapsed by default so the
  // panel stays clean — the labelled "Filter" row is still an obvious entry.
  const [filterOpen, setFilterOpen] = useState(false)

  // Level-2 disclosure: which dimension groups are expanded. Empty = all
  // collapsed (the default).
  const [openGroups, setOpenGroups] = useState<Set<FacetKey>>(new Set())

  // Keep the selection visible when it moves without a click (the J/K keyboard
  // navigation): nudge the selected row into view. block:'nearest' = no scroll
  // at all while the row is already visible, so mouse selection never jumps.
  const listRef = useRef<HTMLDivElement | null>(null)
  useEffect(() => {
    if (!selectedId) return
    listRef.current
      ?.querySelector('[data-selected="true"]')
      ?.scrollIntoView({ block: 'nearest' })
  }, [selectedId])

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

  // Index of the active smart-list — drives the sliding segmented indicator.
  const listIndex = Math.max(
    0,
    LIST_MODES.findIndex(([m]) => m === listMode),
  )

  return (
    <div
      className={`relative flex shrink-0 overflow-hidden border-r border-stone-200 bg-stone-100 transition-[width] duration-300 ease-fluid ${
        collapsed ? 'w-9' : 'w-80'
      }`}
    >
      {/* Collapsed strip: just the expand handle, fading in once narrowed. */}
      <div
        className={`absolute inset-0 flex flex-col items-center pt-3 transition-opacity duration-200 ${
          collapsed ? 'opacity-100 delay-150' : 'pointer-events-none opacity-0'
        }`}
      >
        <button
          onClick={onToggle}
          title="Expand browse panel"
          className="text-stone-500 transition-colors hover:text-stone-800"
        >
          ›
        </button>
      </div>

      {/* Full panel — fixed w-80 so it never reflows while the container width
          animates; cross-fades out when collapsed. */}
      <div
        className={`flex h-full w-80 flex-col transition-opacity duration-200 ${
          collapsed ? 'pointer-events-none opacity-0' : 'opacity-100 delay-100'
        }`}
      >
        {/* Compact toolbar */}
        <div className="flex max-h-[55%] shrink-0 flex-col overflow-y-auto border-b border-stone-200">
          <div className="shrink-0 space-y-2.5 p-2.5">
            <div className="flex items-center gap-2">
              <select
                value={projectScope ?? ''}
                onChange={(e) => onProjectScope(e.target.value || null)}
                className="min-w-0 flex-1 rounded-md border border-stone-300 bg-white px-2 py-1 text-sm text-stone-800 shadow-sm transition focus:border-accent-500 focus:outline-none focus:ring-2 focus:ring-accent-500/25"
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
                className="shrink-0 rounded-md px-1.5 py-1 text-stone-500 transition-colors hover:bg-stone-200 hover:text-stone-800"
              >
                ‹
              </button>
            </div>

            {/* Smart-list views — a macOS segmented control with a sliding white
                indicator that glides between segments. */}
            <div>
              <div className="mb-1 text-[10px] font-semibold uppercase tracking-wider text-stone-400">
                Show
              </div>
              <div className="relative flex rounded-lg bg-stone-200 p-0.5">
                <div
                  className="pointer-events-none absolute inset-y-0.5 left-0.5 rounded-md bg-white shadow-sm transition-transform duration-300 ease-fluid"
                  style={{
                    width: 'calc((100% - 4px) / 3)',
                    transform: `translateX(calc(${listIndex} * 100%))`,
                  }}
                />
                {LIST_MODES.map(([mode, label]) => (
                  <button
                    key={mode}
                    onClick={() => onListMode(mode)}
                    className={`relative z-10 flex-1 rounded-md px-2 py-1 text-xs font-medium transition-colors ${
                      listMode === mode
                        ? 'text-stone-900'
                        : 'text-stone-600 hover:text-stone-800'
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
          <div className="shrink-0 border-t border-stone-200 bg-stone-200/40">
            <div className="flex items-center justify-between">
              <button
                onClick={() => setFilterOpen((o) => !o)}
                className="flex flex-1 items-center gap-1.5 px-2.5 py-1.5 text-sm font-semibold uppercase tracking-wider text-stone-600 transition-colors hover:bg-stone-200"
              >
                <span
                  className={`text-stone-400 transition-transform duration-300 ease-fluid ${
                    filterOpen ? 'rotate-90' : ''
                  }`}
                >
                  ▸
                </span>
                <span>Filter</span>
                {activeCount > 0 && (
                  <span className="rounded-full bg-accent-500 px-1.5 py-px text-[10px] font-medium normal-case leading-none text-white">
                    {activeCount}
                  </span>
                )}
              </button>
              <div className="flex shrink-0 items-center">
                {filterOpen && visibleDims.length > 0 && (
                  <button
                    onClick={() => setAllGroups(!anyGroupOpen)}
                    title={anyGroupOpen ? 'Collapse all fields' : 'Expand all fields'}
                    className="px-2 py-1 text-[11px] normal-case text-stone-500 transition-colors hover:text-stone-800"
                  >
                    {anyGroupOpen ? 'Collapse all' : 'Expand all'}
                  </button>
                )}
                {activeCount > 0 && (
                  <button
                    onClick={onClearFilters}
                    className="px-2 py-1 text-[11px] normal-case text-stone-500 underline decoration-stone-400 transition-colors hover:text-stone-800"
                  >
                    Clear all
                  </button>
                )}
              </div>
            </div>

            {/* Active-filter pills (summary across all six fields). */}
            {filterOpen && activeCount > 0 && (
              <div className="flex flex-wrap items-center gap-1 px-2.5 pb-1.5">
                {DIMENSIONS.flatMap((dim) =>
                  [...filters[dim.key]].map((value) => (
                    <button
                      key={`${dim.key}:${value}`}
                      onClick={() => onToggleFilter(dim.key, value)}
                      title="Remove filter"
                      className="flex items-center gap-1 rounded-full bg-accent-50 px-2 py-0.5 text-xs text-accent-700 transition-colors hover:bg-accent-100"
                    >
                      {dim.key === 'status' && (
                        <span
                          className={`h-1.5 w-1.5 rounded-full ${statusDotClass(
                            value,
                          )}`}
                        />
                      )}
                      <span className="max-w-[7rem] truncate">{value}</span>
                      <span className="text-accent-500/60">×</span>
                    </button>
                  )),
                )}
              </div>
            )}
          </div>

          {/* Per-dimension facet groups (level 2) — the whole region grows
              fluidly (0fr→1fr) when Filter opens. Each dimension is itself a
              fluid disclosure; one with no values in `scoped` is hidden, and if
              none has any value we show a hint instead of an empty box. */}
          <div
            className={`grid transition-[grid-template-rows] duration-300 ease-fluid ${
              filterOpen ? 'grid-rows-[1fr]' : 'grid-rows-[0fr]'
            }`}
          >
            <div className="overflow-hidden">
              <div className="px-2 pb-2 pt-1">
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
                    <div key={dim.key}>
                      <button
                        onClick={() => toggleGroup(dim.key)}
                        className="flex w-full items-center justify-between rounded-md px-2 py-1.5 text-[11px] font-semibold uppercase tracking-wider text-stone-500 transition-colors hover:bg-stone-200"
                      >
                        <span>
                          {dim.label}
                          {sel.size > 0 && (
                            <span className="ml-1 font-normal normal-case text-accent-600">
                              ({sel.size})
                            </span>
                          )}
                        </span>
                        <span
                          className={`text-stone-400 transition-transform duration-300 ease-fluid ${
                            open ? 'rotate-90' : ''
                          }`}
                        >
                          ▸
                        </span>
                      </button>
                      <div
                        className={`grid transition-[grid-template-rows] duration-300 ease-fluid ${
                          open ? 'grid-rows-[1fr]' : 'grid-rows-[0fr]'
                        }`}
                      >
                        <div className="overflow-hidden">
                          <div
                            className={`space-y-0.5 pb-1 transition-opacity duration-200 ${
                              open ? 'opacity-100' : 'opacity-0'
                            }`}
                          >
                            {entries.map(([value, count]) => {
                              const active = sel.has(value)
                              return (
                                <button
                                  key={value}
                                  onClick={() => onToggleFilter(dim.key, value)}
                                  className={`flex w-full items-center gap-1.5 rounded-md px-2 py-1 text-left text-xs transition-colors ${
                                    active
                                      ? 'bg-accent-100 font-medium text-accent-700'
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
                                  <span
                                    className={`ml-auto ${
                                      active ? 'text-accent-500' : 'text-stone-400'
                                    }`}
                                  >
                                    {count}
                                  </span>
                                </button>
                              )
                            })}
                          </div>
                        </div>
                      </div>
                    </div>
                  )
                })}
              </div>
            </div>
          </div>
        </div>

        {/* Paper list */}
        <div ref={listRef} className="min-h-0 flex-1 overflow-auto bg-white py-1">
          {loading && <div className="p-3 text-sm text-stone-500">Loading…</div>}
          {/* Three empty states, in precedence order: the list fetch failed
           * (server unreachable — never masquerade as an empty library), a
           * truly empty vault gets a getting-started card (the GUI has no add
           * flow — point at the terminal / the agent, and surface add's move
           * semantics), and everything else (filters, search, smart-list,
           * project scope matching nothing) stays a short no-match line.
           * Mirrors the CLI's own empty-vault guidance in list.py. */}
          {!loading && loadFailed && visible.length === 0 && (
            <div className="p-3 text-sm text-stone-400">
              Couldn't reach the server — papers can't load.
            </div>
          )}
          {!loading && !loadFailed && visible.length === 0 && !vaultEmpty && (
            <div className="p-3 text-sm text-stone-400">No papers match.</div>
          )}
          {!loading && !loadFailed && vaultEmpty && (
            <div className="mx-2 my-3 rounded-xl bg-stone-100/80 p-4 text-sm text-stone-500 ring-1 ring-stone-200/80">
              <div className="mb-2 font-medium text-stone-700">
                No papers in your vault yet.
              </div>
              <div>Add your first paper from the terminal:</div>
              <div className="my-2 w-fit rounded-lg bg-stone-200/70 px-2.5 py-1.5 font-mono text-xs text-stone-700">
                lit add &lt;pdf&gt; --doi &lt;doi&gt;
              </div>
              <div>…or ask your coding agent to add it for you.</div>
              <div className="mt-2.5 text-xs text-stone-400">
                add moves the source PDF into the vault.
              </div>
            </div>
          )}
          {visible.map((p) => {
            const selected = p.id === selectedId
            // Dropped papers are shown (in `all`) but muted + tagged, so they
            // read as low-priority records rather than active entries.
            const isDropped = p.status === 'dropped'
            return (
              <div
                key={p.id}
                data-selected={selected || undefined}
                className={`mx-2 my-0.5 overflow-hidden rounded-xl ring-1 transition-[background-color,box-shadow] duration-300 ease-fluid ${
                  selected
                    ? 'bg-accent-50 shadow-[0_1px_8px_rgba(0,122,255,0.10)] ring-accent-200/70'
                    : 'ring-transparent hover:bg-stone-200/50'
                }`}
              >
                <button
                  onClick={() => onSelect(p.id)}
                  title={p.title ?? p.id}
                  className={`flex w-full items-center gap-2 px-2.5 py-1.5 text-left ${
                    isDropped ? 'opacity-55' : ''
                  }`}
                >
                  <span
                    className={`h-2 w-2 shrink-0 rounded-full ${statusDotClass(
                      p.status,
                    )}`}
                    title={p.status ?? 'unknown'}
                  />
                  <span
                    className={`truncate font-mono text-xs transition-colors ${
                      selected ? 'font-medium text-accent-800' : 'text-stone-700'
                    }`}
                  >
                    {p.id}
                  </span>
                  {isDropped && (
                    <span
                      className="shrink-0 rounded bg-stone-200 px-1 py-px text-[9px] font-medium uppercase tracking-wide text-stone-500 dark:bg-stone-700 dark:text-stone-300"
                      title="Dropped — evaluated and set aside (kept as a record)"
                    >
                      dropped
                    </span>
                  )}
                  {p.year != null && (
                    <span
                      className={`ml-auto shrink-0 text-xs ${
                        selected ? 'text-accent-500/80' : 'text-stone-400'
                      }`}
                    >
                      {p.year}
                    </span>
                  )}
                </button>
                {/* Dynamic-Island-style fluid reveal: the detail grows out of the
                    row via an animatable 0fr→1fr grid track (no height guessing)
                    plus a gentle settle. Always mounted so open AND close animate;
                    inert (pointer-events-none) while collapsed. */}
                <div
                  className={`grid transition-[grid-template-rows,opacity] duration-300 ease-fluid ${
                    selected
                      ? 'grid-rows-[1fr] opacity-100'
                      : 'pointer-events-none grid-rows-[0fr] opacity-0'
                  }`}
                >
                  <div className="overflow-hidden">
                    <div
                      className={`px-2.5 pb-2.5 transition-transform duration-300 ease-fluid ${
                        selected ? 'translate-y-0' : '-translate-y-1'
                      }`}
                    >
                      <div className="mb-2 text-sm text-stone-800">
                        {p.title || p.id}
                      </div>
                      <div className="flex items-center gap-1.5">
                        <button
                          onClick={() => onOpenPdf(p.id)}
                          className="rounded-lg bg-white px-2 py-0.5 text-xs text-stone-700 shadow-sm ring-1 ring-stone-200 transition-colors hover:text-accent-700 hover:ring-accent-300"
                        >
                          📄 PDF
                        </button>
                        <button
                          onClick={() => onOpenDoc(p.id, 'notes')}
                          className="rounded-lg bg-white px-2 py-0.5 text-xs text-stone-700 shadow-sm ring-1 ring-stone-200 transition-colors hover:text-accent-700 hover:ring-accent-300"
                        >
                          📝 notes
                        </button>
                        <button
                          onClick={() => onOpenDoc(p.id, 'discussion')}
                          className="rounded-lg bg-white px-2 py-0.5 text-xs text-stone-700 shadow-sm ring-1 ring-stone-200 transition-colors hover:text-accent-700 hover:ring-accent-300"
                        >
                          💬 discussion
                        </button>
                        {/* Remove from library — set apart at the row's right edge
                            (ml-auto) and rose-on-hover so it never reads as another
                            "open" pill. Only present on the expanded (selected) card,
                            so it is not a stray destructive button in the list. The
                            default-No confirm in App is the mis-click guard. */}
                        <button
                          onClick={() => onRemovePaper(p.id)}
                          title="Remove paper from library (move to trash)"
                          aria-label="Remove paper from library"
                          className="ml-auto grid h-6 w-6 shrink-0 place-items-center rounded-lg text-stone-400 ring-1 ring-transparent transition-colors hover:bg-rose-50 hover:text-rose-500 hover:ring-rose-200"
                        >
                          <IconTrash />
                        </button>
                      </div>
                    </div>
                  </div>
                </div>
              </div>
            )
          })}
        </div>

        {/* Trash entry — divided footer (macOS Mail/Notes convention). Enters the
            read-only trash-recovery view; the count shows N / 100 because the
            trash is capped at TRASH_MAX_ENTRIES = 100 (core/trash.py, ADR-011) —
            past it the oldest entries are permanently evicted. */}
        <div className="shrink-0 border-t border-stone-200">
          <button
            onClick={onOpenTrash}
            title="Open the trash (recover deleted papers) · capped at 100"
            className="flex w-full items-center gap-2 px-3 py-2 text-left text-sm text-stone-600 transition-colors hover:bg-stone-200"
          >
            <span>🗑</span>
            <span>Trash</span>
            <span className="ml-auto text-xs text-stone-400">{trashCount} / 100</span>
          </button>
        </div>
      </div>
    </div>
  )
}

/** Trash can — the expanded card's "remove paper from library" action. */
function IconTrash() {
  return (
    <svg
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth={1.8}
      strokeLinecap="round"
      strokeLinejoin="round"
      className="h-3.5 w-3.5"
      aria-hidden
    >
      <path d="M4 7h16M9 7V5a1 1 0 0 1 1-1h4a1 1 0 0 1 1 1v2M6 7l1 13a2 2 0 0 0 2 2h6a2 2 0 0 0 2-2l1-13" />
    </svg>
  )
}
