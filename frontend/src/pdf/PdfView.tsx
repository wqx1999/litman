import { useCallback, useEffect, useRef, useState } from 'react'
import * as pdfjsLib from 'pdfjs-dist'
import type { PDFDocumentProxy, RenderTask } from 'pdfjs-dist'
import workerUrl from 'pdfjs-dist/build/pdf.worker.min.mjs?url'
import { pdfUrl } from '../api'

// pdf.js needs its worker registered once, before any document is parsed. The
// `?url` import gives vite the hashed worker path under assets/ at build time.
pdfjsLib.GlobalWorkerOptions.workerSrc = workerUrl

interface Props {
  paperId: string
}

const MIN_SCALE = 0.5
const MAX_SCALE = 3.0
const DEFAULT_SCALE = 1.3
const ZOOM_STEP = 0.2
const WHEEL_STEP = 0.1

// Clamp to [MIN, MAX] and snap to 0.1 so button/wheel zoom lands on tidy steps.
// The % input bypasses the snap so the field can hold any integer percentage.
const clampScale = (s: number) =>
  Math.min(MAX_SCALE, Math.max(MIN_SCALE, Math.round(s * 10) / 10))

/** Read-only pdf.js render with HiDPI-sharp pages and zoom (Phase 1 — no
 * annotation editor layer, that lands in Phase 2).
 *
 * Sharpness: each page's canvas backing store is sized at device pixels
 * (`viewport * devicePixelRatio`) while displayed at the logical CSS size, and
 * the page is rendered through a `scale * dpr` viewport so glyphs are crisp on
 * Retina/HiDPI and at any zoom (we re-render rather than CSS-stretch a bitmap).
 *
 * Concurrency: the document load and the per-page render loop both key off a
 * monotonically incremented `renderGen`. A zoom change, paperId change, or
 * unmount bumps the generation, cancels the in-flight `RenderTask`, and the
 * stale loop bails on its next iteration — avoiding "Cannot use the same
 * canvas" / "render cancelled" errors from overlapping renders. */
export default function PdfView({ paperId }: Props) {
  const containerRef = useRef<HTMLDivElement>(null)
  const docRef = useRef<PDFDocumentProxy | null>(null)
  const renderTaskRef = useRef<RenderTask | null>(null)
  // Bumped on every (re)render trigger; the async loop compares against it to
  // detect that it has been superseded and must stop.
  const renderGenRef = useRef(0)
  // The scroll viewport; the ctrl/cmd-wheel zoom listener binds here.
  const scrollRef = useRef<HTMLDivElement>(null)

  const [error, setError] = useState<string | null>(null)
  const [pageCount, setPageCount] = useState(0)
  const [scale, setScale] = useState(DEFAULT_SCALE)
  // Draft string while the user is typing in the % box; null = show live scale.
  const [pctDraft, setPctDraft] = useState<string | null>(null)

  const zoomBy = useCallback((delta: number) => {
    setScale((s) => clampScale(s + delta))
  }, [])
  const resetZoom = useCallback(() => setScale(DEFAULT_SCALE), [])

  // Commit a typed percentage from the toolbar input: parse, clamp, apply. The
  // input holds an integer percent so the field can land on any value (e.g.
  // 137%), unlike the 0.1-snapped button/wheel zoom.
  const commitPct = useCallback((raw: string) => {
    setPctDraft(null)
    const n = parseInt(raw, 10)
    if (Number.isFinite(n) && n > 0) {
      setScale(Math.min(MAX_SCALE, Math.max(MIN_SCALE, n / 100)))
    }
  }, [])

  // Render (or re-render) all pages of the currently-loaded document at `scale`.
  async function renderAll(doc: PDFDocumentProxy, gen: number) {
    const container = containerRef.current
    if (!container) return
    container.replaceChildren()

    const dpr = window.devicePixelRatio || 1
    for (let n = 1; n <= doc.numPages; n++) {
      if (gen !== renderGenRef.current) return
      const page = await doc.getPage(n)
      if (gen !== renderGenRef.current) return

      const viewport = page.getViewport({ scale })
      const canvas = document.createElement('canvas')
      canvas.className = 'mx-auto mb-4 rounded-sm bg-white shadow-md ring-1 ring-black/5'
      canvas.width = Math.ceil(viewport.width * dpr)
      canvas.height = Math.ceil(viewport.height * dpr)
      canvas.style.width = `${Math.floor(viewport.width)}px`
      canvas.style.height = `${Math.floor(viewport.height)}px`
      const ctx = canvas.getContext('2d')
      if (!ctx) continue
      container.appendChild(canvas)

      const task = page.render({
        canvas,
        canvasContext: ctx,
        viewport: page.getViewport({ scale: scale * dpr }),
      })
      renderTaskRef.current = task
      try {
        await task.promise
      } catch {
        // Cancelled (superseded by a newer render) or genuine render failure;
        // either way a newer loop owns the canvas, so just stop this one.
        return
      }
      if (renderTaskRef.current === task) renderTaskRef.current = null
    }
  }

  // Load the document when the paper changes.
  useEffect(() => {
    const gen = ++renderGenRef.current
    renderTaskRef.current?.cancel()
    renderTaskRef.current = null
    setError(null)
    setPageCount(0)
    // Mount-time initialization / defensive reset. TabArea keys PdfView per tab,
    // so `paperId` is fixed for an instance's lifetime and this effect runs only
    // at mount today; the reset matters if Phase 2 changes TabArea keying so the
    // same instance can switch papers.
    setScale(DEFAULT_SCALE)

    const loadingTask = pdfjsLib.getDocument({ url: pdfUrl(paperId) })
    loadingTask.promise
      .then((doc) => {
        if (gen !== renderGenRef.current) {
          void doc.destroy()
          return
        }
        docRef.current = doc
        setPageCount(doc.numPages)
        void renderAll(doc, gen)
      })
      .catch((err: unknown) => {
        if (gen === renderGenRef.current) {
          setError(err instanceof Error ? err.message : String(err))
        }
      })

    return () => {
      renderGenRef.current++
      renderTaskRef.current?.cancel()
      renderTaskRef.current = null
      void loadingTask.destroy()
      docRef.current = null
    }
    // `scale` intentionally excluded: it is initialized at mount above, and
    // zoom-only changes are handled by the separate re-render effect below.
  }, [paperId])

  // Re-render in place when zoom changes (the doc is already loaded).
  useEffect(() => {
    const doc = docRef.current
    if (!doc) return
    const gen = ++renderGenRef.current
    renderTaskRef.current?.cancel()
    renderTaskRef.current = null
    void renderAll(doc, gen)
    // Own cleanup rather than leaning on the load effect's: cancel this render
    // and bump the generation so an in-flight loop bails. Decouples the two
    // effects (Phase 2 will stack an annotation effect on the same canvas).
    return () => {
      renderGenRef.current++
      renderTaskRef.current?.cancel()
      renderTaskRef.current = null
    }
    // `renderAll` reads `scale`/refs directly; re-running only on `scale` is the
    // intended trigger (the doc comes from a ref, not the dep array).
  }, [scale])

  // Ctrl/Cmd + wheel zooms the PDF instead of the browser page (also catches
  // trackpad pinch, which fires wheel events with ctrlKey set). Plain wheel is
  // left alone so it still scrolls the page stack. Bound manually with
  // passive:false because a React onWheel handler can't preventDefault reliably.
  useEffect(() => {
    const el = scrollRef.current
    if (!el) return
    const onWheel = (e: WheelEvent) => {
      if (!(e.ctrlKey || e.metaKey)) return
      e.preventDefault()
      zoomBy(e.deltaY < 0 ? WHEEL_STEP : -WHEEL_STEP)
    }
    el.addEventListener('wheel', onWheel, { passive: false })
    return () => el.removeEventListener('wheel', onWheel)
  }, [zoomBy])

  // Ctrl/Cmd + (+ / - / 0), including the numpad, drives PDF zoom rather than
  // the browser's. Only one PdfView is mounted at a time (TabArea renders the
  // active tab only), so a window listener is unambiguous.
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (!(e.ctrlKey || e.metaKey)) return
      if (e.key === '+' || e.key === '=' || e.code === 'NumpadAdd') {
        e.preventDefault()
        zoomBy(ZOOM_STEP)
      } else if (e.key === '-' || e.key === '_' || e.code === 'NumpadSubtract') {
        e.preventDefault()
        zoomBy(-ZOOM_STEP)
      } else if (e.key === '0' || e.code === 'Numpad0') {
        e.preventDefault()
        resetZoom()
      }
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [zoomBy, resetZoom])

  return (
    <div className="flex h-full flex-col">
      <div className="flex shrink-0 items-center gap-2 border-b border-stone-200 bg-stone-100 px-3 py-1.5">
        <button
          onClick={() => zoomBy(-ZOOM_STEP)}
          disabled={scale <= MIN_SCALE}
          title="Zoom out"
          className="rounded-md px-2 py-0.5 text-sm text-stone-600 transition-colors hover:bg-stone-200 disabled:text-stone-300 disabled:hover:bg-transparent"
        >
          −
        </button>
        <div className="flex items-center gap-0.5">
          <input
            value={pctDraft ?? String(Math.round(scale * 100))}
            onChange={(e) =>
              setPctDraft(e.target.value.replace(/[^0-9]/g, '').slice(0, 3))
            }
            onFocus={(e) => {
              setPctDraft(String(Math.round(scale * 100)))
              e.currentTarget.select()
            }}
            onBlur={(e) => commitPct(e.currentTarget.value)}
            onKeyDown={(e) => {
              if (e.key === 'Enter') {
                e.currentTarget.blur()
              } else if (e.key === 'Escape') {
                setPctDraft(null)
                e.currentTarget.blur()
              }
            }}
            inputMode="numeric"
            aria-label="Zoom percentage"
            title="Type a zoom %, then Enter"
            className="w-9 rounded bg-transparent text-right text-xs tabular-nums text-stone-600 hover:bg-stone-200 focus:bg-white focus:outline-none focus:ring-1 focus:ring-stone-300"
          />
          <span className="text-xs text-stone-500">%</span>
        </div>
        <button
          onClick={() => zoomBy(ZOOM_STEP)}
          disabled={scale >= MAX_SCALE}
          title="Zoom in"
          className="rounded-md px-2 py-0.5 text-sm text-stone-600 transition-colors hover:bg-stone-200 disabled:text-stone-300 disabled:hover:bg-transparent"
        >
          +
        </button>
        {pageCount > 0 && (
          <span className="ml-auto text-xs text-stone-500">
            {pageCount} page{pageCount === 1 ? '' : 's'}
          </span>
        )}
      </div>

      <div ref={scrollRef} className="min-h-0 flex-1 overflow-auto bg-stone-200 p-6">
        {error && (
          <div className="text-sm text-red-700">Failed to load PDF: {error}</div>
        )}
        {!error && pageCount === 0 && (
          <div className="text-sm text-stone-500">Loading PDF…</div>
        )}
        <div ref={containerRef} />
      </div>
    </div>
  )
}
