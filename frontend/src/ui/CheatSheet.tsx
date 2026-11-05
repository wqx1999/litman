import { createPortal } from 'react-dom'
import { useT, type Messages } from '../i18n'
import { useEscapeLayer } from './escapeStack'
import { modalBackdropProps } from './modalShell'

/** The keyboard-shortcut cheat sheet (Phase 4, `?` toggles it).
 *
 * A read-only overlay listing the three-tier scheme defined in task-web-gui.md
 * §2.3. Mirrors the macOS-style modal shell shared across the app (backdrop +
 * grow-in card); Esc and the Done button close it, a click outside does not
 * (ui/modalShell — the rule is app-wide). Esc arrives through the shared layer
 * stack (ui/escapeStack), so it closes the sheet wherever focus happens to be.
 * Pure presentation — no shortcut logic.
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
// macOS too (even though a Mac user habitually presses ⌘). Keycap labels are
// not UI text — they match the printed key — so they stay here, not in the
// text table.
function sections(c: Messages['reader']['cheatSheet']): Section[] {
  return [
    {
      title: c.display.title,
      rows: [
        { chords: [['F']], action: c.display.focusMode },
        { chords: [['L']], action: c.display.theme },
        { chords: [['[']], action: c.display.leftPanel },
        { chords: [[']']], action: c.display.cockpit },
        { chords: [['R']], action: c.display.refresh },
        { chords: [['?']], action: c.display.cheatSheet },
      ],
    },
    {
      title: c.agent.title,
      // Labelled `~`, not `` ` ``: the backtick glyph is near-invisible at keycap
      // size, and the tilde is what the eye finds on the physical key. The
      // dispatcher accepts the key with or without Shift, so this label cannot
      // teach a press that does nothing.
      note: c.agent.note,
      rows: [
        { chords: [['~']], action: c.agent.launch },
        { chords: [['Ctrl', '~']], action: c.agent.manage },
      ],
    },
    {
      title: c.papers.title,
      note: c.papers.note,
      rows: [
        { chords: [['J']], action: c.papers.next },
        { chords: [['K']], action: c.papers.previous },
        { chords: [['P']], action: c.papers.pin },
        { chords: [['Enter']], action: c.papers.open },
        { chords: [['Shift', 'N']], action: c.papers.openNotes },
        { chords: [['Shift', 'D']], action: c.papers.openDiscussion },
        { chords: [['/']], action: c.papers.focusSearch },
        { chords: [['Esc']], action: c.papers.clearSearch, scope: c.scope.search },
      ],
    },
    {
      title: c.tabs.title,
      note: c.tabs.note,
      rows: [
        { chords: [[',']], action: c.tabs.previous },
        { chords: [['.']], action: c.tabs.next },
        { chords: [['1–9']], action: c.tabs.jump },
      ],
    },
    {
      title: c.pdf.title,
      // Third sentence: undo comes from pdf.js and only runs while an annotation
      // tool is on — it is inert in Cursor. Stating that is the whole point of
      // listing it: pressed in Cursor it does nothing, and a row without the
      // precondition would read as a broken shortcut.
      note: c.pdf.note,
      rows: [
        { chords: [['V'], ['Esc']], action: c.pdf.cursor, scope: c.scope.pdf },
        { chords: [['Esc']], action: c.pdf.saveNote, scope: c.scope.note },
        { chords: [['H']], action: c.pdf.highlight, scope: c.scope.pdf },
        { chords: [['T']], action: c.pdf.textNote, scope: c.scope.pdf },
        { chords: [['D']], action: c.pdf.draw, scope: c.scope.pdf },
        { chords: [['Ctrl', 'Z']], action: c.pdf.undo, scope: c.scope.tool },
        { chords: [['Ctrl', 'Y']], action: c.pdf.redo, scope: c.scope.tool },
        { chords: [['Alt', '←']], action: c.pdf.back, scope: c.scope.pdf },
        { chords: [['Alt', '→']], action: c.pdf.forward, scope: c.scope.pdf },
      ],
    },
    {
      title: c.curation.title,
      note: c.curation.note,
      rows: [
        { chords: [['Alt', 'R']], action: c.curation.markRead },
        { chords: [['Alt', 'Shift', 'R']], action: c.curation.markUnread },
        { chords: [['Alt', 'P']], action: c.curation.promote },
        { chords: [['Alt', 'D']], action: c.curation.drop },
        { chords: [['Alt', 'T']], action: c.curation.tagEditor },
        { chords: [['Alt', 'C']], action: c.curation.copyPath },
        { chords: [['Alt', 'Shift', 'C']], action: c.curation.copyId },
      ],
    },
    {
      title: c.system.title,
      rows: [
        { chords: [['Ctrl', 'S']], action: c.system.save },
        // One chord, not three: "Ctrl + / Ctrl − / Ctrl 0" was the only row wide
        // enough to break the keycap column's alignment.
        { chords: [['Ctrl', '+ / − / 0']], action: c.system.zoom },
        { chords: [['Ctrl', 'K']], action: c.system.focusSearch },
      ],
    },
  ]
}

export default function CheatSheet({ onClose }: { onClose: () => void }) {
  const t = useT()
  const c = t.reader.cheatSheet
  useEscapeLayer(true, onClose)
  return createPortal(
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/30 backdrop-blur-sm"
      {...modalBackdropProps}
    >
      <div
        onClick={(e) => e.stopPropagation()}
        role="dialog"
        aria-label={c.title}
        className="max-h-[85vh] w-[52rem] max-w-[94vw] animate-grow-in overflow-y-auto rounded-2xl bg-white p-6 shadow-xl ring-1 ring-stone-200"
      >
        <h2 className="mb-4 text-sm font-semibold text-stone-900">
          {c.title}
        </h2>

        {/* Column FLOW (not a grid): sections pack bottom-up, so a one-row group
         * never leaves a hole the height of its neighbour. break-inside-avoid
         * keeps a section whole. */}
        <div className="columns-1 gap-8 sm:columns-2">
          {sections(c).map((section, si) => (
            <section key={si} className="mb-5 break-inside-avoid">
              <h3 className="mb-1 text-[11px] font-semibold uppercase tracking-wider text-stone-600">
                {section.title}
              </h3>
              {section.note && (
                <p className="mb-2 text-[11px] leading-snug text-stone-400">
                  {section.note}
                </p>
              )}
              <ul className="space-y-1.5">
                {section.rows.map((row, ri) => (
                  // Fixed 9rem keycap column, right-aligned (macOS menu
                  // convention). A rigid track — not flex justify-between —
                  // means the keycaps land on the same x in every row and the
                  // description can never squeeze them into a wrap.
                  <li
                    key={ri}
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
                            <span className="text-[10px] text-stone-400">{c.or}</span>
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
            {t.common.done}
          </button>
        </div>
      </div>
    </div>,
    document.body,
  )
}
