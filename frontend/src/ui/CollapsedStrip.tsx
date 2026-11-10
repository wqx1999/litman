/** The strip a side panel leaves behind when collapsed: the whole strip is the
 * expand button, so there is no small target to hunt for along the edge.
 *
 * Pinned to the panel's left edge at the collapsed width, so it does not slide
 * while the panel's width animates. The chevron sits mid-height and turns half
 * a circle as it fades in: it starts pointing the way the panel's own collapse
 * chevron points and ends pointing the way the panel will open, so it reads as
 * that chevron turning over rather than a second one appearing. The turn waits
 * for the fade; started with the collapse, it is over before the strip shows.
 * With reduced motion it only fades.
 */
export default function CollapsedStrip({
  collapsed,
  side,
  title,
  onExpand,
}: {
  collapsed: boolean
  /** Which side of the window the panel sits on; decides where it opens to. */
  side: 'left' | 'right'
  title: string
  onExpand: () => void
}) {
  return (
    <button
      type="button"
      onClick={onExpand}
      title={title}
      aria-label={title}
      inert={!collapsed}
      className={`group absolute inset-y-0 left-0 flex w-9 cursor-pointer items-center justify-center transition-opacity duration-200 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-accent-500/40 ${
        collapsed ? 'opacity-100 delay-150' : 'pointer-events-none opacity-0'
      }`}
    >
      {/* Hover tint on its own layer: the fade-in delay above must not hold it
          back. Short of the reading area's grey, so the strip keeps its edge. */}
      <span className="absolute inset-0 transition-colors duration-150 group-hover:bg-stone-200/70" />
      <svg
        viewBox="0 0 12 32"
        fill="none"
        stroke="currentColor"
        strokeWidth={1.5}
        strokeLinecap="round"
        strokeLinejoin="round"
        aria-hidden
        className={`relative h-8 w-3 text-stone-400 transition-[rotate,color] ease-in-out group-hover:text-stone-700 ${
          collapsed ? 'duration-500 delay-150' : 'duration-200 motion-safe:rotate-180'
        }`}
      >
        {side === 'left' ? <path d="M3 4l6 12-6 12" /> : <path d="M9 4 3 16l6 12" />}
      </svg>
    </button>
  )
}
