// Ranking + merge for the 4-scope search typeahead. Pure (no React, no JSX) so
// it feeds BOTH the dropdown (sliced to the top few) and the middle-list filter
// (the full matched id set). id/title are matched here instantly off the loaded
// INDEX; notes/discussion hits arrive async from /api/search and merge in.

import type { IndexPaper, SearchHit, SearchScope } from './types'

/** Max rows the dropdown renders; the remainder collapse into a "+N more" row. */
export const DROPDOWN_LIMIT = 5

/** One ranked typeahead candidate — at most one per paper (its best scope). */
export interface Candidate {
  id: string
  title: string | null
  scope: SearchScope
  /** Lower is better: 0 id-exact · 1 id/title-prefix · 2 id/title-substring ·
   * 3 notes-substring · 4 discussion-substring. */
  rank: number
  /** Matched markdown line (notes/discussion scopes); '' for id/title. */
  snippet: string
  /** 1-based line of the match in the .md (notes/discussion scopes only), so the
   * picker can open that doc and scroll to it. Absent for id/title. */
  line?: number
}

// Secondary sort within a rank tier: prefer id, then title, then notes, then
// discussion, so an id-prefix edges out a title-prefix at the same rank.
const SCOPE_ORDER: Record<SearchScope, number> = {
  id: 0,
  title: 1,
  notes: 2,
  discussion: 3,
}

/** Classify a client-side id/title match. Mirrors the spec ordering: id exact >
 * id/title prefix > id/title substring. Returns null when neither field hits. */
function clientMatch(
  id: string,
  title: string | null,
  q: string,
): { rank: number; scope: SearchScope } | null {
  const lid = id.toLowerCase()
  const lt = (title ?? '').toLowerCase()
  if (lid === q) return { rank: 0, scope: 'id' }
  if (lid.startsWith(q)) return { rank: 1, scope: 'id' }
  if (lt && lt.startsWith(q)) return { rank: 1, scope: 'title' }
  if (lt && lt.includes(q)) return { rank: 2, scope: 'title' }
  if (lid.includes(q)) return { rank: 2, scope: 'id' }
  return null
}

/** Merge instant id/title matches (over the full INDEX) with async server
 * notes/discussion hits into one ranked, de-duplicated list — one entry per
 * paper, keeping its highest-ranked scope. Empty/whitespace query → []. */
export function mergeCandidates(
  papers: IndexPaper[],
  serverHits: SearchHit[],
  query: string,
): Candidate[] {
  const q = query.trim().toLowerCase()
  if (!q) return []

  const titleById = new Map<string, string | null>()
  for (const p of papers) titleById.set(p.id, p.title)

  const byId = new Map<string, Candidate>()
  const consider = (c: Candidate) => {
    const cur = byId.get(c.id)
    if (!cur || c.rank < cur.rank) byId.set(c.id, c)
  }

  for (const p of papers) {
    const m = clientMatch(p.id, p.title, q)
    if (m) {
      consider({ id: p.id, title: p.title, scope: m.scope, rank: m.rank, snippet: '' })
    }
  }
  for (const h of serverHits) {
    consider({
      id: h.id,
      title: titleById.get(h.id) ?? null,
      scope: h.scope,
      rank: h.scope === 'notes' ? 3 : 4,
      snippet: h.snippet,
      line: h.line,
    })
  }

  return [...byId.values()].sort(
    (a, b) =>
      a.rank - b.rank ||
      SCOPE_ORDER[a.scope] - SCOPE_ORDER[b.scope] ||
      a.id.localeCompare(b.id),
  )
}
