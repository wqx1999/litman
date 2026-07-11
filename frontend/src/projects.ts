import type { ProjectStatus } from './types'

/** What a project's `status` means to the user, and how loudly to say it.
 *
 * `GET /api/projects` has always reported this — the same JOIN of TAXONOMY.md's
 * projects section and lit-config.yaml's projects map that backs `lit project
 * list` — and the GUI has always thrown it away, listing a project whose folder
 * is gone exactly like a healthy one. This is the one place that decides what
 * each state looks like, so the manager and the paper's project picker cannot
 * drift apart in what they claim.
 *
 * `ok` returns null: a healthy project wears no marker.
 */
export type ProjectHealth = {
  /** `missing` is fixable from the GUI (re-point it); `incomplete` is not. */
  tone: 'missing' | 'incomplete'
  badge: string
  title: string
}

export function projectHealth(status: ProjectStatus): ProjectHealth | null {
  switch (status) {
    case 'path-missing':
      // The common one, and the only one a user reaches by accident: the folder
      // was moved, renamed or deleted behind litman's back. The manager row's
      // re-point button fixes it in place, which is why this marker sits beside
      // it rather than in a warning the user has to remember to act on.
      return {
        tone: 'missing',
        badge: 'missing',
        title:
          'No folder at this path — it was moved, renamed or deleted. ' +
          'Re-point the project at its new location.',
      }
    // The two halves of a split registration. A project lives in two truth
    // sources at once (invariant #2 writes them together, so neither state can
    // arise from litman's own writes) — but lit-config.yaml has no `lit config
    // set`, hand-editing is its only mutation path, so both are reachable. The
    // titles name the file the project is absent from, because that is where the
    // fix has to happen: re-pointing cannot repair either one.
    case 'config-only':
      return {
        tone: 'incomplete',
        badge: 'incomplete',
        title:
          'Has a folder in lit-config.yaml but is not listed in TAXONOMY.md. ' +
          'Papers cannot be tagged with it until it is.',
      }
    case 'taxonomy-only':
      return {
        tone: 'incomplete',
        badge: 'incomplete',
        title:
          'Listed in TAXONOMY.md but has no folder in lit-config.yaml. ' +
          'It points nowhere until one is set.',
      }
    default:
      return null
  }
}
