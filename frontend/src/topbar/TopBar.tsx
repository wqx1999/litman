import { useEffect, useRef, useState } from 'react'
import { createPortal } from 'react-dom'
import type {
  ActivityLogEntry,
  HealthIssue,
  IndexPaper,
  ProjectEntry,
  VaultsPayload,
} from '../types'
import type { Candidate } from '../search'
import { createProject, deleteProject, fetchHealth } from '../api'
import SearchBox from './SearchBox'
import type { ToastVariant } from '../ui/Toast'
import logoUrl from '../assets/litman-logo.png'

interface Props {
  vaults: VaultsPayload | null
  /** Registered projects backing the global Projects manager (P4). */
  projects: ProjectEntry[]
  /** Full INDEX projection — backs the delete-project confirm's "N papers" count. */
  allPapers: IndexPaper[]
  search: string
  onSearch: (q: string) => void
  /** Ranked typeahead candidates (App merges client id/title + server md hits). */
  searchCandidates: Candidate[]
  /** A server notes/discussion search is in flight. */
  searchLoading: boolean
  /** Open the scope picked from the dropdown (PDF for id/title, the notes /
   * discussion doc scrolled to the match for a markdown hit). */
  onSelectResult: (candidate: Candidate) => void
  focusMode: boolean
  onToggleFocus: () => void
  /** Refresh the registered-project list + papers after a create/delete (P4). */
  onProjectsChanged: () => void
  /** Switch the active vault (3c-2) — App handles the confirm + re-fetch. */
  onSwitchVault: (name: string) => void
  /** Register an EXISTING vault dir (vault-manager slice). Rejects with the
   * backend's verbatim VaultRegistryError so the form can show it inline. On
   * success App refreshes the list and, when `setActive`, reuses switchVault. */
  onRegisterVault: (name: string, path: string, setActive: boolean) => Promise<void>
  /** Unregister a vault (registry entry only — the directory on disk is kept).
   * App handles the toast + list refresh; errors are toasted, not thrown. */
  onUnregisterVault: (name: string) => Promise<void>
  /** A vault switch is in flight — disables the selector to block re-entry. */
  switching: boolean
  /** Toast a message (surfaces the backend's raw error verbatim). */
  notify: (msg: string, variant?: ToastVariant) => void
  /** Current theme + toggle, lifted to App (Phase 4) so the `L` shortcut and the
   * header button drive ONE theme state. The toggle still flips the `.dark`
   * class + persists the choice (see App's useDarkMode). */
  dark: boolean
  onToggleDark: () => void
  /** Report the global Projects manager's open state up so the shortcut
   * dispatcher's modal guard suppresses global keys while it is up (Phase 4). */
  onProjectsOpenChange: (open: boolean) => void
  /** Same mirror for the vault-manager panel (+ its nested register form /
   * unregister confirm) — without it the global ⌥-write shortcuts fire behind
   * the open panel (the modal-guard red line). */
  onVaultManagerOpenChange: (open: boolean) => void
  /** Open the keyboard-shortcut cheat sheet (Phase 4). A visible affordance for
   * the otherwise hidden `?` convention — without it the shortcuts are
   * undiscoverable (you can't learn `?` opens them if nothing points at it). */
  onShowShortcuts: () => void
  /** Session activity log (newest last) that the log panel renders newest-first.
   * App owns the buffer so every `notify` auto-records (observability slice). */
  activityLog: ActivityLogEntry[]
  /** A new log entry has arrived since the panel was last opened — lights a dot
   * on the log icon. */
  logUnread: boolean
  /** Clear the unread dot — called when the activity-log panel opens. */
  onLogOpened: () => void
  /** Report whether either observability panel (log / health) is open, so App's
   * shortcut modal-guard suppresses global keys while one is up — mirrors
   * onProjectsOpenChange. Without it ⌥-write shortcuts fire behind the panel. */
  onObservabilityOpenChange: (open: boolean) => void
  /** In trash mode, hide the library-scoped controls (vault switch, Projects,
   * search) — they act on the live library, not the trash being browsed. The
   * vault identity moves into the trash banner (see TrashView). */
  trashMode?: boolean
}

/** Global chrome: brand, current-vault indicator, the global Projects manager,
 * the title/id search, and the focus / dark-mode toggles. No per-paper actions
 * live here — copy-id / cite moved to the Cockpit (selected-paper context), and
 * per-paper project link/unlink stays in the Cockpit's project dropdown. Project
 * register/remove are GLOBAL operations, so they sit in the labeled Projects
 * control here (P4), next to the vault indicator — the workspace-level region
 * that future vault operations will join. */
export default function TopBar({
  vaults,
  projects,
  allPapers,
  search,
  onSearch,
  searchCandidates,
  searchLoading,
  onSelectResult,
  focusMode,
  onToggleFocus,
  onProjectsChanged,
  onSwitchVault,
  onRegisterVault,
  onUnregisterVault,
  switching,
  notify,
  dark,
  onToggleDark,
  onProjectsOpenChange,
  onVaultManagerOpenChange,
  onShowShortcuts,
  activityLog,
  logUnread,
  onLogOpened,
  onObservabilityOpenChange,
  trashMode,
}: Props) {
  const [showProjects, setShowProjects] = useState(false)
  const [showVaults, setShowVaults] = useState(false)
  // Only one of the two observability panels is open at a time; opening either
  // closes the other. Opening the log also clears the unread dot.
  const [panel, setPanel] = useState<null | 'log' | 'health'>(null)
  // Health result lifted above the panel so the shield's count badge survives a
  // panel close (`null` = never run yet → no badge). On-demand only: runHealth
  // fires when the panel opens, never on mount (run_all_checks is Tier-2).
  const [healthIssues, setHealthIssues] = useState<HealthIssue[] | null>(null)
  const [healthLoading, setHealthLoading] = useState(false)
  const [healthError, setHealthError] = useState<string | null>(null)

  const runHealth = () => {
    setHealthLoading(true)
    setHealthError(null)
    fetchHealth()
      .then(setHealthIssues)
      .catch((err) =>
        setHealthError(err instanceof Error ? err.message : String(err)),
      )
      .finally(() => setHealthLoading(false))
  }

  const openLog = () => {
    setPanel('log')
    onLogOpened()
  }
  const openHealth = () => {
    setPanel('health')
    runHealth() // re-run on every open so the report is current (on demand)
  }
  const closePanel = () => setPanel(null)

  // Badge counts off the last run (ruling 1): error wins (rose), else warning
  // (amber), else nothing. Empty until the first run.
  const errorCount = healthIssues?.filter((i) => i.severity === 'error').length ?? 0
  const warningCount =
    healthIssues?.filter((i) => i.severity === 'warning').length ?? 0
  const badge =
    errorCount > 0
      ? { count: errorCount, cls: 'bg-rose-500' }
      : warningCount > 0
        ? { count: warningCount, cls: 'bg-amber-500' }
        : null

  // Mirror the manager's open state up so App's shortcut modal-guard sees it.
  useEffect(() => {
    onProjectsOpenChange(showProjects)
  }, [showProjects, onProjectsOpenChange])

  // Same mirror for the vault manager — its nested register form / unregister
  // confirm live inside the panel, so panel-open covers them all.
  useEffect(() => {
    onVaultManagerOpenChange(showVaults)
  }, [showVaults, onVaultManagerOpenChange])

  // Same mirror for the observability panels: while either is open, App's
  // anyModalOpen must be true so global write-shortcuts (⌥R/⌥P) don't fire
  // behind the panel and the panel's own Esc handler owns the key.
  useEffect(() => {
    onObservabilityOpenChange(panel !== null)
  }, [panel, onObservabilityOpenChange])

  // Focus mode turns the header into an auto-hiding overlay (macOS fullscreen
  // menu-bar idiom): it detaches to `absolute`, slides up out of view, and
  // slides back down only while the pointer is at the very top edge or a header
  // control holds focus. `revealed` drives that slide; reset whenever focus
  // toggles so re-entering focus always starts hidden.
  const headerRef = useRef<HTMLElement>(null)
  // Whether the pointer is currently over the header. A ref (not state) so the
  // hide check reads it synchronously without forcing a re-render.
  const pointerInside = useRef(false)
  const [revealed, setRevealed] = useState(false)
  useEffect(() => {
    setRevealed(false)
    pointerInside.current = false
  }, [focusMode])

  // Hide only once NEITHER the pointer is over the header NOR a header control
  // holds focus (typing in search, an open vault <select>) — so the bar can't
  // vanish mid-interaction. Re-evaluated on BOTH pointer-leave and focus-leave:
  // if the pointer leaves while a control is still focused the first check bails,
  // and the later blur re-runs it once focus clears. Without that second trigger
  // the bar got stuck open after a search (the reported bug) — mouse-out fired
  // once, bailed on the still-focused search box, and nothing re-checked when
  // focus finally moved away.
  const hideIfIdle = () => {
    if (pointerInside.current) return
    if (headerRef.current?.contains(document.activeElement)) return
    setRevealed(false)
  }

  return (
    <>
      {focusMode && (
        // Invisible catch-strip pinned to the top edge: hovering it reveals the
        // hidden header. When revealed the header covers this strip.
        <div
          className="absolute inset-x-0 top-0 z-40 h-2"
          onMouseEnter={() => setRevealed(true)}
        />
      )}
      <header
        ref={headerRef}
        onMouseEnter={
          focusMode
            ? () => {
                pointerInside.current = true
                setRevealed(true)
              }
            : undefined
        }
        onMouseLeave={
          focusMode
            ? () => {
                pointerInside.current = false
                hideIfIdle()
              }
            : undefined
        }
        // Focus entering a header control reveals the bar — this is how ⌘K
        // (which focuses the search box) brings the hidden header into view in
        // focus mode. Focus leaving re-checks the hide so the bar drops once
        // both pointer and focus are gone; the relatedTarget guard avoids
        // hiding when focus merely moves between two header controls.
        onFocus={focusMode ? () => setRevealed(true) : undefined}
        onBlur={
          focusMode
            ? (e) => {
                if (
                  !headerRef.current?.contains(e.relatedTarget as Node | null)
                ) {
                  hideIfIdle()
                }
              }
            : undefined
        }
        className={
          'flex items-center gap-2.5 border-b border-stone-200 bg-stone-50/90 px-3 py-2 backdrop-blur-md ' +
          (focusMode
            ? 'absolute inset-x-0 top-0 z-40 transition-transform duration-200 ease-fluid ' +
              (revealed ? 'translate-y-0 shadow-lg shadow-stone-900/10' : '-translate-y-full')
            : 'relative z-30')
        }
      >
      <img
        src={logoUrl}
        alt="litman"
        title="litman"
        className="h-6 w-auto shrink-0 select-none"
      />

      {!trashMode && (
      <>
      <select
        value={vaults?.active ?? ''}
        onChange={(e) => onSwitchVault(e.target.value)}
        disabled={switching || !vaults?.active || vaults.vaults.length < 2}
        title={
          !vaults?.active
            ? 'No active vault'
            : vaults.vaults.length < 2
              ? 'Only one vault registered'
              : 'Switch the active vault (applies globally)'
        }
        className="rounded-md border border-stone-300 bg-white px-2 py-1 text-sm text-stone-700 shadow-sm transition-colors hover:bg-stone-50 focus:outline-none focus:ring-1 focus:ring-accent-400 disabled:bg-stone-100 disabled:text-stone-500"
      >
        {vaults?.active ? (
          vaults.vaults.map((v) => (
            <option key={v.name} value={v.name}>
              {v.name}
              {v.active ? ' (active)' : ''}
            </option>
          ))
        ) : (
          <option value="">no vault</option>
        )}
      </select>

      {/* Always enabled — NOT gated by the <select>'s <2-vault disable, so a
          single-vault user can still open the manager to register a second. */}
      <button
        type="button"
        onClick={() => setShowVaults(true)}
        title="Register or unregister vaults"
        aria-label="Manage vaults"
        className="grid h-8 w-8 shrink-0 place-items-center rounded-lg border border-stone-300 bg-white text-stone-500 shadow-sm transition duration-200 ease-fluid hover:bg-stone-50 hover:text-stone-700"
      >
        <IconVault />
      </button>

      {showVaults && (
        <VaultManager
          vaults={vaults}
          onRegisterVault={onRegisterVault}
          onUnregisterVault={onUnregisterVault}
          onClose={() => setShowVaults(false)}
        />
      )}

      <button
        type="button"
        onClick={() => setShowProjects(true)}
        title="Register or remove projects"
        className="flex shrink-0 items-center gap-1.5 rounded-lg border border-stone-300 bg-white px-2.5 py-1 text-sm font-medium text-stone-600 shadow-sm transition duration-200 ease-fluid hover:bg-stone-50 hover:text-stone-900"
      >
        <IconFolder /> Projects
      </button>

      {showProjects && (
        <ProjectManager
          projects={projects}
          allPapers={allPapers}
          onChanged={onProjectsChanged}
          onClose={() => setShowProjects(false)}
          notify={notify}
        />
      )}

      <SearchBox
        value={search}
        onChange={onSearch}
        candidates={searchCandidates}
        loading={searchLoading}
        onSelect={onSelectResult}
      />
      </>
      )}
      {/* In trash mode the middle controls are gone; this spacer keeps the
          focus/dark/help cluster pinned to the right edge (the SearchBox's
          flex-grow normally does that). */}
      {trashMode && <div className="flex-1" />}

      {/* Observability cluster (log + health), left of the focus/dark/help
          group. Both are workspace-level, so they sit here in BOTH modes. */}
      <button
        type="button"
        onClick={openLog}
        title="Activity log — recent actions this session"
        aria-label="Activity log"
        className="relative grid h-8 w-8 shrink-0 place-items-center rounded-lg text-stone-500 transition duration-200 ease-fluid hover:bg-stone-200/70 hover:text-stone-700"
      >
        <IconLog />
        {logUnread && (
          <span className="absolute right-1.5 top-1.5 h-1.5 w-1.5 rounded-full bg-rose-400 ring-2 ring-stone-50" />
        )}
      </button>

      <button
        type="button"
        onClick={openHealth}
        title="Health check — audit library consistency"
        aria-label="Health check"
        className="relative grid h-8 w-8 shrink-0 place-items-center rounded-lg text-stone-500 transition duration-200 ease-fluid hover:bg-stone-200/70 hover:text-stone-700"
      >
        <IconShield />
        {badge && (
          <span
            className={`absolute -right-0.5 -top-0.5 grid h-4 min-w-4 place-items-center rounded-full px-1 text-[10px] font-semibold leading-none text-white ring-2 ring-stone-50 ${badge.cls}`}
          >
            {badge.count}
          </span>
        )}
      </button>

      {panel === 'log' && (
        <ActivityLogPanel entries={activityLog} onClose={closePanel} />
      )}
      {panel === 'health' && (
        <HealthPanel
          issues={healthIssues}
          loading={healthLoading}
          error={healthError}
          onRerun={runHealth}
          onClose={closePanel}
        />
      )}

      {/* Hairline divider between the observability cluster and the view toggles. */}
      <div className="mx-0.5 h-5 w-px shrink-0 bg-stone-200" />

      <button
        type="button"
        onClick={onToggleFocus}
        aria-pressed={focusMode}
        title={focusMode ? 'Exit focus mode' : 'Focus mode — hide side panels'}
        className={`grid h-8 w-8 shrink-0 place-items-center rounded-lg transition duration-200 ease-fluid ${
          focusMode
            ? 'bg-accent-50 text-accent-600'
            : 'text-stone-500 hover:bg-stone-200/70 hover:text-stone-700'
        }`}
      >
        <IconFocus />
      </button>

      <button
        type="button"
        onClick={onToggleDark}
        aria-pressed={dark}
        title={dark ? 'Switch to light mode' : 'Switch to dark mode'}
        className="grid h-8 w-8 shrink-0 place-items-center rounded-lg text-stone-500 transition duration-200 ease-fluid hover:bg-stone-200/70 hover:text-stone-700"
      >
        {dark ? <IconSun /> : <IconMoon />}
      </button>

      <button
        type="button"
        onClick={onShowShortcuts}
        title="Keyboard shortcuts (?)"
        aria-label="Keyboard shortcuts"
        className="grid h-8 w-8 shrink-0 place-items-center rounded-lg text-stone-500 transition duration-200 ease-fluid hover:bg-stone-200/70 hover:text-stone-700"
      >
        <IconHelp />
      </button>
      </header>
    </>
  )
}

/** The global Projects manager (P4): lists every registered project with its
 * in-use count, removes one through a default-No confirm into the `lit project
 * rm` backend (cascade: unlink papers + tear down reflib links, directory kept),
 * and registers a new one via NewProjectDialog. Project create/delete are
 * global (not per-paper), so they live here rather than in the Cockpit — the
 * Cockpit's project dropdown only links/unlinks the selected paper. macOS-style
 * modal shell shared with the rest of the app.
 *
 * Portaled to document.body: TopBar's <header> carries `backdrop-blur-md`, and
 * a backdrop-filter establishes a containing block for `position: fixed`
 * descendants. Rendered inline, this modal's `fixed inset-0` would resolve
 * against the ~44px-tall header instead of the viewport, centering (and
 * clipping) the dialog on the header's midline. The portal lifts the whole
 * subtree (this manager + its nested confirm/new dialogs) out of that
 * containing block so `fixed` is viewport-relative again. */
function ProjectManager({
  projects,
  allPapers,
  onChanged,
  onClose,
  notify,
}: {
  projects: ProjectEntry[]
  allPapers: IndexPaper[]
  onChanged: () => void
  onClose: () => void
  notify: (msg: string, variant?: ToastVariant) => void
}) {
  // The project awaiting delete confirmation, the new-project dialog toggle, and
  // whether a delete is in flight (gates the controls).
  const [pendingDelete, setPendingDelete] = useState<string | null>(null)
  const [showNew, setShowNew] = useState(false)
  const [busy, setBusy] = useState(false)

  // Papers linked to a project, off the loaded INDEX (no round-trip).
  function countFor(name: string): number {
    return allPapers.filter((p) => (p.projects ?? []).includes(name)).length
  }

  async function doDelete(name: string) {
    setBusy(true)
    try {
      await deleteProject(name)
      notify(`Removed project “${name}”.`, 'success')
      onChanged()
    } catch (err) {
      notify(err instanceof Error ? err.message : String(err), 'error')
    } finally {
      setBusy(false)
      setPendingDelete(null)
    }
  }

  const sorted = projects.slice().sort((a, b) => a.name.localeCompare(b.name))
  const blocked = busy || pendingDelete != null || showNew

  return createPortal(
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/30 backdrop-blur-sm"
      onClick={blocked ? undefined : onClose}
    >
      <div
        onClick={(e) => e.stopPropagation()}
        onKeyDown={(e) => {
          if (e.key === 'Escape' && !pendingDelete && !showNew) onClose()
        }}
        className="flex max-h-[70vh] w-[28rem] animate-grow-in flex-col rounded-2xl bg-white p-5 shadow-xl ring-1 ring-stone-200"
      >
        <h2 className="text-sm font-semibold text-stone-900">Projects</h2>
        <p className="mt-1.5 text-xs leading-relaxed text-stone-500">
          Register a project, or remove one. Removing unlinks it from every paper
          and tears down its reflib links, but leaves the project folder on disk.
        </p>
        <div className="mt-3 min-h-0 flex-1 overflow-y-auto rounded-lg border border-stone-200">
          {sorted.length === 0 && (
            <div className="px-3 py-4 text-center text-xs text-stone-400">
              No projects registered.
            </div>
          )}
          {sorted.map((p) => (
            <div
              key={p.name}
              className="flex items-center justify-between gap-2 border-b border-stone-100 px-3 py-2 last:border-b-0"
            >
              <div className="min-w-0">
                <div className="truncate text-sm font-medium text-stone-800">
                  {p.name}
                </div>
                <div className="truncate font-mono text-[11px] text-stone-400">
                  {p.path}
                </div>
              </div>
              <div className="flex shrink-0 items-center gap-2">
                <span className="text-[11px] text-stone-400">
                  {countFor(p.name)} papers
                </span>
                <button
                  type="button"
                  aria-label={`Delete project ${p.name}`}
                  title={`Delete project “${p.name}”`}
                  disabled={blocked}
                  onClick={() => setPendingDelete(p.name)}
                  className="grid h-6 w-6 place-items-center rounded-md text-stone-300 transition-colors hover:bg-rose-50 hover:text-rose-500 disabled:opacity-40"
                >
                  <IconTrash />
                </button>
              </div>
            </div>
          ))}
        </div>
        <div className="mt-4 flex justify-between">
          <button
            type="button"
            disabled={blocked}
            onClick={() => setShowNew(true)}
            className="rounded-lg border border-accent-300 bg-accent-50 px-3 py-1.5 text-xs font-medium text-accent-700 shadow-sm transition-colors hover:bg-accent-100 disabled:opacity-50"
          >
            + New project
          </button>
          <button
            type="button"
            disabled={blocked}
            onClick={onClose}
            className="rounded-lg px-3 py-1.5 text-xs text-stone-600 transition-colors hover:bg-stone-100 disabled:opacity-40"
          >
            Done
          </button>
        </div>
      </div>
      {pendingDelete != null && (
        <DeleteProjectConfirm
          name={pendingDelete}
          count={countFor(pendingDelete)}
          busy={busy}
          onCancel={() => setPendingDelete(null)}
          onConfirm={() => doDelete(pendingDelete)}
        />
      )}
      {showNew && (
        <NewProjectDialog
          onClose={() => setShowNew(false)}
          onCreated={() => {
            setShowNew(false)
            onChanged()
          }}
          notify={notify}
        />
      )}
    </div>,
    document.body,
  )
}

/** Default-No confirm for unregistering a project (P4). The body states the
 * cascade (unlinks N papers, drops reflib links) and that the on-disk directory
 * is kept; Cancel is autofocused, the destructive button is rose, and the
 * backdrop stops click-through so dismissing it keeps the manager open. */
function DeleteProjectConfirm({
  name,
  count,
  busy,
  onCancel,
  onConfirm,
}: {
  name: string
  count: number
  busy: boolean
  onCancel: () => void
  onConfirm: () => void
}) {
  return (
    <div
      className="fixed inset-0 z-[60] flex items-center justify-center bg-black/30 backdrop-blur-sm"
      onClick={
        busy
          ? undefined
          : (e) => {
              e.stopPropagation()
              onCancel()
            }
      }
    >
      <div
        onClick={(e) => e.stopPropagation()}
        onKeyDown={(e) => {
          if (e.key === 'Escape') onCancel()
        }}
        className="w-[24rem] animate-grow-in rounded-2xl bg-white p-5 shadow-xl ring-1 ring-stone-200"
      >
        <h2 className="text-sm font-semibold text-stone-900">
          Delete project “{name}”?
        </h2>
        <p className="mt-1.5 text-xs leading-relaxed text-stone-600">
          This unregisters “{name}”
          {count > 0
            ? ` and unlinks it from ${count} paper${count === 1 ? '' : 's'}`
            : ''}{' '}
          and removes its reflib links + REFERENCES.md. The project folder on
          disk is kept. This cannot be undone.
        </p>
        <div className="mt-5 flex justify-end gap-2">
          <button
            autoFocus
            onClick={onCancel}
            disabled={busy}
            className="rounded-lg px-3 py-1.5 text-xs text-stone-600 transition-colors hover:bg-stone-100 disabled:opacity-40"
          >
            Cancel
          </button>
          <button
            onClick={onConfirm}
            disabled={busy}
            className="rounded-lg bg-rose-500 px-3 py-1.5 text-xs font-medium text-white transition-colors hover:bg-rose-600 disabled:opacity-60"
          >
            Delete
          </button>
        </div>
      </div>
    </div>
  )
}

/** Register-a-new-project modal (name + absolute path). Mirrors SaveDialog's
 * shell; the path must be absolute + already exist + be a directory server-side
 * (A7; a relative path is rejected, not resolved against the server cwd), so a
 * bad path surfaces the backend's TaxonomyError verbatim via `notify` and the
 * dialog stays open for correction. Escape cancels. The backdrop stops
 * click-through so, when opened from the ProjectManager, dismissing it does not
 * also close the manager. */
function NewProjectDialog({
  onClose,
  onCreated,
  notify,
}: {
  onClose: () => void
  onCreated: () => void
  notify: (msg: string, variant?: ToastVariant) => void
}) {
  const [name, setName] = useState('')
  const [path, setPath] = useState('')
  const [busy, setBusy] = useState(false)

  const canSubmit = name.trim().length > 0 && path.trim().length > 0 && !busy

  async function submit() {
    if (!canSubmit) return
    setBusy(true)
    try {
      await createProject(name.trim(), path.trim())
      notify(`Linked project “${name.trim()}”.`, 'success')
      onCreated()
    } catch (err) {
      notify(err instanceof Error ? err.message : String(err), 'error')
      setBusy(false) // keep the dialog open so the user can fix the path
    }
  }

  const INPUT =
    'w-full rounded-md border border-stone-300 bg-white px-2.5 py-1.5 text-sm ' +
    'text-stone-800 shadow-sm focus:outline-none focus:ring-1 focus:ring-accent-400 ' +
    'disabled:opacity-50'

  return (
    <div
      className="fixed inset-0 z-[60] flex items-center justify-center bg-black/30 backdrop-blur-sm"
      onClick={
        busy
          ? undefined
          : (e) => {
              e.stopPropagation()
              onClose()
            }
      }
    >
      <div
        onClick={(e) => e.stopPropagation()}
        onKeyDown={(e) => {
          if (e.key === 'Escape') onClose()
        }}
        className="w-[24rem] animate-grow-in rounded-2xl bg-white p-5 shadow-xl ring-1 ring-stone-200"
      >
        <h2 className="text-sm font-semibold text-stone-900">New project</h2>
        <p className="mt-1.5 text-xs leading-relaxed text-stone-500">
          Links a name to an existing folder (nothing is created).
        </p>
        <div className="mt-4 flex flex-col gap-3">
          <label className="flex flex-col gap-1">
            <span className="text-[11px] font-semibold uppercase tracking-wider text-stone-500">
              Name
            </span>
            <input
              autoFocus
              type="text"
              value={name}
              disabled={busy}
              placeholder="pepforge"
              onChange={(e) => setName(e.target.value)}
              className={INPUT}
            />
          </label>
          <label className="flex flex-col gap-1">
            <span className="text-[11px] font-semibold uppercase tracking-wider text-stone-500">
              Absolute path
            </span>
            <input
              type="text"
              value={path}
              disabled={busy}
              placeholder="/work/you/Project/pepforge"
              onChange={(e) => setPath(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === 'Enter') submit()
              }}
              className={`${INPUT} font-mono`}
            />
            <span className="text-[11px] text-stone-400">
              the folder itself, must exist
            </span>
          </label>
        </div>
        <div className="mt-5 flex justify-end gap-2">
          <button
            onClick={onClose}
            disabled={busy}
            className="rounded-lg px-3 py-1.5 text-xs text-stone-600 transition-colors hover:bg-stone-100 disabled:opacity-40"
          >
            Cancel
          </button>
          <button
            onClick={submit}
            disabled={!canSubmit}
            className="rounded-lg bg-accent-500 px-3 py-1.5 text-xs font-medium text-white transition-colors hover:bg-accent-600 disabled:opacity-60"
          >
            {busy ? 'Creating…' : 'Create'}
          </button>
        </div>
      </div>
    </div>
  )
}

/** The vault manager (vault-manager slice): lists every registered vault with an
 * active marker + path, unregisters a non-served one through a default-No confirm
 * into the `lit vault remove` backend (registry entry only — the directory on
 * disk is kept), and registers an EXISTING vault dir via RegisterVaultDialog.
 * Mirrors ProjectManager's portaled, auto-theming modal shell (no `dark:`
 * variants — the inverted `stone` ramp + `.dark .bg-white` handle dark mode).
 *
 * The served vault's row (`v.active`) has Unregister disabled with a "switch
 * first" tooltip — a front-of-house mirror of the server's 409 guard (a GUI-only
 * user who unregistered the served vault and closed the browser would be locked
 * out, since `lit gui` needs an active vault to boot). */
function VaultManager({
  vaults,
  onRegisterVault,
  onUnregisterVault,
  onClose,
}: {
  vaults: VaultsPayload | null
  onRegisterVault: (name: string, path: string, setActive: boolean) => Promise<void>
  onUnregisterVault: (name: string) => Promise<void>
  onClose: () => void
}) {
  const [pendingUnregister, setPendingUnregister] = useState<string | null>(null)
  const [showRegister, setShowRegister] = useState(false)
  const [busy, setBusy] = useState(false)

  async function doUnregister(name: string) {
    setBusy(true)
    try {
      await onUnregisterVault(name)
    } finally {
      setBusy(false)
      setPendingUnregister(null)
    }
  }

  const entries = vaults?.vaults ?? []
  const sorted = entries.slice().sort((a, b) => a.name.localeCompare(b.name))
  const blocked = busy || pendingUnregister != null || showRegister

  return createPortal(
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/30 backdrop-blur-sm"
      onClick={blocked ? undefined : onClose}
    >
      <div
        onClick={(e) => e.stopPropagation()}
        onKeyDown={(e) => {
          if (e.key === 'Escape' && !pendingUnregister && !showRegister) onClose()
        }}
        className="flex max-h-[70vh] w-[30rem] animate-grow-in flex-col rounded-2xl bg-white p-5 shadow-xl ring-1 ring-stone-200"
      >
        <h2 className="text-sm font-semibold text-stone-900">Vaults</h2>
        <p className="mt-1.5 text-xs leading-relaxed text-stone-500">
          Register an existing vault, or unregister one. Unregistering removes the
          registry entry only — the vault folder on disk is kept. Switch vaults
          from the selector in the toolbar.
        </p>
        <div className="mt-3 min-h-0 flex-1 overflow-y-auto rounded-lg border border-stone-200">
          {sorted.length === 0 && (
            <div className="px-3 py-4 text-center text-xs text-stone-400">
              No vaults registered.
            </div>
          )}
          {sorted.map((v) => (
            <div
              key={v.name}
              className="flex items-center justify-between gap-2 border-b border-stone-100 px-3 py-2 last:border-b-0"
            >
              <div className="min-w-0">
                <div className="flex items-center gap-1.5">
                  <span className="truncate text-sm font-medium text-stone-800">
                    {v.name}
                  </span>
                  {v.active && (
                    <span className="shrink-0 rounded-full bg-accent-100 px-1.5 py-0.5 text-[10px] font-semibold uppercase tracking-wide text-accent-700">
                      active
                    </span>
                  )}
                </div>
                <div className="truncate font-mono text-[11px] text-stone-400">
                  {v.path}
                </div>
              </div>
              <div className="flex shrink-0 items-center gap-2">
                <button
                  type="button"
                  aria-label={`Unregister vault ${v.name}`}
                  title={
                    v.active
                      ? 'Switch to another vault first'
                      : `Unregister vault “${v.name}”`
                  }
                  disabled={blocked || v.active}
                  onClick={() => setPendingUnregister(v.name)}
                  className="grid h-6 w-6 place-items-center rounded-md text-stone-300 transition-colors hover:bg-rose-50 hover:text-rose-500 disabled:opacity-40 disabled:hover:bg-transparent disabled:hover:text-stone-300"
                >
                  <IconTrash />
                </button>
              </div>
            </div>
          ))}
        </div>
        <div className="mt-4 flex justify-between">
          <button
            type="button"
            disabled={blocked}
            onClick={() => setShowRegister(true)}
            className="rounded-lg border border-accent-300 bg-accent-50 px-3 py-1.5 text-xs font-medium text-accent-700 shadow-sm transition-colors hover:bg-accent-100 disabled:opacity-50"
          >
            + Register existing vault…
          </button>
          <button
            type="button"
            disabled={blocked}
            onClick={onClose}
            className="rounded-lg px-3 py-1.5 text-xs text-stone-600 transition-colors hover:bg-stone-100 disabled:opacity-40"
          >
            Done
          </button>
        </div>
      </div>
      {pendingUnregister != null && (
        <UnregisterVaultConfirm
          name={pendingUnregister}
          busy={busy}
          onCancel={() => setPendingUnregister(null)}
          onConfirm={() => doUnregister(pendingUnregister)}
        />
      )}
      {showRegister && (
        <RegisterVaultDialog
          onRegisterVault={onRegisterVault}
          onClose={() => setShowRegister(false)}
          onRegistered={(setActive) => {
            setShowRegister(false)
            // When the user asked to set-active, App is already opening the
            // SwitchVaultDialog (via switchVault) — close this panel so that
            // confirm isn't stacked behind it.
            if (setActive) onClose()
          }}
        />
      )}
    </div>,
    document.body,
  )
}

/** Default-No confirm for unregistering a vault (vault-manager slice). The body
 * states that ONLY the registry entry is removed and the on-disk directory is
 * kept; Cancel is autofocused, the destructive button is rose, and the backdrop
 * stops click-through so dismissing it keeps the manager open. Mirrors
 * DeleteProjectConfirm. */
function UnregisterVaultConfirm({
  name,
  busy,
  onCancel,
  onConfirm,
}: {
  name: string
  busy: boolean
  onCancel: () => void
  onConfirm: () => void
}) {
  return (
    <div
      className="fixed inset-0 z-[60] flex items-center justify-center bg-black/30 backdrop-blur-sm"
      onClick={
        busy
          ? undefined
          : (e) => {
              e.stopPropagation()
              onCancel()
            }
      }
    >
      <div
        onClick={(e) => e.stopPropagation()}
        onKeyDown={(e) => {
          if (e.key === 'Escape') onCancel()
        }}
        className="w-[24rem] animate-grow-in rounded-2xl bg-white p-5 shadow-xl ring-1 ring-stone-200"
      >
        <h2 className="text-sm font-semibold text-stone-900">
          Unregister vault “{name}”?
        </h2>
        <p className="mt-1.5 text-xs leading-relaxed text-stone-600">
          This removes “{name}” from the vault registry only.{' '}
          <span className="font-semibold">
            The vault directory on disk is NOT deleted
          </span>{' '}
          — only the registry entry is removed. You can register it again later.
        </p>
        <div className="mt-5 flex justify-end gap-2">
          <button
            autoFocus
            onClick={onCancel}
            disabled={busy}
            className="rounded-lg px-3 py-1.5 text-xs text-stone-600 transition-colors hover:bg-stone-100 disabled:opacity-40"
          >
            Cancel
          </button>
          <button
            onClick={onConfirm}
            disabled={busy}
            className="rounded-lg bg-rose-500 px-3 py-1.5 text-xs font-medium text-white transition-colors hover:bg-rose-600 disabled:opacity-60"
          >
            {busy ? 'Unregistering…' : 'Unregister'}
          </button>
        </div>
      </div>
    </div>
  )
}

/** Register-an-existing-vault modal (vault-manager slice). name + server-side
 * path + a "switch to it after registering" checkbox. Mirrors NewProjectDialog's
 * shell, but surfaces the backend's verbatim VaultRegistryError INLINE (not just
 * a toast) so the user can fix the path without losing the form — the path must
 * resolve server-side to an existing directory containing a lit-config.yaml. On
 * success `onRegistered(setActive)` closes the form; when setActive the parent
 * also closes so App's reused SwitchVaultDialog stands alone. Escape cancels;
 * the backdrop stops click-through so dismissing it keeps the manager open. */
function RegisterVaultDialog({
  onRegisterVault,
  onClose,
  onRegistered,
}: {
  onRegisterVault: (name: string, path: string, setActive: boolean) => Promise<void>
  onClose: () => void
  onRegistered: (setActive: boolean) => void
}) {
  const [name, setName] = useState('')
  const [path, setPath] = useState('')
  const [setActive, setSetActive] = useState(false)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const canSubmit = name.trim().length > 0 && path.trim().length > 0 && !busy

  async function submit() {
    if (!canSubmit) return
    setBusy(true)
    setError(null)
    try {
      await onRegisterVault(name.trim(), path.trim(), setActive)
      onRegistered(setActive)
    } catch (err) {
      // Verbatim backend message inline; keep the dialog open for correction.
      setError(err instanceof Error ? err.message : String(err))
      setBusy(false)
    }
  }

  const INPUT =
    'w-full rounded-md border border-stone-300 bg-white px-2.5 py-1.5 text-sm ' +
    'text-stone-800 shadow-sm focus:outline-none focus:ring-1 focus:ring-accent-400 ' +
    'disabled:opacity-50'

  return (
    <div
      className="fixed inset-0 z-[60] flex items-center justify-center bg-black/30 backdrop-blur-sm"
      onClick={
        busy
          ? undefined
          : (e) => {
              e.stopPropagation()
              onClose()
            }
      }
    >
      <div
        onClick={(e) => e.stopPropagation()}
        onKeyDown={(e) => {
          if (e.key === 'Escape') onClose()
        }}
        className="w-[26rem] animate-grow-in rounded-2xl bg-white p-5 shadow-xl ring-1 ring-stone-200"
      >
        <h2 className="text-sm font-semibold text-stone-900">
          Register existing vault
        </h2>
        <p className="mt-1.5 text-xs leading-relaxed text-stone-500">
          Adds an existing litman vault to the registry (nothing is created). The
          path must already contain a lit-config.yaml.
        </p>
        <div className="mt-4 flex flex-col gap-3">
          <label className="flex flex-col gap-1">
            <span className="text-[11px] font-semibold uppercase tracking-wider text-stone-500">
              Name
            </span>
            <input
              autoFocus
              type="text"
              value={name}
              disabled={busy}
              placeholder="main"
              onChange={(e) => setName(e.target.value)}
              className={INPUT}
            />
          </label>
          <label className="flex flex-col gap-1">
            <span className="text-[11px] font-semibold uppercase tracking-wider text-stone-500">
              Vault path
            </span>
            <input
              type="text"
              value={path}
              disabled={busy}
              placeholder="/work/you/literature_vault"
              onChange={(e) => setPath(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === 'Enter') submit()
              }}
              className={`${INPUT} font-mono`}
            />
            <span className="text-[11px] text-stone-400">
              the vault folder itself, must exist + contain lit-config.yaml
            </span>
          </label>
          <label className="flex items-center gap-2 text-xs text-stone-600">
            <input
              type="checkbox"
              checked={setActive}
              disabled={busy}
              onChange={(e) => setSetActive(e.target.checked)}
              className="h-3.5 w-3.5 rounded border-stone-300 text-accent-500 focus:ring-accent-400"
            />
            Switch to it after registering
          </label>
        </div>
        {error && (
          <p className="mt-3 rounded-md bg-rose-50 px-3 py-2 text-[11px] leading-relaxed text-rose-600">
            {error}
          </p>
        )}
        <div className="mt-5 flex justify-end gap-2">
          <button
            onClick={onClose}
            disabled={busy}
            className="rounded-lg px-3 py-1.5 text-xs text-stone-600 transition-colors hover:bg-stone-100 disabled:opacity-40"
          >
            Cancel
          </button>
          <button
            onClick={submit}
            disabled={!canSubmit}
            className="rounded-lg bg-accent-500 px-3 py-1.5 text-xs font-medium text-white transition-colors hover:bg-accent-600 disabled:opacity-60"
          >
            {busy ? 'Registering…' : 'Register'}
          </button>
        </div>
      </div>
    </div>
  )
}

/** Per-severity accent for a health finding — readable in both themes (these
 * hues sit outside the inverted `stone` ramp, so no `dark:` variant is needed).
 * `dot` tints the leading marker, `text` the severity label. */
const SEVERITY_STYLE: Record<
  HealthIssue['severity'],
  { dot: string; text: string }
> = {
  error: { dot: 'bg-rose-500', text: 'text-rose-600' },
  warning: { dot: 'bg-amber-500', text: 'text-amber-600' },
  info: { dot: 'bg-sky-500', text: 'text-sky-600' },
}

const SEVERITY_ORDER: Record<HealthIssue['severity'], number> = {
  error: 0,
  warning: 1,
  info: 2,
}

/** The health-check report panel (D1). Mirrors the ProjectManager shell — a
 * portaled `fixed inset-0` backdrop + an `animate-grow-in` card — so it auto-
 * themes to dark via the inverted `stone` ramp + the global `.dark .bg-white`
 * rule (no manual `dark:` variants). The fetch is owned by TopBar (the count
 * badge must outlive a panel close); this is the presentational view of that
 * result. Groups by `category` (server/registry order preserved) then orders
 * each group's rows error→warning→info. Esc + click-outside close. */
function HealthPanel({
  issues,
  loading,
  error,
  onRerun,
  onClose,
}: {
  issues: HealthIssue[] | null
  loading: boolean
  error: string | null
  onRerun: () => void
  onClose: () => void
}) {
  // Stable, category-first grouping: walk the (registry-ordered) list once so the
  // first-seen category order is preserved, then sort each bucket by severity.
  const groups: Array<[string, HealthIssue[]]> = []
  if (issues) {
    const byCat = new Map<string, HealthIssue[]>()
    for (const issue of issues) {
      const bucket = byCat.get(issue.category)
      if (bucket) bucket.push(issue)
      else {
        const fresh = [issue]
        byCat.set(issue.category, fresh)
        groups.push([issue.category, fresh])
      }
    }
    for (const [, bucket] of groups) {
      bucket.sort((a, b) => SEVERITY_ORDER[a.severity] - SEVERITY_ORDER[b.severity])
    }
  }

  return createPortal(
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/30 backdrop-blur-sm"
      onClick={onClose}
    >
      <div
        onClick={(e) => e.stopPropagation()}
        onKeyDown={(e) => {
          if (e.key === 'Escape') onClose()
        }}
        className="flex max-h-[70vh] w-[34rem] animate-grow-in flex-col rounded-2xl bg-white p-5 shadow-xl ring-1 ring-stone-200"
      >
        <div className="flex items-center justify-between">
          <h2 className="text-sm font-semibold text-stone-900">Health check</h2>
          <button
            type="button"
            disabled={loading}
            onClick={onRerun}
            className="rounded-md px-2 py-0.5 text-[11px] text-stone-500 transition-colors hover:bg-stone-100 hover:text-stone-700 disabled:opacity-40"
          >
            {loading ? 'Running…' : 'Re-run'}
          </button>
        </div>
        <p className="mt-1.5 text-xs leading-relaxed text-stone-500">
          Audits library consistency (read-only). Fixes still run from the CLI:
          <span className="font-mono text-stone-600"> lit health-check --fix</span>.
        </p>
        <div className="mt-3 min-h-0 flex-1 overflow-y-auto rounded-lg border border-stone-200">
          {loading && (
            <div className="px-3 py-6 text-center text-xs text-stone-400">
              Running checks…
            </div>
          )}
          {!loading && error && (
            <div className="px-3 py-6 text-center text-xs text-rose-600">
              {error}
            </div>
          )}
          {!loading && !error && issues && issues.length === 0 && (
            <div className="px-3 py-6 text-center text-sm font-medium text-emerald-600">
              ✓ No issues
            </div>
          )}
          {!loading &&
            !error &&
            groups.map(([category, bucket]) => (
              <div
                key={category}
                className="border-b border-stone-100 last:border-b-0"
              >
                <div className="bg-stone-50 px-3 py-1.5 font-mono text-[11px] font-semibold uppercase tracking-wider text-stone-500">
                  {category} · {bucket.length}
                </div>
                {bucket.map((issue, i) => (
                  <div
                    key={i}
                    className="flex gap-2 px-3 py-2 last:border-b-0"
                  >
                    <span
                      className={`mt-1 h-1.5 w-1.5 shrink-0 rounded-full ${SEVERITY_STYLE[issue.severity].dot}`}
                    />
                    <div className="min-w-0">
                      <div className="text-sm text-stone-800">{issue.message}</div>
                      <div className="mt-0.5 flex flex-wrap items-center gap-x-2 gap-y-0.5 text-[11px]">
                        <span
                          className={`font-semibold uppercase ${SEVERITY_STYLE[issue.severity].text}`}
                        >
                          {issue.severity}
                        </span>
                        {issue.paper_id && (
                          <span className="font-mono text-stone-400">
                            {issue.paper_id}
                          </span>
                        )}
                      </div>
                      {issue.hint && (
                        <div className="mt-0.5 text-[11px] italic text-stone-400">
                          {issue.hint}
                        </div>
                      )}
                    </div>
                  </div>
                ))}
              </div>
            ))}
        </div>
        <div className="mt-4 flex justify-end">
          <button
            type="button"
            onClick={onClose}
            className="rounded-lg px-3 py-1.5 text-xs text-stone-600 transition-colors hover:bg-stone-100"
          >
            Done
          </button>
        </div>
      </div>
    </div>,
    document.body,
  )
}

/** Per-variant glyph for an activity-log row. */
const LOG_GLYPH: Record<ToastVariant, { ch: string; cls: string }> = {
  success: { ch: '✓', cls: 'text-emerald-500' },
  error: { ch: '✗', cls: 'text-rose-500' },
  warning: { ch: '⚠', cls: 'text-amber-500' },
  info: { ch: 'ℹ', cls: 'text-sky-500' },
}

function _logTime(ts: number): string {
  return new Date(ts).toLocaleTimeString(undefined, {
    hour: '2-digit',
    minute: '2-digit',
    second: '2-digit',
    hour12: false,
  })
}

/** The session activity-log panel (D2). Same portaled, auto-theming shell as
 * HealthPanel / ProjectManager. Renders the buffer newest-first with a per-
 * variant glyph + local HH:MM:SS time. "No activity yet" when empty. Esc +
 * click-outside close. */
function ActivityLogPanel({
  entries,
  onClose,
}: {
  entries: ActivityLogEntry[]
  onClose: () => void
}) {
  // Newest-first without mutating the source buffer.
  const ordered = entries.slice().reverse()
  return createPortal(
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/30 backdrop-blur-sm"
      onClick={onClose}
    >
      <div
        onClick={(e) => e.stopPropagation()}
        onKeyDown={(e) => {
          if (e.key === 'Escape') onClose()
        }}
        className="flex max-h-[70vh] w-[30rem] animate-grow-in flex-col rounded-2xl bg-white p-5 shadow-xl ring-1 ring-stone-200"
      >
        <h2 className="text-sm font-semibold text-stone-900">Activity log</h2>
        <p className="mt-1.5 text-xs leading-relaxed text-stone-500">
          Recent actions this session (newest first). Cleared on refresh.
        </p>
        <div className="mt-3 min-h-0 flex-1 overflow-y-auto rounded-lg border border-stone-200">
          {ordered.length === 0 && (
            <div className="px-3 py-6 text-center text-xs text-stone-400">
              No activity yet.
            </div>
          )}
          {ordered.map((e, i) => {
            const glyph = LOG_GLYPH[e.variant]
            return (
              <div
                key={i}
                className="flex items-start gap-2 border-b border-stone-100 px-3 py-2 last:border-b-0"
              >
                <span className={`mt-px shrink-0 text-sm ${glyph.cls}`}>
                  {glyph.ch}
                </span>
                <span className="shrink-0 font-mono text-[11px] tabular-nums text-stone-400">
                  {_logTime(e.ts)}
                </span>
                <span className="min-w-0 text-sm text-stone-700">{e.message}</span>
              </div>
            )
          })}
        </div>
        <div className="mt-4 flex justify-end">
          <button
            type="button"
            onClick={onClose}
            className="rounded-lg px-3 py-1.5 text-xs text-stone-600 transition-colors hover:bg-stone-100"
          >
            Done
          </button>
        </div>
      </div>
    </div>,
    document.body,
  )
}

const ICON = 'h-[18px] w-[18px]'
const SVG_PROPS = {
  className: ICON,
  viewBox: '0 0 24 24',
  fill: 'none',
  stroke: 'currentColor',
  strokeWidth: 1.8,
  strokeLinecap: 'round' as const,
  strokeLinejoin: 'round' as const,
  'aria-hidden': true,
}

/** Folder — the global Projects manager. */
function IconFolder() {
  return (
    <svg {...SVG_PROPS}>
      <path d="M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z" />
    </svg>
  )
}

/** Stacked cylinders (database) — the vault manager. */
function IconVault() {
  return (
    <svg {...SVG_PROPS}>
      <ellipse cx="12" cy="5" rx="7" ry="3" />
      <path d="M5 5v6c0 1.7 3.1 3 7 3s7-1.3 7-3V5" />
      <path d="M5 11v6c0 1.7 3.1 3 7 3s7-1.3 7-3v-6" />
    </svg>
  )
}

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

/** Viewfinder brackets — "frame the content" (focus mode). */
function IconFocus() {
  return (
    <svg {...SVG_PROPS}>
      <path d="M8 3H6a3 3 0 0 0-3 3v2M16 3h2a3 3 0 0 1 3 3v2M8 21H6a3 3 0 0 1-3-3v-2M16 21h2a3 3 0 0 0 3-3v-2" />
    </svg>
  )
}

/** Question mark in a circle — the macOS help-button idiom; opens the
 * keyboard-shortcut cheat sheet (and so reveals the `?` shortcut). */
function IconHelp() {
  return (
    <svg {...SVG_PROPS}>
      <circle cx="12" cy="12" r="9" />
      <path d="M9.2 9.3a2.8 2.8 0 0 1 5.4 1c0 1.8-2.7 2.3-2.7 4" />
      <circle cx="12" cy="17.2" r="0.6" fill="currentColor" stroke="none" />
    </svg>
  )
}

function IconMoon() {
  return (
    <svg {...SVG_PROPS}>
      <path d="M21 12.8A8.5 8.5 0 1 1 11.2 3a6.5 6.5 0 0 0 9.8 9.8z" />
    </svg>
  )
}

function IconSun() {
  return (
    <svg {...SVG_PROPS}>
      <circle cx="12" cy="12" r="4" />
      <path d="M12 2v2M12 20v2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4M2 12h2M20 12h2M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4" />
    </svg>
  )
}

/** Clock-with-rewind — the session activity history (D2). */
function IconLog() {
  return (
    <svg {...SVG_PROPS}>
      <path d="M3 3v5h5" />
      <path d="M3.05 11A9 9 0 1 0 6 5.3L3 8" />
      <path d="M12 8v4l3 2" />
    </svg>
  )
}

/** Shield-with-check — the library health check (D1). */
function IconShield() {
  return (
    <svg {...SVG_PROPS}>
      <path d="M12 3l7 3v5c0 4.5-3 7.5-7 9-4-1.5-7-4.5-7-9V6z" />
      <path d="M9 12l2 2 4-4" />
    </svg>
  )
}
