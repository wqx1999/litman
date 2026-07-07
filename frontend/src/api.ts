// Typed fetch helpers over the read API. All URLs are same-origin relative, so
// they resolve against whatever host:port `lit gui` bound to.

import type {
  DocMtimes,
  FixedEnums,
  HealthIssue,
  IndexPaper,
  PaperMeta,
  ProjectEntry,
  RestoreResult,
  SearchPayload,
  SmartListView,
  Taxonomy,
  TrashEntry,
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
  method: 'PUT' | 'POST' | 'DELETE',
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

/** Per-paper notes/discussion file mtimes — the pure-read change-detection feed
 * for the resync diff (epoch seconds, null when absent). Part of the resync
 * sweep only; the GUI compares it against its previous snapshot to surface
 * notes/discussion edits made outside the GUI. */
export function fetchDocMtimes(): Promise<DocMtimes> {
  return getJSON<DocMtimes>('/api/doc-mtimes')
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

/** Clear read-date (+ dependent last-revisited) through the modify backend —
 * the guarded reversal of `postRead`. An already-unread paper is a no-op
 * (changed: false). The caller's confirm dialog warns that any revisit record
 * is discarded (the date-ordering rule forbids a revisit without a first read). */
export function postUnread(
  id: string,
): Promise<{ ok: boolean; changed: boolean; message: string }> {
  return mutateJSON(`/api/paper/${encodeURIComponent(id)}/unread`, 'POST')
}

/** What removing a paper would tear down — the soft-delete confirm preview.
 * Each list names the external links the delete breaks (sourced from the same
 * `lit rm --dry-run` discovery the CLI uses). */
export interface RmPreview {
  id: string
  title: string | null
  /** Other papers whose relation fields lose this paper. */
  references: string[]
  /** Repos that stay (still bound by another paper) but drop this binding. */
  reposUnbound: string[]
  /** Cloned repos no other paper uses — their `codes/<repo>/` dir is deleted. */
  reposRemoved: string[]
  /** Projects this paper is unlinked from (reflib symlink + REFERENCES.md). */
  projects: string[]
  /** Referencing notes/discussion that get a `(deleted)` tag. */
  notes: string[]
}

/** Preview the cascade of removing a paper, without deleting (GET, pure read).
 * Backs the tab trash-icon confirm dialog. */
export function fetchRmPreview(id: string): Promise<RmPreview> {
  return getJSON<RmPreview>(`/api/paper/${encodeURIComponent(id)}/rm-preview`)
}

/** Soft-delete a paper through the `lit rm` backend: move it to `.trash/`
 * (recoverable via `lit trash restore` in the CLI) and tear down its external
 * links atomically. The server hard-wires `purge=False` — the GUI never
 * permanently deletes. */
export function removePaper(
  id: string,
): Promise<{ ok: boolean; warnings: string[] }> {
  return mutateJSON(`/api/paper/${encodeURIComponent(id)}`, 'DELETE')
}

export function fetchProjects(): Promise<ProjectEntry[]> {
  return getJSON<ProjectEntry[]>('/api/projects')
}

export function fetchVaults(): Promise<VaultsPayload> {
  return getJSON<VaultsPayload>('/api/vaults')
}

/** Running litman version + the latest available release (or null when none /
 * unknown). PURE READ of the server's update-check cache — the request path
 * never hits PyPI; the server's startup task populates the cache. Backs the
 * TopBar update dot. */
export interface VersionInfo {
  current: string
  latest: string | null
}

export function fetchVersion(): Promise<VersionInfo> {
  return getJSON<VersionInfo>('/api/version')
}

/** Run every health-check probe and return the flat findings list — the pure-read
 * mirror of `lit health-check` (the GET never re-locks / fixes / stamps the
 * registry). On demand only (Tier-2: reads all metadata server-side), so the
 * caller fetches this when the user opens the health panel, never on page load. */
export function fetchHealth(): Promise<HealthIssue[]> {
  return getJSON<HealthIssue[]>('/api/health')
}

/** The configured agent launchers (lit-config.yaml `agents:`) + the default. */
export interface AgentsPayload {
  agents: string[]
  default: string
}

export function fetchAgents(): Promise<AgentsPayload> {
  return getJSON<AgentsPayload>('/api/agents')
}

/** The launch endpoint's outcome: `spawned` = a native terminal window opened
 * on the server's machine; `copy` = it couldn't (headless / remote server), and
 * `command` carries the `lit agent …` line for the user's own terminal. */
export interface AgentLaunchResult {
  ok: boolean
  mode: 'spawned' | 'copy'
  agent: string
  command: string
}

/** Launch a configured agent in a terminal at the vault. Sends the agent NAME
 * only — the command always comes from the server-side config (ADR-020); an
 * absent name launches the default agent. Throws the backend's verbatim
 * message (400) on an unknown name. */
export function launchAgent(name?: string): Promise<AgentLaunchResult> {
  return mutateJSON('/api/agent/launch', 'POST', name ? { agent: name } : {})
}

/** Switch the active vault through the `lit vault use` backend (3c-2). GLOBAL:
 * sets the registry's active vault (affects `lit` in every terminal without an
 * explicit --library/$LIT_LIBRARY) and repoints the running server in place, no
 * restart. Throws the backend's VaultRegistryError (400) on an unknown name or a
 * stale/missing vault path. */
export function putActiveVault(
  name: string,
): Promise<{ ok: boolean; active: string; path: string }> {
  return mutateJSON('/api/vaults/active', 'PUT', { name })
}

/** Register an EXISTING vault directory through the `lit vault add` backend. Pure
 * registry append: never changes the active vault (use `putActiveVault` for
 * that). Throws the backend's verbatim VaultRegistryError (400) on a bad name,
 * a duplicate, or a path that is not an existing litman vault directory. */
export function registerVault(
  name: string,
  path: string,
): Promise<{ ok: boolean; name: string; path: string; active: boolean }> {
  return mutateJSON('/api/vaults', 'POST', { name, path })
}

/** Unregister a vault through the `lit vault remove` backend: drop the registry
 * entry only — the vault directory on disk is NEVER deleted. Throws 409 when the
 * name is the vault the GUI is currently serving (switch away first), or 400 on
 * an unknown name. */
export function unregisterVault(name: string): Promise<{ ok: boolean }> {
  return mutateJSON(`/api/vaults/${encodeURIComponent(name)}`, 'DELETE')
}

/** Link a paper to a registered project through the `lit link` backend
 * (invariant #16 second-class write). Throws the backend's raw LinkError
 * message (400) when the project is unregistered or its dir is missing. */
export function linkProject(
  id: string,
  project: string,
  relevance?: string,
): Promise<{ ok: boolean }> {
  return mutateJSON(
    `/api/paper/${encodeURIComponent(id)}/project`,
    'POST',
    relevance ? { project, relevance } : { project },
  )
}

/** Unlink a paper from a project through the `lit unlink` backend. */
export function unlinkProject(id: string, project: string): Promise<{ ok: boolean }> {
  return mutateJSON(
    `/api/paper/${encodeURIComponent(id)}/project/${encodeURIComponent(project)}`,
    'DELETE',
  )
}

/** Register a new project through the `lit project add` backend (dual-write
 * TAXONOMY + config). The path must already exist and be a directory (A7);
 * the backend's TaxonomyError surfaces verbatim on rejection. */
export function createProject(
  name: string,
  path: string,
): Promise<{ ok: boolean; name: string; path: string }> {
  return mutateJSON('/api/projects', 'POST', { name, path })
}

/** Unregister a project through the `lit project rm` backend (atomic TAXONOMY +
 * config rewrite, then cascade: unlink every paper, drop relevance keys, tear
 * down reflib symlinks + REFERENCES.md). The on-disk project directory is left
 * intact. `changed` counts the papers unlinked. Throws the backend's
 * TaxonomyError (400) verbatim on rejection. */
export function deleteProject(
  name: string,
): Promise<{ ok: boolean; changed: number }> {
  return mutateJSON(`/api/projects/${encodeURIComponent(name)}`, 'DELETE')
}

/** Rename a project through the `lit project rename` backend (atomic rename
 * across both truth sources + every paper's `projects` field + the paired
 * `relevance-<name>`; the on-disk path is carried over unchanged). `changed`
 * counts the papers rewritten. Throws the backend's TaxonomyError (400)
 * verbatim on rejection (unregistered / duplicate / empty). */
export function renameProject(
  name: string,
  newName: string,
): Promise<{ ok: boolean; changed: number }> {
  return mutateJSON(`/api/projects/${encodeURIComponent(name)}`, 'PUT', {
    new: newName,
  })
}

/** Re-point a project's on-disk path through the `lit project set-path` backend
 * (config-only — papers reference the project by NAME, so nothing else changes).
 * For when the folder was moved manually and the registry needs to follow. The
 * directory is NOT moved and symlinks are NOT rebuilt (use rebuild-views for
 * that). `path` must be absolute + already exist + be a directory; a bad path
 * surfaces the backend's TaxonomyError (400) verbatim. `changed` is false when
 * the project already points there (no-op). */
export function setProjectPath(
  name: string,
  path: string,
): Promise<{ ok: boolean; path: string; changed: boolean }> {
  return mutateJSON(`/api/projects/${encodeURIComponent(name)}/path`, 'PUT', {
    path,
  })
}

/** Register a new controlled-vocab value through the `lit taxonomy add` backend
 * (register-first per invariant #2). This only registers the value; the caller
 * then attaches it via `putMetadata` addTag (two-step inline-create). */
export function addTaxonomyValue(
  key: string,
  value: string,
): Promise<{ ok: boolean; added: string[]; skipped: string[] }> {
  return mutateJSON(`/api/taxonomy/${encodeURIComponent(key)}`, 'POST', { value })
}

/** Remove a controlled-vocab value through the `lit taxonomy rm` backend
 * (atomic dictionary + reference rewrite per invariant #2). The value rides as a
 * query param, not a path segment, because a value may contain '/' (e.g.
 * "deep-learning/transformers"). `changed` is the count of papers untagged.
 * Throws the backend's TaxonomyError (400) verbatim on rejection. */
export function deleteTaxonomyValue(
  key: string,
  value: string,
): Promise<{ ok: boolean; changed: number }> {
  return mutateJSON(
    `/api/taxonomy/${encodeURIComponent(key)}?value=${encodeURIComponent(value)}`,
    'DELETE',
  )
}

/** Rename a controlled-vocab value through the `lit taxonomy rename` backend
 * (atomic dictionary + reference rewrite per invariant #2). `old`/`new` ride in
 * the body, not the path, because a value may contain '/'. `changed` is the count
 * of papers rewritten. Throws the backend's TaxonomyError (400) verbatim on
 * rejection (unregistered `old`, or a `new` that already exists → use merge). */
export function renameTaxonomyValue(
  key: string,
  old: string,
  next: string,
): Promise<{ ok: boolean; changed: number }> {
  return mutateJSON(`/api/taxonomy/${encodeURIComponent(key)}`, 'PUT', {
    old,
    new: next,
  })
}

// --- Trash (recoverable-delete bin) — read-only library + restore (Phase 4.9) -

/** The /api/trash projection (snake_case server keys → camelCase). */
interface TrashEntryWire {
  paper_id: string
  title: string | null
  deleted_at: string
  entry_name: string
  orphan_repo_count: number
}

/** List trash entries (newest-first) — the read-only trash library. Thin
 * wrapper over `core.trash.list_trash`; the GUI never re-scans `.trash/`. */
export async function fetchTrash(): Promise<TrashEntry[]> {
  const wire = await getJSON<TrashEntryWire[]>('/api/trash')
  return wire.map((e) => ({
    paperId: e.paper_id,
    title: e.title,
    deletedAt: e.deleted_at,
    entryName: e.entry_name,
    orphanRepoCount: e.orphan_repo_count,
  }))
}

/** Full metadata.yaml for a trashed paper (read from the resolved entry path). */
export function fetchTrashMeta(entryName: string): Promise<PaperMeta> {
  return getJSON<PaperMeta>(`/api/trash/${encodeURIComponent(entryName)}`)
}

/** The URL pdf.js fetches (with HTTP range) for a trashed paper. */
export function trashPdfUrl(entryName: string): string {
  return `/api/trash/${encodeURIComponent(entryName)}/pdf`
}

async function fetchTrashMdText(
  entryName: string,
  doc: 'notes' | 'discussion',
): Promise<string | null> {
  const resp = await fetch(`/api/trash/${encodeURIComponent(entryName)}/${doc}`)
  if (resp.status === 404) return null
  if (!resp.ok) throw new Error(`trash/${doc} → ${resp.status}`)
  const body = (await resp.json()) as { text: string }
  return body.text
}

export function fetchTrashNotes(entryName: string): Promise<string | null> {
  return fetchTrashMdText(entryName, 'notes')
}

export function fetchTrashDiscussion(entryName: string): Promise<string | null> {
  return fetchTrashMdText(entryName, 'discussion')
}

/** The restore summary as the server returns it (snake_case keys). */
interface RestoreResultWire {
  paper_id: string
  title: string | null
  reverse_edges_rebuilt: string[]
  repos_rebound: string[]
  projects_rebuilt: string[]
  missing_repos: Record<string, string>
  dead_edges_dropped: string[]
}

/** Restore a trashed paper through the `lit trash restore` backend
 * (resolve → restore_from_trash → reconcile_derived). The endpoint never
 * re-clones a hard-deleted 1:1 repo (decision b): those surface in
 * `missingRepos` for the CLI / health-check. Throws the backend's verbatim
 * message — 409 when a live paper already holds the id slot, 404 when the entry
 * is gone. */
export async function restorePaper(entryName: string): Promise<RestoreResult> {
  const wire = await mutateJSON<RestoreResultWire>(
    `/api/trash/${encodeURIComponent(entryName)}/restore`,
    'POST',
  )
  return {
    paperId: wire.paper_id,
    title: wire.title,
    reverseEdgesRebuilt: wire.reverse_edges_rebuilt,
    reposRebound: wire.repos_rebound,
    projectsRebuilt: wire.projects_rebuilt,
    missingRepos: wire.missing_repos,
    deadEdgesDropped: wire.dead_edges_dropped,
  }
}
