/** Per-tab "previous view" history for the PDF reader (Acrobat's Alt+←).
 *
 * Following a citation link is a one-way trip in pdf.js: the annotation layer
 * calls `linkService.goToDestination()` and nothing remembers where you were.
 * PdfView records the pre-jump position here before every in-document jump, and
 * these two stacks give it browser-style back / forward over those positions.
 *
 * Pure data — no pdf.js, no React, no DOM. PdfView owns every side effect
 * (reading the live scroll offset, re-applying a restored one); this module only
 * decides which position is next.
 *
 * The stacks live in a module-level Map keyed by tab, NOT in a component ref:
 * TabArea mounts only the active tab, so switching away unmounts PdfView. A
 * component-scoped stack would silently drop the trail on "jump to the
 * references → go read your notes → come back". Same lifetime and same reasoning
 * as PdfView's `viewPositions` map: session-only, cleared by a full reload.
 */

/** A place in the document: the scroll offset in SCALED pixels, the zoom it was
 * measured at (the offset is meaningless without it), and the page number that
 * offset was showing (used for the pill's "Back to p.7" label). */
export interface ViewPos {
  scrollTop: number
  scale: number
  page: number
}

export interface ViewHistory {
  back: ViewPos[]
  forward: ViewPos[]
}

const histories = new Map<string, ViewHistory>()

// Deep enough that a real reading session never hits it, small enough that a
// day-long session can't grow unbounded. Oldest entries are dropped first.
const MAX_DEPTH = 50

/** Two entries describe "the same place" when the viewport barely moved. A few
 * pixels of slack absorbs rounding in the scaled offsets; the zoom must match
 * exactly, since the same offset at a different scale is a different place. */
function samePlace(a: ViewPos, b: ViewPos): boolean {
  return a.scale === b.scale && Math.abs(a.scrollTop - b.scrollTop) <= 4
}

function pushCapped(stack: ViewPos[], pos: ViewPos): void {
  stack.push(pos)
  if (stack.length > MAX_DEPTH) stack.splice(0, stack.length - MAX_DEPTH)
}

/** Index of the newest entry that is somewhere OTHER than `current`, or -1.
 *
 * The one answer to "where would a step actually take you", shared by the
 * steppers and by the peek the pill is drawn from — so the pill can never
 * advertise a destination the key would decline. Entries above the answer
 * describe where you already are: that is the broken-destination case (a
 * dangling link records a position and then fails to move the viewer), and
 * skipping them is what stops the key from looking dead. */
function resolveTarget(stack: ViewPos[], current: ViewPos): number {
  for (let i = stack.length - 1; i >= 0; i--) {
    if (!samePlace(stack[i], current)) return i
  }
  return -1
}

/** Pop to the newest reachable entry of `from`, pushing `current` onto `to`.
 * Anything skipped on the way (it describes `current`) is discarded with it. */
function step(from: ViewPos[], to: ViewPos[], current: ViewPos): ViewPos | null {
  const i = resolveTarget(from, current)
  if (i < 0) {
    // Every remaining entry is this same spot: there is nothing to undo, and
    // keeping them would only make the next press look dead too.
    from.length = 0
    return null
  }
  const target = from[i]
  from.length = i
  pushCapped(to, current)
  return target
}

/** This tab's stacks, created on first use. A view with no tab key (the trash
 * preview) gets a fresh detached history each call, so the caller must hold on
 * to the one it got — it is usable for the life of the mount, which is all a
 * read-only preview needs. */
export function historyFor(tabKey: string | undefined): ViewHistory {
  if (!tabKey) return { back: [], forward: [] }
  let h = histories.get(tabKey)
  if (!h) {
    h = { back: [], forward: [] }
    histories.set(tabKey, h)
  }
  return h
}

/** Record the position a document-internal jump is leaving from. Clears the
 * forward stack, exactly like a browser navigation: once you branch off, the
 * old forward trail leads to views you can no longer reach by going back. */
export function recordJump(h: ViewHistory, from: ViewPos): void {
  pushCapped(h.back, from)
  h.forward.length = 0
}

/** Go back one view, pushing `current` onto the forward stack. Null when there
 * is nothing to go back to — and then `current` is NOT pushed forward, because
 * no navigation happened to undo. */
export function stepBack(h: ViewHistory, current: ViewPos): ViewPos | null {
  return step(h.back, h.forward, current)
}

/** Symmetric redo. Note it must NOT clear the forward stack — only a fresh jump
 * (recordJump) does that. */
export function stepForward(h: ViewHistory, current: ViewPos): ViewPos | null {
  return step(h.forward, h.back, current)
}

/** Exactly where `stepBack` would land, without touching the stacks — including
 * its skip of entries that describe `current`. Drives the pill: null means the
 * key would do nothing, so no pill is offered. */
export function peekBack(h: ViewHistory, current: ViewPos): ViewPos | null {
  const i = resolveTarget(h.back, current)
  return i < 0 ? null : h.back[i]
}
