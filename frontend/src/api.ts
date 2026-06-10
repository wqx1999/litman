// Typed fetch helpers over the read API. All URLs are same-origin relative, so
// they resolve against whatever host:port `lit gui` bound to.

import type {
  IndexPaper,
  PaperMeta,
  ProjectEntry,
  SmartListView,
  Taxonomy,
  VaultsPayload,
} from './types'

async function getJSON<T>(url: string): Promise<T> {
  const resp = await fetch(url)
  if (!resp.ok) {
    throw new Error(`${url} → ${resp.status} ${resp.statusText}`)
  }
  return (await resp.json()) as T
}

/** All papers (INDEX projection), or a recency-ordered smart-list when `view`. */
export function fetchPapers(view?: SmartListView): Promise<IndexPaper[]> {
  const qs = view ? `?view=${encodeURIComponent(view)}` : ''
  return getJSON<IndexPaper[]>(`/api/papers${qs}`)
}

export function fetchPaper(id: string): Promise<PaperMeta> {
  return getJSON<PaperMeta>(`/api/paper/${encodeURIComponent(id)}`)
}

/** The URL pdf.js fetches (with HTTP range) — not the bytes. */
export function pdfUrl(id: string): string {
  return `/api/paper/${encodeURIComponent(id)}/pdf`
}

async function fetchMdText(id: string, doc: 'notes' | 'discussion'): Promise<string | null> {
  const resp = await fetch(`/api/paper/${encodeURIComponent(id)}/${doc}`)
  if (resp.status === 404) return null
  if (!resp.ok) throw new Error(`notes/${doc} → ${resp.status}`)
  const body = (await resp.json()) as { text: string }
  return body.text
}

export function fetchNotes(id: string): Promise<string | null> {
  return fetchMdText(id, 'notes')
}

export function fetchDiscussion(id: string): Promise<string | null> {
  return fetchMdText(id, 'discussion')
}

export function fetchTaxonomy(): Promise<Taxonomy> {
  return getJSON<Taxonomy>('/api/taxonomy')
}

export function fetchProjects(): Promise<ProjectEntry[]> {
  return getJSON<ProjectEntry[]>('/api/projects')
}

export function fetchVaults(): Promise<VaultsPayload> {
  return getJSON<VaultsPayload>('/api/vaults')
}
