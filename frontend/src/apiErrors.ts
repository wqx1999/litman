import { ApiError, OfflineError, logPageFailure } from './api'
import { getT } from './i18n'
import type { ErrorParams } from './i18n/en/errors'

/** A table of lines by the server's code (i18n `errors.*`). */
export type CodeLines = Readonly<Partial<Record<string, (p: ErrorParams) => string>>>

/** The one line the page shows for a failure, or null when it shows none.
 *
 * The person sees what happened in a few words; the agent reads the rest
 * from the GUI log (`lit gui-log`), where the server wrote the full text.
 *
 * - A failure the server named (`ApiError.code`) gets that code's line: the
 *   call site's own table (`lines`) first, then the codes any request can
 *   meet (a sync holding the library, a file that cannot be read, a paper
 *   that is gone).
 * - Any other answer gets the failed action's own line (`fixed`).
 * - No answer at all: the action's line on a write, which says what did not
 *   happen (the banner already says the server is unreachable), otherwise
 *   "can't reach the server".
 * - A 410 shows nothing: the red banner says the library is gone.
 * - A failure of the page's own code gets the action's line too, and is
 *   posted to the GUI log, since the server never saw it.
 */
export function errorLine(
  err: unknown,
  fixed?: string | null,
  lines?: CodeLines,
): string | null {
  const t = getT().errors
  if (err instanceof DOMException && err.name === 'AbortError') return null
  if (err instanceof ApiError) {
    if (err.status === 410) return null
    const code = err.code
    if (code) {
      const line = lines?.[code] ?? (t.codes as CodeLines)[code]
      if (line) return line(err.params)
    }
    return fixed ?? t.actionFailed
  }
  if (err instanceof OfflineError) return fixed ?? t.offline
  logPageFailure('page code', describe(err))
  return fixed ?? t.actionFailed
}

/** What a failure of the page's own code was, for the GUI log. */
export function describe(err: unknown): string {
  if (err instanceof Error) return err.stack || `${err.name}: ${err.message}`
  return String(err)
}
