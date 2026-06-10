import { useMemo } from 'react'
import type { IndexPaper, SmartListView } from '../types'

export type ListMode = 'all' | 'dropped' | SmartListView

export type FacetField = 'topics' | 'methods' | 'data' | 'projects'

export interface Facet {
  field: FacetField
  value: string
}

interface Props {
  /** Papers currently in scope (already project-filtered) — used for counts. */
  scoped: IndexPaper[]
  projects: string[]
  projectScope: string | null
  onProjectScope: (p: string | null) => void
  listMode: ListMode
  onListMode: (m: ListMode) => void
  activeFacet: Facet | null
  onFacet: (f: Facet | null) => void
}

const SMART_LISTS: Array<[SmartListView, string]> = [
  ['reading', '📖 Reading'],
  ['recent-read', '✅ Recent read'],
  ['backlog', '🆕 Backlog'],
]

const FACET_FIELDS: Array<[FacetField, string]> = [
  ['topics', 'Topics'],
  ['methods', 'Methods'],
  ['data', 'Data'],
  ['projects', 'Projects'],
]

function NavButton({
  active,
  onClick,
  children,
}: {
  active: boolean
  onClick: () => void
  children: React.ReactNode
}) {
  return (
    <button
      onClick={onClick}
      className={`w-full rounded px-2 py-1 text-left text-sm ${
        active
          ? 'bg-stone-300 font-medium text-stone-900'
          : 'text-stone-700 hover:bg-stone-200'
      }`}
    >
      {children}
    </button>
  )
}

export default function LeftNav({
  scoped,
  projects,
  projectScope,
  onProjectScope,
  listMode,
  onListMode,
  activeFacet,
  onFacet,
}: Props) {
  // Live facet counts over the scoped set.
  const facetCounts = useMemo(() => {
    const counts: Record<FacetField, Map<string, number>> = {
      topics: new Map(),
      methods: new Map(),
      data: new Map(),
      projects: new Map(),
    }
    for (const p of scoped) {
      for (const [field] of FACET_FIELDS) {
        for (const v of p[field] || []) {
          counts[field].set(v, (counts[field].get(v) || 0) + 1)
        }
      }
    }
    return counts
  }, [scoped])

  return (
    <nav className="flex w-60 shrink-0 flex-col gap-4 overflow-auto border-r border-stone-200 bg-stone-100 p-3">
      <div>
        <label className="mb-1 block text-xs font-semibold uppercase tracking-wide text-stone-500">
          Project
        </label>
        <select
          value={projectScope ?? ''}
          onChange={(e) => onProjectScope(e.target.value || null)}
          className="w-full rounded border border-stone-300 bg-white px-2 py-1 text-sm"
        >
          <option value="">All projects</option>
          {projects.map((p) => (
            <option key={p} value={p}>
              {p}
            </option>
          ))}
        </select>
      </div>

      <div className="space-y-0.5">
        {SMART_LISTS.map(([view, label]) => (
          <NavButton
            key={view}
            active={listMode === view}
            onClick={() => onListMode(view)}
          >
            {label}
          </NavButton>
        ))}
        <NavButton active={listMode === 'all'} onClick={() => onListMode('all')}>
          All
        </NavButton>
        <NavButton
          active={listMode === 'dropped'}
          onClick={() => onListMode('dropped')}
        >
          Dropped
        </NavButton>
      </div>

      {FACET_FIELDS.map(([field, label]) => {
        const entries = [...facetCounts[field].entries()].sort((a, b) =>
          a[0].localeCompare(b[0]),
        )
        if (entries.length === 0) return null
        return (
          <div key={field}>
            <div className="mb-1 text-xs font-semibold uppercase tracking-wide text-stone-500">
              {label}
            </div>
            <div className="space-y-0.5">
              {entries.map(([value, count]) => {
                const active =
                  activeFacet?.field === field && activeFacet.value === value
                return (
                  <button
                    key={value}
                    onClick={() =>
                      onFacet(active ? null : { field, value })
                    }
                    className={`flex w-full items-center justify-between rounded px-2 py-0.5 text-left text-xs ${
                      active
                        ? 'bg-stone-300 font-medium text-stone-900'
                        : 'text-stone-600 hover:bg-stone-200'
                    }`}
                  >
                    <span className="truncate">{value}</span>
                    <span className="ml-2 text-stone-400">{count}</span>
                  </button>
                )
              })}
            </div>
          </div>
        )
      })}
    </nav>
  )
}
