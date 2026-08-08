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
 * A backdrop click gets no feedback at all — no wobble, no flash. The dialog
 * simply stays, which is the whole message. Anything louder than that reads
 * as a reprimand, and the click it would be scolding is usually not even a
 * click on purpose: releasing a text selection past the card's edge lands on
 * the backdrop too, and being buzzed at for that says "you used this wrong"
 * about a gesture that was fine.
 *
 * The price of taking the click away is that every OTHER exit has to be real,
 * which is why both halves live in one file: `modalBackdropProps` removes the
 * mouse exit, `useModalCardFocus` makes sure the keyboard one works.
 */

import { useEffect, useRef } from 'react'

/** Everything a dialog backdrop needs: `<div {...modalBackdropProps}>`.
 *
 * 🔴 The `onMouseDown` is not optional decoration, and it is the only thing
 * here. Pressing on the backdrop blurs whatever was focused inside the card,
 * and focus is what Escape rides on — so without it, the exact gesture that
 * used to close the dialog would instead take away its LAST remaining exit,
 * trapping a person in a dialog with their half-typed edit. Caught by E2E,
 * invisible to a mouse-only pass. `preventDefault` on mousedown suppresses
 * the focus change and nothing else.
 *
 * The guard matters for nested dialogs: an inner backdrop's mousedown bubbles
 * to the outer one, where the target is the inner backdrop rather than the
 * outer's own element, so the outer correctly leaves it alone.
 */
export const modalBackdropProps = {
  onMouseDown: (e: React.MouseEvent<HTMLElement>) => {
    if (e.target === e.currentTarget) e.preventDefault()
  },
}

/** Keep a dialog card focused, so the keyboard lands inside it.
 *
 * Spread onto the card element: `<div {...useModalCardFocus()}>`. A card that
 * opens nested dialogs passes whether one is showing —
 * `useModalCardFocus(childOpen)` — and gets re-focused when the last one goes.
 *
 * ⚠️ Escape no longer depends on this. Dialogs claim Escape through the layer
 * stack (ui/escapeStack), which listens on the window and does not care where
 * focus is. What this hook is still for is the rest of the keyboard: Tab order
 * starting inside the card rather than back at the page behind it, and screen
 * readers landing on the dialog they just opened.
 *
 * 🔴 It used to be the thing Escape rode on, and getting it wrong cost this app
 * three shipped bugs — a card whose dialog was opened from a toolbar button
 * never saw the key (focus was still on the button), and closing a nested dialog
 * dropped focus to <body>, killing the outer dialog's only keyboard exit. Kept
 * here as the reason not to "simplify" the childOpen re-focus away: the failure
 * mode is invisible to any test that drives the UI with a mouse.
 *
 * A dialog whose natural first action is typing should focus its input instead
 * (see AddPaper) — same goal, better landing spot. This hook is for panels and
 * confirms with no obvious first field: `tabIndex={-1}` makes the card itself
 * focusable without adding it to the tab order.
 */
export function useModalCardFocus<T extends HTMLElement = HTMLDivElement>(
  childOpen = false,
): {
  ref: React.RefObject<T | null>
  tabIndex: number
} {
  const ref = useRef<T>(null)
  // Fires on mount (nothing nested is open yet) and on every later edge back to
  // "nothing nested is open" — never while a nested dialog holds the focus it
  // wants. Callers that pass nothing keep the original mount-only behaviour. If
  // the card is unmounting alongside its child, the ref is null and this is a
  // no-op.
  useEffect(() => {
    if (!childOpen) ref.current?.focus()
  }, [childOpen])
  return { ref, tabIndex: -1 }
}
