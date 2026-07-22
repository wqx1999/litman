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
  /** Re-read the current vault from disk through the same path as the TopBar
   * refresh button. */
  refreshFromDisk: () => void

  // --- Tier 1: center tab switching (global, focus-guarded) ---------------
  /** Cycle the active center document tab. delta +1 = next, -1 = previous;
   * wraps around; no-op with fewer than two tabs. */
  activateAdjacentTab: (delta: 1 | -1) => void
  /** Jump straight to the Nth center tab (1-based). Out-of-range = no-op. */
  activateTabByIndex: (n: number) => void

  // --- Tier 1: middle-list navigation (global, focus-guarded) -------------
  /** Move the paper-list selection down/up by one (J/K). Clamped at the ends;
   * J with nothing selected starts at the first row, K at the last. */
  moveSelection: (delta: 1 | -1) => void
  /** Open the selected paper's PDF tab (Enter). No-op without a selection. */
  openSelected: () => void

  // --- Tier 1: agent launch (global, focus-guarded) -----------------------
  /** Open the AI agent: launch it in a terminal, or raise the onboarding panel
   * when it is not configured yet — TopBar's button owns that branch and the
   * shortcut reuses the same handler, so the two can never disagree. Null until
   * TopBar registers its handle (the welcome page renders no TopBar), in which
   * case the key is inert. */
  openAgent: (() => void) | null
  /** Open agent management without launching. Bound to Ctrl+Backquote; plain
   * Backquote remains the one-key default-agent launch path. */
  manageAgents: (() => void) | null

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
 * <textarea>, and any contentEditable host. Exported for the SearchBox's own
 * bare-key listener (`/`), which needs the identical guard. */
export function isEditingTarget(el: EventTarget | null): boolean {
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
  // Except Ctrl+Backquote (handled before this helper for agent management),
  // Cmd/Ctrl combos belong to the browser / existing Tier-0 listeners:
  // PdfView's ⌘S + ⌘-zoom and the search box's ⌘K.
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
    refreshFromDisk,
    activateAdjacentTab,
    activateTabByIndex,
    moveSelection,
    openSelected,
    openAgent,
    manageAgents,
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
      // Modal + focus guards apply to both plain launch and Ctrl+~ management.
      // Handle the one app-owned Ctrl chord before the general reserved-combo
      // return; all other Cmd/Ctrl combinations remain untouched.
      if (anyModalOpen) return
      const editing = isEditingTarget(e.target)
      if (
        !editing &&
        e.ctrlKey &&
        !e.metaKey &&
        !e.altKey &&
        e.code === 'Backquote'
      ) {
        e.preventDefault()
        manageAgents?.()
        return
      }

      // Let the app's own Tier-0 Cmd/Ctrl listeners (or the browser) handle any
      // modifier combo — never handle or preventDefault it here.
      if (isReservedModifierCombo(e)) return

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
            notify('No paper selected')
            return
          }
          action(cockpit)
          return
        }
        // An unmapped ⌥-combo (e.g. ⌥X): leave it to the browser.
        return
      }

      // --- Backquote — launch the agent (Shift optional) --------------------
      // The key left of "1": the universal "summon a console" convention
      // (Quake, VS Code, …), which is what launching the agent is.
      //
      // Accepted WITH OR WITHOUT Shift, so it sits above the Tier-1 modifier
      // reject. The keycap is engraved `~` over `` ` `` and readers scan for
      // the tilde, so the cheat sheet shows `~`; were Shift rejected, anyone
      // following that label would press Shift+` and get silence. Precedent:
      // `?` is itself Shift+/ and is handled the same way, above. Ctrl/Cmd are
      // Ctrl+Backquote is claimed above for management; Cmd is reserved and
      // Alt is handled by the Tier-2 block, so only Shift can still be set here.
      //
      // Matched on e.code (physical position), so a layout printing another
      // glyph on that key still fires.
      if (e.code === 'Backquote') {
        e.preventDefault()
        openAgent?.()
        return
      }

      // --- Tier 1: bare single keys (no modifiers) -------------------------
      // Reject any modifier so e.g. Ctrl+F (page find) is never swallowed.
      if (e.metaKey || e.ctrlKey || e.altKey || e.shiftKey) return

      // --- Enter — open the selected paper's PDF ---------------------------
      // Only on inert targets: a focused button / link / select keeps its
      // native Enter activation (this would otherwise double-fire — e.g.
      // activate the control AND open a tab).
      if (e.key === 'Enter') {
        const t = e.target
        if (
          t instanceof HTMLElement &&
          ['BUTTON', 'A', 'SELECT', 'SUMMARY'].includes(t.tagName)
        ) {
          return
        }
        e.preventDefault()
        openSelected()
        return
      }

      switch (e.code) {
        case 'KeyJ':
          e.preventDefault()
          moveSelection(1)
          return
        case 'KeyK':
          e.preventDefault()
          moveSelection(-1)
          return
        case 'KeyF':
          e.preventDefault()
          toggleFocus()
          return
        case 'KeyL':
          e.preventDefault()
          toggleDark()
          return
        case 'KeyR':
          e.preventDefault()
          refreshFromDisk()
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
    refreshFromDisk,
    activateAdjacentTab,
    activateTabByIndex,
    moveSelection,
    openSelected,
    openAgent,
    manageAgents,
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
