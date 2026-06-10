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
import LeftNav from './nav/LeftNav'
import type { Facet, ListMode } from './nav/LeftNav'
import PaperList from './list/PaperList'
import TabArea from './tabs/TabArea'
import Cockpit from './cockpit/Cockpit'

const SMART_VIEWS: ReadonlySet<string> = new Set([
  'reading',
  'recent-read',
  'backlog',
])

function tabLabel(id: string, kind: TabKind): string {
  if (kind === 'pdf') return id
  return `${id} · ${kind}`
}

export default function App() {
  const [vaults, setVaults] = useState<VaultsPayload | null>(null)
  const [projects, setProjects] = useState<ProjectEntry[]>([])

  // The current paper set the middle list shows. `all`/`dropped` use the full
  // INDEX list (filtered client-side); the smart-lists come pre-ordered from
  // the server (recency / read-date), so they replace `papers` wholesale.
  const [papers, setPapers] = useState<IndexPaper[]>([])
  const [loadingList, setLoadingList] = useState(false)

  const [listMode, setListMode] = useState<ListMode>('reading')
  const [projectScope, setProjectScope] = useState<string | null>(null)
  const [activeFacet, setActiveFacet] = useState<Facet | null>(null)
  const [search, setSearch] = useState('')

  const [tabs, setTabs] = useState<Tab[]>([])
  const [activeTab, setActiveTab] = useState<string | null>(null)
  const [selectedId, setSelectedId] = useState<string | null>(null)

  const [cockpitPaper, setCockpitPaper] = useState<PaperMeta | null>(null)
  const [cockpitLoading, setCockpitLoading] = useState(false)
  const [cockpitCollapsed, setCockpitCollapsed] = useState(false)

  const loadList = useCallback((mode: ListMode) => {
    setLoadingList(true)
    const view = SMART_VIEWS.has(mode) ? (mode as SmartListView) : undefined
    fetchPapers(view)
      .then(setPapers)
      .finally(() => setLoadingList(false))
  }, [])

  const refresh = useCallback(() => {
    fetchVaults().then(setVaults)
    fetchProjects().then(setProjects)
    loadList(listMode)
  }, [listMode, loadList])

  useEffect(() => {
    fetchVaults().then(setVaults)
    fetchProjects().then(setProjects)
  }, [])

  useEffect(() => {
    loadList(listMode)
  }, [listMode, loadList])

  // Project scope filters every list mode; `all`/`dropped` filter status here
  // too (the smart-lists already excluded dropped / split by read-date server
  // side, so they pass through unchanged except for project + facet + search).
  const scoped = useMemo(() => {
    let out = papers
    if (projectScope) {
      out = out.filter((p) => (p.projects || []).includes(projectScope))
    }
    if (listMode === 'dropped') {
      out = out.filter((p) => p.status === 'dropped')
    } else if (listMode === 'all') {
      out = out.filter((p) => p.status !== 'dropped')
    }
    return out
  }, [papers, projectScope, listMode])

  const visible = useMemo(() => {
    let out = scoped
    if (activeFacet) {
      out = out.filter((p) =>
        (p[activeFacet.field] || []).includes(activeFacet.value),
      )
    }
    const q = search.trim().toLowerCase()
    if (q) {
      out = out.filter(
        (p) =>
          (p.title || '').toLowerCase().includes(q) ||
          p.id.toLowerCase().includes(q),
      )
    }
    return out
  }, [scoped, activeFacet, search])

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

  return (
    <div className="flex h-full flex-col">
      <TopBar
        vaults={vaults}
        search={search}
        onSearch={setSearch}
        selectedId={selectedId}
        onRefresh={refresh}
      />
      <div className="flex min-h-0 flex-1">
        <LeftNav
          scoped={scoped}
          projects={projectNames}
          projectScope={projectScope}
          onProjectScope={setProjectScope}
          listMode={listMode}
          onListMode={(m) => {
            setActiveFacet(null)
            setListMode(m)
          }}
          activeFacet={activeFacet}
          onFacet={setActiveFacet}
        />
        <PaperList
          papers={visible}
          loading={loadingList}
          selectedId={selectedId}
          onOpenPdf={openPdf}
          onOpenDoc={openDoc}
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
        />
      </div>
    </div>
  )
}
