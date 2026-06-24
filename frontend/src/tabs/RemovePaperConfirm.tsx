import { useEffect, useState } from 'react'
import { createPortal } from 'react-dom'
import { fetchRmPreview, type RmPreview } from '../api'

/** Default-No confirm for soft-deleting a paper from the tab's trash icon.
 *
 * Removing a paper is destructive-but-recoverable: it routes through the
 * `lit rm` backend (DELETE /api/paper/{id}), which moves the folder to
 * `.trash/` and atomically tears down its external links. So this dialog (a)
 * states it is recoverable via `lit trash restore`, and (b) fetches the cascade
 * preview on mount and lists exactly which soft-links break — the user asked to
 * be warned before deleting "which symlinks will break". Cancel is autofocused
 * and the destructive button is rose; the backdrop / Esc cancel unless a delete
 * is in flight. macOS-style modal shell shared with UnreadConfirm /
 * DeleteProjectConfirm; portaled to document.body so `fixed inset-0` resolves
 * against the viewport, not the tab strip's backdrop-filter ancestor. */
export default function RemovePaperConfirm({
  paperId,
  busy,
  onCancel,
  onConfirm,
}: {
  paperId: string
  /** A delete is in flight — gates the buttons + backdrop dismiss. */
  busy: boolean
  onCancel: () => void
  onConfirm: () => void
}) {
  const [preview, setPreview] = useState<RmPreview | null>(null)
  const [error, setError] = useState<string | null>(null)

  // Fetch the cascade on mount (and whenever the target changes). The `alive`
  // guard drops a late response if the dialog closed first.
  useEffect(() => {
    let alive = true
    setPreview(null)
    setError(null)
    fetchRmPreview(paperId)
      .then((p) => alive && setPreview(p))
      .catch((e) => alive && setError(e instanceof Error ? e.message : String(e)))
    return () => {
      alive = false
    }
  }, [paperId])

  // One human line per kind of link that would break. Empty when nothing else
  // references the paper.
  const lines: string[] = []
  if (preview) {
    const { references, reposUnbound, reposRemoved, projects, notes } = preview
    if (references.length)
      lines.push(
        `Clears it from ${count(references.length, 'paper')}: ${references.join(', ')}`,
      )
    if (reposRemoved.length)
      lines.push(
        `Deletes ${count(reposRemoved.length, 'cloned repo')} no other paper uses: ${reposRemoved.join(', ')}`,
      )
    if (reposUnbound.length)
      lines.push(
        `Unbinds ${count(reposUnbound.length, 'repo')} (kept — still used elsewhere): ${reposUnbound.join(', ')}`,
      )
    if (projects.length)
      lines.push(`Unlinks from ${count(projects.length, 'project')}: ${projects.join(', ')}`)
    if (notes.length)
      lines.push(`Tags ${count(notes.length, 'referencing note')} with “(deleted)”`)
  }

  return createPortal(
    <div
      className="fixed inset-0 z-[60] flex items-center justify-center bg-black/30 backdrop-blur-sm"
      onClick={busy ? undefined : onCancel}
    >
      <div
        onClick={(e) => e.stopPropagation()}
        onKeyDown={(e) => {
          if (e.key === 'Escape' && !busy) onCancel()
        }}
        role="dialog"
        aria-label="Remove paper from library"
        className="w-[26rem] max-w-[92vw] animate-grow-in rounded-2xl bg-white p-5 shadow-xl ring-1 ring-stone-200"
      >
        <h2 className="text-sm font-semibold text-stone-900">
          Remove “{paperId}” from the library?
        </h2>
        <p className="mt-1.5 text-xs leading-relaxed text-stone-600">
          Moves the paper to <code className="text-stone-500">.trash/</code> and
          drops it from INDEX and every smart list. Recoverable with{' '}
          <code className="text-stone-500">lit trash restore {paperId}</code> in
          the CLI.
        </p>

        {/* Cascade preview: which external links the delete breaks. */}
        <div className="mt-3 rounded-lg border border-stone-200 bg-stone-50 px-3 py-2.5">
          {error ? (
            <p className="text-xs text-rose-600">
              Could not load the impact preview: {error}
            </p>
          ) : !preview ? (
            <p className="text-xs text-stone-400">Checking what this affects…</p>
          ) : lines.length === 0 ? (
            <p className="text-xs text-stone-500">
              Nothing else links to this paper.
            </p>
          ) : (
            <>
              <p className="mb-1 text-[11px] font-semibold uppercase tracking-wider text-stone-500">
                This also breaks
              </p>
              <ul className="space-y-1">
                {lines.map((line, i) => (
                  <li
                    key={i}
                    className="flex gap-1.5 text-xs leading-snug text-stone-700"
                  >
                    <span className="text-stone-400">•</span>
                    <span className="min-w-0 break-words">{line}</span>
                  </li>
                ))}
              </ul>
            </>
          )}
        </div>

        <div className="mt-5 flex justify-end gap-2">
          <button
            autoFocus
            onClick={onCancel}
            disabled={busy}
            className="rounded-lg px-3 py-1.5 text-xs text-stone-600 transition-colors hover:bg-stone-100 disabled:opacity-40"
          >
            Cancel
          </button>
          <button
            onClick={onConfirm}
            // Block until the preview resolves (or errors) so the user always
            // sees the impact before confirming; a failed preview leaves only
            // Cancel.
            disabled={busy || preview === null}
            className="rounded-lg bg-rose-500 px-3 py-1.5 text-xs font-medium text-white transition-colors hover:bg-rose-600 disabled:opacity-50"
          >
            {busy ? 'Removing…' : 'Remove to trash'}
          </button>
        </div>
      </div>
    </div>,
    document.body,
  )
}

/** "1 paper" / "3 papers" — singular/plural noun with its count. */
function count(n: number, noun: string): string {
  return `${n} ${noun}${n === 1 ? '' : 's'}`
}
