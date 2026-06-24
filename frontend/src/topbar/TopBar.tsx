import { useEffect, useRef, useState } from 'react'
import { createPortal } from 'react-dom'
import type { IndexPaper, ProjectEntry, VaultsPayload } from '../types'
import type { Candidate } from '../search'
import { createProject, deleteProject } from '../api'
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
  /** Open the keyboard-shortcut cheat sheet (Phase 4). A visible affordance for
   * the otherwise hidden `?` convention — without it the shortcuts are
   * undiscoverable (you can't learn `?` opens them if nothing points at it). */
  onShowShortcuts: () => void
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
  switching,
  notify,
  dark,
  onToggleDark,
  onProjectsOpenChange,
  onShowShortcuts,
}: Props) {
  const [showProjects, setShowProjects] = useState(false)

  // Mirror the manager's open state up so App's shortcut modal-guard sees it.
  useEffect(() => {
    onProjectsOpenChange(showProjects)
  }, [showProjects, onProjectsOpenChange])

  // Focus mode turns the header into an auto-hiding overlay (macOS fullscreen
  // menu-bar idiom): it detaches to `absolute`, slides up out of view, and
  // slides back down only while the pointer is at the very top edge or a header
  // control holds focus. `revealed` drives that slide; reset whenever focus
  // toggles so re-entering focus always starts hidden.
  const headerRef = useRef<HTMLElement>(null)
  const [revealed, setRevealed] = useState(false)
  useEffect(() => setRevealed(false), [focusMode])

  // Hide on mouse-out, but keep the bar down while any header control still has
  // focus (typing in search, the open vault <select>) so it can't vanish
  // mid-interaction.
  const hideUnlessFocused = () => {
    if (!headerRef.current?.contains(document.activeElement)) setRevealed(false)
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
        onMouseEnter={focusMode ? () => setRevealed(true) : undefined}
        onMouseLeave={focusMode ? hideUnlessFocused : undefined}
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
