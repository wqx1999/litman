import { useEffect } from 'react'

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
}

/** Close-time confirmation for a PDF tab that has unsaved annotations.
 *
 * The WebUI flushes annotations into paper.pdf on tab close (invariant #16);
 * this dialog gives the close an explicit Save / Don't Save / Cancel choice
 * instead of a silent write, so the user is never surprised by an edited PDF
 * (or by lost edits). Escape cancels, Enter saves. */
export default function SaveDialog({
  label,
  onSave,
  onDiscard,
  onCancel,
  saving,
}: Props) {
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') onCancel()
      else if (e.key === 'Enter') onSave()
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [onCancel, onSave])

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/30 backdrop-blur-sm"
      onClick={saving ? undefined : onCancel}
    >
      <div
        onClick={(e) => e.stopPropagation()}
        className="w-[22rem] animate-grow-in rounded-2xl bg-white p-5 shadow-xl ring-1 ring-stone-200"
      >
        <h2 className="text-sm font-semibold text-stone-900">
          Save annotations?
        </h2>
        <p className="mt-1.5 text-xs leading-relaxed text-stone-500">
          <span className="font-medium text-stone-700">{label}</span> has
          unsaved annotations. Embed them into the PDF before closing?
        </p>
        <div className="mt-4 flex justify-end gap-2">
          <button
            onClick={onCancel}
            disabled={saving}
            className="rounded-lg px-3 py-1.5 text-xs text-stone-600 transition-colors hover:bg-stone-100 disabled:opacity-40"
          >
            Cancel
          </button>
          <button
            onClick={onDiscard}
            disabled={saving}
            className="rounded-lg px-3 py-1.5 text-xs text-red-600 transition-colors hover:bg-red-50 disabled:opacity-40"
          >
            Don't save
          </button>
          <button
            onClick={onSave}
            disabled={saving}
            className="rounded-lg bg-accent-500 px-3 py-1.5 text-xs font-medium text-white transition-colors hover:bg-accent-600 disabled:opacity-60"
          >
            {saving ? 'Saving…' : 'Save'}
          </button>
        </div>
      </div>
    </div>
  )
}
