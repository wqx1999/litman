import { useMemo, useState } from 'react'
import type { PaperMeta } from '../types'
import type { MetadataWrite } from '../api'
import AuthorRows from '../ui/AuthorRows'
import { useEscapeLayer } from '../ui/escapeStack'
import { isYearShape, YEAR_HINT } from '../ui/year'
import { modalBackdropProps } from '../ui/modalShell'

/** The bibliographic scalars the edit dialog exposes, in display order.
 * `status`/`type` are NOT here (they have dropdowns), nor are the
 * taxonomy dicts (register-first chip flow) — this dialog is for the fields
 * whose only prior edit path was the CLI. `id` is deliberately absent: an id
 * change is `lit rename`'s cascade (wikilinks, back-references), not a field
 * write, and the dialog says so instead of offering an input. */
const EDIT_SCALARS: ReadonlyArray<{
  key: string
  label: string
  wide: boolean
  /** Shown beside the label, always — the shape rule, not an error. */
  hint?: string
}> = [
  { key: 'title', label: 'Title', wide: true },
  { key: 'year', label: 'Year', wide: false, hint: YEAR_HINT },
  { key: 'journal', label: 'Journal', wide: true },
  { key: 'doi', label: 'DOI', wide: true },
  { key: 'volume', label: 'Volume', wide: false },
  { key: 'issue', label: 'Issue', wide: false },
  { key: 'pages', label: 'Pages', wide: false },
  { key: 'publisher', label: 'Publisher', wide: true },
  { key: 'venue-type', label: 'Venue type', wide: false },
  { key: 'booktitle', label: 'Book title', wide: true },
]

/** Edit dialog for bibliographic metadata (task-gui-metadata-edit).
 *
 * Scalars ride the existing `set` channel; the author list rides `setList`
 * (ordered wholesale rewrite — the op add/rm cannot express). One Save = one
 * PUT = one atomic backend transaction. The backend does ALL content
 * validation (year-must-be-int, DOI uniqueness, list shape); a rejection
 * surfaces its raw message inside the dialog and the dialog stays open with
 * the user's input intact — nothing is silently dropped or half-saved.
 *
 * Author rows reorder by drag handle or the ↑/↓ buttons (the buttons are also
 * what the E2E drives — HTML5 drag is unscriptable in practice). Blank author
 * rows are dropped on save; emptying the list entirely is the backend's call
 * to refuse, not ours. */
export default function MetadataEditDialog({
  paper,
  onSave,
  onClose,
}: {
  paper: PaperMeta
  onSave: (body: MetadataWrite) => Promise<void>
  onClose: () => void
}) {
  const original = useMemo(() => {
    const scalars: Record<string, string> = {}
    for (const f of EDIT_SCALARS) {
      const v = (paper as unknown as Record<string, unknown>)[f.key]
      scalars[f.key] = v == null ? '' : String(v)
    }
    return scalars
  }, [paper])
  const [scalars, setScalars] = useState<Record<string, string>>(original)
  const [authors, setAuthors] = useState<string[]>(
    paper.authors.length > 0 ? [...paper.authors] : [''],
  )
  const [error, setError] = useState<string | null>(null)
  const [saving, setSaving] = useState(false)

  async function doSave() {
    // Diff against the original so untouched fields are not sent at all: a
    // field the paper never had must not be created as null, and (belt to the
    // backend's skip_set_noop braces) an unchanged value must not count as an
    // edit. A cleared field IS sent, as null — that is the unset gesture.
    const set: Record<string, string | null> = {}
    for (const f of EDIT_SCALARS) {
      const next = scalars[f.key].trim()
      if (next === original[f.key].trim()) continue
      set[f.key] = next === '' ? null : next
    }
    // Only a year the user actually touched is judged. A paper already
    // holding `12311` (the backend has always taken any integer, and still
    // does — `lit modify` is the escape hatch) must not have its title edit
    // held hostage by a field nobody came here to change.
    if (typeof set.year === 'string' && !isYearShape(set.year)) {
      setError(`Year must be ${YEAR_HINT} — got '${set.year}'.`)
      return
    }

    const cleanedAuthors = authors.map((a) => a.trim()).filter(Boolean)
    const authorsChanged =
      cleanedAuthors.length !== paper.authors.length ||
      cleanedAuthors.some((a, i) => a !== paper.authors[i])

    const body: MetadataWrite = {}
    if (Object.keys(set).length > 0) body.set = set
    if (authorsChanged) body.setList = { authors: cleanedAuthors }
    if (!body.set && !body.setList) {
      onClose() // nothing changed — a save of nothing is a close
      return
    }
    setSaving(true)
    setError(null)
    try {
      await onSave(body)
      onClose()
    } catch (err) {
      // The backend's raw message (DOI collision, non-numeric year, emptied
      // author list). The dialog stays open, the input stays as typed.
      setError(err instanceof Error ? err.message : String(err))
    } finally {
      setSaving(false)
    }
  }

  useEscapeLayer(true, saving ? null : onClose)
  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/30 backdrop-blur-sm"
      {...modalBackdropProps}
    >
      <div
        onClick={(e) => e.stopPropagation()}
        className="flex max-h-[85vh] w-[30rem] animate-grow-in flex-col rounded-2xl bg-white p-5 shadow-xl ring-1 ring-stone-200"
      >
        <h2 className="text-sm font-semibold text-stone-900">Edit metadata</h2>
        <p className="mt-0.5 font-mono text-[11px] text-stone-400" title="The id is derived from year + first author + title keyword and is referenced by wikilinks. Changing it is `lit rename`, which updates every reference — not a field edit.">
          {paper.id} <span className="font-sans">· id is fixed — rename via CLI</span>
        </p>

        <div className="mt-3 grid min-h-0 grid-cols-2 gap-x-3 gap-y-2.5 overflow-y-auto pr-1">
          {EDIT_SCALARS.map((f, i) => (
            <label
              key={f.key}
              className={f.wide ? 'col-span-2 block' : 'block'}
            >
              <span className="mb-0.5 block text-[11px] font-semibold uppercase tracking-wider text-stone-600">
                {f.label}
                {f.hint && (
                  <span className="ml-1.5 font-normal normal-case tracking-normal text-stone-400">
                    {f.hint}
                  </span>
                )}
              </span>
              <input
                type="text"
                // Focus the first field on open: it is where editing starts,
                // and — now that a backdrop click no longer closes this dialog
                // — it is also what puts focus inside the card, without which
                // the Escape handler above never receives a key event.
                autoFocus={i === 0}
                value={scalars[f.key]}
                onChange={(e) =>
                  setScalars((prev) => ({ ...prev, [f.key]: e.target.value }))
                }
                disabled={saving}
                className="w-full rounded-md border border-stone-300 bg-white px-2 py-1 text-sm text-stone-800 focus:border-accent-500 focus:outline-none disabled:opacity-50"
              />
            </label>
          ))}

          <div className="col-span-2">
            <AuthorRows
              authors={authors}
              onChange={setAuthors}
              disabled={saving}
            />
          </div>
        </div>

        {error && (
          <div className="mt-3 rounded-md border border-red-300 bg-red-50 px-2.5 py-1.5 text-xs leading-relaxed text-red-700">
            {error}
          </div>
        )}

        <div className="mt-4 flex justify-end gap-2">
          <button
            onClick={onClose}
            disabled={saving}
            className="rounded-lg px-3 py-1.5 text-xs text-stone-600 transition-colors hover:bg-stone-100 disabled:opacity-40"
          >
            Cancel
          </button>
          <button
            onClick={doSave}
            disabled={saving}
            className="rounded-lg bg-accent-600 px-3 py-1.5 text-xs font-medium text-white shadow-sm transition-colors hover:bg-accent-700 disabled:opacity-40"
          >
            {saving ? 'Saving…' : 'Save'}
          </button>
        </div>
      </div>
    </div>
  )
}
