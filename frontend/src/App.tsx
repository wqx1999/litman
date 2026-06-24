import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import {
  fetchFixedEnums,
  fetchPaper,
  fetchPapers,
  fetchProjects,
  fetchSearch,
  fetchTaxonomy,
  fetchVaults,
  putActiveVault,
  putDiscussion,
  putNotes,
} from './api'
import type { PdfHandle } from './pdf/PdfView'
import type { MdDraft } from './md/MdView'
import type { CockpitHandle } from './cockpit/Cockpit'
import { useKeyboardShortcuts } from './useKeyboardShortcuts'
import CheatSheet from './ui/CheatSheet'
import SaveDialog from './tabs/SaveDialog'
import { mergeCandidates, type Candidate } from './search'
import type {
  FixedEnums,
  IndexPaper,
  PaperMeta,
  ProjectEntry,
  SearchHit,
  SmartListView,
  Tab,
  TabKind,
  Taxonomy,
  VaultsPayload,
} from './types'
import TopBar from './topbar/TopBar'
import SwitchVaultDialog from './topbar/SwitchVaultDialog'
import BrowsePanel from './nav/BrowsePanel'
import type { FacetKey, Filters, ListMode } from './nav/BrowsePanel'
import { emptyFilters } from './nav/BrowsePanel'
import TabArea from './tabs/TabArea'
import Cockpit from './cockpit/Cockpit'
import Toast, { type ToastVariant } from './ui/Toast'

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

/** Reads the `.dark` class the no-FOUC script (index.html) already set on
 * <html>, then flips it and persists the choice. Lifted from TopBar to App
 * (Phase 4) so the `L` keyboard shortcut and the header toggle drive ONE theme
 * state; `toggle` is a stable useCallback so the shortcut effect doesn't re-bind. */
function useDarkMode(): readonly [boolean, () => void] {
  const [dark, setDark] = useState(
    () =>
      typeof document !== 'undefined' &&
      document.documentElement.classList.contains('dark'),
  )
  const toggle = useCallback(
    () =>
      setDark((d) => {
        const next = !d
        document.documentElement.classList.toggle('dark', next)
        try {
          localStorage.setItem('litman-theme', next ? 'dark' : 'light')
        } catch {
          /* private mode / no storage — class still applies this session */
        }
        return next
      }),
    [],
  )
  return [dark, toggle] as const
}

export default function App() {
  const [vaults, setVaults] = useState<VaultsPayload | null>(null)
  const [projects, setProjects] = useState<ProjectEntry[]>([])
  // Controlled vocabulary + fixed-enum whitelists feed the cockpit's tag-add
  // affordance and dropdowns (3b). Fetched once on mount; taxonomy re-fetches
  // after a write are deferred to 3c (inline-create), so a stale-but-present
  // vocabulary is fine here (3b attaches existing values only).
  const [taxonomy, setTaxonomy] = useState<Taxonomy | null>(null)
  const [fixedEnums, setFixedEnums] = useState<FixedEnums | null>(null)

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

  // A single transient toast (dangling wikilink, failed save, project linked).
  // Last write wins; `variant` tints the dot (default neutral 'info').
  const [toast, setToast] = useState<{ message: string; variant: ToastVariant } | null>(
    null,
  )
  const notify = useCallback(
    (msg: string, variant: ToastVariant = 'info') => setToast({ message: msg, variant }),
    [],
  )

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

  // Lifted md edit sessions, keyed by tab. State (so the active MdView re-renders
  // on each keystroke) plus a ref mirror (so the stable close/unload callbacks
  // read fresh values without re-binding). An entry exists ONLY while that tab is
  // mid-edit; it survives MdView's unmount on tab switch (the whole point of the
  // lift — TabArea mounts one tab, so a switch would otherwise drop the draft).
  // Unlike PDF (whose handle is only registered for the mounted tab), an md draft
  // here outlives the unmount, so App can prompt/save even a non-active md tab.
  const [mdDrafts, setMdDrafts] = useState<Map<string, MdDraft>>(new Map())
  const mdDraftsRef = useRef(mdDrafts)
  mdDraftsRef.current = mdDrafts

  // Tab key awaiting a close decision (drives the SaveDialog), and whether its
  // save is in flight.
  const [pendingClose, setPendingClose] = useState<string | null>(null)
  const [savingClose, setSavingClose] = useState(false)

  // Vault switch (3c-2): the target awaiting confirmation, and whether the
  // switch (flush + PUT + reload) is in flight.
  const [pendingVault, setPendingVault] = useState<string | null>(null)
  const [switchingVault, setSwitchingVault] = useState(false)

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

  // --- Keyboard shortcuts (Phase 4) ----------------------------------------
  // Theme state lifted here so the `L` shortcut and the TopBar toggle share it.
  const [dark, toggleDark] = useDarkMode()
  // The `?` cheat-sheet overlay (a non-blocking overlay the dispatcher owns —
  // NOT counted in anyModalOpen, so `?`/Esc still toggle it while it is up).
  const [cheatSheetOpen, setCheatSheetOpen] = useState(false)
  // Modal-open flags reported up from the children, so the dispatcher's modal
  // guard can suppress global shortcuts while a blocking surface is open.
  const [cockpitModalOpen, setCockpitModalOpen] = useState(false)
  const [projectsOpen, setProjectsOpen] = useState(false)
  // The Cockpit's imperative handle (curation triggers for ⌥-shortcuts). A ref
  // so registering it doesn't re-render; a state copy drives the hook's deps.
  const [cockpitHandle, setCockpitHandle] = useState<CockpitHandle | null>(null)
  const registerCockpit = useCallback(
    (handle: CockpitHandle | null) => setCockpitHandle(handle),
    [],
  )

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
    fetchTaxonomy().then(setTaxonomy)
    fetchFixedEnums().then(setFixedEnums)
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

  // After a cockpit structured write: re-fetch the selected paper so the cockpit
  // reflects the change, AND refresh both the current smart-list (a status /
  // read-date change moves smart-list membership + ordering) and the full INDEX
  // (it backs search + wikilink resolution). The backend already recomputed
  // INDEX/views atomically — these are read refreshes, not a re-derivation.
  const refreshAfterWrite = useCallback(() => {
    const id = selectedId
    if (id) {
      fetchPaper(id)
        .then(setCockpitPaper)
        .catch(() => setCockpitPaper(null))
    }
    loadList(listMode)
    fetchPapers().then(setAllPapers)
  }, [selectedId, loadList, listMode])

  // After a write that changes the shared vocabulary (a new taxonomy value, a
  // project link/unlink, a new project): refresh the cached /api/taxonomy +
  // /api/projects so the cockpit autocomplete + link dropdown reflect the new
  // values. Decoupled from refreshAfterWrite (which refreshes per-paper + lists)
  // so a plain status/tag-attach write doesn't pay for two extra fetches.
  const refreshVocab = useCallback(() => {
    fetchTaxonomy().then(setTaxonomy)
    fetchProjects().then(setProjects)
  }, [])

  // After a project create/delete from the TopBar manager: refresh the project
  // list + taxonomy (refreshVocab) AND the papers/selected/list (refreshAfterWrite).
  // A delete cascades untags across papers, so the middle list + cockpit must
  // re-pull, not just the project dropdown; a create only needs the vocab refresh
  // but the extra read is cheap and keeps one callback for both.
  const refreshProjects = useCallback(() => {
    refreshVocab()
    refreshAfterWrite()
  }, [refreshVocab, refreshAfterWrite])

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

  // A [[id]] wikilink click resolves against the full loaded library. A target
  // not in the vault would 404 the PDF tab, so a dangling link toasts instead of
  // opening a broken tab (decision 4). allPapers is the same full INDEX the
  // search quick-jump uses, so the check matches what's actually loadable.
  //
  // A `[[vault:id]]` cross-vault target (a documented wikilink form, see
  // core/notes.py) can never live in THIS vault's INDEX, so it must NOT fall
  // through to the same-vault "no paper" toast. Cross-vault navigation is out of
  // scope (single-vault GUI); we surface a distinct hint instead. Paper ids never
  // contain ':' (core/id.py), so any colon marks the vault separator.
  const openWikilink = useCallback(
    (target: string) => {
      if (target.includes(':')) {
        notify('Cross-vault link — open that vault to follow it.')
        return
      }
      if (allPapers.some((p) => p.id === target)) {
        openPdf(target)
      } else {
        notify(`No paper "${target}" in this vault.`)
      }
    },
    [allPapers, openPdf, notify],
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

  // --- Lifted md edit sessions (Fix 1) -------------------------------------
  // App owns each md tab's draft so it survives MdView's unmount on tab switch,
  // and so the close prompt / page-unload guard can see "unsaved md edits" the
  // same way they see dirty PDF annotations.

  const mdBeginEdit = useCallback((tabKey: string, seed: string) => {
    setMdDrafts((prev) => {
      const next = new Map(prev)
      next.set(tabKey, { draft: seed, savedText: seed })
      return next
    })
  }, [])

  const mdDraftChange = useCallback((tabKey: string, draft: string) => {
    setMdDrafts((prev) => {
      const cur = prev.get(tabKey)
      if (!cur) return prev // not editing (stale event) — ignore
      const next = new Map(prev)
      next.set(tabKey, { ...cur, draft })
      return next
    })
  }, [])

  const mdEndEdit = useCallback((tabKey: string) => {
    setMdDrafts((prev) => {
      if (!prev.has(tabKey)) return prev
      const next = new Map(prev)
      next.delete(tabKey)
      return next
    })
  }, [])

  // A tab has unsaved md edits when its lifted draft diverges from the on-disk
  // text it began from — the md analogue of PdfHandle.isDirty(). Reads the ref so
  // close/unload callbacks stay stable.
  const mdTabDirty = useCallback((key: string): boolean => {
    const entry = mdDraftsRef.current.get(key)
    return entry !== undefined && entry.draft !== entry.savedText
  }, [])

  // Remove a tab from the bar and re-point the active tab. Does NOT save — the
  // caller decides (closeTab prompts; the dialog handlers save/discard first).
  // Also drops any lifted md draft for the tab so a later tab reusing the key
  // (same paper reopened) starts clean.
  const removeTab = useCallback(
    (key: string) => {
      setTabs((prev) => {
        const next = prev.filter((t) => t.key !== key)
        setActiveTab((cur) =>
          cur === key ? (next.length ? next[next.length - 1].key : null) : cur,
        )
        return next
      })
      setMdDrafts((prev) => {
        if (!prev.has(key)) return prev
        const next = new Map(prev)
        next.delete(key)
        return next
      })
    },
    [],
  )

  const registerPdf = useCallback((key: string, handle: PdfHandle | null) => {
    if (handle) handlesRef.current.set(key, handle)
    else handlesRef.current.delete(key)
  }, [])

  const closeTab = useCallback(
    (key: string) => {
      // Both dirty-tab kinds route through the same SaveDialog: a PDF tab with
      // un-embedded annotations, or an md tab with an unsaved draft.
      const handle = handlesRef.current.get(key)
      if (handle?.isDirty() || mdTabDirty(key)) {
        setPendingClose(key)
        return
      }
      removeTab(key)
    },
    [removeTab, mdTabDirty],
  )

  // SaveDialog actions for the tab pending a close decision. The tab may be a
  // PDF tab (flush via its registered handle) or an md tab (PUT the lifted draft
  // directly — the md tab need not be mounted, so App owns the write).
  const confirmSave = useCallback(async () => {
    const key = pendingClose
    if (!key) return
    setSavingClose(true)
    try {
      const tab = tabs.find((t) => t.key === key)
      const entry = mdDraftsRef.current.get(key)
      if (tab && tab.kind !== 'pdf' && entry) {
        const put = tab.kind === 'notes' ? putNotes : putDiscussion
        await put(tab.paperId, entry.draft)
      } else {
        await handlesRef.current.get(key)?.flush()
      }
    } catch (err) {
      console.error('Failed to save before closing tab:', err)
      // The write failed — keep the tab open and the draft intact rather than
      // closing and silently losing the edit. Mirrors PDF flush keeping dirty.
      setSavingClose(false)
      setPendingClose(null)
      return
    }
    setSavingClose(false)
    setPendingClose(null)
    removeTab(key)
  }, [pendingClose, removeTab, tabs])

  const confirmDiscard = useCallback(() => {
    const key = pendingClose
    if (!key) return
    handlesRef.current.get(key)?.discard()
    setPendingClose(null)
    removeTab(key) // also drops the md draft entry
  }, [pendingClose, removeTab])

  const cancelClose = useCallback(() => setPendingClose(null), [])

  // The tab pending close, and whether it is an md tab (drives the dialog copy
  // and the body noun: an md tab has unsaved "edits", not "annotations").
  const pendingTab = pendingClose
    ? tabs.find((t) => t.key === pendingClose) ?? null
    : null
  const pendingIsMd = pendingTab !== null && pendingTab.kind !== 'pdf'

  // Warn before a full-page unload (browser/tab close, reload) if any PDF has
  // unsaved annotations OR any md tab has an unsaved draft — the in-app close
  // prompt cannot run there.
  useEffect(() => {
    const onBeforeUnload = (e: BeforeUnloadEvent) => {
      const pdfDirty = [...handlesRef.current.values()].some((h) => h.isDirty())
      const mdDirty = [...mdDraftsRef.current.values()].some(
        (d) => d.draft !== d.savedText,
      )
      if (pdfDirty || mdDirty) {
        e.preventDefault()
        e.returnValue = ''
        return
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

  // --- Vault switch (3c-2) -------------------------------------------------
  // Switching is global (registry active) + closes every tab. App owns the
  // orchestration: flush any unsaved annotations/notes, PUT the switch (the
  // server repoints in place), then reload all vault-scoped data and reset the
  // tab / selection / filter state for the new vault.

  const switchVault = useCallback(
    (name: string) => {
      if (!name || name === vaults?.active) return
      setPendingVault(name)
    },
    [vaults],
  )

  // Unsaved tabs at the moment the confirm opens (PDF annotations + md drafts),
  // for the dialog's "N will be saved" note. Recomputed when the dialog opens.
  const dirtyTabCount = useMemo(() => {
    let n = 0
    for (const h of handlesRef.current.values()) if (h.isDirty()) n++
    for (const d of mdDraftsRef.current.values()) if (d.draft !== d.savedText) n++
    return n
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [pendingVault])

  // Flush every dirty tab before a switch (the tabs are about to close). PDF
  // annotations embed via their handle; md drafts PUT the lifted text. Throws on
  // the first failure so the caller aborts the switch and keeps the edits.
  const flushAllDirty = useCallback(async () => {
    for (const handle of handlesRef.current.values()) {
      if (handle.isDirty()) await handle.flush()
    }
    for (const [key, entry] of mdDraftsRef.current) {
      if (entry.draft === entry.savedText) continue
      const tab = tabs.find((t) => t.key === key)
      if (tab && tab.kind !== 'pdf') {
        const put = tab.kind === 'notes' ? putNotes : putDiscussion
        await put(tab.paperId, entry.draft)
      }
    }
  }, [tabs])

  // Reload everything for the now-active vault and reset per-vault view state
  // (the old vault's tabs / selection / filters / scope no longer apply).
  const reloadForVault = useCallback(() => {
    setTabs([])
    setActiveTab(null)
    setSelectedId(null)
    setCockpitPaper(null)
    setMdDrafts(new Map())
    // Tabs are gone, so each PdfView unmounts and fires registerPdf(null) on a
    // now-absent key (a harmless delete); clear the handle map directly rather
    // than wait on those callbacks.
    handlesRef.current.clear()
    setProjectScope(null)
    setFilters(emptyFilters())
    setSearch('')
    setServerHits([])
    setMdJump(null)
    fetchVaults().then(setVaults)
    fetchProjects().then(setProjects)
    fetchTaxonomy().then(setTaxonomy)
    fetchFixedEnums().then(setFixedEnums)
    fetchPapers().then(setAllPapers)
    loadList(listMode)
  }, [loadList, listMode])

  const confirmSwitchVault = useCallback(async () => {
    const name = pendingVault
    if (!name) return
    setSwitchingVault(true)
    try {
      await flushAllDirty()
      await putActiveVault(name)
      reloadForVault()
      setPendingVault(null)
    } catch (err) {
      notify(err instanceof Error ? err.message : String(err))
    } finally {
      setSwitchingVault(false)
    }
  }, [pendingVault, flushAllDirty, reloadForVault, notify])

  // --- Keyboard shortcuts wiring (Phase 4) ---------------------------------
  // Panel toggles for `[` / `]` (focus mode keeps the same setters).
  const toggleLeft = useCallback(() => setLeftCollapsed((c) => !c), [])
  const toggleRight = useCallback(() => setCockpitCollapsed((c) => !c), [])
  const toggleCheatSheet = useCallback(() => setCheatSheetOpen((o) => !o), [])
  const closeCheatSheet = useCallback(() => setCheatSheetOpen(false), [])

  // PDF-tool keys (V/H/T/D/Esc) only act when the active center tab is a PDF
  // tab; the handle is resolved live from the ref Map (see getPdfHandle's note).
  const activeTabKind = useMemo(
    () => tabs.find((t) => t.key === activeTab)?.kind ?? null,
    [tabs, activeTab],
  )
  const pdfActive = activeTabKind === 'pdf'
  const getPdfHandle = useCallback(
    () => (activeTab ? handlesRef.current.get(activeTab) ?? null : null),
    [activeTab],
  )

  // ⌥R toasts a post-write hint with the undo affordance (AC B5 ②). Wrap the
  // cockpit handle's triggerRead so the shortcut path adds that toast on top of
  // the same markRead write (the cockpit button itself stays toast-free — the
  // visible date in the panel is its own feedback). ⌥-actions are no-ops without
  // a selection, handled in the dispatcher; the wrapper only adds the toast.
  const shortcutCockpit = useMemo<CockpitHandle | null>(() => {
    if (!cockpitHandle) return null
    return {
      ...cockpitHandle,
      triggerRead: () => {
        cockpitHandle.triggerRead()
        notify('已标记已读 · ⌥⇧R 撤销')
      },
    }
  }, [cockpitHandle, notify])

  // A blocking surface is up when a SaveDialog / SwitchVaultDialog is pending, a
  // cockpit confirm/panel is open, or the TopBar Projects manager is open. The
  // cheat sheet is deliberately EXCLUDED — it is a non-blocking overlay the
  // dispatcher owns (so `?`/Esc keep toggling it). While anyModalOpen, the
  // dispatcher suppresses every global shortcut and lets the modal own its keys.
  const anyModalOpen =
    pendingClose !== null ||
    pendingVault !== null ||
    cockpitModalOpen ||
    projectsOpen

  useKeyboardShortcuts({
    anyModalOpen,
    toggleFocus,
    toggleDark,
    toggleLeft,
    toggleRight,
    cheatSheetOpen,
    toggleCheatSheet,
    closeCheatSheet,
    pdfActive,
    getPdfHandle,
    selectedId,
    cockpit: shortcutCockpit,
    notify,
  })

  return (
    // `relative` anchors the focus-mode TopBar, which detaches to `absolute`
    // and slides in/out from the top edge (see TopBar) so the reading area
    // reclaims the header's height.
    <div className="relative flex h-full flex-col bg-stone-100 text-stone-800 antialiased">
      <TopBar
        vaults={vaults}
        projects={projects}
        allPapers={allPapers}
        search={search}
        onSearch={setSearch}
        searchCandidates={searchCandidates}
        searchLoading={searchLoading}
        onSelectResult={onSearchSelect}
        focusMode={focusMode}
        onToggleFocus={toggleFocus}
        onProjectsChanged={refreshProjects}
        onSwitchVault={switchVault}
        switching={switchingVault}
        notify={notify}
        dark={dark}
        onToggleDark={toggleDark}
        onProjectsOpenChange={setProjectsOpen}
        onShowShortcuts={toggleCheatSheet}
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
          onOpenPaper={openWikilink}
          onRegisterPdf={registerPdf}
          onNotify={notify}
          mdJump={mdJump}
          mdDraft={activeTab ? mdDrafts.get(activeTab) : undefined}
          onMdBeginEdit={mdBeginEdit}
          onMdDraftChange={mdDraftChange}
          onMdEndEdit={mdEndEdit}
        />
        <Cockpit
          paper={cockpitPaper}
          loading={cockpitLoading}
          collapsed={cockpitCollapsed}
          onToggle={() => setCockpitCollapsed((c) => !c)}
          onOpenPaper={openPdf}
          vaultPath={vaultPath}
          taxonomy={taxonomy}
          projects={projects}
          allPapers={allPapers}
          fixedEnums={fixedEnums}
          onChanged={refreshAfterWrite}
          onVocabChanged={refreshVocab}
          notify={notify}
          onRegisterHandle={registerCockpit}
          onModalState={setCockpitModalOpen}
        />
      </div>
      {pendingClose && (
        <SaveDialog
          label={pendingTab?.label ?? pendingClose}
          saving={savingClose}
          onSave={confirmSave}
          onDiscard={confirmDiscard}
          onCancel={cancelClose}
          title={pendingIsMd ? 'Save edits?' : undefined}
          bodyNoun={pendingIsMd ? 'unsaved edits' : undefined}
        />
      )}
      {pendingVault && (
        <SwitchVaultDialog
          targetName={pendingVault}
          tabCount={tabs.length}
          dirtyCount={dirtyTabCount}
          switching={switchingVault}
          onCancel={() => setPendingVault(null)}
          onConfirm={confirmSwitchVault}
        />
      )}
      {cheatSheetOpen && <CheatSheet onClose={closeCheatSheet} />}
      {toast && (
        <Toast
          message={toast.message}
          variant={toast.variant}
          onDismiss={() => setToast(null)}
        />
      )}
    </div>
  )
}
