const UNITS = ['KB', 'MB', 'GB', 'TB']

/** A byte count the way the SI window and the cockpit show it: `7.6 MB`,
 * `350 MB`. Binary steps (1024), like the CLI's sizes; one decimal below 10,
 * where it still says something.
 *
 * Rounded before the unit is chosen, so a value that rounds up to the next
 * step reads in that step: 1023.6 KB is `1.0 MB`, never `1024 KB`, and
 * 9.96 MB is `10 MB`, never `10.0 MB`. */
export function formatBytes(n: number): string {
  if (n < 1024) return `${n} B`
  let value = n / 1024
  for (let i = 0; ; i++) {
    const shown = value < 9.95 ? Math.round(value * 10) / 10 : Math.round(value)
    if (shown < 1024 || i === UNITS.length - 1) {
      return `${shown < 10 ? shown.toFixed(1) : shown} ${UNITS[i]}`
    }
    value /= 1024
  }
}
