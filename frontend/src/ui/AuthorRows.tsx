import { useState } from 'react'

/** The ordered author-list editor, shared by the metadata edit dialog and the
 * hand-entry half of Add paper.
 *
 * One editor rather than two because author ORDER is load-bearing in a way
 * that invites divergence otherwise: the first author's family name becomes
 * the paper id, so a second implementation that quietly sorted, trimmed
 * differently, or dropped blanks at a different moment would produce a
 * different id for the same typing.
 *
 * Rows reorder by drag handle or by the ↑/↓ buttons. The buttons are not a
 * fallback — HTML5 drag is unscriptable in practice, so they are what the E2E
 * drives, which makes their aria-labels part of the contract.
 *
 * Blank rows are left alone here; each caller decides when to drop them
 * (on save, on submit) so neither one loses a row the user is mid-typing. */
export default function AuthorRows({
  authors,
  onChange,
  disabled = false,
}: {
  authors: string[]
  onChange: (next: string[]) => void
  disabled?: boolean
}) {
  // Index of the row being dragged, or null. Reorder happens live on dragover
  // (the sortable-list idiom), so drop needs no handler of its own.
  const [dragIdx, setDragIdx] = useState<number | null>(null)

  function move(from: number, to: number) {
    if (to < 0 || to >= authors.length) return
    const next = [...authors]
    const [row] = next.splice(from, 1)
    next.splice(to, 0, row)
    onChange(next)
  }

  return (
    <div>
      <span className="mb-0.5 block text-[11px] font-semibold uppercase tracking-wider text-stone-500">
        Authors{' '}
        <span className="normal-case tracking-normal text-stone-400">
          — order matters: the first author names the paper
        </span>
      </span>
      <div className="space-y-1.5">
        {authors.map((name, i) => (
          <div
            key={i}
            onDragOver={(e) => {
              e.preventDefault()
              if (dragIdx === null || dragIdx === i) return
              move(dragIdx, i)
              setDragIdx(i)
            }}
            className={`flex items-center gap-1.5 ${dragIdx === i ? 'opacity-60' : ''}`}
          >
            <span
              draggable={!disabled}
              onDragStart={() => setDragIdx(i)}
              onDragEnd={() => setDragIdx(null)}
              title="Drag to reorder"
              className="cursor-grab select-none px-0.5 text-stone-400 hover:text-stone-600 active:cursor-grabbing"
            >
              ⋮⋮
            </span>
            <input
              type="text"
              value={name}
              placeholder="Family, Given"
              onChange={(e) =>
                onChange(authors.map((a, j) => (j === i ? e.target.value : a)))
              }
              disabled={disabled}
              className="min-w-0 flex-1 rounded-md border border-stone-300 bg-white px-2 py-1 text-sm text-stone-800 focus:border-accent-500 focus:outline-none disabled:opacity-50"
            />
            <button
              type="button"
              onClick={() => move(i, i - 1)}
              disabled={disabled || i === 0}
              title="Move up"
              aria-label={`Move author ${i + 1} up`}
              className="rounded px-1 text-xs text-stone-400 transition-colors hover:bg-stone-100 hover:text-stone-700 disabled:opacity-30"
            >
              ↑
            </button>
            <button
              type="button"
              onClick={() => move(i, i + 1)}
              disabled={disabled || i === authors.length - 1}
              title="Move down"
              aria-label={`Move author ${i + 1} down`}
              className="rounded px-1 text-xs text-stone-400 transition-colors hover:bg-stone-100 hover:text-stone-700 disabled:opacity-30"
            >
              ↓
            </button>
            <button
              type="button"
              onClick={() => onChange(authors.filter((_, j) => j !== i))}
              disabled={disabled}
              title="Remove this author"
              aria-label={`Remove author ${i + 1}`}
              className="rounded px-1 text-sm leading-none text-stone-400 transition-colors hover:bg-red-50 hover:text-red-600 disabled:opacity-30"
            >
              ×
            </button>
          </div>
        ))}
      </div>
      <button
        type="button"
        onClick={() => onChange([...authors, ''])}
        disabled={disabled}
        className="mt-1.5 text-xs font-medium text-accent-600 transition-colors hover:underline disabled:opacity-40"
      >
        + Add author
      </button>
    </div>
  )
}
