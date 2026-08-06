interface Props {
  /** Page number the back step would return to (the pill's whole message). */
  page: number
  /** Expanded shows the label; collapsed is the bare ← button. Hover expands a
   * collapsed pill on its own (CSS), without telling the parent. */
  expanded: boolean
  onClick: () => void
}

/** The "you can get back" affordance: a small pill that floats at the bottom of
 * the PDF after a citation jump, showing where Alt+← would land.
 *
 * Two states, one element: expanded (`← Back to p.7`) right after the jump, then
 * collapsed to a bare `←` a few seconds later — the way back stays visible
 * without a permanent toolbar button. The parent hides it entirely when there is
 * nothing to go back to.
 *
 * The label is always in the DOM and collapses by animating its own max-width to
 * zero, so hover-to-expand is pure CSS (`group-hover:`) — no rAF loop, no state
 * round-trip, and it re-expands under the cursor even while the parent still
 * thinks it is collapsed. The two class strings are mutually exclusive so
 * Tailwind never has to arbitrate between competing max-w/opacity utilities.
 *
 * Presentational only: it holds no history, no timer, and no keyboard binding —
 * clicking it runs the same handler as Alt+←. */
export default function JumpBackPill({ page, expanded, onClick }: Props) {
  return (
    <button
      type="button"
      onClick={onClick}
      // Keep the click off pdf.js: a pointer event reaching the editor layer
      // would deselect the annotation under way / start a gesture. Same guard as
      // the editor popover.
      onPointerDown={(e) => e.stopPropagation()}
      onPointerUp={(e) => e.stopPropagation()}
      aria-label={`Back to page ${page}`}
      title={`Back to page ${page} (Alt+←)`}
      className="group flex items-center rounded-full border border-stone-200 bg-stone-50/95 px-2.5 py-1.5 text-xs text-stone-700 shadow-xl shadow-stone-900/10 backdrop-blur-sm transition-colors hover:bg-stone-100"
    >
      <span aria-hidden className="leading-none">
        ←
      </span>
      <span
        className={
          'overflow-hidden whitespace-nowrap leading-none transition-all duration-200 ' +
          (expanded
            ? 'ml-1.5 max-w-[12rem] opacity-100'
            : 'ml-0 max-w-0 opacity-0 group-hover:ml-1.5 group-hover:max-w-[12rem] group-hover:opacity-100')
        }
      >
        Back to p.{page}
      </span>
    </button>
  )
}
