import { useEffect, useRef } from 'react'

/** One Escape authority for every dismissible layer in the app.
 *
 * Before this module, four unrelated mechanisms decided who owned Escape: each
 * panel hand-enumerated its nested dialogs (`!pendingDelete && !pendingRename
 * && …`), two overlays bound their own capture-phase listener, ~25 cards relied
 * on being focused so their inline `onKeyDown` would hear the key, and the
 * global dispatcher swallowed the key on their behalf off a separately
 * maintained `modalCardOpen` flag. Three of those four required whoever adds
 * the next dialog to remember to do something, and forgetting produced two
 * different bugs: one Escape closing two levels, or the key leaking to the host
 * (on macOS an unconsumed Escape is what AppKit reads as "leave fullscreen").
 *
 * Here instead: layers register themselves, and a single window-CAPTURE
 * listener hands Escape to the top of the stack. Capture is the first stop in
 * the propagation path, so this decision happens once, before any React
 * synthetic handler and before the `document`-level shortcut dispatcher — it no
 * longer depends on where focus is or on who called stopPropagation first.
 *
 * Two invariants make the rest of the app keep working:
 *
 *   * With the stack EMPTY the listener does nothing at all — no
 *     preventDefault, no stopPropagation. Escape then flows on exactly as it
 *     used to, which is what leaves the search box's clear-the-query Escape,
 *     the dispatcher's drop-the-PDF-tool Escape, and pdf.js's own Escape alive.
 *     Those three are focus- and mode-dependent, so they are deliberately NOT
 *     layers.
 *   * With the stack NON-EMPTY the key is always consumed, even when the top
 *     layer declines to act (a dialog mid-write passes a null handler). A
 *     dialog on screen owns Escape whether or not it is ready to close.
 *
 * 🔴 Ordering constraint: the stack is ordered by mount, and React runs effects
 * child-first, so a layer must NOT mount in the same commit as the layer it
 * sits on top of — the two would register in the wrong order. Every nesting in
 * this app opens in response to a click inside its parent, which is a later
 * commit, so this holds; it stops holding the moment some state starts out with
 * two levels already open.
 */

type Layer = { onEscape: (() => void) | null }

const stack: Layer[] = []

function onKeyDown(e: KeyboardEvent) {
  if (e.key !== 'Escape') return
  // Mid-composition the key belongs to the input method: it cancels the
  // half-typed characters, not the dialog around them.
  if (e.isComposing) return
  const top = stack[stack.length - 1]
  if (!top) return
  e.preventDefault()
  e.stopPropagation()
  top.onEscape?.()
}

/** Bound while at least one layer is registered, so an app with no dialog open
 * has no listener of ours in the path at all. */
function sync() {
  if (stack.length > 0) {
    window.addEventListener('keydown', onKeyDown, true)
  } else {
    window.removeEventListener('keydown', onKeyDown, true)
  }
}

/** Claim Escape while `mounted`, ordered above any layer already registered.
 *
 * Pass `null` for `onEscape` to hold the layer without acting on the key — the
 * shape for a dialog that is mid-write and must not be dismissed, which still
 * has to stop Escape from reaching the layer underneath it.
 */
export function useEscapeLayer(
  mounted: boolean,
  onEscape: (() => void) | null,
): void {
  // The registered object is stable for the layer's lifetime while its handler
  // is refreshed every render, so a re-render never re-orders the stack.
  const layer = useRef<Layer>({ onEscape })
  layer.current.onEscape = onEscape

  useEffect(() => {
    if (!mounted) return
    const self = layer.current
    stack.push(self)
    sync()
    return () => {
      const i = stack.lastIndexOf(self)
      if (i !== -1) stack.splice(i, 1)
      sync()
    }
  }, [mounted])
}
