import { useEffect, useRef, useState } from 'react'
import type { SearchScope } from '../types'
import { DROPDOWN_LIMIT, type Candidate } from '../search'

interface Props {
  value: string
  onChange: (q: string) => void
  /** Full ranked candidate list (App merges client id/title + server md hits). */
  candidates: Candidate[]
  /** A server notes/discussion fetch is in flight (drives the spinner row). */
  loading: boolean
  /** Selecting a row opens the matched scope: the PDF for an id/title hit, or the
   * notes/discussion doc (scrolled to the match) for a markdown hit. */
  onSelect: (candidate: Candidate) => void
}

const SCOPE_LABEL: Record<SearchScope, string> = {
  id: 'id',
  title: 'title',
  notes: 'notes',
  discussion: 'discussion',
}

/** Bold every case-insensitive occurrence of `q` in `text`. Plain substring
 * (no regex), so a query with special characters never breaks. */
function highlight(text: string, q: string) {
  const query = q.trim()
  if (!query) return text
  const lower = text.toLowerCase()
  const lq = query.toLowerCase()
  const parts: React.ReactNode[] = []
  let i = 0
  let key = 0
  for (;;) {
    const idx = lower.indexOf(lq, i)
    if (idx === -1) {
      parts.push(text.slice(i))
      break
    }
    if (idx > i) parts.push(text.slice(i, idx))
    parts.push(
      <mark
        key={key++}
        className="rounded-sm bg-accent-100 px-0.5 text-accent-700"
      >
        {text.slice(idx, idx + query.length)}
      </mark>,
    )
    i = idx + query.length
  }
  return parts
}

/** Global quick-jump search: an input plus a ranked typeahead dropdown spanning
 * id / title / notes / discussion. The dropdown previews the top few matches
 * (the middle list holds the full set); arrow keys move, Enter jumps, Esc
 * closes, and clicking outside dismisses it. */
export default function SearchBox({
  value,
  onChange,
  candidates,
  loading,
  onSelect,
}: Props) {
  const [open, setOpen] = useState(false)
  const [active, setActive] = useState(0)
  const boxRef = useRef<HTMLDivElement>(null)
  const inputRef = useRef<HTMLInputElement>(null)

  const trimmed = value.trim()
  const shown = candidates.slice(0, DROPDOWN_LIMIT)
  const overflow = candidates.length - shown.length
  // Show the panel while typing — even before results land we render a spinner
  // or an empty-state row so the box never feels unresponsive.
  const panelOpen = open && trimmed !== ''

  // Keep the active row in range as results change (or reset to the top).
  useEffect(() => {
    setActive(0)
  }, [value, candidates.length])

  // ⌘/Ctrl+K focuses the search box from anywhere — the universal quick-jump
  // convention. A window listener so it fires regardless of where focus sits;
  // the global shortcut dispatcher deliberately ignores Cmd/Ctrl combos and
  // leaves this one to us (see useKeyboardShortcuts' reserved-combo bail).
  // e.code (not e.key) keeps it layout-stable; selecting the existing text means
  // the next keystroke replaces the current query.
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if ((e.metaKey || e.ctrlKey) && e.code === 'KeyK') {
        e.preventDefault()
        inputRef.current?.focus()
        inputRef.current?.select()
      }
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [])

  const choose = (candidate: Candidate) => {
    onSelect(candidate)
    setOpen(false)
  }

  const onKeyDown = (e: React.KeyboardEvent<HTMLInputElement>) => {
    if (e.key === 'Escape') {
      setOpen(false)
      return
    }
    if (!panelOpen || shown.length === 0) return
    if (e.key === 'ArrowDown') {
      e.preventDefault()
      setActive((i) => Math.min(i + 1, shown.length - 1))
    } else if (e.key === 'ArrowUp') {
      e.preventDefault()
      setActive((i) => Math.max(i - 1, 0))
    } else if (e.key === 'Enter') {
      e.preventDefault()
      const pick = shown[active]
      if (pick) choose(pick)
    }
  }

  return (
    <div
      ref={boxRef}
      className="relative flex-1"
      // Focus leaving the whole box (input + rows) closes the dropdown. Rows
      // keep input focus via onMouseDown preventDefault, so a click selects
      // without first triggering this blur.
      onBlur={(e) => {
        if (!boxRef.current?.contains(e.relatedTarget as Node | null)) {
          setOpen(false)
        }
      }}
    >
      <span className="pointer-events-none absolute left-3 top-1/2 -translate-y-1/2 text-sm text-stone-400">
        ⌕
      </span>
      <input
        ref={inputRef}
        value={value}
        onChange={(e) => {
          onChange(e.target.value)
          setOpen(true)
        }}
        onFocus={() => setOpen(true)}
        onKeyDown={onKeyDown}
        placeholder="Search id, title, notes, discussion…"
        role="combobox"
        aria-expanded={panelOpen}
        aria-controls="search-listbox"
        autoComplete="off"
        className="w-full max-w-md rounded-lg border border-stone-300 bg-white py-1.5 pl-8 pr-3 text-sm text-stone-800 shadow-sm transition placeholder:text-stone-400 focus:border-accent-500 focus:outline-none focus:ring-2 focus:ring-accent-500/25"
      />

      {panelOpen && (
        <div
          id="search-listbox"
          role="listbox"
          className="animate-grow-in absolute left-0 top-full z-50 mt-1.5 w-full max-w-md overflow-hidden rounded-xl border border-stone-200 bg-white shadow-lg shadow-stone-900/5"
        >
          {shown.map((c, i) => {
            const isActive = i === active
            return (
              <button
                key={c.id}
                role="option"
                aria-selected={isActive}
                // Keep input focus so the box's onBlur doesn't fire first.
                onMouseDown={(e) => e.preventDefault()}
                onMouseEnter={() => setActive(i)}
                onClick={() => choose(c)}
                className={`flex w-full flex-col gap-0.5 px-3 py-2 text-left transition-colors ${
                  isActive ? 'bg-accent-50' : 'hover:bg-stone-100'
                }`}
              >
                <div className="flex items-center gap-2">
                  <span className="truncate font-mono text-xs text-stone-500">
                    {highlight(c.id, value)}
                  </span>
                  <span className="ml-auto shrink-0 rounded bg-stone-200 px-1.5 py-0.5 text-[10px] font-medium uppercase tracking-wide text-stone-500">
                    {SCOPE_LABEL[c.scope]}
                  </span>
                </div>
                <div className="truncate text-sm text-stone-800">
                  {c.title ? highlight(c.title, value) : c.id}
                </div>
                {c.snippet && (
                  <div className="truncate text-xs text-stone-500">
                    {highlight(c.snippet, value)}
                  </div>
                )}
              </button>
            )
          })}

          {shown.length === 0 && (
            <div className="px-3 py-2.5 text-xs text-stone-400">
              {loading ? 'Searching…' : 'No matches.'}
            </div>
          )}

          {(overflow > 0 || (loading && shown.length > 0)) && (
            <div className="flex items-center justify-between border-t border-stone-200 px-3 py-1.5 text-[11px] text-stone-400">
              <span>{overflow > 0 ? `+${overflow} more in the list` : ''}</span>
              {loading && <span>Searching notes…</span>}
            </div>
          )}
        </div>
      )}
    </div>
  )
}
