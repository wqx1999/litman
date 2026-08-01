import { useCallback, useEffect, useRef, useState } from 'react'
import { createPortal } from 'react-dom'
import {
  confirmIngest,
  fetchIngestPreview,
  type IngestPreview,
  type IngestUploadResult,
} from '../api'

/** Confirm dialog for the drag-in ingest: DOI (sniffed, editable) → CrossRef
 * preview → Add.
 *
 * One flow, no forks: the DOI field is always there — pre-filled when the
 * sniff found one (looked up immediately), empty for scanned/DOI-less PDFs.
 * Editing the DOI invalidates the preview; Add only ever submits a DOI the
 * user has seen resolved. A DOI already in the vault shows a jump-to link and
 * keeps Add disabled (replacing is a CLI affair). Ids are shown, never edited
 * (collisions auto-suffix server-side; renames belong to `lit rename`).
 *
 * This is a BLOCKING modal (it owns a text input), so unlike the CheatSheet /
 * What's-new overlays it joins App's `anyModalOpen` — the global shortcut
 * dispatcher goes quiet and Esc is handled here, on the card itself. Initial
 * focus always lands inside the card (the DOI input), so the card-level
 * key handler is reachable from the first keystroke. */
export default function AddPaper({
  upload,
  fileName,
  onClose,
  onAdded,
  onOpenExisting,
}: {
  upload: IngestUploadResult
  fileName: string
  onClose: () => void
  onAdded: (id: string) => void
  onOpenExisting: (id: string) => void
}) {
  const [doi, setDoi] = useState(upload.doi ?? '')
  const [preview, setPreview] = useState<IngestPreview | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState<'lookup' | 'add' | null>(null)
  const inputRef = useRef<HTMLInputElement>(null)

  const lookup = useCallback((value: string) => {
    const trimmed = value.trim()
    if (!trimmed) return
    setBusy('lookup')
    setError(null)
    setPreview(null)
    fetchIngestPreview(trimmed)
      .then(setPreview)
      .catch((e) => setError(e instanceof Error ? e.message : String(e)))
      .finally(() => setBusy(null))
  }, [])

  // Focus ALWAYS lands in the DOI field — it is both the first thing to
  // correct and what makes the card's own Esc handler reachable (a key event
  // outside the card never reaches it, and this modal is excluded from the
  // global dispatcher because it owns a text input). A sniffed DOI is looked
  // up right away on top of that, so the common case needs zero extra clicks.
  useEffect(() => {
    inputRef.current?.focus()
    if (upload.doi) lookup(upload.doi)
  }, [upload, lookup])

  const canAdd =
    busy === null &&
    preview !== null &&
    preview.inVault === null &&
    preview.proposedId !== null

  const add = () => {
    if (!canAdd || preview === null) return
    setBusy('add')
    setError(null)
    confirmIngest(upload.handle, preview.doi)
      .then((r) => onAdded(r.id))
      .catch((e) => {
        setError(e instanceof Error ? e.message : String(e))
        setBusy(null)
      })
  }

  const authorLine = (authors: string[]): string => {
    if (authors.length === 0) return '(no authors)'
    if (authors.length <= 3) return authors.join(' · ')
    return `${authors[0]} et al. (${authors.length} authors)`
  }

  return createPortal(
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/30 backdrop-blur-sm"
      onClick={onClose}
    >
      <div
        onClick={(e) => e.stopPropagation()}
        onKeyDown={(e) => {
          if (e.key === 'Escape') onClose()
        }}
        role="dialog"
        aria-label="Add paper"
        className="max-h-[85vh] w-[28rem] max-w-[94vw] animate-grow-in overflow-y-auto rounded-2xl bg-white p-6 shadow-xl ring-1 ring-stone-200"
      >
        <div className="text-[11px] font-semibold uppercase tracking-wider text-accent-600">
          Add paper
        </div>
        <h2
          className="mt-0.5 truncate text-sm font-semibold text-stone-900"
          title={fileName}
        >
          {fileName}
        </h2>

        <div className="mt-3 flex items-center gap-2">
          <input
            ref={inputRef}
            value={doi}
            onChange={(e) => {
              setDoi(e.target.value)
              // A changed DOI invalidates the resolved preview: Add must
              // never submit a DOI the user hasn't seen looked up.
              setPreview(null)
              setError(null)
            }}
            onKeyDown={(e) => {
              if (e.key === 'Enter') {
                e.preventDefault()
                lookup(doi)
              }
            }}
            placeholder="DOI (e.g. 10.1093/nar/gkab813)"
            spellCheck={false}
            className="min-w-0 flex-1 rounded-lg bg-stone-50 px-2.5 py-1.5 font-mono text-xs text-stone-900 ring-1 ring-stone-200 transition duration-200 ease-fluid placeholder:text-stone-400 focus:outline-none focus:ring-accent-500"
          />
          <button
            type="button"
            onClick={() => lookup(doi)}
            disabled={busy !== null || !doi.trim() || preview !== null}
            className="rounded-lg px-2.5 py-1.5 text-xs font-medium text-accent-600 ring-1 ring-stone-200 transition duration-200 ease-fluid hover:bg-stone-50 disabled:opacity-40 disabled:hover:bg-transparent"
          >
            Look up
          </button>
        </div>

        {busy === 'lookup' && (
          <p className="mt-3 text-xs leading-5 text-stone-400">Looking up…</p>
        )}

        {error && (
          <p className="mt-3 whitespace-pre-wrap text-xs leading-5 text-red-600">
            {error}
          </p>
        )}

        {preview && (
          <div className="mt-3 rounded-lg bg-stone-50 p-3 ring-1 ring-stone-200">
            <div className="text-sm font-medium leading-5 text-stone-900">
              {preview.title || '(no title)'}
            </div>
            <div className="mt-1 text-xs leading-5 text-stone-500">
              {authorLine(preview.authors)}
            </div>
            <div className="mt-0.5 text-xs text-stone-500">
              {[preview.year, preview.journal].filter(Boolean).join(' · ')}
            </div>
            {preview.inVault ? (
              <div className="mt-2 text-xs leading-5 text-stone-600">
                Already in your library:{' '}
                <button
                  type="button"
                  onClick={() => onOpenExisting(preview.inVault!.id)}
                  className="font-mono font-medium text-accent-600 transition duration-200 ease-fluid hover:text-accent-700"
                >
                  {preview.inVault.id}
                </button>
              </div>
            ) : preview.proposedId ? (
              <div className="mt-2 text-[11px] text-stone-400">
                Saved as{' '}
                <span className="font-mono text-stone-500">
                  {preview.proposedId}
                </span>
              </div>
            ) : (
              <p className="mt-2 text-xs leading-5 text-stone-500">
                {preview.idError}
              </p>
            )}
          </div>
        )}

        <div className="mt-4 flex items-center justify-end gap-2">
          <button
            type="button"
            onClick={onClose}
            className="rounded-lg px-3 py-1.5 text-sm font-medium text-stone-500 transition duration-200 ease-fluid hover:bg-stone-100 hover:text-stone-700"
          >
            Cancel
          </button>
          <button
            type="button"
            onClick={add}
            disabled={!canAdd}
            className="rounded-lg bg-accent-500 px-3 py-1.5 text-sm font-medium text-white transition duration-200 ease-fluid hover:bg-accent-600 disabled:opacity-40 disabled:hover:bg-accent-500"
          >
            {busy === 'add' ? 'Adding…' : 'Add to library'}
          </button>
        </div>
      </div>
    </div>,
    document.body,
  )
}
