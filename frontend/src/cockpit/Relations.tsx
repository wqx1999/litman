import { useState } from 'react'
import type { PaperMeta } from '../types'
import { useEscapeLayer } from '../ui/escapeStack'
import { modalBackdropProps } from '../ui/modalShell'

/** The five relation fields the cockpit renders, in display order. */
export type RelField =
  | 'related'
  | 'extends'
  | 'extended-by'
  | 'contradicts'
  | 'contradicted-by'

const REL_FIELDS: RelField[] = [
  'related',
  'extends',
  'extended-by',
  'contradicts',
  'contradicted-by',
]

/** Every linked paper across the five fields — the number on the Relations label. */
export function relationCount(paper: PaperMeta): number {
  return REL_FIELDS.reduce((n, rel) => n + (paper[rel]?.length ?? 0), 0)
}

/** Where a removal on a REVERSE row has to be sent. The backend refuses a write
 * that names a reverse field (ADR-012: those edges exist only as the mirror of
 * a forward write), so removing `extended-by: A` while standing on B means
 * writing A's `extends` with B's id — one request, both sides cleared in the
 * same transaction. `related` is symmetric and needs no flip. */
export const REVERSE_TO_FORWARD = {
  'extended-by': 'extends',
  'contradicted-by': 'contradicts',
} as const

/** Links a relation group lists before folding the rest behind "+ N more". */
const REL_PREVIEW = 5

/** The five relation groups, each a label over one id per line. A long id is
 * cut with … (the full id is its tooltip) so every link stays one line, and a
 * group longer than REL_PREVIEW folds the rest behind "+ N more". */
export function Relations({
  paper,
  onOpenPaper,
  onRemove,
  busy,
}: {
  paper: PaperMeta
  onOpenPaper: (id: string) => void
  /** Absent on the trash inspector: relations there are read-only. */
  onRemove?: (rel: RelField, id: string) => void
  /** A structured write is in flight — the × stops taking clicks. */
  busy?: boolean
}) {
  // Groups unfolded past REL_PREVIEW. The call sites key this component by
  // paper id, so the next paper starts folded again.
  const [unfolded, setUnfolded] = useState<ReadonlySet<RelField>>(new Set())
  const groups: Array<[RelField, string[] | undefined]> = REL_FIELDS.map((rel) => [
    rel,
    paper[rel],
  ])
  const nonEmpty = groups.filter(([, ids]) => ids && ids.length > 0)
  if (nonEmpty.length === 0) return <span className="text-stone-400">—</span>
  function toggle(rel: RelField) {
    setUnfolded((prev) => {
      const next = new Set(prev)
      if (!next.delete(rel)) next.add(rel)
      return next
    })
  }
  return (
    <div className="space-y-1.5">
      {nonEmpty.map(([rel, ids]) => {
        const shown = unfolded.has(rel) ? ids! : ids!.slice(0, REL_PREVIEW)
        const folded = ids!.length - shown.length
        return (
          <div key={rel}>
            <div className="text-[11px] text-stone-500">{rel}</div>
            <ul className="mt-0.5 border-l border-stone-200 pl-2">
              {shown.map((id) => (
                // The × is a SIBLING of the link, never nested inside it (buttons
                // cannot nest), and it is always in the DOM — `opacity-0` rather
                // than a conditional render keeps the box, so the id does not shift
                // sideways on hover. A moving target is exactly how a click meant
                // for the link lands on the delete.
                <li key={id} className="group/rel flex items-center gap-0.5">
                  <button
                    onClick={() => onOpenPaper(id)}
                    title={id}
                    className="min-w-0 truncate text-left text-xs leading-5 text-accent-600 transition-colors hover:underline"
                  >
                    {id}
                  </button>
                  {onRemove && (
                    <button
                      type="button"
                      aria-label={`Remove ${rel} link to ${id}`}
                      title="Remove link"
                      disabled={busy}
                      onClick={(e) => {
                        e.stopPropagation()
                        onRemove(rel, id)
                      }}
                      className="shrink-0 px-0.5 text-xs leading-none text-stone-400 opacity-0 transition-opacity hover:text-rose-600 focus-visible:opacity-100 group-hover/rel:opacity-100 disabled:opacity-0"
                    >
                      ×
                    </button>
                  )}
                </li>
              ))}
            </ul>
            {ids!.length > REL_PREVIEW && (
              <button
                type="button"
                onClick={() => toggle(rel)}
                className="mt-0.5 pl-2.5 text-[11px] text-stone-500 transition-colors hover:text-stone-800"
              >
                {folded > 0 ? `+ ${folded} more` : 'Show less'}
              </button>
            )}
          </div>
        )
      })}
    </div>
  )
}

/** Default-No confirm for removing one relation from the Relations row.
 *
 * The × that opens this is the cockpit's first one-click delete reachable from
 * the main panel — every other destructive action sits behind a panel the user
 * had to open first — and it sits a few pixels from a navigation link, so the
 * three guards below are structural, not styling:
 *   · Cancel is autofocused, so Return on a confirm nobody meant to open cancels;
 *   · `useEscapeLayer` closes AND consumes Esc (an unconsumed Esc reaches the
 *     host, where macOS reads it as "leave fullscreen");
 *   · the caller keeps `pendingRel` in `modalOpen`, so global shortcuts stay
 *     suppressed while this is up.
 * Confirming goes through the existing putMetadata rmTag path (invariant #16),
 * which removes the paired edge on the other paper in the same transaction
 * (when that paper is gone, doRemoveRelation removes this side alone).
 * Wording stays short deliberately: no "cannot be undone", because it can — one
 * `lit modify --add-tag` puts the pair back. */
export function RelationRemoveConfirm({
  rel,
  id,
  busy,
  onCancel,
  onConfirm,
}: {
  rel: RelField
  id: string
  busy: boolean
  onCancel: () => void
  onConfirm: () => void
}) {
  useEscapeLayer(true, onCancel)
  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/30 backdrop-blur-sm"
      {...modalBackdropProps}
    >
      <div
        onClick={(e) => e.stopPropagation()}
        className="w-[22rem] animate-grow-in rounded-2xl bg-white p-5 shadow-xl ring-1 ring-stone-200"
      >
        <h2 className="text-sm font-semibold text-stone-900">Remove this link?</h2>
        <p className="mt-1.5 font-mono text-xs leading-relaxed text-stone-600">
          {rel} → {id}
        </p>
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
            disabled={busy}
            className="rounded-lg bg-rose-500 px-3 py-1.5 text-xs font-medium text-white transition-colors hover:bg-rose-600 disabled:opacity-60"
          >
            Remove
          </button>
        </div>
      </div>
    </div>
  )
}
