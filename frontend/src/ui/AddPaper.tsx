import { useCallback, useEffect, useRef, useState } from 'react'
import { createPortal } from 'react-dom'
import {
  confirmIngest,
  confirmIngestManual,
  fetchIngestPreview,
  type IngestPreview,
  type IngestUploadResult,
} from '../api'
import AuthorRows from './AuthorRows'
import { modalBackdropProps } from './modalShell'

/** Confirm dialog for the drag-in ingest: DOI (sniffed, editable) → CrossRef
 * preview → Add, with a hand-entry path for the papers CrossRef lacks.
 *
 * The DOI field is always there — pre-filled when the sniff found one (looked
 * up immediately), empty for scanned/DOI-less PDFs. Editing the DOI
 * invalidates the preview; Add only ever submits a DOI the user has seen
 * resolved. A DOI already in the vault shows a jump-to link and keeps Add
 * disabled (replacing is a CLI affair). Ids are shown, never edited
 * (collisions auto-suffix server-side; renames belong to `lit rename`).
 *
 * The second path exists because "CrossRef does not have it" is a common,
 * permanent state, not a failure to retry: a patent carries no DOI at all,
 * and a Chinese journal article usually carries a real one registered with
 * CNKI, which CrossRef will never resolve. Both used to be simply un-addable
 * by dragging. The hand-entry form therefore offers itself whenever there is
 * no confirmed record — including before any lookup, since a patent's owner
 * has nothing to look up and would otherwise never see the way in.
 *
 * Six fields, not the eleven the metadata editor shows: the three that reach
 * the paper id (title / year / first author) plus the three that cannot be
 * inferred later (journal, DOI, venue type — the last is what makes a patent
 * export as `@patent`). Everything else is one pencil click away once the
 * paper is in, and a drag-and-drop dialog that scrolls is a worse trade.
 *
 * This is a BLOCKING modal (it owns a text input), so unlike the CheatSheet /
 * What's-new overlays it joins App's `anyModalOpen` — the global shortcut
 * dispatcher goes quiet and Esc is handled here, on the card itself. Initial
 * focus always lands inside the card (the DOI input), so the card-level
 * key handler is reachable from the first keystroke. */

/** CrossRef's own vocabulary plus `patent`, which CrossRef has no type for.
 * These are the values `lit export` maps to bibtex entry types
 * (exporters/bibtex.py `_VENUE_TYPE_TO_ENTRY`); anything else exports as
 * `@misc`, so the list is closed rather than free text. */
const VENUE_TYPES = [
  ['journal-article', 'Journal article'],
  ['proceedings-article', 'Conference paper'],
  ['preprint', 'Preprint'],
  ['patent', 'Patent'],
  ['book', 'Book'],
  ['book-chapter', 'Book chapter'],
  ['dissertation', 'Thesis'],
  ['report', 'Report'],
] as const
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

  // Hand-entry state. `manual` opens the form; the fields below only ever
  // reach the server through the metadata path, so nothing here can leak into
  // a DOI-based add.
  const [manual, setManual] = useState(false)
  const [mTitle, setMTitle] = useState('')
  const [mYear, setMYear] = useState('')
  const [mJournal, setMJournal] = useState('')
  const [mDoi, setMDoi] = useState('')
  const [mVenue, setMVenue] = useState<string>('journal-article')
  const [mAuthors, setMAuthors] = useState<string[]>([''])

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

  // Carry whatever the user already typed into the form rather than making
  // them retype it. A DOI CrossRef could not resolve is usually still the
  // paper's real DOI (CNKI-registered, say), so it belongs in the record.
  const openManual = () => {
    setMDoi(doi.trim())
    setError(null)
    setManual(true)
  }

  // Focus ALWAYS lands in the DOI field — it is both the first thing to
  // correct and what makes the card's own Esc handler reachable (a key event
  // outside the card never reaches it, and this modal is excluded from the
  // global dispatcher because it owns a text input). A sniffed DOI is looked
  // up right away on top of that, so the common case needs zero extra clicks.
  useEffect(() => {
    inputRef.current?.focus()
    if (upload.doi) lookup(upload.doi)
  }, [upload, lookup])

  // Only the three fields that reach the paper id are required, and only in
  // the shape the id needs: a year the server can parse, a first author with
  // something in it. Everything else the schema will judge on submit — the
  // button's job is to stop a request that cannot possibly succeed, not to
  // duplicate the validator.
  const manualReady =
    mTitle.trim() !== '' &&
    /^\d{3,4}$/.test(mYear.trim()) &&
    (mAuthors[0] ?? '').trim() !== ''

  const canAdd =
    busy === null &&
    (manual
      ? manualReady
      : preview !== null &&
        preview.inVault === null &&
        preview.proposedId !== null)

  const add = () => {
    if (!canAdd) return
    setBusy('add')
    setError(null)
    const request = manual
      ? confirmIngestManual(upload.handle, {
          title: mTitle.trim(),
          // Blank rows are dropped here rather than in the editor, so a row
          // someone is halfway through typing never vanishes under them.
          authors: mAuthors.map((a) => a.trim()).filter(Boolean),
          year: Number(mYear.trim()),
          journal: mJournal.trim() || null,
          doi: mDoi.trim() || null,
          'venue-type': mVenue || null,
        })
      : confirmIngest(upload.handle, preview!.doi)
    request
      .then((r) => onAdded(r.id))
      .catch((e) => {
        setError(e instanceof Error ? e.message : String(e))
        setBusy(null)
      })
  }

  // Closing the dialog throws the stashed upload away (App fires the DELETE),
  // so it must not be reachable while the ingest is mid-flight: the server
  // would be copying the very file the dismissal deletes. Everything else —
  // Esc and Cancel — the only two exits — route through here. A click outside
  // does NOT: it would delete the upload the person just waited for.
  const dismiss = () => {
    if (busy !== 'add') onClose()
  }

  const authorLine = (authors: string[]): string => {
    if (authors.length === 0) return '(no authors)'
    if (authors.length <= 3) return authors.join(' · ')
    return `${authors[0]} et al. (${authors.length} authors)`
  }

  return createPortal(
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/30 backdrop-blur-sm"
      {...modalBackdropProps}
    >
      <div
        onClick={(e) => e.stopPropagation()}
        onKeyDown={(e) => {
          if (e.key === 'Escape') dismiss()
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

        {/* Hidden in hand-entry mode: the DOI moved into the form, and two
            DOI boxes on one card is a question nobody should have to answer. */}
        <div className={`mt-3 flex items-center gap-2 ${manual ? 'hidden' : ''}`}>
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

        {/* Offered whenever there is no confirmed record — not only after a
            failed lookup. A patent has no DOI to look up, so gating this on
            an error would hide the only way in from the person who needs it
            most. Hidden once a preview resolves: a paper CrossRef knows
            should not tempt anyone into retyping it. */}
        {!manual && preview === null && busy !== 'lookup' && (
          <button
            type="button"
            onClick={openManual}
            className="mt-3 text-xs font-medium text-accent-600 transition-colors hover:underline"
          >
            No DOI, or CrossRef doesn't have it? Enter the details yourself →
          </button>
        )}

        {manual && (
          <div className="mt-3 space-y-2.5 border-t border-stone-200 pt-3">
            <div className="flex justify-end">
              <button
                type="button"
                onClick={() => setManual(false)}
                disabled={busy === 'add'}
                className="text-[11px] font-medium text-stone-400 transition-colors hover:text-accent-600 disabled:opacity-40"
              >
                ← Look up a DOI instead
              </button>
            </div>
            <label className="block">
              <span className="mb-0.5 block text-[11px] font-semibold uppercase tracking-wider text-stone-500">
                Title <span className="text-red-500">*</span>
              </span>
              <input
                type="text"
                autoFocus
                value={mTitle}
                onChange={(e) => setMTitle(e.target.value)}
                disabled={busy === 'add'}
                className="w-full rounded-md border border-stone-300 bg-white px-2 py-1 text-sm text-stone-800 focus:border-accent-500 focus:outline-none disabled:opacity-50"
              />
            </label>

            <div className="grid grid-cols-3 gap-2.5">
              <label className="block">
                <span className="mb-0.5 block text-[11px] font-semibold uppercase tracking-wider text-stone-500">
                  Year <span className="text-red-500">*</span>
                </span>
                <input
                  type="text"
                  inputMode="numeric"
                  value={mYear}
                  onChange={(e) => setMYear(e.target.value)}
                  disabled={busy === 'add'}
                  className="w-full rounded-md border border-stone-300 bg-white px-2 py-1 text-sm text-stone-800 focus:border-accent-500 focus:outline-none disabled:opacity-50"
                />
              </label>
              <label className="col-span-2 block">
                <span className="mb-0.5 block text-[11px] font-semibold uppercase tracking-wider text-stone-500">
                  Journal / venue
                </span>
                <input
                  type="text"
                  value={mJournal}
                  onChange={(e) => setMJournal(e.target.value)}
                  disabled={busy === 'add'}
                  className="w-full rounded-md border border-stone-300 bg-white px-2 py-1 text-sm text-stone-800 focus:border-accent-500 focus:outline-none disabled:opacity-50"
                />
              </label>
            </div>

            <div className="grid grid-cols-2 gap-2.5">
              <label className="block">
                <span className="mb-0.5 block text-[11px] font-semibold uppercase tracking-wider text-stone-500">
                  DOI
                </span>
                <input
                  type="text"
                  value={mDoi}
                  onChange={(e) => setMDoi(e.target.value)}
                  spellCheck={false}
                  placeholder="if it has one"
                  disabled={busy === 'add'}
                  className="w-full rounded-md border border-stone-300 bg-white px-2 py-1 font-mono text-xs text-stone-800 placeholder:font-sans placeholder:text-stone-400 focus:border-accent-500 focus:outline-none disabled:opacity-50"
                />
              </label>
              <label className="block">
                <span className="mb-0.5 block text-[11px] font-semibold uppercase tracking-wider text-stone-500">
                  Type
                </span>
                <select
                  value={mVenue}
                  onChange={(e) => setMVenue(e.target.value)}
                  disabled={busy === 'add'}
                  className="w-full rounded-md border border-stone-300 bg-white px-2 py-1 text-sm text-stone-800 focus:border-accent-500 focus:outline-none disabled:opacity-50"
                >
                  {VENUE_TYPES.map(([value, label]) => (
                    <option key={value} value={value}>
                      {label}
                    </option>
                  ))}
                </select>
              </label>
            </div>

            <AuthorRows
              authors={mAuthors}
              onChange={setMAuthors}
              disabled={busy === 'add'}
            />
            <p className="text-[11px] leading-4 text-stone-400">
              Volume, pages and the rest are editable once it's in.
            </p>
          </div>
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
            onClick={dismiss}
            disabled={busy === 'add'}
            className="rounded-lg px-3 py-1.5 text-sm font-medium text-stone-500 transition duration-200 ease-fluid hover:bg-stone-100 hover:text-stone-700 disabled:opacity-40 disabled:hover:bg-transparent"
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
