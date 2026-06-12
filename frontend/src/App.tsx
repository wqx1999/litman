import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import {
  fetchPaper,
  fetchPapers,
  fetchProjects,
  fetchSearch,
  fetchVaults,
} from './api'
import type { PdfHandle } from './pdf/PdfView'
import SaveDialog from './tabs/SaveDialog'
import { mergeCandidates, type Candidate } from './search'
import type {
  IndexPaper,
  PaperMeta,
  ProjectEntry,
  SearchHit,
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

  // Search spans all four scopes. id/title are matched instantly client-side,
  // but over the WHOLE library (a global quick-jump), not just the current
  // smart-list — so we keep the full INDEX separate from the list-mode `papers`.
  const [allPapers, setAllPapers] = useState<IndexPaper[]>([])
  // notes/discussion hits from /api/search (debounced, async). The token guards
  // against an earlier slower response overwriting a newer one.
  const [serverHits, setServerHits] = useState<SearchHit[]>([])
  const [searchLoading, setSearchLoading] = useState(false)
  const searchToken = useRef(0)

  const [tabs, setTabs] = useState<Tab[]>([])
  const [activeTab, setActiveTab] = useState<string | null>(null)
  const [selectedId, setSelectedId] = useState<string | null>(null)
  // A notes/discussion tab opened from a search hit, plus the query to scroll to
  // and highlight inside it. Keyed by tab so only that doc highlights.
  const [mdJump, setMdJump] = useState<{ key: string; query: string } | null>(null)

  // Flush handles for mounted PDF tabs, used to prompt Save / Don't-save when a
  // tab with unsaved annotations is closed. Only the active PDF tab is mounted
  // (TabArea renders one tab), so at most one entry is live.
  const handlesRef = useRef<Map<string, PdfHandle>>(new Map())
  // Tab key awaiting a close decision (drives the SaveDialog), and whether its
  // save is in flight.
  const [pendingClose, setPendingClose] = useState<string | null>(null)
  const [savingClose, setSavingClose] = useState(false)

  const [cockpitPaper, setCockpitPaper] = useState<PaperMeta | null>(null)
  const [cockpitLoading, setCockpitLoading] = useState(false)
  const [cockpitCollapsed, setCockpitCollapsed] = useState(false)
  const [leftCollapsed, setLeftCollapsed] = useState(false)
  // Focus mode collapses both side panels at once (PDF fills the middle) and
  // restores their prior collapse state on exit.
  const [focusMode, setFocusMode] = useState(false)
  const prevCollapseRef = useRef<{ left: boolean; right: boolean }>({
    left: false,
    right: false,
  })

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
    // Full INDEX (no view) backs global id/title matching + title lookup for
    // notes/discussion hits — independent of which smart-list the middle list
    // is showing.
    fetchPapers().then(setAllPapers)
  }, [])

  useEffect(() => {
    loadList(listMode)
  }, [listMode, loadList])

  // Debounced notes/discussion search. id/title match instantly off allPapers
  // (no network); only the markdown scopes need the server. An empty query
  // clears hits without a request.
  useEffect(() => {
    const q = search.trim()
    if (!q) {
      // Invalidate any in-flight response so a late fetch can't repopulate
      // hits after the box is cleared.
      searchToken.current++
      setServerHits([])
      setSearchLoading(false)
      return
    }
    setSearchLoading(true)
    const token = ++searchToken.current
    const timer = setTimeout(() => {
      fetchSearch(q)
        .then((res) => {
          if (searchToken.current === token) setServerHits(res.hits)
        })
        .catch(() => {
          if (searchToken.current === token) setServerHits([])
        })
        .finally(() => {
          if (searchToken.current === token) setSearchLoading(false)
        })
    }, 200)
    return () => clearTimeout(timer)
  }, [search])

  // Ranked candidates feed both the dropdown (top few) and the list filter
  // (the full matched id set), from one merge so the two never disagree.
  const searchCandidates = useMemo(
    () => mergeCandidates(allPapers, serverHits, search),
    [allPapers, serverHits, search],
  )
  const matchedIds = useMemo(
    () => new Set(searchCandidates.map((c) => c.id)),
    [searchCandidates],
  )

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
    // SEARCH (applied last): keep papers in the merged 4-scope match set. The
    // set spans id/title (instant) + notes/discussion (async, fills in shortly
    // after typing). Dropped/facet narrowing above still ANDs with it. The
    // dropdown caps its preview; the list keeps the full matched set.
    if (search.trim()) {
      out = out.filter((p) => matchedIds.has(p.id))
    }
    return out
  }, [scoped, filters, listMode, search, matchedIds])

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

  // Route a picked search result by where it matched: an id/title hit opens the
  // paper PDF; a notes/discussion hit opens that doc and scrolls to / highlights
  // the matched query (captured now, since `search` may be edited afterwards).
  const onSearchSelect = useCallback(
    (c: Candidate) => {
      if (c.scope === 'notes' || c.scope === 'discussion') {
        openDoc(c.id, c.scope)
        setMdJump({ key: `${c.scope}:${c.id}`, query: search })
      } else {
        openPdf(c.id)
      }
    },
    [openDoc, openPdf, search],
  )

  // Remove a tab from the bar and re-point the active tab. Does NOT save — the
  // caller decides (closeTab prompts; the dialog handlers save/discard first).
  const removeTab = useCallback((key: string) => {
    setTabs((prev) => {
      const next = prev.filter((t) => t.key !== key)
      setActiveTab((cur) =>
        cur === key ? (next.length ? next[next.length - 1].key : null) : cur,
      )
      return next
    })
  }, [])

  const registerPdf = useCallback((key: string, handle: PdfHandle | null) => {
    if (handle) handlesRef.current.set(key, handle)
    else handlesRef.current.delete(key)
  }, [])

  const closeTab = useCallback(
    (key: string) => {
      const handle = handlesRef.current.get(key)
      if (handle?.isDirty()) {
        setPendingClose(key)
        return
      }
      removeTab(key)
    },
    [removeTab],
  )

  // SaveDialog actions for the tab pending a close decision.
  const confirmSave = useCallback(async () => {
    const key = pendingClose
    if (!key) return
    setSavingClose(true)
    try {
      await handlesRef.current.get(key)?.flush()
    } catch (err) {
      console.error('Failed to embed PDF annotations:', err)
    } finally {
      setSavingClose(false)
      setPendingClose(null)
      removeTab(key)
    }
  }, [pendingClose, removeTab])

  const confirmDiscard = useCallback(() => {
    const key = pendingClose
    if (!key) return
    handlesRef.current.get(key)?.discard()
    setPendingClose(null)
    removeTab(key)
  }, [pendingClose, removeTab])

  const cancelClose = useCallback(() => setPendingClose(null), [])

  // Warn before a full-page unload (browser/tab close, reload) if any PDF has
  // unsaved annotations — the in-app close prompt cannot run there.
  useEffect(() => {
    const onBeforeUnload = (e: BeforeUnloadEvent) => {
      for (const handle of handlesRef.current.values()) {
        if (handle.isDirty()) {
          e.preventDefault()
          e.returnValue = ''
          return
        }
      }
    }
    window.addEventListener('beforeunload', onBeforeUnload)
    return () => window.removeEventListener('beforeunload', onBeforeUnload)
  }, [])

  const projectNames = useMemo(() => projects.map((p) => p.name), [projects])

  // Active vault's filesystem path (server-side), used by the Cockpit to build
  // the copy-path action. Null until /api/vaults resolves or if none is active.
  const vaultPath = useMemo(
    () => vaults?.vaults.find((v) => v.active)?.path ?? null,
    [vaults],
  )

  // Enter/exit focus mode: stash the current panel collapse, hide both panels,
  // restore on exit.
  const toggleFocus = useCallback(() => {
    setFocusMode((on) => {
      if (!on) {
        prevCollapseRef.current = { left: leftCollapsed, right: cockpitCollapsed }
        setLeftCollapsed(true)
        setCockpitCollapsed(true)
      } else {
        setLeftCollapsed(prevCollapseRef.current.left)
        setCockpitCollapsed(prevCollapseRef.current.right)
      }
      return !on
    })
  }, [leftCollapsed, cockpitCollapsed])

  return (
    <div className="flex h-full flex-col bg-stone-100 text-stone-800 antialiased">
      <TopBar
        vaults={vaults}
        search={search}
        onSearch={setSearch}
        searchCandidates={searchCandidates}
        searchLoading={searchLoading}
        onSelectResult={onSearchSelect}
        focusMode={focusMode}
        onToggleFocus={toggleFocus}
      />
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
          onRegisterPdf={registerPdf}
          mdJump={mdJump}
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
      {pendingClose && (
        <SaveDialog
          label={tabs.find((t) => t.key === pendingClose)?.label ?? pendingClose}
          saving={savingClose}
          onSave={confirmSave}
          onDiscard={confirmDiscard}
          onCancel={cancelClose}
        />
      )}
    </div>
  )
}
