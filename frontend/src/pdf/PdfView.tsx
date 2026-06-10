import { useEffect, useRef, useState } from 'react'
import * as pdfjsLib from 'pdfjs-dist'
import workerUrl from 'pdfjs-dist/build/pdf.worker.min.mjs?url'
import { pdfUrl } from '../api'

// pdf.js needs its worker registered once, before any document is parsed. The
// `?url` import gives vite the hashed worker path under assets/ at build time.
pdfjsLib.GlobalWorkerOptions.workerSrc = workerUrl

interface Props {
  paperId: string
}

/** Read-only pdf.js render: every page to its own canvas (Phase 1 — no
 * annotation editor layer, that lands in Phase 2). */
export default function PdfView({ paperId }: Props) {
  const containerRef = useRef<HTMLDivElement>(null)
  const [error, setError] = useState<string | null>(null)
  const [pageCount, setPageCount] = useState(0)

  useEffect(() => {
    let cancelled = false
    const container = containerRef.current
    if (!container) return

    container.replaceChildren()
    setError(null)
    setPageCount(0)

    const loadingTask = pdfjsLib.getDocument({ url: pdfUrl(paperId) })
    loadingTask.promise
      .then(async (doc) => {
        if (cancelled) return
        setPageCount(doc.numPages)
        for (let n = 1; n <= doc.numPages; n++) {
          if (cancelled) return
          const page = await doc.getPage(n)
          const viewport = page.getViewport({ scale: 1.4 })
          const canvas = document.createElement('canvas')
          canvas.className = 'mx-auto mb-4 shadow-sm bg-white'
          canvas.width = viewport.width
          canvas.height = viewport.height
          const ctx = canvas.getContext('2d')
          if (!ctx) continue
          container.appendChild(canvas)
          await page.render({ canvas, canvasContext: ctx, viewport }).promise
        }
      })
      .catch((err: unknown) => {
        if (!cancelled) setError(err instanceof Error ? err.message : String(err))
      })

    return () => {
      cancelled = true
      void loadingTask.destroy()
    }
  }, [paperId])

  return (
    <div className="h-full overflow-auto bg-stone-200 p-4">
      {error && (
        <div className="text-sm text-red-700">Failed to load PDF: {error}</div>
      )}
      {!error && pageCount === 0 && (
        <div className="text-sm text-stone-500">Loading PDF…</div>
      )}
      <div ref={containerRef} />
    </div>
  )
}
