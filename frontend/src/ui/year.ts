/** The year shape the GUI's two write paths agree on.
 *
 * They used to disagree, which is how a library ends up holding both `145`
 * and `12311`: the add card asked for three-or-four digits, the metadata
 * editor asked for nothing at all and left the judgement to the backend —
 * which only requires an integer, because `lit modify` is the escape hatch
 * for records the guided path cannot express and has no business inventing a
 * calendar. One rule, one copy, so the two forms cannot drift again.
 *
 * Four digits and not a range: a plausible-year check would have to guess
 * where the future ends (preprints carry next year's date routinely) and
 * where the past begins, and being wrong about either costs more than the
 * typo it would catch. Length is the part that is never a matter of opinion.
 */
export const YEAR_HINT = '4 digits'

export function isYearShape(raw: string): boolean {
  return /^\d{4}$/.test(raw.trim())
}
