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
 * - A non-PDF *file* drop gets no response either — but its default IS
 *   prevented, so a stray drop never navigates the SPA away.
 * - Exactly one file per drop (curation is one paper at a time, ADR-006);
 *   more → the caller toasts.
 *
 * 🔴 The preventDefault half runs for EVERY file drag, including while
 * `accepting` is false (dialog already up, trash mode, upload in flight).
 * Skipping it is not "do nothing": a `dragover` that does not preventDefault
 * makes the window an illegal drop target, so no `drop` event fires and the
 * browser performs its own default — opening the dropped file in the tab, i.e.
 * replacing the whole app. Prevent always; decide what to *do* separately.
 *
 * During a drag the browser exposes item *types* but not contents, and some
 * platforms report an empty type — those still light the overlay; the drop
 * handler (extension/MIME) and the server's %PDF- magic check are the real
 * gate. The overlay is pointer-events-none: pure indication, never a target.
 */
export default function DropZone({
  accepting,
  busy,
  onPdf,
  onReject,
}: {
  /** Whether a dropped PDF should actually be picked up right now. */
  accepting: boolean
  /** An upload is in flight — show the reading state, take no new drop. */
  busy: boolean
  onPdf: (file: File) => void
  onReject: (message: string) => void
}) {
  const [active, setActive] = useState(false)
  const depth = useRef(0)

  useEffect(() => {
    const hasFiles = (dt: DataTransfer | null): boolean =>
      dt !== null && Array.from(dt.types).includes('Files')

    const couldBePdf = (dt: DataTransfer | null): boolean => {
      if (!dt) return false
      return Array.from(dt.items).some(
        (it) =>
          it.kind === 'file' &&
          (it.type === 'application/pdf' || it.type === ''),
      )
    }

    const onDragEnter = (e: DragEvent) => {
      if (!hasFiles(e.dataTransfer)) return
      e.preventDefault()
      depth.current += 1
      if (accepting && couldBePdf(e.dataTransfer)) setActive(true)
    }
    const onDragOver = (e: DragEvent) => {
      // preventDefault here is what makes the window a legal drop target —
      // and what stops the browser from navigating to the file (see above).
      if (!hasFiles(e.dataTransfer)) return
      e.preventDefault()
    }
    const onDragLeave = (e: DragEvent) => {
      if (!hasFiles(e.dataTransfer)) return
      depth.current = Math.max(0, depth.current - 1)
      if (depth.current === 0) setActive(false)
    }
    // Esc mid-drag / a drag that ends outside the window: the counter never
    // gets its matching dragleave, so reset rather than leave a stuck overlay.
    const onDragEnd = () => {
      depth.current = 0
      setActive(false)
    }
    const onDrop = (e: DragEvent) => {
      const dt = e.dataTransfer
      // Text/image drags pass through untouched (notes editor keeps them).
      if (!hasFiles(dt)) return
      e.preventDefault()
      depth.current = 0
      setActive(false)
      if (!accepting) return
      const files = Array.from(dt!.files)
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
    window.addEventListener('dragend', onDragEnd)
    window.addEventListener('drop', onDrop)
    return () => {
      window.removeEventListener('dragenter', onDragEnter)
      window.removeEventListener('dragover', onDragOver)
      window.removeEventListener('dragleave', onDragLeave)
      window.removeEventListener('dragend', onDragEnd)
      window.removeEventListener('drop', onDrop)
      depth.current = 0
      setActive(false)
    }
  }, [accepting, onPdf, onReject])

  // `busy` outlives the drag itself: the overlay stays up, now reading, so the
  // seconds between letting go of a fat PDF and the dialog are never silent.
  if (!active && !busy) return null
  return createPortal(
    <div className="pointer-events-none fixed inset-0 z-50 flex items-center justify-center bg-black/20 backdrop-blur-sm">
      <div className="animate-grow-in rounded-2xl bg-white px-8 py-6 text-center shadow-xl ring-1 ring-stone-200">
        <div className="text-sm font-semibold text-stone-900">
          {busy ? 'Reading PDF…' : 'Drop PDF to add'}
        </div>
        <div className="mt-1 text-xs text-stone-500">
          {busy ? 'Looking for a DOI' : 'One paper at a time'}
        </div>
      </div>
    </div>,
    document.body,
  )
}
