import { useEffect, useRef, useState } from 'react'
import { createPortal } from 'react-dom'
import type { Tab } from '../types'
import PdfView from '../pdf/PdfView'
import type { PdfHandle } from '../pdf/PdfView'
import MdView from '../md/MdView'
import type { MdDraft } from '../md/MdView'

interface Props {
  tabs: Tab[]
  activeKey: string | null
  onActivate: (key: string) => void
  onClose: (key: string) => void
  /** Move the tab at index `from` to index `to` (pointer-drag reorder). */
  onReorder: (from: number, to: number) => void
  /** Close every tab except the ACTIVE one (not the one under the cursor —
   * the strip menu is bar-level, so it acts the same wherever it is invoked). */
  onCloseOthers: () => void
  onCloseAll: () => void
  onOpenPaper: (id: string) => void
  /** Register/unregister a PDF tab's flush handle for the close-time prompt. */
  onRegisterPdf: (key: string, handle: PdfHandle | null) => void
  /** Surface a transient message (e.g. a failed md save) to the host. */
  onNotify: (message: string) => void
  /** A notes/discussion tab (by key) to scroll to + highlight a query in, set
   * when the doc was opened from a search hit. */
  mdJump: { key: string; query: string } | null
  /** The active md tab's lifted edit session (App-owned), or undefined when the
   * active tab is not an md tab or is not being edited. */
  mdDraft?: MdDraft
  /** Bumped by a resync-from-disk so the active md tab re-reads its file (CLI/
   * agent edits to notes.md / discussion.md). Forwarded to MdView. */
  mdReloadToken?: number
  /** Forwarded md edit-session callbacks (App owns the per-tab draft map). */
  onMdBeginEdit: (tabKey: string, seed: string) => void
  onMdDraftChange: (tabKey: string, draft: string) => void
  onMdEndEdit: (tabKey: string) => void
  /** A successful md save — App advances its doc-mtime baseline (D2). */
  onMdSaved?: (paperId: string, doc: 'notes' | 'discussion') => void
}

/** Pixels of horizontal travel before a press becomes a drag. Below this a
 * press-and-release is a plain click (tab activation). */
const DRAG_THRESHOLD = 4
/** Strip-edge band (px) that auto-scrolls the overflowing strip mid-drag. */
const EDGE_BAND = 24
const EDGE_SCROLL_STEP = 8

export default function TabArea({
  tabs,
  activeKey,
  onActivate,
  onClose,
  onReorder,
  onCloseOthers,
  onCloseAll,
  onOpenPaper,
  onRegisterPdf,
  onNotify,
  mdJump,
  mdDraft,
  mdReloadToken,
  onMdBeginEdit,
  onMdDraftChange,
  onMdEndEdit,
  onMdSaved,
}: Props) {
  const active = tabs.find((t) => t.key === activeKey) ?? null

  const stripRef = useRef<HTMLDivElement | null>(null)

  // --- Pointer-drag reorder -----------------------------------------------
  // Deliberately NOT HTML5 drag-and-drop: that path is unscriptable for the
  // E2E (see AuthorRows), shows an OS ghost image, and would rub against the
  // window-level file-drop listeners. Pointer events behave the same in
  // Chromium and WKWebView, and a real drag only begins past a small travel
  // threshold so plain clicks keep their native path (capturing on pointerdown
  // would retarget the ensuing click away from the label button — WebKit and
  // Chromium disagree on the details, so we never capture before the
  // threshold).
  const dragRef = useRef<{
    key: string
    pointerId: number
    /** The origin tab element — capture target once the threshold is passed. */
    el: HTMLElement
    startX: number
    lastX: number
    started: boolean
  } | null>(null)
  // Visual-only mirror of dragRef.started (dims the dragged tab).
  const [dragKey, setDragKey] = useState<string | null>(null)
  // Eat the click that follows a completed drag (cleared on the next tick —
  // engines differ on whether that click even reaches the label button).
  const suppressClickRef = useRef(false)
  // Edge auto-scroll: direction (-1/0/+1) + the live rAF loop id.
  const scrollDirRef = useRef(0)
  const rafRef = useRef(0)

  // The swap check reads `tabs`/`onReorder` from the CURRENT render (a ref
  // reassigned every render, App.tsx's mdDraftsRef idiom) so the rAF loop and
  // stale pointermove closures never reorder against an outdated tab list.
  const swapCheckRef = useRef<(x: number) => void>(() => {})
  swapCheckRef.current = (x: number) => {
    const drag = dragRef.current
    const strip = stripRef.current
    if (!drag || !drag.started || !strip) return
    const els = Array.from(strip.querySelectorAll<HTMLElement>('[data-tabkey]'))
    const from = tabs.findIndex((t) => t.key === drag.key)
    if (from === -1 || els.length !== tabs.length) return
    // Insertion index = how many OTHER tabs sit with their midpoint left of the
    // pointer. Stable at boundaries: after a swap the two thresholds differ by
    // the dragged tab's own width, a built-in hysteresis (no flapping).
    let to = 0
    els.forEach((el, i) => {
      if (i === from) return
      const r = el.getBoundingClientRect()
      if (r.left + r.width / 2 < x) to++
    })
    if (to !== from) onReorder(from, to)
  }

  const stopAutoScroll = () => {
    scrollDirRef.current = 0
    if (rafRef.current) cancelAnimationFrame(rafRef.current)
    rafRef.current = 0
  }

  const endDrag = () => {
    const drag = dragRef.current
    if (!drag) return
    if (drag.started) {
      try {
        drag.el.releasePointerCapture(drag.pointerId)
      } catch {
        /* already released */
      }
      suppressClickRef.current = true
      setTimeout(() => {
        suppressClickRef.current = false
      }, 0)
    }
    dragRef.current = null
    setDragKey(null)
    stopAutoScroll()
  }

  const onStripPointerMove = (e: React.PointerEvent<HTMLDivElement>) => {
    const drag = dragRef.current
    if (!drag || e.pointerId !== drag.pointerId) return
    // Self-heal: a pointerup that landed outside the window pre-capture never
    // reached us — do not let a buttonless hover keep "dragging".
    if (e.buttons === 0) {
      endDrag()
      return
    }
    drag.lastX = e.clientX
    if (!drag.started) {
      if (Math.abs(e.clientX - drag.startX) < DRAG_THRESHOLD) return
      drag.started = true
      drag.el.setPointerCapture(drag.pointerId)
      setDragKey(drag.key)
    }
    swapCheckRef.current(e.clientX)
    // Near a strip edge, run an rAF scroll loop so a tab can travel beyond the
    // visible region (the crowded strip is the whole point of reordering).
    const strip = stripRef.current
    if (strip && strip.scrollWidth > strip.clientWidth) {
      const r = strip.getBoundingClientRect()
      const dir =
        e.clientX < r.left + EDGE_BAND ? -1 : e.clientX > r.right - EDGE_BAND ? 1 : 0
      if (dir !== scrollDirRef.current) {
        scrollDirRef.current = dir
        if (dir !== 0 && !rafRef.current) {
          const tick = () => {
            const s = stripRef.current
            const d = dragRef.current
            if (!s || !d || !d.started || scrollDirRef.current === 0) {
              rafRef.current = 0
              return
            }
            s.scrollLeft += scrollDirRef.current * EDGE_SCROLL_STEP
            // Tabs slide under a stationary pointer while we scroll; re-run
            // the swap check so the order tracks without a wiggle.
            swapCheckRef.current(d.lastX)
            rafRef.current = requestAnimationFrame(tick)
          }
          rafRef.current = requestAnimationFrame(tick)
        }
      }
    }
  }

  // --- Bar-level context menu (Close other / Close all) --------------------
  const [menu, setMenu] = useState<{ x: number; y: number } | null>(null)

  // Close on Escape. Capture phase + stopPropagation so the global shortcut
  // dispatcher (and a PDF tool's own Esc) never sees the keypress that was
  // aimed at this menu.
  useEffect(() => {
    if (!menu) return
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') {
        e.stopPropagation()
        setMenu(null)
      }
    }
    window.addEventListener('keydown', onKey, true)
    return () => window.removeEventListener('keydown', onKey, true)
  }, [menu])

  const menuItem =
    'block w-full rounded-lg px-2.5 py-1.5 text-left text-sm text-stone-700 transition-colors enabled:hover:bg-stone-100 disabled:opacity-40'

  return (
    <section className="flex min-w-0 flex-1 flex-col bg-white">
      <div
        ref={stripRef}
        onPointerMove={onStripPointerMove}
        onPointerUp={endDrag}
        onPointerCancel={endDrag}
        onContextMenu={(e) => {
          // With no tabs there is nothing to close — leave the native menu be.
          if (tabs.length === 0) return
          e.preventDefault()
          setMenu({
            x: Math.max(8, Math.min(e.clientX, window.innerWidth - 200)),
            y: e.clientY + 2,
          })
        }}
        className="flex select-none items-stretch gap-1 overflow-x-auto border-b border-stone-200 bg-stone-100 px-2 pt-1.5"
      >
        {tabs.length === 0 && (
          <div className="px-2 py-2 text-xs text-stone-400">
            Open a paper from the list.
          </div>
        )}
        {tabs.map((t) => {
          const isActive = t.key === activeKey
          return (
            <div
              key={t.key}
              data-tabkey={t.key}
              onPointerDown={(e) => {
                if (e.button !== 0 || e.pointerType === 'touch') return
                // The × keeps its plain click; a press there never drags.
                if ((e.target as HTMLElement).closest('[data-tab-close]')) return
                dragRef.current = {
                  key: t.key,
                  pointerId: e.pointerId,
                  el: e.currentTarget,
                  startX: e.clientX,
                  lastX: e.clientX,
                  started: false,
                }
              }}
              className={`group flex shrink-0 animate-grow-in items-center gap-2 rounded-t-lg border border-b-0 px-3 py-1.5 text-sm transition-colors ${
                isActive
                  ? 'border-stone-200 bg-white text-stone-900'
                  : 'border-transparent text-stone-500 hover:bg-stone-200/70'
              } ${dragKey === t.key ? 'opacity-70' : ''}`}
            >
              <button
                onClick={() => {
                  if (suppressClickRef.current) return // that was a drag, not a click
                  onActivate(t.key)
                }}
                className="max-w-48 truncate"
              >
                {t.label}
              </button>
              <button
                data-tab-close
                onClick={() => onClose(t.key)}
                title="Close tab"
                className={`rounded p-0.5 leading-none text-stone-400 transition-colors hover:bg-stone-300 hover:text-stone-700 ${
                  isActive ? '' : 'opacity-0 group-hover:opacity-100'
                }`}
              >
                ×
              </button>
            </div>
          )
        })}
      </div>

      {menu &&
        createPortal(
          <div
            className="fixed inset-0 z-50"
            onPointerDown={() => setMenu(null)}
            onContextMenu={(e) => {
              e.preventDefault()
              setMenu(null)
            }}
          >
            <div
              onPointerDown={(e) => e.stopPropagation()}
              style={{ left: menu.x, top: menu.y }}
              className="fixed min-w-44 animate-grow-in rounded-xl border border-stone-200 bg-white p-1 shadow-lg shadow-stone-900/5"
            >
              <button
                onClick={() => {
                  setMenu(null)
                  onCloseOthers()
                }}
                disabled={tabs.length < 2}
                className={menuItem}
              >
                Close other tabs
              </button>
              <button
                onClick={() => {
                  setMenu(null)
                  onCloseAll()
                }}
                className={menuItem}
              >
                Close all tabs
              </button>
            </div>
          </div>,
          document.body,
        )}

      <div className="min-h-0 flex-1">
        {active === null && (
          <div className="flex h-full items-center justify-center text-sm text-stone-400">
            No document open.
          </div>
        )}
        {active && active.kind === 'pdf' && (
          <PdfView
            key={active.key}
            paperId={active.paperId}
            tabKey={active.key}
            onRegister={onRegisterPdf}
          />
        )}
        {active && active.kind !== 'pdf' && (
          <MdView
            key={active.key}
            paperId={active.paperId}
            doc={active.kind}
            tabKey={active.key}
            onOpenPaper={onOpenPaper}
            onNotify={onNotify}
            highlightQuery={
              mdJump && mdJump.key === active.key ? mdJump.query : undefined
            }
            draftEntry={mdDraft}
            reloadToken={mdReloadToken}
            onBeginEdit={onMdBeginEdit}
            onDraftChange={onMdDraftChange}
            onEndEdit={onMdEndEdit}
            onSaved={onMdSaved}
          />
        )}
      </div>
    </section>
  )
}
