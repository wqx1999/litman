import { useEffect } from 'react'
import { useT } from '../i18n'
import { rich } from '../i18n/rich'
import { useEscapeLayer } from '../ui/escapeStack'
import { modalBackdropProps } from '../ui/modalShell'

interface Props {
  /** Human label of the tab being closed (shown in the prompt body). */
  label: string
  /** Embed the pending annotations into the PDF, then close the tab. */
  onSave: () => void
  /** Close the tab and drop the unsaved annotations. */
  onDiscard: () => void
  /** Keep the tab open. */
  onCancel: () => void
  /** True while the save (saveDocument + PUT) is in flight. */
  saving: boolean
  /** Which tab is closing: a PDF (unsaved annotations, the default) or an md
   * tab (unsaved edits). Picks the heading and the sentence. */
  kind?: 'pdf' | 'md'
}

/** Close-time confirmation for a tab with unsaved changes.
 *
 * Shared by the PDF tab (annotations embed into paper.pdf on close, invariant
 * #16) and the md tabs (notes/discussion edits). It gives the close an explicit
 * Save / Don't Save / Cancel choice instead of a silent write/loss, so the user
 * is never surprised by an edited file or by dropped edits. The wording follows
 * `kind` (defaults to the PDF copy) so one component covers both kinds.
 * Escape cancels, Enter saves. */
export default function SaveDialog({
  label,
  onSave,
  onDiscard,
  onCancel,
  saving,
  kind = 'pdf',
}: Props) {
  const t = useT()
  const tx = t.reader.saveDialog
  useEscapeLayer(true, onCancel)
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Enter') onSave()
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [onSave])

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/30 backdrop-blur-sm"
      {...modalBackdropProps}
    >
      <div
        onClick={(e) => e.stopPropagation()}
        className="w-[22rem] animate-grow-in rounded-2xl bg-white p-5 shadow-xl ring-1 ring-stone-200"
      >
        <h2 className="text-sm font-semibold text-stone-900">{tx[kind].title}</h2>
        <p className="mt-1.5 text-xs leading-relaxed text-stone-500">
          {rich(tx[kind].body, {
            label: <span className="font-medium text-stone-700">{label}</span>,
          })}
        </p>
        <div className="mt-4 flex justify-end gap-2">
          <button
            onClick={onCancel}
            disabled={saving}
            className="rounded-lg px-3 py-1.5 text-xs text-stone-600 transition-colors hover:bg-stone-100 disabled:opacity-40"
          >
            {t.common.cancel}
          </button>
          <button
            onClick={onDiscard}
            disabled={saving}
            className="rounded-lg px-3 py-1.5 text-xs text-red-600 transition-colors hover:bg-red-50 disabled:opacity-40"
          >
            {tx.discard}
          </button>
          <button
            onClick={onSave}
            disabled={saving}
            className="rounded-lg bg-accent-500 px-3 py-1.5 text-xs font-medium text-white transition-colors hover:bg-accent-600 disabled:opacity-60"
          >
            {saving ? tx.saving : tx.save}
          </button>
        </div>
      </div>
    </div>
  )
}
