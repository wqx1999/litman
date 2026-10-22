import { useCallback, useEffect, useLayoutEffect, useRef, useState } from 'react'
import type { SimilarPair } from '../api'
import { useEscapeLayer } from './escapeStack'

/** One value in a VocabPopover. */
export interface VocabItem {
  value: string
  /** Papers using it; null = no count on the row (projects). */
  count: number | null
  /** Ticked: attached to the paper, or a selected filter. */
  on: boolean
  /** Zero papers in the current scope (left column only): grey, a dash for the
   * count, and not clickable — unless it is selected, so that a filter picked in
   * a wider view can still be cleared after switching to a narrower one. */
  dimmed?: boolean
  /** The status hue, for the left column's Status dimension. */
  dotClass?: string
}

interface Props {
  /** The field's display name, for the dialog's accessible name. */
  label: string
  /** The field in running text: "Search topics…", "Manage topics…". */
  noun: string
  /** Every value, already in display order. */
  values: VocabItem[]
  /** The header or pill this opens from. A getter, because a keyboard shortcut
   * opens the cockpit's popover with no click to take the element from, and the
   * element is only read once the popover places itself. */
  anchor: () => HTMLElement | null
  /** Which anchor edge the popover lines up with, opening the other way. */
  align: 'left' | 'right'
  /** A write is in flight: rows and Create wait, the search box keeps focus. */
  busy?: boolean
  searchable?: boolean
  /** Values attached to the paper but not in `values` (unregistered); typing
   * one offers no Create. */
  taken?: readonly string[]
  onToggle: (value: string) => void
  /** Register `value` (and attach it). When the server finds it too close to
   * registered values the caller asks about them in a dialog of its own; the
   * promise settles once that is over, with the matches if nothing was created
   * (the typed text then stays), or null once it is done or failed elsewhere. */
  onCreate?: (value: string) => Promise<SimilarPair[] | null>
  onManage?: () => void
  /** Draws a whole row in place of the plain one (projects' graded row). */
  renderRow?: (item: VocabItem) => React.ReactNode
  onClose: () => void
}

const ROW = 24
const WIDTHS = [240, 470, 700]
const EDGE = 16
const GAP = 6

/** Every value of one vocabulary at once, each with its paper count, so that a
 * value nobody remembers is still in plain sight before a near-copy of it gets
 * made. One component behind two anchors: a dimension header in the browse
 * panel (filters, counted over the current scope) and a Tags pill in the
 * cockpit (tags, counted over the whole library).
 *
 * Up to three columns of up to 18 rows (fewer in a short window), filled top to
 * bottom and then left to right, balanced; past three full columns the list
 * scrolls. The size follows the rows listed: typing narrows it from the far side
 * and shortens it from the bottom, while its top and search box stay where they
 * opened. It opens under its anchor and slides up to keep 16px clear of the
 * window's bottom edge, placed again whenever the window changes size or the
 * anchor moves (a scroll, a chip appearing above it).
 *
 * Closes on Escape (a layer of its own) and on a press anywhere outside it and
 * its anchor; a press on the anchor is left to the anchor's own toggle. */
export default function VocabPopover({
  label,
  noun,
  values,
  anchor,
  align,
  busy = false,
  searchable = true,
  taken = [],
  onToggle,
  onCreate,
  onManage,
  renderRow,
  onClose,
}: Props) {
  const [draft, setDraft] = useState('')
  const [viewportH, setViewportH] = useState(() => window.innerHeight)
  const rootRef = useRef<HTMLDivElement | null>(null)
  const inputRef = useRef<HTMLInputElement | null>(null)
  // What the window listeners need from the latest render; refreshed before
  // every placement.
  const latest = useRef({ anchor, align, onClose })
  // The last placement: its top, and the anchor box it was measured against.
  const placed = useRef<{ top: number; anchor: DOMRect } | null>(null)
  // The window height last seen, and whether a change to it still has to be
  // placed by the render it causes.
  const seenH = useRef(window.innerHeight)
  const resized = useRef(false)
  // A create is pending, which may mean the caller's dialog is up over the
  // popover: a press in it is not a press outside the popover.
  const creating = useRef(false)

  const n = values.length
  const typed = draft.trim()
  const lower = typed.toLowerCase()
  // Inline-create is offered only on a controlled-vocab field, for a genuinely
  // new value: non-empty, matching no registered value and nothing attached
  // (case-insensitive, so "Test" won't offer to re-create "test").
  const isNew =
    onCreate != null &&
    typed.length > 0 &&
    !values.some((v) => v.value.toLowerCase() === lower) &&
    !taken.some((v) => v.toLowerCase() === lower)
  const shown = typed
    ? values.filter((v) => v.value.toLowerCase().includes(lower))
    : values

  // The layout follows the rows actually listed: one column while they fit in
  // one, wider only when they need it. A column never holds more rows than it
  // does with nothing typed, so a filtered list is never taller than the one
  // that opened and the search box never moves.
  const listed = shown.length
  const perCol = Math.min(18, Math.max(10, Math.floor((viewportH - 200) / ROW)))
  const openRows = Math.ceil(n / Math.min(3, Math.max(1, Math.ceil(n / perCol))))
  const limit = Math.min(perCol, Math.max(1, openRows))
  const cols = Math.min(3, Math.max(1, Math.ceil(listed / limit)))
  const rows = Math.max(1, Math.ceil(listed / cols))
  const scroll = rows > perCol
  // Rows each column holds: the first listed % cols take the extra one, so 37
  // values stand 13 / 12 / 12 rather than 13 / 13 / 11.
  const caps = Array.from({ length: cols }, (_, c) =>
    listed % cols === 0 || c < listed % cols ? rows : rows - 1,
  )
  // Where the i-th listed value sits: down a column, then on to the next.
  function cellOf(i: number): React.CSSProperties {
    let c = 0
    while (c < cols - 1 && i >= caps[c]) i -= caps[c++]
    return { gridColumn: c + 1, gridRow: i + 1 }
  }

  // Placed afresh (on opening, when the window height changes, when the anchor
  // moves) it goes under the anchor and slides up to fit. Otherwise only its
  // rows changed (typing, a tick, a refresh): the top stays, and moves up only
  // as far as a taller list needs to stay inside the window, so the search box
  // never shifts under the typing. Sideways it always lines up with the anchor.
  const position = useCallback((fresh: boolean) => {
    const el = rootRef.current
    const a = latest.current.anchor()?.getBoundingClientRect()
    if (!a || !el) return
    // offset sizes, not the bounding box: the entrance animation scales it.
    const w = el.offsetWidth
    const h = el.offsetHeight
    let left = latest.current.align === 'left' ? a.left : a.right - w
    left = Math.min(Math.max(8, left), window.innerWidth - 8 - w)
    const prev = placed.current
    const moved =
      prev == null ||
      Math.abs(prev.anchor.bottom - a.bottom) > 0.5 ||
      Math.abs(prev.anchor.left - a.left) > 0.5 ||
      Math.abs(prev.anchor.right - a.right) > 0.5
    const from = !prev || fresh || moved ? a.bottom + GAP : prev.top
    const top = Math.max(EDGE, Math.min(from, window.innerHeight - EDGE - h))
    el.style.left = `${left}px`
    el.style.top = `${top}px`
    placed.current = { top, anchor: a }
  }, [])
  const place = useCallback(() => position(false), [position])

  // Every render, before paint, so a changed list never shows misplaced. Left
  // and top are written straight to the element, never through React state, so
  // placing never renders again.
  useLayoutEffect(() => {
    latest.current = { anchor, align, onClose }
    position(resized.current)
    resized.current = false
  })

  // Focus after the first placement, without scrolling anything to reach it.
  useLayoutEffect(() => {
    const first = inputRef.current ?? rootRef.current
    first?.focus({ preventScroll: true })
  }, [])

  useEffect(() => {
    const root = rootRef.current
    // Its own height changes (the Create row appearing, a late font) and so
    // does the window's: the page's root element is sized to the window
    // (index.css), so watching it stands in for a window size listener. A new
    // window height also changes the rows a column holds; the render that
    // follows places the popover afresh with them.
    const sized = new ResizeObserver(() => {
      const vh = window.innerHeight
      if (vh !== seenH.current) {
        seenH.current = vh
        resized.current = true
        setViewportH(vh)
      }
      position(resized.current)
    })
    if (root) sized.observe(root)
    sized.observe(document.documentElement)
    function onDown(e: MouseEvent) {
      const t = e.target as Node
      if (creating.current || root?.contains(t) || latest.current.anchor()?.contains(t)) return
      latest.current.onClose()
    }
    // Capture: a scroll inside the cockpit or the browse toolbar moves the
    // anchor without reaching the window.
    window.addEventListener('scroll', place, true)
    document.addEventListener('mousedown', onDown)
    return () => {
      sized.disconnect()
      window.removeEventListener('scroll', place, true)
      document.removeEventListener('mousedown', onDown)
    }
  }, [place, position])

  useEscapeLayer(true, onClose)

  async function create() {
    creating.current = true
    let refusedFor: SimilarPair[] | null
    try {
      refusedFor = await onCreate!(typed)
    } finally {
      creating.current = false
    }
    if (!refusedFor?.length) setDraft('')
    // The clicked row or the caller's dialog had the focus, and both are gone.
    inputRef.current?.focus({ preventScroll: true })
  }

  return (
    // Positioned against the viewport but rendered in place, so DOM nesting
    // and event bubbling stay what they were. That holds only while no ancestor
    // takes over as the box it is measured from (a transformed, filtered or
    // contained one would); the cockpit's dialogs rely on the same.
    <div
      ref={rootRef}
      role="dialog"
      aria-label={label}
      tabIndex={searchable ? undefined : -1}
      className="fixed z-50 flex animate-grow-in flex-col rounded-lg border border-stone-200 bg-white p-1.5 shadow-md focus:outline-none"
      style={{
        width: WIDTHS[cols - 1],
        maxWidth: 'calc(100vw - 16px)',
        maxHeight: 'calc(100vh - 32px)',
      }}
    >
      {searchable && (
        <input
          ref={inputRef}
          type="text"
          aria-label={`Search ${noun}`}
          placeholder={`Search ${noun}…`}
          value={draft}
          // Read-only rather than disabled while a write runs: a disabled
          // input drops focus, and the next keys would reach the app.
          readOnly={busy}
          onChange={(e) => setDraft(e.target.value)}
          className={`mb-1 w-full rounded-md border border-stone-300 bg-white px-2 py-1 text-sm text-stone-800 shadow-sm focus:outline-none focus:ring-1 focus:ring-accent-400 ${
            busy ? 'opacity-50' : ''
          }`}
        />
      )}
      <div
        className="min-h-0 overflow-y-auto overscroll-contain"
        style={scroll ? { maxHeight: perCol * ROW } : undefined}
      >
        {/* As many rows and columns as the listed values need; cellOf puts
            each value in its cell. */}
        <div
          className="grid gap-x-1"
          style={{
            gridTemplateColumns: `repeat(${cols}, minmax(0, 1fr))`,
            gridTemplateRows: `repeat(${rows}, minmax(${ROW}px, auto))`,
          }}
        >
          {shown.length === 0 && !isNew && (
            <div className="px-2 py-1 text-xs text-stone-400">
              {n === 0 ? 'No values yet.' : 'No match.'}
            </div>
          )}
          {shown.map((item, i) =>
            renderRow ? (
              <div key={item.value} style={cellOf(i)}>
                {renderRow(item)}
              </div>
            ) : (
              <VocabRow
                key={item.value}
                item={item}
                cell={cellOf(i)}
                busy={busy}
                onToggle={() => onToggle(item.value)}
              />
            ),
          )}
        </div>
      </div>
      {isNew && (
        <button
          type="button"
          disabled={busy}
          onClick={() => void create()}
          className="flex w-full items-center gap-2 rounded-md px-2 py-1 text-left text-sm text-accent-700 transition-colors hover:bg-accent-50 disabled:opacity-50"
        >
          <span className="w-3 shrink-0 text-accent-600">+</span>
          <span className="truncate">Create “{typed}”</span>
        </button>
      )}
      {onManage && (
        <div className="mt-1 border-t border-stone-100 pt-1">
          <button
            type="button"
            disabled={busy}
            onClick={onManage}
            className="flex w-full items-center gap-1.5 rounded-md px-2 py-1 text-left text-xs text-stone-500 transition-colors hover:bg-stone-100 hover:text-stone-700 disabled:opacity-50"
          >
            <span className="text-stone-400">⚙</span> Manage {noun}…
          </button>
        </div>
      )}
    </div>
  )
}

/** The plain row: a tick, the status dot where there is one, the value, and its
 * count against the right edge. A dimmed row is greyed only while it cannot be
 * clicked; a selected one keeps its normal text, and the tick and the dash say
 * the rest. */
function VocabRow({
  item,
  cell,
  busy,
  onToggle,
}: {
  item: VocabItem
  cell: React.CSSProperties
  busy: boolean
  onToggle: () => void
}) {
  const inert = item.dimmed === true && !item.on
  return (
    <button
      type="button"
      aria-pressed={item.on}
      disabled={busy || inert}
      onClick={onToggle}
      style={cell}
      className={`flex w-full items-center gap-2 rounded-md px-2 py-1 text-left text-xs transition-colors ${
        inert ? 'text-stone-400' : 'text-stone-700 hover:bg-stone-100'
      } ${busy ? 'opacity-50' : ''}`}
    >
      <span className={`w-3 shrink-0 ${item.on ? 'text-accent-600' : 'text-transparent'}`}>
        ✓
      </span>
      {item.dotClass && (
        <span className={`h-1.5 w-1.5 shrink-0 rounded-full ${item.dotClass}`} />
      )}
      <span title={item.value} className="truncate">
        {item.value}
      </span>
      {item.count !== null && (
        <span
          className={`ml-auto shrink-0 text-[11px] ${
            item.dimmed ? 'text-stone-300' : 'text-stone-400'
          }`}
        >
          {item.dimmed ? '–' : item.count}
        </span>
      )}
    </button>
  )
}
