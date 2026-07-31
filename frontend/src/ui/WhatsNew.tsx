import { createPortal } from 'react-dom'
import type { WhatsNewInfo } from '../api'

/** The post-update "What's new" card.
 *
 * Opens once after an update (App compares the running version against the
 * last version this browser was shown) and on demand from the TopBar logo.
 * Content is the hand-curated highlights that ship inside the package —
 * plain sentences, no markdown — plus a link to the full changelog for the
 * details the digest leaves out.
 *
 * Mirrors the macOS-style modal shell shared across the app (CheatSheet:
 * backdrop + grow-in card, portaled to document.body so `fixed inset-0`
 * resolves against the viewport). Esc, a click outside, and Done all close;
 * closing is what marks the version as seen (App's onClose), so an abandoned
 * page reload shows the card again rather than losing it. */
export default function WhatsNew({
  info,
  onClose,
}: {
  info: WhatsNewInfo
  onClose: () => void
}) {
  return createPortal(
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/30 backdrop-blur-sm"
      onClick={onClose}
    >
      <div
        onClick={(e) => e.stopPropagation()}
        onKeyDown={(e) => {
          if (e.key === 'Escape') onClose()
        }}
        role="dialog"
        aria-label={`What's new in litman ${info.version}`}
        className="max-h-[85vh] w-[28rem] max-w-[94vw] animate-grow-in overflow-y-auto rounded-2xl bg-white p-6 shadow-xl ring-1 ring-stone-200"
      >
        <div className="text-[11px] font-semibold uppercase tracking-wider text-accent-600">
          Updated
        </div>
        <h2 className="mt-0.5 text-sm font-semibold text-stone-900">
          What&apos;s new in litman {info.version}
        </h2>

        {info.bullets.length > 0 ? (
          <ul className="mt-3 space-y-2">
            {info.bullets.map((b) => (
              <li key={b} className="flex gap-2 text-xs leading-5 text-stone-700">
                <span aria-hidden="true" className="mt-px select-none text-accent-500">
                  •
                </span>
                <span>{b}</span>
              </li>
            ))}
          </ul>
        ) : (
          // Manual reopen on a version with no digest recorded (should not
          // happen for released builds — the release gate requires one).
          <p className="mt-3 text-xs leading-5 text-stone-500">
            No highlights recorded for this version.
          </p>
        )}

        <div className="mt-4 flex items-center justify-between">
          <a
            href={info.changelogUrl}
            target="_blank"
            rel="noreferrer"
            className="text-xs font-medium text-accent-600 transition duration-200 ease-fluid hover:text-accent-700"
          >
            Read the full changelog →
          </a>
          <button
            type="button"
            onClick={onClose}
            className="rounded-lg bg-accent-500 px-3 py-1.5 text-sm font-medium text-white transition duration-200 ease-fluid hover:bg-accent-600"
          >
            Done
          </button>
        </div>
      </div>
    </div>,
    document.body,
  )
}
