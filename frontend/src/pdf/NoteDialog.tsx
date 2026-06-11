import { useEffect, useRef, useState } from 'react'

interface Props {
  /** Existing note text ('' when adding a fresh note). */
  initialText: string
  /** Resolve the note: the new text, '' to delete, or undefined on cancel. */
  onResolve: (value: string | undefined) => void
}

/** Add / edit / delete the note attached to a highlight or drawing.
 *
 * pdf.js carries the note in the annotation's `contents` and embeds it into the
 * PDF on save (invariant #16); this modal is just the text-capture UI. A
 * non-empty initial text means we're editing an existing note (offer Delete);
 * empty means adding. Escape cancels, Cmd/Ctrl+Enter saves. */
export default function NoteDialog({ initialText, onResolve }: Props) {
  const [text, setText] = useState(initialText)
  const taRef = useRef<HTMLTextAreaElement>(null)
  const editing = initialText.trim() !== ''

  useEffect(() => {
    // Focus + caret-to-end so editing an existing note is immediately typeable.
    const ta = taRef.current
    if (ta) {
      ta.focus()
      ta.setSelectionRange(ta.value.length, ta.value.length)
    }
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') onResolve(undefined)
      else if (e.key === 'Enter' && (e.metaKey || e.ctrlKey)) onResolve(text)
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [onResolve, text])

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/30 backdrop-blur-sm"
      onClick={() => onResolve(undefined)}
    >
      <div
        onClick={(e) => e.stopPropagation()}
        className="w-[24rem] animate-grow-in rounded-2xl bg-white p-5 shadow-xl ring-1 ring-stone-200"
      >
        <h2 className="text-sm font-semibold text-stone-900">
          {editing ? 'Edit note' : 'Add note'}
        </h2>
        <textarea
          ref={taRef}
          value={text}
          onChange={(e) => setText(e.target.value)}
          rows={4}
          placeholder="Type a note for this annotation…"
          className="mt-3 w-full resize-none rounded-lg border border-stone-200 bg-stone-50 p-2.5 text-sm text-stone-800 placeholder:text-stone-400 focus:border-accent-400 focus:bg-white focus:outline-none focus:ring-1 focus:ring-accent-400"
        />
        <div className="mt-1 text-right text-[10px] text-stone-400">
          ⌘/Ctrl+Enter to save · Esc to cancel
        </div>
        <div className="mt-3 flex justify-end gap-2">
          <button
            onClick={() => onResolve(undefined)}
            className="rounded-lg px-3 py-1.5 text-xs text-stone-600 transition-colors hover:bg-stone-100"
          >
            Cancel
          </button>
          {editing && (
            <button
              onClick={() => onResolve('')}
              className="rounded-lg px-3 py-1.5 text-xs text-red-600 transition-colors hover:bg-red-50"
            >
              Delete note
            </button>
          )}
          <button
            onClick={() => onResolve(text)}
            className="rounded-lg bg-accent-500 px-3 py-1.5 text-xs font-medium text-white transition-colors hover:bg-accent-600"
          >
            {editing ? 'Save' : 'Add'}
          </button>
        </div>
      </div>
    </div>
  )
}
