// Typed fetch helpers over the read API. All URLs are same-origin relative, so
// they resolve against whatever host:port `lit gui` bound to.

import type {
  FixedEnums,
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

/** A mutating JSON request that surfaces the server's `detail` on failure.
 *
 * Unlike `getJSON` (which throws a generic status-line Error), this parses the
 * FastAPI error body's `detail` and throws `new Error(detail)` so the cockpit
 * can toast the backend's real message verbatim — e.g. a ModifyError like
 * "Refusing to write: a revisit presupposes a first read" or
 * "Invalid status 'bogus'. Allowed values: …". Falls back to the status text
 * when the body has no parseable `detail`. Returns the parsed JSON response.
 */
async function mutateJSON<T>(
  url: string,
  method: 'PUT' | 'POST',
  body?: unknown,
): Promise<T> {
  const resp = await fetch(url, {
    method,
    body: body === undefined ? undefined : JSON.stringify(body),
    headers: body === undefined ? undefined : { 'Content-Type': 'application/json' },
  })
  if (!resp.ok) {
    let detail = `${resp.status} ${resp.statusText}`
    try {
      const parsed = (await resp.json()) as { detail?: unknown }
      if (typeof parsed.detail === 'string' && parsed.detail) detail = parsed.detail
    } catch {
      /* non-JSON error body — keep the status-line fallback */
    }
    throw new Error(detail)
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

/** The status/priority/type whitelists (+ allowsNone) backing the cockpit
 * dropdowns. Sourced from the server, never hard-coded here. */
export function fetchFixedEnums(): Promise<FixedEnums> {
  return getJSON<FixedEnums>('/api/fixed-enums')
}

/** The body of a structured metadata write (one transaction). All optional;
 * `set` carries scalar fields (status/priority/type), the tag maps carry
 * topics/methods/data add/remove. */
export interface MetadataWrite {
  set?: Record<string, string | null>
  addTag?: Record<string, string[]>
  rmTag?: Record<string, string[]>
}

/** Apply a structured metadata change through the `lit modify` backend
 * (invariant #16 second-class write: the server runs `_apply_modify`, which
 * validates + writes + recomputes INDEX/views atomically). Throws the backend's
 * raw message on rejection so the cockpit can toast it. */
export function putMetadata(
  id: string,
  body: MetadataWrite,
): Promise<{ ok: boolean; changed: boolean }> {
  return mutateJSON(`/api/paper/${encodeURIComponent(id)}/metadata`, 'PUT', body)
}

/** Stamp read-date through the `lit read` backend (idempotent first-read). An
 * absent `date` defaults to today server-side. */
export function postRead(
  id: string,
  date?: string,
): Promise<{ ok: boolean; changed: boolean; message: string }> {
  return mutateJSON(
    `/api/paper/${encodeURIComponent(id)}/read`,
    'POST',
    date ? { date } : {},
  )
}

/** Stamp last-revisited through the `lit revisit` backend. Throws the backend's
 * "a revisit presupposes a first read" message (400) when the paper is unread. */
export function postRevisit(id: string, date?: string): Promise<{ ok: boolean }> {
  return mutateJSON(
    `/api/paper/${encodeURIComponent(id)}/revisit`,
    'POST',
    date ? { date } : {},
  )
}

export function fetchProjects(): Promise<ProjectEntry[]> {
  return getJSON<ProjectEntry[]>('/api/projects')
}

export function fetchVaults(): Promise<VaultsPayload> {
  return getJSON<VaultsPayload>('/api/vaults')
}
