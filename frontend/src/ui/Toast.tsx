import { useEffect } from 'react'
import { createPortal } from 'react-dom'

export type ToastVariant = 'info' | 'success' | 'error' | 'warning'

interface Props {
  /** The message to show; the host clears it via onDismiss. */
  message: string
  onDismiss: () => void
  /** Tints the leading status dot so success/error read at a glance; the pill
   * itself stays neutral dark. Defaults to a neutral 'info'. */
  variant?: ToastVariant
  /** Auto-dismiss after this many ms. Defaults to 6s for errors (most to read),
   * 5s for warnings, 3.5s otherwise. */
  duration?: number
  /** When true the toast does NOT auto-dismiss — it stays until the user clicks
   * it (or the next notify replaces it). For warnings that must not vanish
   * unread (e.g. a restore that left a code-clone link dangling). */
  sticky?: boolean
}

/** Leading dot colour per variant — a restrained accent, never a filled block.
 * Used for info/success; error/warning swap the dot for a louder glyph (below). */
const DOT: Record<ToastVariant, string> = {
  info: 'bg-stone-400',
  success: 'bg-emerald-400',
  error: 'bg-rose-400',
  warning: 'bg-amber-400',
}

/** Error/warning get a leading glyph + a coloured ring so a brief bottom flash
 * still registers (D3). info/success keep the quiet dot and the neutral ring.
 * The hues are the same rose-400/amber-400 the DOT map already uses — they read
 * on BOTH the light pill and the dark (`stone-50`) pill, so no theme variant is
 * needed (the pill itself is never light; see the surface comment below). */
const GLYPH: Partial<Record<ToastVariant, string>> = {
  error: '✗',
  warning: '⚠',
}
// error/warning carry a coloured ring that must survive BOTH themes — so they
// own the full ring spec here (no separate `dark:` override in the className,
// which would otherwise win on the single `ring-color` property and repaint it
// white in dark mode). info/success fall back to the neutral edge below.
const RING: Partial<Record<ToastVariant, string>> = {
  error: 'ring-rose-400/60',
  warning: 'ring-amber-400/60',
}
const NEUTRAL_RING = 'ring-black/10 dark:ring-white/10'
const GLYPH_COLOR: Record<ToastVariant, string> = {
  info: 'text-stone-300',
  success: 'text-emerald-400',
  error: 'text-rose-400',
  warning: 'text-amber-400',
}

/** A single transient notification, bottom-center, auto-dismissing.
 *
 * Used for non-blocking feedback the user shouldn't have to acknowledge — a
 * dangling [[id]] wikilink click, a failed save, a project registered. Pulls
 * its entrance curve from the shared `animate-grow-in` token (index.css), never
 * a hard-coded one.
 *
 * Portaled to document.body at z-[100]: modals render their own full-viewport
 * `backdrop-blur` overlay (which establishes a containing block + paints over
 * lower siblings), so an inline toast would be dimmed under the backdrop. The
 * portal + high z keep it floating above any open modal so failures stay
 * readable while the dialog is up.
 */
export default function Toast({
  message,
  onDismiss,
  variant = 'info',
  duration,
  sticky,
}: Props) {
  const ms =
    duration ?? (variant === 'error' ? 6000 : variant === 'warning' ? 5000 : 3500)
  useEffect(() => {
    if (sticky) return
    const t = setTimeout(onDismiss, ms)
    return () => clearTimeout(t)
  }, [message, ms, onDismiss, sticky])

  const glyph = GLYPH[variant]
  // error/warning use a coloured ring (both themes); info/success use the
  // neutral edge. The chosen value carries its own dark variant when needed, so
  // the className has NO trailing `dark:ring-*` that could override the colour.
  const ring = RING[variant] ?? NEUTRAL_RING

  return createPortal(
    <div className="pointer-events-none fixed inset-x-0 bottom-6 z-[100] flex justify-center">
      <div
        role="status"
        // The pill must stay a dark surface in BOTH themes. `bg-stone-900` is
        // dark in light mode but the `.dark` ramp inverts it to white, which
        // (with the preserved `text-white`) renders white-on-white. Pin a dark
        // elevated surface in dark mode (stone-50 → #2c2c2e, matching modals).
        // The ring is supplied by `ring` above: a coloured edge for error/
        // warning that holds in BOTH themes (D3), else the neutral black/white
        // edge. No `dark:ring-*` here — it would override the coloured ring.
        className={`pointer-events-auto flex animate-grow-in items-center gap-2 rounded-xl bg-stone-900/90 px-4 py-2 text-sm text-white shadow-lg ring-1 ${ring} backdrop-blur-sm dark:bg-stone-50/95`}
        onClick={onDismiss}
      >
        {glyph ? (
          // A louder leading mark for the variants that must not be missed.
          <span className={`shrink-0 text-sm leading-none ${GLYPH_COLOR[variant]}`}>
            {glyph}
          </span>
        ) : (
          <span className={`h-1.5 w-1.5 shrink-0 rounded-full ${DOT[variant]}`} />
        )}
        {message}
      </div>
    </div>,
    document.body,
  )
}
