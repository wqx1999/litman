/** The two halves of this app's dialog-dismissal contract.
 *
 * The rule, applied without exceptions: a click outside a dialog does NOT
 * close it. Dismissal is an explicit instruction — Esc, Done, Cancel, ✕ — and
 * a stray click is not one. The cost of getting this wrong is asymmetric: the
 * click-to-close someone meant costs them one keystroke, while the one they
 * did not costs a half-typed metadata edit, or (in the Add-paper dialog) the
 * uploaded PDF too, which means dragging the file in again.
 *
 * It is deliberately uniform rather than "only dialogs with a text field":
 * a rule you can state in one sentence is one people can predict, and
 * "which dialogs close when I click away?" is not a game worth playing.
 *
 * The price of taking the click away is that every OTHER exit has to be real,
 * which is why both halves live in one file: `nudgeOnBackdropClick` removes
 * the mouse exit, `useModalCardFocus` makes sure the keyboard one works.
 */

import { useEffect, useRef } from 'react'

/** Wobble the dialog instead of closing it. Wire to the backdrop's `onClick`.
 *
 * Silence would read as a frozen app, so the card moves — the `.modal-nudge`
 * rule in index.css, which animates the backdrop's CHILDREN so adopting this
 * costs each dialog one attribute and no ref of its own. */
export function nudgeOnBackdropClick(e: React.MouseEvent<HTMLElement>): void {
  // Only a click on the backdrop ITSELF — clicks inside the card bubble up
  // here with a different target and must be left entirely alone. (This is
  // also what makes nested dialogs safe: the outer backdrop sees the inner
  // one as the target and correctly does nothing.)
  if (e.target !== e.currentTarget) return
  const el = e.currentTarget
  el.classList.remove('modal-nudge')
  // Force a reflow so a second impatient click replays the animation instead
  // of doing nothing (the class would otherwise never leave the element).
  void el.offsetWidth
  el.classList.add('modal-nudge')
}

/** Everything a dialog backdrop needs: `<div {...modalBackdropProps}>`.
 *
 * 🔴 The `onMouseDown` is not optional decoration. Pressing on the backdrop
 * blurs whatever was focused inside the card, and focus is what Escape rides
 * on — so without it, the exact gesture that used to close the dialog would
 * instead take away its LAST remaining exit, trapping a person in a dialog
 * with their half-typed edit. Caught by E2E, invisible to a mouse-only pass.
 * `preventDefault` on mousedown suppresses the focus change and nothing else;
 * the click event still fires, so the nudge still plays.
 */
export const modalBackdropProps = {
  onMouseDown: (e: React.MouseEvent<HTMLElement>) => {
    if (e.target === e.currentTarget) e.preventDefault()
  },
  onClick: nudgeOnBackdropClick,
}

/** Focus a dialog card on mount so its own `onKeyDown` can hear Escape.
 *
 * Spread onto the card element: `<div {...useModalCardFocus()} onKeyDown={…}>`.
 *
 * 🔴 Writing `onKeyDown={e => e.key === 'Escape' && onClose()}` on a card does
 * NOT mean Escape closes it. React key events start at the focused element, so
 * a card whose dialog was opened by clicking a toolbar button — focus still on
 * that button, outside the card — never receives one. Blocking dialogs (the
 * ones counted in `anyModalOpen`) are the dangerous case: they silence the
 * global shortcut dispatcher, so the card's handler is the ONLY listener, and
 * the dialog ends up with no keyboard exit at all. That is invisible to any
 * test that drives the UI with a mouse, and this app has shipped it twice.
 *
 * A dialog whose natural first action is typing should focus its input instead
 * (see AddPaper) — same goal, better landing spot. This hook is for panels and
 * confirms with no obvious first field: `tabIndex={-1}` makes the card itself
 * focusable without adding it to the tab order.
 */
export function useModalCardFocus<T extends HTMLElement = HTMLDivElement>(): {
  ref: React.RefObject<T | null>
  tabIndex: number
} {
  const ref = useRef<T>(null)
  useEffect(() => {
    ref.current?.focus()
  }, [])
  return { ref, tabIndex: -1 }
}
