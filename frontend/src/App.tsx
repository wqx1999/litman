import { useCallback, useEffect, useMemo, useState } from 'react'
import {
  fetchPaper,
  fetchPapers,
  fetchProjects,
  fetchVaults,
} from './api'
import type {
  IndexPaper,
  PaperMeta,
  ProjectEntry,
  SmartListView,
  Tab,
  TabKind,
  VaultsPayload,
} from './types'
import TopBar from './topbar/TopBar'
import BrowsePanel from './nav/BrowsePanel'
import type { FacetKey, Filters, ListMode } from './nav/BrowsePanel'
import { emptyFilters } from './nav/BrowsePanel'
import TabArea from './tabs/TabArea'
import Cockpit from './cockpit/Cockpit'

const SMART_VIEWS: ReadonlySet<string> = new Set(['reading', 'recent-read'])

// Single-value fields filter on `p[f]` (string | null); array fields filter on
// `p[f]` (string[]). Status carries the dropped-default-hide special case.
const SINGLE_FILTER_FIELDS: Array<'priority' | 'type'> = ['priority', 'type']
const ARRAY_FILTER_FIELDS: Array<'topics' | 'methods' | 'data'> = [
  'topics',
  'methods',
  'data',
]

function tabLabel(id: string, kind: TabKind): string {
  if (kind === 'pdf') return id
  return `${id} · ${kind}`
}

export default function App() {
  const [vaults, setVaults] = useState<VaultsPayload | null>(null)
  const [projects, setProjects] = useState<ProjectEntry[]>([])

  // The current paper set the middle list shows. `all` uses the full INDEX list
  // (filtered client-side); the smart-lists come pre-ordered from the server
  // (recency / read-date), so they replace `papers` wholesale.
  const [papers, setPapers] = useState<IndexPaper[]>([])
  const [loadingList, setLoadingList] = useState(false)

  const [listMode, setListMode] = useState<ListMode>('reading')
  const [projectScope, setProjectScope] = useState<string | null>(null)
  // Multi-dimensional client-side filters: one multi-select Set per field, over
  // all six facet dimensions (status/priority/type single-value; topics/methods/
  // data array). Cross-dimension AND, within-dimension OR. `project` is NOT a
  // facet — it stays the top dropdown (single-select scope).
  const [filters, setFilters] = useState<Filters>(emptyFilters)
  const [search, setSearch] = useState('')

  const [tabs, setTabs] = useState<Tab[]>([])
  const [activeTab, setActiveTab] = useState<string | null>(null)
  const [selectedId, setSelectedId] = useState<string | null>(null)

  const [cockpitPaper, setCockpitPaper] = useState<PaperMeta | null>(null)
  const [cockpitLoading, setCockpitLoading] = useState(false)
  const [cockpitCollapsed, setCockpitCollapsed] = useState(false)
  const [leftCollapsed, setLeftCollapsed] = useState(false)

  const loadList = useCallback((mode: ListMode) => {
    setLoadingList(true)
    const view = SMART_VIEWS.has(mode) ? (mode as SmartListView) : undefined
    fetchPapers(view)
      .then(setPapers)
      .finally(() => setLoadingList(false))
  }, [])

  useEffect(() => {
    fetchVaults().then(setVaults)
    fetchProjects().then(setProjects)
  }, [])

  useEffect(() => {
    loadList(listMode)
  }, [listMode, loadList])

  // `scoped` is ONLY the project filter now; the all/dropped status logic moved
  // into `visible` so every dimension composes through one AND pipeline. The
  // smart-lists (reading/recent-read) already came pre-ordered + dropped-free
  // from the server, so project scope is the only narrowing applied here.
  const scoped = useMemo(() => {
    if (!projectScope) return papers
    return papers.filter((p) => (p.projects || []).includes(projectScope))
  }, [papers, projectScope])

  // Multi-dimensional filter: cross-dimension AND, within-dimension OR.
  const visible = useMemo(() => {
    let out = scoped
    // STATUS (single, OR) with dropped default-hide preserved: when nothing is
    // ticked, `all` still suppresses dropped; reading/recent-read server views
    // already exclude it.
    const st = filters.status
    if (st.size > 0) {
      out = out.filter((p) => p.status != null && st.has(p.status))
    } else if (listMode === 'all') {
      out = out.filter((p) => p.status !== 'dropped')
    }
    // PRIORITY, TYPE (single, OR).
    for (const field of SINGLE_FILTER_FIELDS) {
      const sel = filters[field]
      if (sel.size > 0) {
        out = out.filter((p) => p[field] != null && sel.has(p[field]!))
      }
    }
    // TOPICS, METHODS, DATA (array, OR within / AND across).
    for (const field of ARRAY_FILTER_FIELDS) {
      const sel = filters[field]
      if (sel.size > 0) {
        out = out.filter((p) => (p[field] ?? []).some((v) => sel.has(v)))
      }
    }
    // SEARCH: title/id substring (unchanged), applied last.
    const q = search.trim().toLowerCase()
    if (q) {
      out = out.filter(
        (p) =>
          (p.title || '').toLowerCase().includes(q) ||
          p.id.toLowerCase().includes(q),
      )
    }
    return out
  }, [scoped, filters, listMode, search])

  const toggleFilter = useCallback((field: FacetKey, value: string) => {
    setFilters((prev) => {
      const next = new Set(prev[field])
      next.has(value) ? next.delete(value) : next.add(value)
      return { ...prev, [field]: next }
    })
  }, [])

  const clearFilters = useCallback(() => {
    setFilters(emptyFilters())
  }, [])

  const selectPaper = useCallback((id: string) => {
    setSelectedId(id)
    setCockpitLoading(true)
    fetchPaper(id)
      .then(setCockpitPaper)
      .catch(() => setCockpitPaper(null))
      .finally(() => setCockpitLoading(false))
  }, [])

  const openTab = useCallback(
    (id: string, kind: TabKind) => {
      const key = `${kind}:${id}`
      setTabs((prev) =>
        prev.some((t) => t.key === key)
          ? prev
          : [...prev, { key, kind, paperId: id, label: tabLabel(id, kind) }],
      )
      setActiveTab(key)
      selectPaper(id)
    },
    [selectPaper],
  )

  const openPdf = useCallback((id: string) => openTab(id, 'pdf'), [openTab])
  const openDoc = useCallback(
    (id: string, doc: 'notes' | 'discussion') => openTab(id, doc),
    [openTab],
  )

  const closeTab = useCallback(
    (key: string) => {
      setTabs((prev) => {
        const next = prev.filter((t) => t.key !== key)
        setActiveTab((cur) =>
          cur === key ? (next.length ? next[next.length - 1].key : null) : cur,
        )
        return next
      })
    },
    [],
  )

  const projectNames = useMemo(() => projects.map((p) => p.name), [projects])

  // Active vault's filesystem path (server-side), used by the Cockpit to build
  // the copy-path action. Null until /api/vaults resolves or if none is active.
  const vaultPath = useMemo(
    () => vaults?.vaults.find((v) => v.active)?.path ?? null,
    [vaults],
  )

  return (
    <div className="flex h-full flex-col bg-stone-100 text-stone-800 antialiased">
      <TopBar vaults={vaults} search={search} onSearch={setSearch} />
      <div className="flex min-h-0 flex-1">
        <BrowsePanel
          scoped={scoped}
          visible={visible}
          loading={loadingList}
          projects={projectNames}
          projectScope={projectScope}
          onProjectScope={setProjectScope}
          listMode={listMode}
          onListMode={setListMode}
          filters={filters}
          onToggleFilter={toggleFilter}
          onClearFilters={clearFilters}
          selectedId={selectedId}
          onSelect={selectPaper}
          onOpenPdf={openPdf}
          onOpenDoc={openDoc}
          collapsed={leftCollapsed}
          onToggle={() => setLeftCollapsed((c) => !c)}
        />
        <TabArea
          tabs={tabs}
          activeKey={activeTab}
          onActivate={(key) => {
            setActiveTab(key)
            const tab = tabs.find((t) => t.key === key)
            if (tab) selectPaper(tab.paperId)
          }}
          onClose={closeTab}
          onOpenPaper={openPdf}
        />
        <Cockpit
          paper={cockpitPaper}
          loading={cockpitLoading}
          collapsed={cockpitCollapsed}
          onToggle={() => setCockpitCollapsed((c) => !c)}
          onOpenPaper={openPdf}
          vaultPath={vaultPath}
        />
      </div>
    </div>
  )
}
