import { useState } from 'react'
import type { VaultsPayload } from '../types'
import type { Candidate } from '../search'
import { createProject } from '../api'
import SearchBox from './SearchBox'
import logoUrl from '../assets/litman-logo.png'

interface Props {
  vaults: VaultsPayload | null
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
  /** Refresh the registered-project list after a successful create (3c-1). */
  onProjectCreated: () => void
  /** Toast a message (surfaces the backend's raw create error verbatim). */
  notify: (msg: string) => void
}

/** Reads the `.dark` class the no-FOUC script (index.html) already set on
 * <html>, then flips it and persists the choice. Self-contained here so the
 * theme toggle needs no App-level state. */
function useDarkMode(): readonly [boolean, () => void] {
  const [dark, setDark] = useState(
    () =>
      typeof document !== 'undefined' &&
      document.documentElement.classList.contains('dark'),
  )
  const toggle = () =>
    setDark((d) => {
      const next = !d
      document.documentElement.classList.toggle('dark', next)
      try {
        localStorage.setItem('litman-theme', next ? 'dark' : 'light')
      } catch {
        /* private mode / no storage — class still applies this session */
      }
      return next
    })
  return [dark, toggle] as const
}

/** Global chrome: brand, current-vault indicator, the title/id search, and the
 * focus / dark-mode toggles. No per-paper or mutating actions live here —
 * copy-id / copy-wikilink moved to the Cockpit (selected-paper context);
 * project creation lands next to the project dropdown in Phase 3. */
export default function TopBar({
  vaults,
  search,
  onSearch,
  searchCandidates,
  searchLoading,
  onSelectResult,
  focusMode,
  onToggleFocus,
  onProjectCreated,
  notify,
}: Props) {
  const [dark, toggleDark] = useDarkMode()
  const [showNewProject, setShowNewProject] = useState(false)

  return (
    <header className="relative z-30 flex items-center gap-2.5 border-b border-stone-200 bg-stone-50/90 px-3 py-2 backdrop-blur-md">
      <img
        src={logoUrl}
        alt="litman"
        title="litman"
        className="h-6 w-auto shrink-0 select-none"
      />

      <select
        value={vaults?.active ?? ''}
        disabled
        title="Vault switching lands in Phase 3"
        className="rounded-md border border-stone-300 bg-stone-100 px-2 py-1 text-sm text-stone-500 shadow-sm"
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
        onClick={() => setShowNewProject(true)}
        title="Register a new project (name + absolute path)"
        className="grid h-8 w-8 shrink-0 place-items-center rounded-lg text-stone-500 transition duration-200 ease-fluid hover:bg-stone-200/70 hover:text-stone-700"
        aria-label="New project"
      >
        <IconPlus />
      </button>

      {showNewProject && (
        <NewProjectDialog
          onClose={() => setShowNewProject(false)}
          onCreated={() => {
            setShowNewProject(false)
            onProjectCreated()
          }}
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
        onClick={toggleDark}
        aria-pressed={dark}
        title={dark ? 'Switch to light mode' : 'Switch to dark mode'}
        className="grid h-8 w-8 shrink-0 place-items-center rounded-lg text-stone-500 transition duration-200 ease-fluid hover:bg-stone-200/70 hover:text-stone-700"
      >
        {dark ? <IconSun /> : <IconMoon />}
      </button>
    </header>
  )
}

/** Register-a-new-project modal (name + absolute path). Mirrors SaveDialog's
 * shell; the path must already exist + be a directory server-side (A7), so a
 * bad path surfaces the backend's TaxonomyError verbatim via `notify` and the
 * dialog stays open for correction. Escape cancels. */
function NewProjectDialog({
  onClose,
  onCreated,
  notify,
}: {
  onClose: () => void
  onCreated: () => void
  notify: (msg: string) => void
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
      onCreated()
    } catch (err) {
      notify(err instanceof Error ? err.message : String(err))
      setBusy(false) // keep the dialog open so the user can fix the path
    }
  }

  const INPUT =
    'w-full rounded-md border border-stone-300 bg-white px-2.5 py-1.5 text-sm ' +
    'text-stone-800 shadow-sm focus:outline-none focus:ring-1 focus:ring-accent-400 ' +
    'disabled:opacity-50'

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/30 backdrop-blur-sm"
      onClick={busy ? undefined : onClose}
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
          Register a project name bound to an existing absolute path on this
          machine.
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

function IconPlus() {
  return (
    <svg {...SVG_PROPS}>
      <path d="M12 5v14M5 12h14" />
    </svg>
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

/** Viewfinder brackets — "frame the content" (focus mode). */
function IconFocus() {
  return (
    <svg {...SVG_PROPS}>
      <path d="M8 3H6a3 3 0 0 0-3 3v2M16 3h2a3 3 0 0 1 3 3v2M8 21H6a3 3 0 0 1-3-3v-2M16 21h2a3 3 0 0 0 3-3v-2" />
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
