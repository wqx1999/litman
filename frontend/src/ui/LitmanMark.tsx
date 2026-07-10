interface Props {
  /** Sizing utilities from the caller; the mark is square. */
  className?: string
}

/** The litman mark: an open book whose right page is lifted off the spine.
 *
 * Inlined rather than served as an <img> because the ink page is `currentColor`
 * — an image file could only follow the OS colour scheme, and this app drives
 * dark mode from the `.dark` class instead. The lifted page keeps the accent,
 * which the token ramp already flips to #0a84ff on dark grounds.
 *
 * The whole thing is scaled to 0.94 about its centre so the lifted page's top
 * corner clears the viewBox after the 4-degree rotation. */
export default function LitmanMark({ className }: Props) {
  return (
    <svg className={className} viewBox="0 0 64 64" role="img">
      <title>litman</title>
      <g transform="translate(32 33) scale(0.94) translate(-32 -32)">
        <path
          d="M32 21 C 24 9 10 9 5 17 C 1.5 23 3.5 40 8 47 C 15 42 26 44 32 52 Z"
          fill="currentColor"
          transform="translate(-1 0)"
        />
        <path
          d="M32 21 C 40 9 54 9 59 17 C 62.5 23 60.5 40 56 47 C 49 42 38 44 32 52 Z"
          fill="var(--color-accent-500)"
          transform="translate(2.5 -4) rotate(4 32 36)"
        />
      </g>
    </svg>
  )
}
