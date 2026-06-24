import { createPortal } from 'react-dom'

/** The keyboard-shortcut cheat sheet (Phase 4, `?` toggles it).
 *
 * A read-only overlay listing the three-tier scheme defined in task-web-gui.md
 * §2.3. Mirrors the macOS-style modal shell shared across the app (backdrop +
 * grow-in card); Esc, a click outside, and the Done button all close it (the
 * Esc handling lives in the global dispatcher, which closes the sheet first —
 * see useKeyboardShortcuts; the in-card Escape here is a belt-and-braces close
 * for when focus is inside the card). Pure presentation — no shortcut logic.
 *
 * Portaled to document.body so its `fixed inset-0` resolves against the viewport
 * regardless of any backdrop-filter ancestor (same reasoning as Toast /
 * ProjectManager). */

/** One chord, rendered as keycap chip(s). A multi-key chord ("⌥⇧R") stays a
 * single chip so the modifier order reads as one gesture. */
function Key({ children }: { children: string }) {
  return (
    <kbd className="inline-flex min-w-[1.6rem] items-center justify-center rounded-md border border-stone-300 bg-stone-50 px-1.5 py-0.5 font-mono text-[11px] font-medium text-stone-700 shadow-sm">
      {children}
    </kbd>
  )
}

interface Row {
  /** The key chord(s) for this action. Multiple entries render as "A or B". */
  keys: string[]
  action: string
  /** Optional scope tag (e.g. "PDF") shown muted after the action. */
  scope?: string
}

interface Section {
  title: string
  /** A muted one-line note under the section title. */
  note?: string
  rows: Row[]
}

// The scheme verbatim from §2.3. macOS glyphs (⌘ ⌥ ⇧) match what the user sees
// on their keyboard; the dispatcher matches on e.code, but the cheat sheet is
// for humans, so it shows the symbols.
const SECTIONS: Section[] = [
  {
    title: 'Display & panels',
    rows: [
      { keys: ['F'], action: 'Focus mode (hide side panels)' },
      { keys: ['L'], action: 'Toggle light / dark theme' },
      { keys: ['['], action: 'Toggle the left panel' },
      { keys: [']'], action: 'Toggle the right cockpit' },
      { keys: ['?'], action: 'Toggle this cheat sheet' },
    ],
  },
  {
    title: 'PDF tools',
    note: 'Only while a PDF tab is active. Tools switch freely in any order.',
    rows: [
      { keys: ['V', 'Esc'], action: 'Cursor (select / exit tool)', scope: 'PDF' },
      { keys: ['H'], action: 'Highlight', scope: 'PDF' },
      { keys: ['T'], action: 'Text note', scope: 'PDF' },
      { keys: ['D'], action: 'Draw (ink)', scope: 'PDF' },
    ],
  },
  {
    title: 'Curation — selected paper',
    note: 'Hold ⌥ (Alt). Acts on the selected paper; none selected is a no-op.',
    rows: [
      { keys: ['⌥R'], action: 'Mark read (idempotent)' },
      { keys: ['⌥⇧R'], action: 'Mark unread (confirm)' },
      { keys: ['⌥P'], action: 'Promote (deep-read)' },
      { keys: ['⌥D'], action: 'Drop (confirm)' },
      { keys: ['⌥T'], action: 'Open the tag editor' },
      { keys: ['⌥C'], action: 'Copy paper path' },
      { keys: ['⌥⇧C'], action: 'Copy paper id' },
    ],
  },
  {
    title: 'System (unchanged)',
    rows: [
      { keys: ['⌘S'], action: 'Save the current tab' },
      { keys: ['⌘+', '⌘−', '⌘0'], action: 'PDF zoom' },
      { keys: ['⌘K'], action: 'Focus search' },
    ],
  },
]

export default function CheatSheet({ onClose }: { onClose: () => void }) {
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
        aria-label="Keyboard shortcuts"
        className="max-h-[80vh] w-[34rem] max-w-[92vw] animate-grow-in overflow-y-auto rounded-2xl bg-white p-5 shadow-xl ring-1 ring-stone-200"
      >
        <div className="mb-3 flex items-baseline justify-between">
          <h2 className="text-sm font-semibold text-stone-900">
            Keyboard shortcuts
          </h2>
          <span className="text-[11px] text-stone-400">⌘ = Ctrl · ⌥ = Alt</span>
        </div>

        {/* Two-column flow keeps the four groups compact on a single card. */}
        <div className="columns-1 gap-6 sm:columns-2">
          {SECTIONS.map((section) => (
            <section key={section.title} className="mb-4 break-inside-avoid">
              <h3 className="mb-0.5 text-[11px] font-semibold uppercase tracking-wider text-stone-500">
                {section.title}
              </h3>
              {section.note && (
                <p className="mb-1.5 text-[11px] leading-snug text-stone-400">
                  {section.note}
                </p>
              )}
              <ul className="space-y-1">
                {section.rows.map((row) => (
                  <li
                    key={row.action}
                    className="flex items-center justify-between gap-3"
                  >
                    <span className="text-xs text-stone-700">
                      {row.action}
                      {row.scope && (
                        <span className="ml-1.5 text-[10px] font-medium text-stone-400">
                          {row.scope}
                        </span>
                      )}
                    </span>
                    <span className="flex shrink-0 items-center gap-1">
                      {row.keys.map((k, i) => (
                        <span key={k} className="flex items-center gap-1">
                          {i > 0 && (
                            <span className="text-[10px] text-stone-400">or</span>
                          )}
                          <Key>{k}</Key>
                        </span>
                      ))}
                    </span>
                  </li>
                ))}
              </ul>
            </section>
          ))}
        </div>

        <div className="mt-2 flex justify-end">
          <button
            onClick={onClose}
            className="rounded-lg px-3 py-1.5 text-xs text-stone-600 transition-colors hover:bg-stone-100"
          >
            Done
          </button>
        </div>
      </div>
    </div>,
    document.body,
  )
}
