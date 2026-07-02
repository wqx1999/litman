import { useEffect } from 'react'
import type { PdfHandle, PdfEditMode } from './pdf/PdfView'
import type { CockpitHandle } from './cockpit/Cockpit'

/** Everything the global keyboard dispatcher needs from App. All values are read
 * live through this object (App rebinds the effect when any reference changes),
 * so the handlers always act on the current selection / active tab / handles. */
export interface ShortcutDeps {
  /** True when ANY blocking surface is up (SaveDialog, SwitchVaultDialog, the
   * cheat sheet itself, a cockpit confirm/panel, the TopBar Projects manager).
   * While true, all global shortcuts are suppressed so a key meant for the open
   * modal (Enter/Esc/typing) never also fires a global action. The modal owns
   * its own Esc handler, so we deliberately do NOT handle Esc here in that case. */
  anyModalOpen: boolean

  // --- Tier 1: display + panels (global, focus-guarded) -------------------
  toggleFocus: () => void
  toggleDark: () => void
  toggleLeft: () => void
  toggleRight: () => void

  // --- Tier 1: center tab switching (global, focus-guarded) ---------------
  /** Cycle the active center document tab. delta +1 = next, -1 = previous;
   * wraps around; no-op with fewer than two tabs. */
  activateAdjacentTab: (delta: 1 | -1) => void
  /** Jump straight to the Nth center tab (1-based). Out-of-range = no-op. */
  activateTabByIndex: (n: number) => void

  // --- Cheat sheet (`?`) ---------------------------------------------------
  cheatSheetOpen: boolean
  toggleCheatSheet: () => void
  closeCheatSheet: () => void

  // --- Tier 1: PDF tools (only when a PDF tab is active) -------------------
  /** True when the active center tab is a PDF tab. PDF-tool keys only fire then. */
  pdfActive: boolean
  /** Resolve the active PDF tab's imperative handle (setEditMode) at call time.
   * A getter, not a value, because App stores PDF handles in a ref Map (mutated
   * without a re-render when a tab mounts/unmounts), so reading it live avoids a
   * stale snapshot the moment a PDF tab becomes active. */
  getPdfHandle: () => PdfHandle | null

  // --- Tier 2: curation (⌥+letter, acts on the selected paper) ------------
  /** The currently selected paper, or null. ⌥-actions no-op + toast when null. */
  selectedId: string | null
  /** Cockpit imperative handle routing to the existing curation handlers. */
  cockpit: CockpitHandle | null
  /** Subtle feedback after a curation shortcut (post-write hint, or "no paper"). */
  notify: (message: string) => void
}

/** Is focus currently inside a text-entry surface? Bare single-keys and
 * ⌥-combos must not fire there (typing "H" in notes must not switch a PDF tool;
 * ⌥-combos can compose characters / move the caret in a field). Covers <input>,
 * <textarea>, and any contentEditable host. */
function isEditingTarget(el: EventTarget | null): boolean {
  if (!(el instanceof HTMLElement)) return false
  const tag = el.tagName
  if (tag === 'INPUT' || tag === 'TEXTAREA') return true
  if (el.isContentEditable) return true
  return false
}

/** Does this event carry a Tier-0 Cmd/Ctrl combo the app already owns elsewhere
 * (save / PDF zoom / search focus)? Those have their own listeners (PdfView's
 * capture-phase ⌘S + ⌘-zoom, the search box ⌘K), so the global dispatcher must
 * let them propagate untouched — it neither handles nor preventDefaults them.
 * `e.code` is used for the letter (S/K) so a composed key can't slip past. */
function isReservedModifierCombo(e: KeyboardEvent): boolean {
  // EVERY Cmd/Ctrl combo is intentionally left to the browser / existing Tier-0
  // listeners — PdfView's capture-phase ⌘S + bubble-phase ⌘-zoom (+/−/0) and
  // the search box's ⌘K. This new bubble-phase dispatcher binds none of them, so
  // bailing here guarantees it never shadows them; the app defines no Tier-1/2
  // shortcut on a Cmd/Ctrl combo, so there is nothing to discriminate.
  return e.metaKey || e.ctrlKey
}

/**
 * The single global keyboard-shortcut dispatcher (Phase 4, scheme in §2.3).
 *
 * Bound on `document` in the BUBBLE phase so it never shadows PdfView's
 * capture-phase ⌘S listener: capture-phase handlers run first, ⌘S is in our
 * reserved set anyway, so it is handled by PdfView and we no-op on it.
 *
 * Two global rules from §2.3 are enforced first, before any dispatch:
 *   1. Focus guard — bare keys and ⌥-combos are inert while focus is in a text
 *      field (so typing "H" in notes, or ⌥-composing a character, is safe).
 *   2. Cross-platform — ⌘ ≡ Ctrl, ⌥ ≡ Alt; LETTER shortcuts match on `e.code`
 *      (KeyR, KeyP, …) NOT `e.key`, because with Alt held macOS returns a
 *      composed glyph for `e.key` (⌥R → "®"), which would silently break every
 *      Tier-2 shortcut. `?` is the one exception (matched on `e.key === '?'`,
 *      i.e. Shift+/), since there is no stable letter code for it.
 *
 * preventDefault is called ONLY for a key actually handled.
 */
export function useKeyboardShortcuts(deps: ShortcutDeps): void {
  const {
    anyModalOpen,
    toggleFocus,
    toggleDark,
    toggleLeft,
    toggleRight,
    activateAdjacentTab,
    activateTabByIndex,
    cheatSheetOpen,
    toggleCheatSheet,
    closeCheatSheet,
    pdfActive,
    getPdfHandle,
    selectedId,
    cockpit,
    notify,
  } = deps

  useEffect(() => {
    const onKeyDown = (e: KeyboardEvent) => {
      // Let the app's own Tier-0 Cmd/Ctrl listeners (or the browser) handle any
      // modifier combo — never handle or preventDefault it here.
      if (isReservedModifierCombo(e)) return

      // Modal guard: while a blocking surface is up, suppress all global
      // shortcuts. The open modal owns Esc/Enter/typing; we get out of the way
      // entirely (do NOT preventDefault, do NOT handle — including Esc).
      if (anyModalOpen) return

      const editing = isEditingTarget(e.target)

      // --- `?` (Shift+/) — toggle the cheat sheet (focus-guarded) ----------
      // Matched on e.key (no stable letter code for '?'). Skipped while typing.
      if (!editing && e.key === '?') {
        e.preventDefault()
        toggleCheatSheet()
        return
      }

      // --- Esc — close the cheat sheet first, else exit the PDF tool -------
      // The cheat sheet (rendered when open) is not in anyModalOpen's "block
      // shortcuts" set — it is a non-blocking overlay this dispatcher owns — so
      // Esc reaches here. Esc is allowed even while editing: leaving a text
      // field via Esc should still drop a tool / close the sheet, matching the
      // PDF Cursor key (`V`/`Esc`).
      if (e.key === 'Escape') {
        if (cheatSheetOpen) {
          e.preventDefault()
          closeCheatSheet()
          return
        }
        if (pdfActive && !editing) {
          const handle = getPdfHandle()
          if (handle) {
            e.preventDefault()
            handle.setEditMode('none')
            return
          }
        }
        return
      }

      // Everything below is a bare key or an ⌥-combo: inert while editing.
      if (editing) return

      // --- Tier 2: ⌥ (Alt) + letter — curation on the selected paper -------
      // Matched on e.code (Mac gotcha: e.key is a composed glyph under Alt).
      // Plain Alt only (Cmd/Ctrl+Alt combos fall through to the browser). Shift
      // distinguishes ⌥R from ⌥⇧R and ⌥C from ⌥⇧C. No selected paper → no-op +
      // a subtle toast, never a write.
      if (e.altKey && !e.metaKey && !e.ctrlKey) {
        const shift = e.shiftKey
        let action: ((c: CockpitHandle) => void) | null = null
        switch (e.code) {
          case 'KeyR':
            action = shift ? (c) => c.triggerUnread() : (c) => c.triggerRead()
            break
          case 'KeyP':
            if (!shift) action = (c) => c.triggerPromote()
            break
          case 'KeyD':
            if (!shift) action = (c) => c.triggerDrop()
            break
          case 'KeyT':
            if (!shift) action = (c) => c.openTags()
            break
          case 'KeyC':
            action = shift ? (c) => c.copyId() : (c) => c.copyPath()
            break
        }
        if (action) {
          // ⌥+letter is unused by the browser, so claim it regardless of
          // selection (preventDefault stops the OS menu-key / dead-key glyph).
          e.preventDefault()
          if (!selectedId || !cockpit) {
            notify('未选中论文')
            return
          }
          action(cockpit)
          return
        }
        // An unmapped ⌥-combo (e.g. ⌥X): leave it to the browser.
        return
      }

      // --- Tier 1: bare single keys (no modifiers) -------------------------
      // Reject any modifier so e.g. Ctrl+F (page find) is never swallowed.
      if (e.metaKey || e.ctrlKey || e.altKey || e.shiftKey) return

      switch (e.code) {
        case 'KeyF':
          e.preventDefault()
          toggleFocus()
          return
        case 'KeyL':
          e.preventDefault()
          toggleDark()
          return
        case 'BracketLeft':
          e.preventDefault()
          toggleLeft()
          return
        case 'BracketRight':
          e.preventDefault()
          toggleRight()
          return
        case 'Comma':
          e.preventDefault()
          activateAdjacentTab(-1)
          return
        case 'Period':
          e.preventDefault()
          activateAdjacentTab(1)
          return
      }

      // 1–9 jump straight to the Nth center tab (main number row only). Bare
      // digits are free in-browser; the Cmd/Ctrl+digit browser tab-jump was
      // rejected earlier (it fights the host browser). e.code is "DigitN".
      if (/^Digit[1-9]$/.test(e.code)) {
        e.preventDefault()
        activateTabByIndex(Number(e.code.slice(5)))
        return
      }

      // PDF tools fire only when a PDF tab is active (V/H/T/D; Esc handled above).
      if (pdfActive) {
        const tool: PdfEditMode | null =
          e.code === 'KeyV'
            ? 'none'
            : e.code === 'KeyH'
              ? 'highlight'
              : e.code === 'KeyT'
                ? 'freetext'
                : e.code === 'KeyD'
                  ? 'ink'
                  : null
        if (tool) {
          const handle = getPdfHandle()
          if (handle) {
            e.preventDefault()
            handle.setEditMode(tool)
            return
          }
        }
      }
    }

    document.addEventListener('keydown', onKeyDown)
    return () => document.removeEventListener('keydown', onKeyDown)
  }, [
    anyModalOpen,
    toggleFocus,
    toggleDark,
    toggleLeft,
    toggleRight,
    activateAdjacentTab,
    activateTabByIndex,
    cheatSheetOpen,
    toggleCheatSheet,
    closeCheatSheet,
    pdfActive,
    getPdfHandle,
    selectedId,
    cockpit,
    notify,
  ])
}
