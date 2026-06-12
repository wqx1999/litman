import { useState } from 'react'
import type { VaultsPayload } from '../types'
import type { Candidate } from '../search'
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
}: Props) {
  const [dark, toggleDark] = useDarkMode()

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
