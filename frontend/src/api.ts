// Typed fetch helpers over the read API. All URLs are same-origin relative, so
// they resolve against whatever host:port `lit gui` bound to.

import type {
  IndexPaper,
  PaperMeta,
  ProjectEntry,
  SearchPayload,
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

/** A compact ACS-style citation plus any caveats (unverified journal
 * abbreviation, missing volume/pages, preprint venue). `text` is paste-clean;
 * `warnings` are shown beside the Cite button, never folded into `text`. */
export interface CitePayload {
  text: string
  warnings: string[]
}

export function fetchCite(id: string): Promise<CitePayload> {
  return getJSON<CitePayload>(`/api/paper/${encodeURIComponent(id)}/cite`)
}

/** Vault-wide notes/discussion substring search (the typeahead's async scopes;
 * id/title are matched client-side). Returns one hit per paper, notes-first. */
export function fetchSearch(q: string): Promise<SearchPayload> {
  return getJSON<SearchPayload>(`/api/search?q=${encodeURIComponent(q)}`)
}

/** The URL pdf.js fetches (with HTTP range) — not the bytes. */
export function pdfUrl(id: string): string {
  return `/api/paper/${encodeURIComponent(id)}/pdf`
}

/** Overwrite paper.pdf with annotated bytes (pdf.js `saveDocument()` output).
 *
 * Invariant #16 whitelist direct-write: the server atomically replaces
 * `papers/{id}/paper.pdf` via staged_write. Called once on flush (tab
 * close/switch), never per annotation. Throws on a non-ok response so a
 * fire-and-forget caller's `.then` chain rejects rather than silently dropping
 * a failed save.
 */
export async function putPdfAnnotations(id: string, bytes: Uint8Array): Promise<void> {
  // Send the bytes' underlying buffer slice as the body. fetch accepts an
  // ArrayBuffer as BodyInit and sends it verbatim; slicing by byteOffset/Length
  // copies exactly the view's region (saveDocument hands back a tight view, but
  // slicing is correct regardless of any offset).
  const buffer = bytes.buffer.slice(
    bytes.byteOffset,
    bytes.byteOffset + bytes.byteLength,
  ) as ArrayBuffer
  const resp = await fetch(`/api/paper/${encodeURIComponent(id)}/pdf-annotations`, {
    method: 'PUT',
    body: buffer,
    headers: { 'Content-Type': 'application/pdf' },
  })
  if (!resp.ok) {
    throw new Error(`PUT pdf-annotations/${id} → ${resp.status} ${resp.statusText}`)
  }
}

/** Overwrite a paper's notes.md / discussion.md with the full edited text.
 *
 * Invariant #16 whitelist direct-write (symmetric with the GET read endpoints,
 * which return `{text}`): the server atomically replaces the file via
 * staged_write. Both are whole-file overwrites (the md tab edits the entire
 * file, not a patch); the server re-inserts the wikilink reminder on notes only.
 * Throws on a non-ok response so the caller can surface a failed save instead of
 * silently returning to render mode having lost the edit.
 */
async function putMdText(
  id: string,
  doc: 'notes' | 'discussion',
  text: string,
): Promise<void> {
  const resp = await fetch(`/api/paper/${encodeURIComponent(id)}/${doc}`, {
    method: 'PUT',
    body: JSON.stringify({ text }),
    headers: { 'Content-Type': 'application/json' },
  })
  if (!resp.ok) {
    throw new Error(`PUT ${doc}/${id} → ${resp.status} ${resp.statusText}`)
  }
}

export function putNotes(id: string, text: string): Promise<void> {
  return putMdText(id, 'notes', text)
}

export function putDiscussion(id: string, text: string): Promise<void> {
  return putMdText(id, 'discussion', text)
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
