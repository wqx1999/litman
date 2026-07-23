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

/** One keycap chip — a single physical key. Rendered in the app's system sans,
 * NOT a monospace face: a mono keycap reads as out of place against the native
 * UI. Multi-char labels (Esc / Alt / Shift / Ctrl) widen via padding; single
 * chars keep a min width so they stay roughly square. */
function Key({ children }: { children: string }) {
  return (
    <kbd className="inline-flex min-w-[1.5rem] items-center justify-center whitespace-nowrap rounded-md border border-stone-300 bg-stone-50 px-1.5 py-0.5 text-[11px] font-medium text-stone-700 shadow-sm">
      {children}
    </kbd>
  )
}

/** One chord = the keys pressed together, each its own keycap sitting adjacent
 * (Alt Shift R). Modifiers are spelled out (Alt / Shift / Ctrl) instead of
 * glyphs (⌥ ⇧ ⌘): the symbol is an extra decode step, and the word matches what
 * is printed on the physical key. */
function Chord({ keys }: { keys: string[] }) {
  return (
    <span className="flex items-center gap-1">
      {keys.map((k, i) => (
        <Key key={`${k}-${i}`}>{k}</Key>
      ))}
    </span>
  )
}

interface Row {
  /** Alternative chords for the action; each chord is one set of keys pressed
   * together. Multiple entries render joined by "or" (e.g. V or Esc). */
  chords: string[][]
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

// The scheme verbatim from §2.3. Modifiers are spelled out (Alt / Shift / Ctrl)
// rather than shown as glyphs — see Chord. Ctrl is accurate on every platform:
// the app binds every system shortcut on metaKey OR ctrlKey, so Ctrl works on
// macOS too (even though a Mac user habitually presses ⌘).
const SECTIONS: Section[] = [
  {
    title: 'Display & panels',
    rows: [
      { chords: [['F']], action: 'Focus mode (hide side panels)' },
      { chords: [['L']], action: 'Toggle light / dark theme' },
      { chords: [['[']], action: 'Toggle the left panel' },
      { chords: [[']']], action: 'Toggle the right cockpit' },
      { chords: [['R']], action: 'Refresh from disk' },
      { chords: [['?']], action: 'Toggle this cheat sheet' },
    ],
  },
  {
    title: 'Agent',
    // Labelled `~`, not `` ` ``: the backtick glyph is near-invisible at keycap
    // size, and the tilde is what the eye finds on the physical key. The
    // dispatcher accepts the key with or without Shift, so this label cannot
    // teach a press that does nothing.
    note: 'The key left of "1", with or without Shift. Plain launch opens setup when the default is not ready.',
    rows: [
      { chords: [['~']], action: 'Launch the default AI agent' },
      { chords: [['Ctrl', '~']], action: 'Manage agents / change default' },
    ],
  },
  {
    title: 'Papers list & search',
    note: 'Move through the middle list; Enter opens the PDF.',
    rows: [
      { chords: [['J']], action: 'Next paper' },
      { chords: [['K']], action: 'Previous paper' },
      { chords: [['Enter']], action: 'Open the selected paper' },
      { chords: [['/']], action: 'Focus search' },
    ],
  },
  {
    title: 'Tabs',
    note: 'Switch the open document tabs.',
    rows: [
      { chords: [[',']], action: 'Previous tab' },
      { chords: [['.']], action: 'Next tab' },
      { chords: [['1–9']], action: 'Jump to tab 1–9' },
    ],
  },
  {
    title: 'PDF tools',
    note: 'Only while a PDF tab is active. Tools switch freely in any order.',
    rows: [
      { chords: [['V'], ['Esc']], action: 'Cursor (select / exit tool)', scope: 'PDF' },
      { chords: [['H']], action: 'Highlight', scope: 'PDF' },
      { chords: [['T']], action: 'Text note', scope: 'PDF' },
      { chords: [['D']], action: 'Draw (ink)', scope: 'PDF' },
    ],
  },
  {
    title: 'Curation — selected paper',
    note: 'Hold Alt. Acts on the selected paper; none selected is a no-op.',
    rows: [
      { chords: [['Alt', 'R']], action: 'Mark read (idempotent)' },
      { chords: [['Alt', 'Shift', 'R']], action: 'Mark unread (confirm)' },
      { chords: [['Alt', 'P']], action: 'Promote (deep-read)' },
      { chords: [['Alt', 'D']], action: 'Drop (confirm)' },
      { chords: [['Alt', 'T']], action: 'Open the tag editor' },
      { chords: [['Alt', 'C']], action: 'Copy paper path' },
      { chords: [['Alt', 'Shift', 'C']], action: 'Copy paper id' },
    ],
  },
  {
    title: 'System (unchanged)',
    rows: [
      { chords: [['Ctrl', 'S']], action: 'Save the current tab' },
      // One chord, not three: "Ctrl + / Ctrl − / Ctrl 0" was the only row wide
      // enough to break the keycap column's alignment.
      { chords: [['Ctrl', '+ / − / 0']], action: 'PDF zoom' },
      { chords: [['Ctrl', 'K']], action: 'Focus search' },
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
        className="max-h-[85vh] w-[52rem] max-w-[94vw] animate-grow-in overflow-y-auto rounded-2xl bg-white p-6 shadow-xl ring-1 ring-stone-200"
      >
        <h2 className="mb-4 text-sm font-semibold text-stone-900">
          Keyboard shortcuts
        </h2>

        {/* Column FLOW (not a grid): sections pack bottom-up, so a one-row group
         * never leaves a hole the height of its neighbour. break-inside-avoid
         * keeps a section whole. */}
        <div className="columns-1 gap-8 sm:columns-2">
          {SECTIONS.map((section) => (
            <section key={section.title} className="mb-5 break-inside-avoid">
              <h3 className="mb-1 text-[11px] font-semibold uppercase tracking-wider text-stone-500">
                {section.title}
              </h3>
              {section.note && (
                <p className="mb-2 text-[11px] leading-snug text-stone-400">
                  {section.note}
                </p>
              )}
              <ul className="space-y-1.5">
                {section.rows.map((row) => (
                  // Fixed 9rem keycap column, right-aligned (macOS menu
                  // convention). A rigid track — not flex justify-between —
                  // means the keycaps land on the same x in every row and the
                  // description can never squeeze them into a wrap.
                  <li
                    key={row.action}
                    className="grid grid-cols-[1fr_9rem] items-baseline gap-3"
                  >
                    <span className="text-xs leading-5 text-stone-700">
                      {row.action}
                      {row.scope && (
                        <span className="ml-1.5 text-[10px] font-medium text-stone-400">
                          {row.scope}
                        </span>
                      )}
                    </span>
                    <span className="flex items-center justify-end gap-1.5">
                      {row.chords.map((chord, i) => (
                        <span key={i} className="flex items-center gap-1.5">
                          {i > 0 && (
                            <span className="text-[10px] text-stone-400">or</span>
                          )}
                          <Chord keys={chord} />
                        </span>
                      ))}
                    </span>
                  </li>
                ))}
              </ul>
            </section>
          ))}
        </div>

        <div className="mt-1 flex justify-end">
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
