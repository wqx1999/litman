import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import {
  ApiError,
  createVault,
  fetchCapabilities,
  fetchDocMtimes,
  fetchFixedEnums,
  fetchPaper,
  fetchPapers,
  fetchProjects,
  fetchTaxonomy,
  fetchTrash,
  fetchSearch,
  fetchVaults,
  fetchVersion,
  putActiveVault,
  putDiscussion,
  putNotes,
  registerVault,
  removePaper,
  restorePaper,
  setVaultPath,
  unregisterVault,
} from './api'
import type { PdfHandle } from './pdf/PdfView'
import type { MdDraft } from './md/MdView'
import type { CockpitHandle } from './cockpit/Cockpit'
import { useKeyboardShortcuts } from './useKeyboardShortcuts'
import CheatSheet from './ui/CheatSheet'
import SaveDialog from './tabs/SaveDialog'
import RemovePaperConfirm from './tabs/RemovePaperConfirm'
import { mergeCandidates, type Candidate } from './search'
import type {
  ActivityLogEntry,
  DocMtimes,
  FixedEnums,
  IndexPaper,
  PaperMeta,
  ProjectEntry,
  SearchHit,
  SmartListView,
  Tab,
  TabKind,
  Taxonomy,
  TrashEntry,
  VaultsPayload,
} from './types'
import TopBar from './topbar/TopBar'
import WelcomePage from './welcome/WelcomePage'
import TrashView from './trash/TrashView'
import SwitchVaultDialog from './topbar/SwitchVaultDialog'
import BrowsePanel from './nav/BrowsePanel'
import type { FacetKey, Filters, ListMode } from './nav/BrowsePanel'
import { emptyFilters } from './nav/BrowsePanel'
import TabArea from './tabs/TabArea'
import Cockpit from './cockpit/Cockpit'
import Toast, { type ToastVariant } from './ui/Toast'

const SMART_VIEWS: ReadonlySet<string> = new Set(['reading', 'recent-read'])

// Link advisory, dismissed for good. localStorage (not the server) because
// this is a per-person "yes, I know" — nothing about the library changed, and a
// second machine reading the same vault deserves to be told once too.
const LINK_NOTICE_DISMISSED = 'litman.linkNoticeDismissed'

// Single-value fields filter on `p[f]` (string | null); array fields filter on
// `p[f]` (string[]). Status is filtered in the `visible` pipeline like the rest
// now — dropped is never hidden; every view (all/reading/recent-read) shows it,
// muted, so a set-aside paper never vanishes from the list.
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

// --- Resync change-log diff (pure) -----------------------------------------
// A resync (focus / visibility / manual refresh) pulls fresh vault state; this
// diffs it against the previous in-memory snapshot and synthesizes one activity-
// log entry per net change made OUTSIDE the GUI (a `lit` command, an agent
// writing notes.md). Pure read — no writes (invariant #16); see the task spec
// §5.2 and §9 (D3/D4/D7).

/** The state a resync diffs: the last-seen truth vs the freshly-fetched truth. */
interface ResyncSnapshot {
  papers: IndexPaper[]
  taxonomy: Taxonomy | null
  projects: ProjectEntry[]
  trash: TrashEntry[]
  mtimes: DocMtimes | null
}

// Per-paper tag arrays, with the singular label used in the message. `data` has
// NO trailing-s to strip, so it is spelled out (not derived from the key).
const RESYNC_TAG_FIELDS: ReadonlyArray<{
  key: 'topics' | 'methods' | 'data' | 'projects'
  label: string
}> = [
  { key: 'topics', label: 'topic' },
  { key: 'methods', label: 'method' },
  { key: 'data', label: 'data' },
  { key: 'projects', label: 'project' },
]

// Taxonomy keys the diff inspects (D3): `projects` is owned by the projects-
// registry diff (so it is excluded here — no duplicate entry), and the fixed
// enums (type/status/priority) are never diffed.
const RESYNC_TAXONOMY_KEYS: ReadonlyArray<'topics' | 'methods' | 'data'> = [
  'topics',
  'methods',
  'data',
]

/** Null/empty scalar → em dash (D7), e.g. `priority — → B`. */
function resyncScalar(v: string | null | undefined): string {
  return v == null || v === '' ? '—' : v
}

function diffResync(
  prev: ResyncSnapshot,
  fresh: ResyncSnapshot,
): ActivityLogEntry[] {
  // Structured changes have no real timestamp → stamp detection time. Free-form
  // (notes/discussion) changes carry the file mtime. appendLog sorts the batch
  // by ts ascending (D4) so the panel orders them sensibly.
  const now = Date.now()
  const out: ActivityLogEntry[] = []
  const add = (message: string, variant: ToastVariant = 'info') =>
    out.push({ ts: now, variant, message })

  // Papers: add / remove (→ trashed when it lands in trash) / per-survivor diff.
  const prevById = new Map(prev.papers.map((p) => [p.id, p]))
  const freshById = new Map(fresh.papers.map((p) => [p.id, p]))
  const trashedIds = new Set(fresh.trash.map((t) => t.paperId))

  for (const p of fresh.papers) {
    if (!prevById.has(p.id)) add(`+ ${p.id} added`, 'success')
  }
  for (const p of prev.papers) {
    if (!freshById.has(p.id)) {
      add(trashedIds.has(p.id) ? `🗑 ${p.id} trashed` : `− ${p.id} removed`)
    }
  }
  for (const [id, f] of freshById) {
    const p = prevById.get(id)
    if (!p) continue
    if (f.status !== p.status)
      add(`${id}: status ${resyncScalar(p.status)} → ${resyncScalar(f.status)}`)
    if (f.priority !== p.priority)
      add(`${id}: priority ${resyncScalar(p.priority)} → ${resyncScalar(f.priority)}`)
    if (f.type !== p.type)
      add(`${id}: type ${resyncScalar(p.type)} → ${resyncScalar(f.type)}`)
    const prevRead = p['read-date']
    const freshRead = f['read-date']
    if (!prevRead && freshRead) add(`${id}: marked read`)
    else if (prevRead && !freshRead) add(`${id}: read-date cleared`)
    for (const { key, label } of RESYNC_TAG_FIELDS) {
      const before = new Set(p[key] ?? [])
      const after = new Set(f[key] ?? [])
      for (const v of after) if (!before.has(v)) add(`${id}: +${label} ${v}`)
      for (const v of before) if (!after.has(v)) add(`${id}: −${label} ${v}`)
    }
  }

  // Taxonomy (topics/methods/data only — D3).
  if (prev.taxonomy && fresh.taxonomy) {
    for (const key of RESYNC_TAXONOMY_KEYS) {
      const before = new Set(prev.taxonomy[key] ?? [])
      const after = new Set(fresh.taxonomy[key] ?? [])
      for (const v of after) if (!before.has(v)) add(`taxonomy: +${key} ${v}`)
      for (const v of before) if (!after.has(v)) add(`taxonomy: −${key} ${v}`)
    }
  }

  // Projects registry (owns project add/remove — D3; a rename reads as one
  // removed + one added, which is acceptable).
  const prevProjects = new Set(prev.projects.map((p) => p.name))
  const freshProjects = new Set(fresh.projects.map((p) => p.name))
  for (const name of freshProjects)
    if (!prevProjects.has(name)) add(`project: + ${name}`)
  for (const name of prevProjects)
    if (!freshProjects.has(name)) add(`project: − ${name}`)

  // Doc mtimes: a notes/discussion edit made outside the GUI bumps the file
  // mtime. Only papers tracked in the previous snapshot are compared — a brand-
  // new paper is already covered by its "+ added" entry, so its file is not
  // double-logged. The entry ts is the real edit time (mtime × 1000).
  if (prev.mtimes && fresh.mtimes) {
    for (const [id, before] of Object.entries(prev.mtimes)) {
      const after = fresh.mtimes[id]
      if (!after) continue
      if (after.notes != null && (before.notes == null || after.notes > before.notes))
        out.push({ ts: after.notes * 1000, variant: 'info', message: `${id}: notes updated` })
      if (
        after.discussion != null &&
        (before.discussion == null || after.discussion > before.discussion)
      )
        out.push({
          ts: after.discussion * 1000,
          variant: 'info',
          message: `${id}: discussion appended`,
        })
    }
  }

  return out
}

/** A fetch that failed at the network level (connection refused, tunnel down)
 * rejects with a TypeError; an HTTP error Response reaches the api helpers,
 * which throw an ApiError. Only the former means "server unreachable". */
function isNetworkError(err: unknown): boolean {
  return err instanceof TypeError
}

/** The server can no longer see the library it is bound to — the vault directory
 * was moved, renamed or deleted while the GUI was open. The server's vault guard
 * answers 410 before any route runs, so this arrives over a perfectly healthy
 * connection: it is not a failed request, it is a lost library. Kept strictly
 * apart from the 409 "no vault yet" that raises the welcome page — a user whose
 * library merely moved must never be invited to create a new one on top of it. */
function isVaultGone(err: unknown): boolean {
  return err instanceof ApiError && err.status === 410
}

export default function App() {
  const [vaults, setVaults] = useState<VaultsPayload | null>(null)
  // The vault this server is serving: `undefined` = not yet known (pre-bootstrap),
  // `null` = none (render the welcome page), a string = a real vault (render the
  // normal three-column view). Gates the mount seed + list load so a no-vault
  // server never fires a storm of 409s behind the welcome page.
  const [served, setServed] = useState<string | null | undefined>(undefined)
  // The newer litman version on PyPI (null = up to date / unknown). Read once on
  // mount from the server's update-check cache; drives the TopBar update dot.
  const [updateLatest, setUpdateLatest] = useState<string | null>(null)
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
  // Whether the full INDEX has been fetched successfully at least once. Gates
  // the vault-empty distinction (an unfetched or failed [] must never read as
  // "your vault is empty" — that state belongs only to a confirmed 0-paper
  // INDEX). Stays true across vault switches: reloadForVault re-fetches without
  // clearing, so allPapers is always a real (possibly stale-for-a-tick) INDEX.
  const [allLoaded, setAllLoaded] = useState(false)
  // Server reachability. Set when a read-path fetch fails at the network level
  // (server down, SSH tunnel dropped — fetch rejects with a TypeError), cleared
  // by the next successful read. Drives the top-center banner plus a 5s retry
  // loop below. An HTTP error status deliberately does NOT trip this: that is
  // a server bug, not a disconnect, and keeps its existing error paths.
  const [disconnected, setDisconnected] = useState(false)
  // The library this server is bound to is no longer on disk (410 from the vault
  // guard): the user moved, renamed or deleted the directory while the GUI was
  // open. Drives its own banner and shares the 5s retry loop, so putting the
  // folder back heals the session with no restart.
  const [vaultGone, setVaultGone] = useState(false)
  // This drive cannot hold folder links at all (FAT32 / exFAT, network
  // shares — POSIX symlinks and Windows junctions alike), so views/ and the
  // litman_reflib / litman_code project shortcuts are silently absent. The
  // library is FINE — this is an advisory, not an error, and it is the only
  // channel a GUI-only user has: the CLI's warning goes to stderr and the
  // desktop shortcut launches the console-less `litw`, so nobody ever sees
  // it. Dismissable, and the dismissal sticks. Never shown for a working
  // mechanism ('symlink' / 'junction'): those are silent full-function states.
  const [linkNotice, setLinkNotice] = useState(false)
  // One place decides what a failed read means, so every catch site agrees.
  // Each verdict clears the other flag, so the banners are mutually exclusive
  // and the one showing is always the FRESHER fact: a 410 arrived over a
  // working connection (proof the server is reachable), and a network error
  // means nothing can currently back the claim that the library is gone — if
  // the server comes back still missing it, the next sweep's 410 re-raises
  // the red banner.
  const classifyFetchError = useCallback((err: unknown) => {
    if (isVaultGone(err)) {
      setVaultGone(true)
      setDisconnected(false)
      // Refresh the vault list on its OWN request, decoupled from the read
      // sweep that just 410'd. `GET /api/vaults` is whitelisted, so it answers
      // even in the gone state with `exists:false` for the moved vault — but in
      // doResync it shares one all-or-nothing `Promise.all` with `fetchPapers`,
      // whose 410 rejects the whole batch and discards this payload. Without a
      // committed `exists:false`, the manager's Locate button (gated on
      // `!v.exists`) never renders and the user is stuck. Best-effort: a failure
      // here just leaves the last-known list, which the next sweep repairs.
      void fetchVaults()
        .then((v) => {
          setVaults(v)
          setServed(v.served)
        })
        .catch(() => {})
    } else if (isNetworkError(err)) {
      setDisconnected(true)
      setVaultGone(false)
    } else if (err instanceof ApiError) {
      // Any other HTTP status proves both facts these flags track: a response
      // arrived (not disconnected), and the 410 guard — which runs before
      // every /api route — let the request through (vault not gone). Without
      // this arm, one sweep route failing persistently (a 500, a 409) froze
      // whichever banner was up: the sweep kept rejecting, neither branch
      // above matched, and the red "library is gone" banner outlived the
      // relocate that had already healed the server. The failure itself still
      // surfaces through its own channels (the list-failure line, the toasts);
      // these flags only ever claim what the error can prove.
      setVaultGone(false)
      setDisconnected(false)
    }
  }, [])
  // The current list fetch failed — BrowsePanel renders an explicit failure
  // line instead of an empty state that would read as "no papers".
  const [listFailed, setListFailed] = useState(false)
  // notes/discussion hits from /api/search (debounced, async). The token guards
  // against an earlier slower response overwriting a newer one.
  const [serverHits, setServerHits] = useState<SearchHit[]>([])
  const [searchLoading, setSearchLoading] = useState(false)
  const searchToken = useRef(0)

  // A single transient toast (dangling wikilink, failed save, project linked).
  // Last write wins; `variant` tints the dot (default neutral 'info').
  const [toast, setToast] = useState<{
    message: string
    variant: ToastVariant
    sticky?: boolean
  } | null>(null)
  // Session activity log: every notify (toast) also appends here so a GUI-only
  // user has a scrollback the last-write-wins toast can't give. In-memory ring
  // (cap 200, newest kept), cleared on refresh (AC4 — no persistence, no second
  // write path to disk, invariant #16). `logUnread` lights a dot on the log icon
  // until the panel is opened.
  const [activityLog, setActivityLog] = useState<ActivityLogEntry[]>([])
  const [logUnread, setLogUnread] = useState(false)
  const notify = useCallback(
    (msg: string, variant: ToastVariant = 'info', opts?: { sticky?: boolean }) => {
      setToast({ message: msg, variant, sticky: opts?.sticky })
      setActivityLog((prev) =>
        [...prev, { ts: Date.now(), variant, message: msg }].slice(-200),
      )
      setLogUnread(true)
    },
    [],
  )
  const markLogRead = useCallback(() => setLogUnread(false), [])

  // Silent log sink for resync-diff entries: appends to the same ring + lights
  // the unread dot like notify, but does NOT toast (a resync can yield dozens of
  // entries — see §5.2). Each batch is sorted ts-ascending first (D4) so the
  // panel (newest-first) interleaves mtime-stamped notes entries and detection-
  // time structured entries sensibly.
  const appendLog = useCallback((entries: ActivityLogEntry[]) => {
    if (entries.length === 0) return
    const sorted = [...entries].sort((a, b) => a.ts - b.ts)
    setActivityLog((prev) => [...prev, ...sorted].slice(-200))
    setLogUnread(true)
  }, [])

  const [tabs, setTabs] = useState<Tab[]>([])
  const [activeTab, setActiveTab] = useState<string | null>(null)
  const [selectedId, setSelectedId] = useState<string | null>(null)
  // A notes/discussion tab opened from a search hit, plus the query to scroll to
  // and highlight inside it. Keyed by tab so only that doc highlights.
  const [mdJump, setMdJump] = useState<{ key: string; query: string } | null>(null)

  // Bumped on a resync-from-disk so the active md tab (notes/discussion) re-reads
  // its file — CLI/agent edits to notes.md don't reach an already-open tab
  // otherwise. The draft (mdDrafts, App-owned) is independent of the rendered
  // text, so MdView refetching the file never clobbers an in-progress GUI edit.
  const [mdReloadToken, setMdReloadToken] = useState(0)

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

  // Paper id awaiting a soft-delete decision (drives RemovePaperConfirm), and
  // whether the `lit rm` DELETE is in flight.
  const [pendingRemove, setPendingRemove] = useState<string | null>(null)
  const [removing, setRemoving] = useState(false)

  // Vault switch (3c-2): the target awaiting confirmation, and whether the
  // switch (flush + PUT + reload) is in flight.
  const [pendingVault, setPendingVault] = useState<string | null>(null)
  const [switchingVault, setSwitchingVault] = useState(false)

  // Trash (recovery) view (Phase 4.9): full-screen read-only mode over this
  // vault's .trash/. `trashEntries` backs both the left-nav footer count and the
  // view's list; `restoringEntry` is the entry whose restore is in flight.
  const [trashMode, setTrashMode] = useState(false)
  const [trashEntries, setTrashEntries] = useState<TrashEntry[]>([])
  const [trashLoading, setTrashLoading] = useState(false)
  const [restoringEntry, setRestoringEntry] = useState<string | null>(null)

  // --- Resync diff baseline (activity-log change detection) ----------------
  // doResync diffs the previously-seen vault state against fresh data. These
  // refs are the baseline. The four state-backed ones are MIRRORED from state
  // via tiny effects (D1), so EVERY commit path (mount, refreshAfterWrite,
  // confirmRemove, restoreFromTrash, reloadForVault) advances the baseline
  // automatically — a resync-only snapshot would re-log a GUI self-write as an
  // external change. docMtimes is the exception: never rendered, so it is a
  // ref-only baseline seeded on mount + advanced in doResync and on a GUI md
  // save (D2). DIFF against the full INDEX (allPapers), never the filtered list.
  const allPapersRef = useRef<IndexPaper[]>([])
  const taxonomyRef = useRef<Taxonomy | null>(null)
  const projectsRef = useRef<ProjectEntry[]>([])
  const trashRef = useRef<TrashEntry[]>([])
  useEffect(() => {
    allPapersRef.current = allPapers
  }, [allPapers])
  useEffect(() => {
    taxonomyRef.current = taxonomy
  }, [taxonomy])
  useEffect(() => {
    projectsRef.current = projects
  }, [projects])
  useEffect(() => {
    trashRef.current = trashEntries
  }, [trashEntries])
  const docMtimesRef = useRef<DocMtimes | null>(null)
  // Flipped true only after the mount seed Promise.all settles (D5), so the
  // first resync has a real baseline (never logs every paper as "+ added").
  const resyncReadyRef = useRef(false)

  // After a GUI md save (MdView, save-on-close, save-on-vault-switch) advance the
  // doc-mtime baseline to "now" (≥ the file's just-written mtime, single-machine
  // localhost so no clock skew) so the next resync diff sees no increase and does
  // NOT mislabel the user's own edit as external (D2). No-op until seeded.
  const bumpDocMtime = useCallback((paperId: string, doc: 'notes' | 'discussion') => {
    const cur = docMtimesRef.current
    if (!cur) return
    const next = { ...(cur[paperId] ?? { notes: null, discussion: null }) }
    next[doc] = Date.now() / 1000
    // Clone-on-write, not in-place: an in-flight doResync holds this object as its
    // prev.mtimes baseline; replacing the ref leaves that snapshot untouched.
    docMtimesRef.current = { ...cur, [paperId]: next }
  }, [])

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
  // True while either observability panel (activity log / health) is open, so
  // the shortcut dispatcher's modal guard suppresses global write-shortcuts
  // behind them — mirrors projectsOpen.
  const [observabilityOpen, setObservabilityOpen] = useState(false)
  // True while the vault-manager panel (or its nested register form / unregister
  // confirm) is open, so the shortcut dispatcher's modal guard suppresses global
  // write-shortcuts behind it — mirrors projectsOpen / observabilityOpen.
  const [vaultManagerOpen, setVaultManagerOpen] = useState(false)
  // Same mirror for the agent panel (picker / copy box).
  const [agentPanelOpen, setAgentPanelOpen] = useState(false)
  // TopBar's agent opener, registered on mount, so the `` ` `` shortcut drives
  // the same handler as the button. State (not a bare ref) because the shortcut
  // hook takes it as a dep. The setState updater form is required: a function
  // value would otherwise be mistaken for an updater and invoked.
  const [openAgent, setOpenAgent] = useState<(() => void) | null>(null)
  const registerAgentOpen = useCallback(
    (open: (() => void) | null) => setOpenAgent(() => open),
    [],
  )
  // Secondary action on the same toolbar icon: Ctrl+~ opens management while
  // plain ~ remains the fast path that launches the configured default.
  const [manageAgents, setManageAgents] = useState<(() => void) | null>(null)
  const registerAgentManage = useCallback(
    (open: (() => void) | null) => setManageAgents(() => open),
    [],
  )
  // Same idiom for TopBar's vault manager, so the vault-gone banner's button and
  // the toolbar's vault icon open the one panel — the banner is a shortcut to an
  // existing door, not a second door.
  const [openVaultManager, setOpenVaultManager] = useState<(() => void) | null>(null)
  const registerVaultManagerOpen = useCallback(
    (open: (() => void) | null) => setOpenVaultManager(() => open),
    [],
  )
  // The Cockpit's imperative handle (curation triggers for ⌥-shortcuts). A ref
  // so registering it doesn't re-render; a state copy drives the hook's deps.
  const [cockpitHandle, setCockpitHandle] = useState<CockpitHandle | null>(null)
  const registerCockpit = useCallback(
    (handle: CockpitHandle | null) => setCockpitHandle(handle),
    [],
  )

  const loadList = useCallback(
    (mode: ListMode) => {
      setLoadingList(true)
      const view = SMART_VIEWS.has(mode) ? (mode as SmartListView) : undefined
      fetchPapers(view)
        .then((ps) => {
          setPapers(ps)
          setListFailed(false)
          // A guarded read landed: the server answered AND the library is on
          // disk (the 410 guard runs before every list route), so both banners
          // can retire — not just the amber one.
          setDisconnected(false)
          setVaultGone(false)
        })
        .catch((err) => {
          setListFailed(true)
          classifyFetchError(err)
        })
        .finally(() => setLoadingList(false))
    },
    [classifyFetchError],
  )

  // Refresh this vault's trash list (backs the left-nav footer count + the trash
  // view). Cheap (cap-100, one bounded call), so it is pulled on mount and again
  // after any delete / restore so the count stays live.
  const loadTrash = useCallback(() => {
    setTrashLoading(true)
    return fetchTrash()
      .then(setTrashEntries)
      .catch((err) => {
        // The empty fallback keeps the footer count harmless, but the failure
        // itself must still be classified — the trash view may be the only
        // thing being fetched (trash mode), and a 410 here has to raise the
        // vault-gone banner like everywhere else.
        classifyFetchError(err)
        setTrashEntries([])
      })
      .finally(() => setTrashLoading(false))
  }, [classifyFetchError])

  // Bootstrap: learn which vault (if any) the server is serving. This runs
  // BEFORE the heavy seed below so a no-vault server renders the welcome page
  // instead of firing a storm of 409s. Re-run by the welcome page after it
  // creates or opens a vault (which flips `served` and mounts the normal view).
  const bootstrap = useCallback(() => {
    return fetchVaults()
      .then((v) => {
        setVaults(v)
        setServed(v.served)
      })
      .catch(classifyFetchError)
  }, [classifyFetchError])

  useEffect(() => {
    void bootstrap()
  }, [bootstrap])

  useEffect(() => {
    // Only seed once a real vault is being served: `undefined` = still
    // bootstrapping, `null` = welcome page (no vault to seed from).
    if (!served) return
    fetchFixedEnums().then(setFixedEnums).catch(classifyFetchError)
    // Update-check badge: pure cache read, best-effort (a failure just leaves
    // the dot off — this is a passive reminder, never blocking).
    fetchVersion()
      .then((v) => setUpdateLatest(v.latest))
      .catch(() => {})
    // Link-capability advisory: cheap (one cached probe server-side) so it
    // can run on load, unlike the Tier-2 health panel. Best-effort — a failure
    // just leaves the notice off; it must never block or error the boot path.
    if (localStorage.getItem(LINK_NOTICE_DISMISSED) !== '1') {
      fetchCapabilities()
        .then((c) => {
          if (c.links === 'none') setLinkNotice(true)
        })
        .catch(() => {})
    }
    // Seed the resync diff baseline (D5): gate resyncReadyRef on a Promise.all
    // over ALL of papers / taxonomy / projects / trash / doc-mtimes so the first
    // resync never diffs against an empty baseline (which would log every paper
    // as "+ added"). The four state-backed refs seed themselves via the mirror
    // effects above; docMtimesRef (never rendered) is seeded here directly. The
    // full INDEX (no view) also backs global id/title matching + wikilink lookup.
    void Promise.all([
      fetchPapers().then((ps) => {
        setAllPapers(ps)
        setAllLoaded(true)
      }),
      fetchTaxonomy().then(setTaxonomy),
      fetchProjects().then(setProjects),
      loadTrash(),
      fetchDocMtimes().then((m) => {
        docMtimesRef.current = m
      }),
    ])
      .then(() => {
        // Open the diff gate ONLY when the whole seed succeeded. On a partial
        // failure (e.g. papers rejects while doc-mtimes resolves) leave it shut:
        // the first resync then takes the seed-only branch and the second one
        // diffs correctly — self-heal instead of logging every paper as "+ added".
        resyncReadyRef.current = true
      })
      .catch((err) => {
        // Swallow the rejected Promise.all so it isn't an unhandled rejection;
        // the gate stays false and a later resync re-seeds. A network-level
        // failure additionally raises the disconnected banner, a vanished
        // library the vault-gone one.
        classifyFetchError(err)
      })
  }, [served, loadTrash, classifyFetchError])

  useEffect(() => {
    if (!served) return
    loadList(listMode)
  }, [served, listMode, loadList])

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
  // smart-lists (reading/recent-read) already came pre-ordered from the server
  // (dropped included, muted), so project scope is the only narrowing here.
  const scoped = useMemo(() => {
    if (!projectScope) return papers
    return papers.filter((p) => (p.projects || []).includes(projectScope))
  }, [papers, projectScope])

  // A truly empty vault: the full INDEX was fetched and holds zero papers.
  // Distinct from "the current view/filters match nothing" — BrowsePanel shows
  // a getting-started card for the former and a plain no-match line otherwise.
  const vaultEmpty = allLoaded && allPapers.length === 0

  // Multi-dimensional filter: cross-dimension AND, within-dimension OR.
  const visible = useMemo(() => {
    let out = scoped
    // STATUS (single, OR). Every view shows dropped now — "all" means all, and
    // reading/recent-read keep a dropped paper in place (unread → reading, read
    // → recent-read) so it is never a ghost (invisible but still rotting in the
    // vault). It is rendered muted + tagged in the list instead (see
    // BrowsePanel) — the grey pile IS the anti-drift signal.
    const st = filters.status
    if (st.size > 0) {
      out = out.filter((p) => p.status != null && st.has(p.status))
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

  const selectPaper = useCallback(
    (id: string) => {
      setSelectedId(id)
      setCockpitLoading(true)
      fetchPaper(id)
        .then(setCockpitPaper)
        .catch((err) => {
          classifyFetchError(err)
          setCockpitPaper(null)
        })
        .finally(() => setCockpitLoading(false))
    },
    [classifyFetchError],
  )

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
    fetchPapers().then((ps) => {
      setAllPapers(ps)
      setAllLoaded(true)
    })
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

  // Re-pull every on-disk-derived view so changes made OUTSIDE the GUI (a CLI
  // command, an agent writing notes.md, a project registered from the terminal)
  // surface without a manual browser refresh, AND synthesize an activity-log
  // entry per net change (diff vs the snapshot refs). Non-destructive — unlike
  // reloadForVault it keeps open tabs / selection / filters; it only re-reads:
  // the full INDEX + current list, the taxonomy + projects, the trash, the
  // doc-mtimes, the vault registry, and the active md tab (token bump). A pure
  // read sweep — no writes, so invariant #16 is untouched. The diff runs ONLY
  // here (the resync path), never in the direct refreshAfterWrite a GUI write
  // fires, so a GUI action is not double-logged (red line #3).
  const doResync = useCallback(async () => {
    // Snapshot the last-seen truth BEFORE fresh data lands; the refs are kept
    // mirrored from every commit path (D1), so this is the true prior baseline.
    const prev: ResyncSnapshot = {
      papers: allPapersRef.current,
      taxonomy: taxonomyRef.current,
      projects: projectsRef.current,
      trash: trashRef.current,
      mtimes: docMtimesRef.current,
    }
    // The first sweep (or the first after a vault switch reset docMtimes) just
    // seeds — no baseline to diff against yet (D5).
    const canDiff = resyncReadyRef.current && docMtimesRef.current !== null
    const listView = SMART_VIEWS.has(listMode)
      ? (listMode as SmartListView)
      : undefined
    try {
      const [freshAll, freshList, freshTax, freshProjects, freshTrash, freshMtimes, freshVaults] =
        await Promise.all([
          fetchPapers(),
          fetchPapers(listView),
          fetchTaxonomy(),
          fetchProjects(),
          fetchTrash(),
          fetchDocMtimes(),
          fetchVaults(),
        ])
      if (canDiff) {
        // The diff only feeds the activity log — a bug in it must degrade to
        // "no log entries", never fail the sweep: rethrowing from here would
        // skip the setters below (stale data) and leave the banner flags
        // frozen at their pre-sweep truth.
        try {
          appendLog(
            diffResync(prev, {
              papers: freshAll,
              taxonomy: freshTax,
              projects: freshProjects,
              trash: freshTrash,
              mtimes: freshMtimes,
            }),
          )
        } catch {
          // degrade silently; the fresh data still lands
        }
      }
      // Commit through the same setters the rest of the app uses (the mirror
      // effects advance the diff refs from here); docMtimesRef is ref-only.
      setAllPapers(freshAll)
      setAllLoaded(true)
      setPapers(freshList)
      setTaxonomy(freshTax)
      setProjects(freshProjects)
      setTrashEntries(freshTrash)
      setVaults(freshVaults)
      // Track the server's binding, not just the registry list: a vault switch
      // repoints the server, and the vault-gone banner names `served` — left
      // stale, it would name the PREVIOUS library's (perfectly healthy) path
      // if the current one later went missing.
      setServed(freshVaults.served)
      docMtimesRef.current = freshMtimes
      resyncReadyRef.current = true
      // A whole sweep landed — the server is reachable and the library is back
      // under the path it is bound to (moving the folder back heals the session).
      setDisconnected(false)
      setVaultGone(false)
      setListFailed(false)
      if (selectedId) {
        fetchPaper(selectedId)
          .then(setCockpitPaper)
          .catch(() => setCockpitPaper(null))
      }
      setMdReloadToken((t) => t + 1)
    } catch (err) {
      // A failed sweep is a data no-op: leave the UI and the diff baseline
      // untouched (a transient fetch error must not wipe the view or desync
      // the baseline); the next resync retries. A network-level failure raises
      // the disconnected banner, a 410 the vault-gone one — both cleared by the
      // next successful sweep. Keeping the stale list on screen is deliberate:
      // it is the last thing that was true, and the banner says so.
      classifyFetchError(err)
    }
  }, [listMode, selectedId, appendLog, classifyFetchError])

  // Auto-resync when the browser regains focus / the tab becomes visible — the
  // "go to the terminal, run CLI/agent, come back to the browser" loop. `focus`
  // fires on app switch, `visibilitychange` on browser-tab switch; they can both
  // fire on one return, so a short throttle coalesces them into one sweep. (A
  // background agent writing while the GUI stays focused won't trip this — use
  // the TopBar refresh button, which calls doResync directly, for that.)
  const lastResyncRef = useRef(0)
  useEffect(() => {
    const onReturn = () => {
      const now = Date.now()
      if (now - lastResyncRef.current < 800) return
      lastResyncRef.current = now
      doResync()
    }
    const onVisible = () => {
      if (document.visibilityState === 'visible') onReturn()
    }
    window.addEventListener('focus', onReturn)
    document.addEventListener('visibilitychange', onVisible)
    return () => {
      window.removeEventListener('focus', onReturn)
      document.removeEventListener('visibilitychange', onVisible)
    }
  }, [doResync])

  // While either banner is up, re-run the sweep every 5s so recovery is automatic
  // (banner clears, data refreshes) without waiting for a focus switch or a
  // manual refresh. The interval exists only while a banner is up: a successful
  // sweep flips both flags off, which tears it down. For the vault-gone banner
  // this is what makes "put the folder back where it was" a complete fix — the
  // session heals itself within 5s, no restart, no re-registration.
  useEffect(() => {
    if (!disconnected && !vaultGone) return
    const t = setInterval(() => void doResync(), 5000)
    return () => clearInterval(t)
  }, [disconnected, vaultGone, doResync])

  // Manual refresh (TopBar button): force an immediate sweep (bypass the throttle)
  // and confirm with a toast — this is the explicit-feedback path, whereas the
  // focus-driven sweep stays silent so alt-tabbing doesn't spam toasts.
  const manualResync = useCallback(() => {
    lastResyncRef.current = Date.now()
    doResync()
    notify('Refreshed from disk.', 'success')
  }, [doResync, notify])

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
        bumpDocMtime(tab.paperId, tab.kind) // D2: this is a GUI md save too
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
  }, [pendingClose, removeTab, tabs, bumpDocMtime])

  const confirmDiscard = useCallback(() => {
    const key = pendingClose
    if (!key) return
    handlesRef.current.get(key)?.discard()
    setPendingClose(null)
    removeTab(key) // also drops the md draft entry
  }, [pendingClose, removeTab])

  const cancelClose = useCallback(() => setPendingClose(null), [])

  const cancelRemove = useCallback(() => {
    if (!removing) setPendingRemove(null)
  }, [removing])

  // Soft-delete the paper after the confirm. Routes through the `lit rm` DELETE
  // (server moves it to .trash/ + tears down its links), then closes every tab
  // showing that paper, clears the cockpit if it was selected, and refreshes the
  // lists. A failed delete keeps the dialog open with the backend message.
  const confirmRemove = useCallback(async () => {
    const id = pendingRemove
    if (!id) return
    setRemoving(true)
    try {
      await removePaper(id)
    } catch (err) {
      notify(err instanceof Error ? err.message : String(err), 'error')
      setRemoving(false)
      return
    }
    // Close any open tabs (pdf / notes / discussion) for the removed paper —
    // their content is gone now. removeTab re-points the active tab each call.
    tabs.filter((t) => t.paperId === id).forEach((t) => removeTab(t.key))
    if (selectedId === id) {
      setSelectedId(null)
      setCockpitPaper(null)
    }
    setRemoving(false)
    setPendingRemove(null)
    notify(`Removed “${id}” to trash · restore from the 🗑 Trash`, 'success')
    // The removed paper drops out of the list + counts; do NOT re-fetch its own
    // metadata (it's gone) — just reload the list, the INDEX projection, and the
    // trash count (the paper just landed in trash).
    loadList(listMode)
    fetchPapers().then(setAllPapers)
    loadTrash()
  }, [pendingRemove, tabs, removeTab, selectedId, notify, loadList, listMode, loadTrash])

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
        bumpDocMtime(tab.paperId, tab.kind) // D2: GUI md save (covers a switch
        // that then fails before reloadForVault resets the baseline).
      }
    }
  }, [tabs, bumpDocMtime])

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
    // The trash is per-vault: leave trash mode and re-pull the new vault's trash.
    setTrashMode(false)
    setRestoringEntry(null)
    // The doc-mtime baseline is per-vault; drop it so the first resync in the new
    // vault seeds afresh instead of diffing it against the old vault's mtimes.
    docMtimesRef.current = null
    // The switch that brought us here just succeeded, so the server answered
    // and is bound to a live vault again — retire both banners now rather than
    // leaving the red one to claim (for up to one 5s sweep) that the NEW
    // library is the one that is gone.
    setDisconnected(false)
    setVaultGone(false)
    // Every one of these goes through classifyFetchError. A vault switch is
    // exactly when the server is most likely to blink (it is rebinding), and
    // without a catch the rejection was silent: the panels stayed empty and
    // nothing said why — the failure mode this banner exists to prevent.
    fetchVaults()
      .then((v) => {
        setVaults(v)
        // Keep `served` on the server's actual binding (see doResync) — this is
        // the path the vault-gone banner would name.
        setServed(v.served)
      })
      .catch(classifyFetchError)
    fetchProjects().then(setProjects).catch(classifyFetchError)
    fetchTaxonomy().then(setTaxonomy).catch(classifyFetchError)
    fetchFixedEnums().then(setFixedEnums).catch(classifyFetchError)
    fetchPapers()
      .then((ps) => {
        setAllPapers(ps)
        setAllLoaded(true)
      })
      .catch(classifyFetchError)
    loadList(listMode)
    loadTrash()
  }, [loadList, listMode, loadTrash, classifyFetchError])

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
      // A failed switch closes the confirm too: the server rejects a switch to a
      // vault whose folder is gone, and pressing Switch again would fail the same
      // way — leaving the dialog up reads as if nothing happened. Refresh the
      // vault list on the way out so the selector marks it missing (best-effort:
      // if the switch failed because the server died, this fetch dies with it).
      notify(err instanceof Error ? err.message : String(err), 'error')
      setPendingVault(null)
      fetchVaults()
        .then(setVaults)
        .catch(() => {})
    } finally {
      setSwitchingVault(false)
    }
  }, [pendingVault, flushAllDirty, reloadForVault, notify])

  // --- Vault register / unregister (vault-manager slice) -------------------
  // Both are PURE registry writes (the routes never touch app.state.vault). App
  // owns them next to switchVault so library ops also enter the activity log via
  // notify. onRegisterVault rethrows so the manager's form can show the backend's
  // verbatim 400 inline; on success it refreshes the vault list and, when the
  // user asked to set-active, reuses the EXISTING switchVault flow (which opens
  // the SwitchVaultDialog confirm + flush + reload) — no new repoint logic.
  const onRegisterVault = useCallback(
    async (name: string, path: string, setActive: boolean) => {
      await registerVault(name, path)
      notify(`Registered vault “${name}”.`, 'success')
      // Best-effort: the register already succeeded, so a failure HERE must not
      // propagate into the dialog's catch — it would render as if the register
      // failed, contradicting the success toast, and a resubmit would then 400
      // on the duplicate name. The next sweep repairs a stale list.
      await fetchVaults()
        .then(setVaults)
        .catch(() => {})
      if (setActive) switchVault(name)
    },
    [notify, switchVault],
  )

  // Create a NEW vault directory + register it — the `lit init` backend, the same
  // route the welcome page uses. Until now that route was reachable only on the
  // welcome page, i.e. only for a user who had no vault at all: once you had one,
  // a second library could be made only from the CLI. Creating never switches you
  // (only the very first vault becomes active), so `setActive` reuses the same
  // switchVault flow onRegisterVault does — no new repoint logic. Rethrows so the
  // dialog can show the backend's verbatim 400 (missing parent, non-empty target,
  // name clash) inline.
  const onCreateVault = useCallback(
    async (parentDir: string, name: string, setActive: boolean) => {
      const created = await createVault(parentDir, name)
      notify(`Created vault “${created.name}” at ${created.path}.`, 'success')
      // Best-effort, same reason as onRegisterVault: the vault exists on disk
      // now, so a failed list refresh must not reach the dialog as a failed
      // create — resubmitting would 400 on the (real) name clash.
      await fetchVaults()
        .then(setVaults)
        .catch(() => {})
      // The first-ever vault is already active and the server already repointed
      // itself; asking to switch to it would be a no-op confirm dialog.
      if (setActive && !created.active) switchVault(created.name)
    },
    [notify, switchVault],
  )

  const onUnregisterVault = useCallback(
    async (name: string) => {
      try {
        await unregisterVault(name)
        notify(`Unregistered vault “${name}”.`, 'success')
        await fetchVaults().then(setVaults)
      } catch (err) {
        notify(err instanceof Error ? err.message : String(err), 'error')
      }
    },
    [notify],
  )

  // Re-point a moved vault at its new directory — the `lit vault set-path`
  // backend, the move-recovery door the vault manager's Locate leads to (the 410
  // banner's "Find it" opens that manager). When the relocated vault is the
  // active/served one the server repoints itself in place, so a full resync
  // re-pulls the library and clears the 410 banner in one step — no restart, no
  // forced rename, no zombie entry. Rethrows so the inline Locate input can show
  // the backend's verbatim 400 (bad path / unknown name).
  const onRelocateVault = useCallback(
    async (name: string, path: string) => {
      await setVaultPath(name, path)
      notify(`Re-pointed vault “${name}” to ${path}.`, 'success')
      await doResync()
    },
    [notify, doResync],
  )

  // --- Trash (recovery) view (Phase 4.9) -----------------------------------
  const openTrash = useCallback(() => {
    setTrashMode(true)
    loadTrash() // re-pull so the view opens with the live list, not a stale count
  }, [loadTrash])

  const exitTrash = useCallback(() => setTrashMode(false), [])

  // Restore a trashed paper: POST (resolve → restore_from_trash → reconcile),
  // toast a summary (and a CLI-reclone hint when repos are missing), then refresh
  // the trash list + the library lists so the paper reappears. Restore is the
  // safe recovery direction, so no confirm dialog (decision c) — just a toast.
  // A 409 (a live paper holds the id) / 404 surfaces the backend message.
  const restoreFromTrash = useCallback(
    async (entry: TrashEntry) => {
      setRestoringEntry(entry.entryName)
      try {
        const res = await restorePaper(entry.entryName)
        const parts: string[] = []
        if (res.reverseEdgesRebuilt.length)
          parts.push(`${res.reverseEdgesRebuilt.length} reverse edge(s)`)
        if (res.reposRebound.length)
          parts.push(`${res.reposRebound.length} repo(s)`)
        if (res.projectsRebuilt.length)
          parts.push(`${res.projectsRebuilt.length} project(s)`)
        let msg = `Restored “${res.paperId}”`
        if (parts.length) msg += ` · rebuilt ${parts.join(', ')}`
        const missing = Object.keys(res.missingRepos).length
        if (missing) {
          // The 1:1 code repo(s) were hard-deleted on rm and the GUI deliberately
          // does NOT re-clone (decision b) — warn prominently and don't auto-
          // dismiss, since the code-clones link now dangles until the CLI
          // re-clones it.
          msg += ` · ⚠ ${missing} code repo(s) were deleted and NOT re-cloned — finish in the CLI: lit trash restore -y (or lit health-check)`
          notify(msg, 'warning', { sticky: true })
        } else {
          notify(msg, 'success')
        }
        // The restored paper is back in papers/ and out of trash: refresh both.
        loadTrash()
        loadList(listMode)
        fetchPapers().then(setAllPapers)
      } catch (err) {
        notify(err instanceof Error ? err.message : String(err), 'error')
      } finally {
        setRestoringEntry(null)
      }
    },
    [notify, loadTrash, loadList, listMode],
  )

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

  // Switching a center tab = make it active + sync the selection. This is the
  // exact path the tab strip's click uses (TabArea onActivate), extracted so the
  // keyboard switchers below drive identical behavior — no second activation
  // path (and whatever flush-on-switch the click does is preserved verbatim).
  const activateTab = useCallback(
    (key: string) => {
      setActiveTab(key)
      const tab = tabs.find((t) => t.key === key)
      if (tab) selectPaper(tab.paperId)
    },
    [tabs, selectPaper],
  )
  // `,` / `.` cycle the open tabs (wrap-around); both no-op with < 2 tabs.
  const activateAdjacentTab = useCallback(
    (delta: 1 | -1) => {
      if (tabs.length < 2) return
      const idx = tabs.findIndex((t) => t.key === activeTab)
      if (idx === -1) {
        activateTab(tabs[0].key)
        return
      }
      const next = (idx + delta + tabs.length) % tabs.length
      activateTab(tabs[next].key)
    },
    [tabs, activeTab, activateTab],
  )
  // `1`–`9` jump straight to the Nth tab; out-of-range is a no-op.
  const activateTabByIndex = useCallback(
    (n: number) => {
      if (n >= 1 && n <= tabs.length) activateTab(tabs[n - 1].key)
    },
    [tabs, activateTab],
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
        // Modifiers spelled out, per the cheat sheet's own convention: a glyph
        // is an extra decode step, and ⌥ names a key Windows keyboards lack.
        notify('Marked read · Alt+Shift+R to undo')
      },
    }
  }, [cockpitHandle, notify])

  // A blocking surface is up when a SaveDialog / SwitchVaultDialog is pending, a
  // cockpit confirm/panel is open, or a TopBar manager (Projects / observability
  // / vault) is open. The
  // cheat sheet is deliberately EXCLUDED — it is a non-blocking overlay the
  // dispatcher owns (so `?`/Esc keep toggling it). While anyModalOpen, the
  // dispatcher suppresses every global shortcut and lets the modal own its keys.
  const anyModalOpen =
    pendingClose !== null ||
    pendingRemove !== null ||
    pendingVault !== null ||
    cockpitModalOpen ||
    projectsOpen ||
    observabilityOpen ||
    vaultManagerOpen ||
    agentPanelOpen ||
    // Trash mode owns its own (read-only) surface; suppress the library's global
    // shortcuts (PDF tools, ⌥-curation) while it is up — none apply there.
    trashMode

  // J/K walk the middle-list selection through the same rows BrowsePanel
  // renders (`visible` — every filter applied), Enter opens the selection's
  // PDF. Clamped at the ends; J with nothing selected starts at the first
  // row, K at the last. BrowsePanel scrolls the moved selection into view.
  const moveSelection = useCallback(
    (delta: 1 | -1) => {
      if (visible.length === 0) return
      const idx = selectedId ? visible.findIndex((p) => p.id === selectedId) : -1
      const next =
        idx === -1
          ? delta === 1
            ? 0
            : visible.length - 1
          : Math.min(visible.length - 1, Math.max(0, idx + delta))
      const target = visible[next]
      if (target && target.id !== selectedId) selectPaper(target.id)
    },
    [visible, selectedId, selectPaper],
  )
  const openSelected = useCallback(() => {
    if (selectedId) openPdf(selectedId)
  }, [selectedId, openPdf])

  useKeyboardShortcuts({
    anyModalOpen,
    toggleFocus,
    toggleDark,
    toggleLeft,
    toggleRight,
    activateAdjacentTab,
    activateTabByIndex,
    moveSelection,
    openSelected,
    openAgent,
    manageAgents,
    cheatSheetOpen,
    toggleCheatSheet,
    closeCheatSheet,
    pdfActive,
    getPdfHandle,
    selectedId,
    cockpit: shortcutCockpit,
    notify,
  })

  // Welcome page: no vault to serve (fresh install, or the active registry entry
  // moved). Rendered in place of the whole three-column view; creating or opening
  // a vault re-runs bootstrap, which flips `served` and mounts the normal view.
  // (All hooks above run unconditionally — this branch only gates rendering.)
  if (served === undefined) return null // pre-bootstrap; sub-100ms on localhost
  if (served === null) {
    return <WelcomePage vaults={vaults} onEnter={() => void bootstrap()} />
  }

  return (
    // `relative` anchors the focus-mode TopBar, which detaches to `absolute`
    // and slides in/out from the top edge (see TopBar) so the reading area
    // reclaims the header's height.
    <div className="relative flex h-full flex-col bg-stone-100 text-stone-800 antialiased">
      {/* Vault-gone banner: the library this window is bound to is no longer on
       * disk. Takes precedence over the disconnected banner (a 410 proves the
       * server answered, so the two can never both be true). The list behind it
       * is deliberately left on screen — it is the last thing that was true, and
       * every write is refused server-side until the library is found again, so
       * nothing stale can be acted on. Red, not amber: this one is not retrying
       * its way out of a blip. Putting the folder back still heals it silently
       * (the 5s sweep clears the banner); the button is for the other case. */}
      {vaultGone ? (
        <div className="fixed left-1/2 top-3 z-50 flex max-w-[min(44rem,92vw)] -translate-x-1/2 items-center gap-3 rounded-full bg-red-100 py-1.5 pl-4 pr-1.5 text-sm text-red-900 shadow-md ring-1 ring-red-200 dark:bg-red-950/80 dark:text-red-100 dark:ring-red-900">
          <span className="h-2 w-2 shrink-0 rounded-full bg-red-500" />
          <span className="truncate">
            This library is no longer at{' '}
            <span className="font-mono text-[0.8em]">{served}</span> — it was moved,
            renamed or deleted.
          </span>
          <button
            type="button"
            onClick={() => openVaultManager?.()}
            className="shrink-0 rounded-full bg-white/80 px-3 py-1 text-xs font-medium text-red-900 shadow-sm ring-1 ring-red-200 transition duration-200 ease-fluid hover:bg-white dark:bg-red-900/60 dark:text-red-50 dark:ring-red-800 dark:hover:bg-red-900"
          >
            Find it
          </button>
        </div>
      ) : (
        /* Server-unreachable banner: floats top-center above everything, lives
         * exactly as long as `disconnected` (the 5s retry loop clears it). Amber
         * needs explicit dark: overrides — unlike stone it is not ramp-inverted
         * by the .dark block in index.css. */
        disconnected && (
          <div className="fixed left-1/2 top-3 z-50 flex -translate-x-1/2 items-center gap-2 rounded-full bg-amber-100 px-4 py-1.5 text-sm text-amber-800 shadow-md ring-1 ring-amber-200 dark:bg-amber-950/70 dark:text-amber-200 dark:ring-amber-900">
            <span className="h-2 w-2 animate-pulse rounded-full bg-amber-500" />
            Can't reach the litman server — retrying…
          </div>
        )
      )}
      {/* Link advisory. Sits BELOW the two fault banners (top-14, and it is
       * not part of their either/or) because it is not a fault: the library is
       * healthy, it just cannot be decorated with views/ and project shortcuts
       * on this drive. Stone, not red or amber — the colour is the message.
       * Fires only when NO link mechanism works (links === 'none'), i.e. the
       * drive itself cannot store links — so it states that fact and stops.
       * Deliberately no settings deep-link, no Developer Mode, no elevation
       * advice: none of them would change a FAT32 verdict, and an app steering
       * users into system dialogs reads as malware. */}
      {linkNotice && !vaultGone && !disconnected && (
        <div className="fixed left-1/2 top-14 z-40 flex max-w-[min(46rem,92vw)] -translate-x-1/2 items-start gap-3 rounded-2xl bg-stone-100 py-2 pl-4 pr-2 text-sm text-stone-700 shadow-md ring-1 ring-stone-200 dark:bg-stone-800 dark:text-stone-200 dark:ring-stone-700">
          <div className="min-w-0 py-0.5">
            <span>
              This drive can't hold folder links, so{' '}
              <span className="font-mono text-[0.85em]">views/</span> and the
              project shortcuts aren't being made.{' '}
              <span className="text-stone-500 dark:text-stone-400">
                Your library is fine — every command, this app and the agent
                workflow all work normally.
              </span>
            </span>
            <div className="mt-1 text-xs text-stone-500 dark:text-stone-400">
              Usual on USB sticks (FAT32 / exFAT) and network drives; a library
              on an internal drive gets the shortcuts automatically.
            </div>
          </div>
          <div className="flex shrink-0 items-center gap-1">
            <button
              type="button"
              aria-label="Dismiss"
              onClick={() => {
                localStorage.setItem(LINK_NOTICE_DISMISSED, '1')
                setLinkNotice(false)
              }}
              className="rounded-full px-2 py-1 text-xs text-stone-500 transition duration-200 ease-fluid hover:bg-stone-200 hover:text-stone-700 dark:text-stone-400 dark:hover:bg-stone-700 dark:hover:text-stone-100"
            >
              ✕
            </button>
          </div>
        </div>
      )}
      <TopBar
        vaults={vaults}
        updateAvailable={updateLatest}
        projects={projects}
        allPapers={allPapers}
        search={search}
        onSearch={setSearch}
        searchCandidates={searchCandidates}
        searchLoading={searchLoading}
        onSelectResult={onSearchSelect}
        focusMode={focusMode}
        onToggleFocus={toggleFocus}
        onRefresh={manualResync}
        onProjectsChanged={refreshProjects}
        onSwitchVault={switchVault}
        onRegisterVault={onRegisterVault}
        onCreateVault={onCreateVault}
        onUnregisterVault={onUnregisterVault}
        onRelocateVault={onRelocateVault}
        switching={switchingVault}
        notify={notify}
        dark={dark}
        onToggleDark={toggleDark}
        onProjectsOpenChange={setProjectsOpen}
        onVaultManagerOpenChange={setVaultManagerOpen}
        onShowShortcuts={toggleCheatSheet}
        activityLog={activityLog}
        logUnread={logUnread}
        onLogOpened={markLogRead}
        onObservabilityOpenChange={setObservabilityOpen}
        onAgentOpenChange={setAgentPanelOpen}
        onRegisterAgentOpen={registerAgentOpen}
        onRegisterAgentManage={registerAgentManage}
        onRegisterVaultManagerOpen={registerVaultManagerOpen}
        trashMode={trashMode}
      />
      <div className="flex min-h-0 flex-1">
        {trashMode ? (
          <TrashView
            entries={trashEntries}
            loading={trashLoading}
            vaultName={vaults?.active ?? ''}
            onExit={exitTrash}
            onRestore={restoreFromTrash}
            restoringEntry={restoringEntry}
          />
        ) : (
          <>
            <BrowsePanel
              scoped={scoped}
              visible={visible}
              vaultEmpty={vaultEmpty}
              loadFailed={listFailed}
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
              onRemovePaper={setPendingRemove}
              trashCount={trashEntries.length}
              onOpenTrash={openTrash}
              collapsed={leftCollapsed}
              onToggle={() => setLeftCollapsed((c) => !c)}
            />
            <TabArea
              tabs={tabs}
              activeKey={activeTab}
              onActivate={activateTab}
              onClose={closeTab}
              onOpenPaper={openWikilink}
              onRegisterPdf={registerPdf}
              onNotify={notify}
              mdJump={mdJump}
              mdDraft={activeTab ? mdDrafts.get(activeTab) : undefined}
              mdReloadToken={mdReloadToken}
              onMdBeginEdit={mdBeginEdit}
              onMdDraftChange={mdDraftChange}
              onMdEndEdit={mdEndEdit}
              onMdSaved={bumpDocMtime}
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
          </>
        )}
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
      {pendingRemove && (
        <RemovePaperConfirm
          paperId={pendingRemove}
          busy={removing}
          onCancel={cancelRemove}
          onConfirm={confirmRemove}
        />
      )}
      {cheatSheetOpen && <CheatSheet onClose={closeCheatSheet} />}
      {toast && (
        <Toast
          message={toast.message}
          variant={toast.variant}
          sticky={toast.sticky}
          onDismiss={() => setToast(null)}
        />
      )}
    </div>
  )
}
