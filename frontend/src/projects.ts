import { getT } from './i18n'
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
  /** A paper not linked yet can be linked to it from its project picker. */
  linkable: boolean
  badge: string
  title: string
}

export function projectHealth(status: ProjectStatus): ProjectHealth | null {
  const t = getT().browse.projectHealth
  switch (status) {
    case 'path-missing':
      // The common one, and the only one a user reaches by accident: the folder
      // was moved, renamed or deleted behind litman's back. The manager row's
      // re-point button fixes it in place, which is why this marker sits beside
      // it rather than in a warning the user has to remember to act on.
      return { tone: 'missing', linkable: false, ...t.missing }
    // The two halves of a split registration. A project lives in two truth
    // sources at once (invariant #2 writes them together on one machine). Two-
    // way sync makes taxonomy-only the normal state of a project added on
    // another computer: it carries TAXONOMY.md, never lit-config.yaml, and
    // "Change path" gives it a folder here. Config-only comes from a hand edit
    // of lit-config.yaml. Taxonomy-only takes links from the picker: linking
    // writes the paper's record alone, and the computer that has the folder
    // builds the link at its next sync. Config-only keeps its badge there, as
    // a link would write a project value TAXONOMY.md does not list (`lit link`
    // still links it in full). Neither title claims tagging is blocked.
    case 'config-only':
      return { tone: 'incomplete', linkable: false, ...t.configOnly }
    case 'taxonomy-only':
      return { tone: 'incomplete', linkable: true, ...t.taxonomyOnly }
    default:
      return null
  }
}
