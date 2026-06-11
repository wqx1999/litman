import { useEffect, useRef, useState } from 'react'
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

  const [error, setError] = useState<string | null>(null)
  const [pageCount, setPageCount] = useState(0)
  const [scale, setScale] = useState(DEFAULT_SCALE)

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
      canvas.className = 'mx-auto mb-4 shadow-sm bg-white'
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

  const zoomOut = () =>
    setScale((s) => Math.max(MIN_SCALE, Math.round((s - ZOOM_STEP) * 10) / 10))
  const zoomIn = () =>
    setScale((s) => Math.min(MAX_SCALE, Math.round((s + ZOOM_STEP) * 10) / 10))

  return (
    <div className="flex h-full flex-col">
      <div className="flex shrink-0 items-center gap-2 border-b border-stone-200 bg-stone-100 px-3 py-1.5">
        <button
          onClick={zoomOut}
          disabled={scale <= MIN_SCALE}
          title="Zoom out"
          className="rounded px-2 py-0.5 text-sm text-stone-700 hover:bg-stone-200 disabled:text-stone-400"
        >
          −
        </button>
        <span className="w-12 text-center text-xs tabular-nums text-stone-600">
          {Math.round(scale * 100)}%
        </span>
        <button
          onClick={zoomIn}
          disabled={scale >= MAX_SCALE}
          title="Zoom in"
          className="rounded px-2 py-0.5 text-sm text-stone-700 hover:bg-stone-200 disabled:text-stone-400"
        >
          +
        </button>
        {pageCount > 0 && (
          <span className="ml-auto text-xs text-stone-500">
            {pageCount} page{pageCount === 1 ? '' : 's'}
          </span>
        )}
      </div>

      <div className="min-h-0 flex-1 overflow-auto bg-stone-200 p-4">
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
