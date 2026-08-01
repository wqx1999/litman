import { useEffect, useRef, useState } from 'react'
import { createPortal } from 'react-dom'

/** Window-level PDF drag catcher for the drag-in ingest (task-gui-doi-add).
 *
 * The ONLY entry to GUI add — deliberately no button/file-picker twin (wangq:
 * a click-through picker flow is worse than asking the agent; an extra button
 * is noise). Behavior contract:
 *
 * - Reacts only to drags carrying a file that could be a PDF; dragging text
 *   or images shows nothing and is left entirely to the browser (so dragging
 *   text into the notes editor keeps working).
 * - A non-PDF *file* drop gets no response either — but its default is still
 *   prevented, so a stray drop never navigates the SPA away.
 * - Exactly one file per drop (curation is one paper at a time, ADR-006);
 *   more → the caller toasts.
 *
 * During a drag the browser exposes item *types* but not contents, and some
 * platforms report an empty type — those still light the overlay; the drop
 * handler (extension/MIME) and the server's %PDF- magic check are the real
 * gate. The overlay is pointer-events-none: pure indication, never a target.
 */
export default function DropZone({
  enabled,
  onPdf,
  onReject,
}: {
  enabled: boolean
  onPdf: (file: File) => void
  onReject: (message: string) => void
}) {
  const [active, setActive] = useState(false)
  const depth = useRef(0)

  useEffect(() => {
    if (!enabled) return

    const couldBePdf = (dt: DataTransfer | null): boolean => {
      if (!dt) return false
      return Array.from(dt.items).some(
        (it) =>
          it.kind === 'file' &&
          (it.type === 'application/pdf' || it.type === ''),
      )
    }

    const onDragEnter = (e: DragEvent) => {
      if (!couldBePdf(e.dataTransfer)) return
      e.preventDefault()
      depth.current += 1
      setActive(true)
    }
    const onDragOver = (e: DragEvent) => {
      // preventDefault here is what makes the window a legal drop target.
      if (!couldBePdf(e.dataTransfer)) return
      e.preventDefault()
    }
    const onDragLeave = (e: DragEvent) => {
      if (!couldBePdf(e.dataTransfer)) return
      depth.current = Math.max(0, depth.current - 1)
      if (depth.current === 0) setActive(false)
    }
    const onDrop = (e: DragEvent) => {
      const dt = e.dataTransfer
      // Text/image drags pass through untouched (notes editor keeps them).
      if (!dt || !Array.from(dt.types).includes('Files')) return
      e.preventDefault()
      depth.current = 0
      setActive(false)
      const files = Array.from(dt.files)
      const pdfs = files.filter(
        (f) =>
          f.type === 'application/pdf' ||
          f.name.toLowerCase().endsWith('.pdf'),
      )
      if (pdfs.length === 0) return
      if (files.length > 1) {
        onReject('One paper at a time.')
        return
      }
      onPdf(pdfs[0])
    }

    window.addEventListener('dragenter', onDragEnter)
    window.addEventListener('dragover', onDragOver)
    window.addEventListener('dragleave', onDragLeave)
    window.addEventListener('drop', onDrop)
    return () => {
      window.removeEventListener('dragenter', onDragEnter)
      window.removeEventListener('dragover', onDragOver)
      window.removeEventListener('dragleave', onDragLeave)
      window.removeEventListener('drop', onDrop)
      depth.current = 0
      setActive(false)
    }
  }, [enabled, onPdf, onReject])

  if (!active) return null
  return createPortal(
    <div className="pointer-events-none fixed inset-0 z-50 flex items-center justify-center bg-black/20 backdrop-blur-sm">
      <div className="animate-grow-in rounded-2xl bg-white px-8 py-6 text-center shadow-xl ring-1 ring-stone-200">
        <div className="text-sm font-semibold text-stone-900">
          Drop PDF to add
        </div>
        <div className="mt-1 text-xs text-stone-500">One paper at a time</div>
      </div>
    </div>,
    document.body,
  )
}
