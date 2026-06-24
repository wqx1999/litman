import { useEffect } from 'react'
import { createPortal } from 'react-dom'

export type ToastVariant = 'info' | 'success' | 'error'

interface Props {
  /** The message to show; the host clears it via onDismiss. */
  message: string
  onDismiss: () => void
  /** Tints the leading status dot so success/error read at a glance; the pill
   * itself stays neutral dark. Defaults to a neutral 'info'. */
  variant?: ToastVariant
  /** Auto-dismiss after this many ms. Defaults to 6s for errors (more to read)
   * and 3.5s otherwise. */
  duration?: number
}

/** Leading dot colour per variant — a restrained accent, never a filled block. */
const DOT: Record<ToastVariant, string> = {
  info: 'bg-stone-400',
  success: 'bg-emerald-400',
  error: 'bg-rose-400',
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
export default function Toast({ message, onDismiss, variant = 'info', duration }: Props) {
  const ms = duration ?? (variant === 'error' ? 6000 : 3500)
  useEffect(() => {
    const t = setTimeout(onDismiss, ms)
    return () => clearTimeout(t)
  }, [message, ms, onDismiss])

  return createPortal(
    <div className="pointer-events-none fixed inset-x-0 bottom-6 z-[100] flex justify-center">
      <div
        role="status"
        className="pointer-events-auto flex animate-grow-in items-center gap-2 rounded-xl bg-stone-900/90 px-4 py-2 text-sm text-white shadow-lg ring-1 ring-black/10 backdrop-blur-sm"
        onClick={onDismiss}
      >
        <span className={`h-1.5 w-1.5 shrink-0 rounded-full ${DOT[variant]}`} />
        {message}
      </div>
    </div>,
    document.body,
  )
}
