import { useEffect } from 'react'

interface Props {
  /** The message to show; the host clears it via onDismiss. */
  message: string
  onDismiss: () => void
  /** Auto-dismiss after this many ms (default 3.5s). */
  duration?: number
}

/** A single transient notification, bottom-center, auto-dismissing.
 *
 * Used for non-blocking feedback the user shouldn't have to acknowledge —
 * a dangling [[id]] wikilink click, a failed save. Pulls its entrance curve
 * from the shared `animate-grow-in` token (index.css), never a hard-coded one.
 */
export default function Toast({ message, onDismiss, duration = 3500 }: Props) {
  useEffect(() => {
    const t = setTimeout(onDismiss, duration)
    return () => clearTimeout(t)
  }, [message, duration, onDismiss])

  return (
    <div className="pointer-events-none fixed inset-x-0 bottom-6 z-50 flex justify-center">
      <div
        role="status"
        className="pointer-events-auto animate-grow-in rounded-xl bg-stone-900/90 px-4 py-2 text-sm text-white shadow-lg ring-1 ring-black/10 backdrop-blur-sm"
        onClick={onDismiss}
      >
        {message}
      </div>
    </div>
  )
}
