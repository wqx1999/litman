import { useEffect, useState } from 'react'
import { createPortal } from 'react-dom'
import { fetchRmPreview, type RmPreview } from '../api'
import { useT } from '../i18n'
import { rich } from '../i18n/rich'
import { useEscapeLayer } from '../ui/escapeStack'
import { modalBackdropProps } from '../ui/modalShell'

/** Default-No confirm for soft-deleting a paper from the tab's trash icon.
 *
 * Removing a paper is destructive-but-recoverable: it routes through the
 * `lit rm` backend (DELETE /api/paper/{id}), which moves the folder to
 * `.trash/` and atomically tears down its external links. So this dialog (a)
 * states it is recoverable from the in-app Trash, and (b) fetches the cascade
 * preview on mount and lists exactly which soft-links break — the user asked to
 * be warned before deleting "which symlinks will break". Cancel is autofocused
 * and the destructive button is rose; Esc cancels unless a delete is in flight
 * (a click outside only nudges the card — see ui/modalShell). macOS-style modal
 * shell shared with UnreadConfirm / DeleteProjectConfirm; portaled to document.body so `fixed inset-0` resolves
 * against the viewport, not the tab strip's backdrop-filter ancestor. */
export default function RemovePaperConfirm({
  paperId,
  busy,
  onCancel,
  onConfirm,
}: {
  paperId: string
  /** A delete is in flight — gates the buttons and Esc. */
  busy: boolean
  onCancel: () => void
  onConfirm: () => void
}) {
  const t = useT()
  const tx = t.reader.removePaper
  const [preview, setPreview] = useState<RmPreview | null>(null)
  const [error, setError] = useState<string | null>(null)

  // Mid-delete the layer stays registered with no handler: Escape must not
  // dismiss this dialog while the write is in flight, and must not fall through
  // to whatever is underneath either (ui/escapeStack).
  useEscapeLayer(true, busy ? null : onCancel)

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
    const {
      references,
      reposUnbound,
      reposRemoved,
      reposKept,
      reposEdited,
      reposUnchecked,
      projects,
      notes,
    } = preview
    if (references.length) lines.push(tx.references(references))
    if (reposRemoved.length) lines.push(tx.reposRemoved(reposRemoved))
    if (reposUnbound.length) lines.push(tx.reposUnbound(reposUnbound))
    if (reposKept.length) lines.push(tx.reposKept(reposKept))
    if (reposEdited.length) lines.push(tx.reposEdited(reposEdited))
    if (reposUnchecked.length) lines.push(tx.reposUnchecked(reposUnchecked))
    if (projects.length) lines.push(tx.projects(projects))
    if (notes.length) lines.push(tx.notes(notes.length))
  }

  return createPortal(
    <div
      className="fixed inset-0 z-[60] flex items-center justify-center bg-black/30 backdrop-blur-sm"
      {...modalBackdropProps}
    >
      <div
        onClick={(e) => e.stopPropagation()}
        role="dialog"
        aria-label={tx.dialogLabel}
        className="w-[26rem] max-w-[92vw] animate-grow-in rounded-2xl bg-white p-5 shadow-xl ring-1 ring-stone-200"
      >
        <h2 className="text-sm font-semibold text-stone-900">
          {tx.title(paperId)}
        </h2>
        <p className="mt-1.5 text-xs leading-relaxed text-stone-600">
          {rich(tx.body, {
            trash: <code className="text-stone-500">.trash/</code>,
          })}
        </p>

        {/* Cascade preview: which external links the delete breaks. */}
        <div className="mt-3 rounded-lg border border-stone-200 bg-stone-50 px-3 py-2.5">
          {error ? (
            <p className="text-xs text-rose-600">
              {tx.previewFailed(error)}
            </p>
          ) : !preview ? (
            <p className="text-xs text-stone-400">{tx.checking}</p>
          ) : lines.length === 0 ? (
            <p className="text-xs text-stone-500">
              {tx.nothingLinks}
            </p>
          ) : (
            <>
              <p className="mb-1 text-[11px] font-semibold uppercase tracking-wider text-stone-600">
                {tx.alsoBreaks}
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
            {t.common.cancel}
          </button>
          <button
            onClick={onConfirm}
            // Block until the preview resolves (or errors) so the user always
            // sees the impact before confirming; a failed preview leaves only
            // Cancel.
            disabled={busy || preview === null}
            className="rounded-lg bg-rose-500 px-3 py-1.5 text-xs font-medium text-white transition-colors hover:bg-rose-600 disabled:opacity-50"
          >
            {busy ? tx.removing : tx.remove}
          </button>
        </div>
      </div>
    </div>,
    document.body,
  )
}
