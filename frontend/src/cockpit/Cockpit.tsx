import { useEffect, useRef, useState } from 'react'
import type { FixedEnums, PaperMeta, ProjectEntry, Taxonomy } from '../types'
import {
  addTaxonomyValue,
  fetchCite,
  linkProject,
  postRead,
  postRevisit,
  postUnread,
  putMetadata,
  unlinkProject,
} from '../api'

interface Props {
  paper: PaperMeta | null
  loading: boolean
  collapsed: boolean
  onToggle: () => void
  onOpenPaper: (id: string) => void
  /** Active vault's filesystem path (server-side), for the copy-path action. */
  vaultPath: string | null
  /** TAXONOMY controlled vocabulary — the add-chip affordance offers existing
   * values plus an explicit inline-create (3c-1). */
  taxonomy: Taxonomy | null
  /** Registered projects (name/path/status) backing the link dropdown (3c-1). */
  projects: ProjectEntry[]
  /** status/priority/type whitelists for the dropdowns. */
  fixedEnums: FixedEnums | null
  /** Called after a successful structured write so the parent re-fetches the
   * cockpit paper AND the left list (status/read-date move smart-list members). */
  onChanged: () => void
  /** Called after a write that changes the shared vocabulary (a new taxonomy
   * value or project link/unlink) so the parent re-fetches /api/taxonomy +
   * /api/projects, keeping the dropdowns/autocomplete current (3c-1). */
  onVocabChanged: () => void
  /** Toast a message (used to surface the backend's raw error verbatim). */
  notify: (msg: string) => void
}

/** A read-only chip group (relations / code-clones stay read-only in 3b). */
function Chips({ values }: { values: string[] | undefined }) {
  if (!values || values.length === 0) return <span className="text-stone-400">—</span>
  return (
    <div className="flex flex-wrap gap-1">
      {values.map((v) => (
        <span
          key={v}
          className="rounded-md bg-stone-200 px-2 py-0.5 text-xs text-stone-700"
        >
          {v}
        </span>
      ))}
    </div>
  )
}

function Field({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className="mb-3.5">
      <div className="mb-1 text-[11px] font-semibold uppercase tracking-wider text-stone-500">
        {label}
      </div>
      <div className="text-sm text-stone-800">{children}</div>
    </div>
  )
}

const SELECT_CLASS =
  'w-full rounded-md border border-stone-300 bg-white px-2 py-1 text-sm text-stone-800 ' +
  'shadow-sm transition-colors hover:bg-stone-50 focus:outline-none focus:ring-1 ' +
  'focus:ring-accent-400 disabled:opacity-50'

/** A single fixed-enum dropdown (status / priority / type). Options come from
 * `fixedEnums`; the unset option is offered only when `allowsNone`. */
function EnumSelect({
  field,
  value,
  options,
  allowsNone,
  disabled,
  onPick,
}: {
  field: string
  value: string | null | undefined
  options: string[]
  allowsNone: boolean
  disabled: boolean
  onPick: (next: string | null) => void
}) {
  // Empty string is the sentinel for "— (unset)"; a real value never is.
  const current = value ?? ''
  return (
    <select
      aria-label={field}
      className={SELECT_CLASS}
      value={current}
      disabled={disabled}
      onChange={(e) => onPick(e.target.value === '' ? null : e.target.value)}
    >
      {allowsNone && <option value="">— (unset)</option>}
      {options.map((opt) => (
        <option key={opt} value={opt}>
          {opt}
        </option>
      ))}
    </select>
  )
}

/** Editable tag chips for a USER_DICTS field (topics/methods/data). Each value
 * carries an × (remove); a select offers TAXONOMY values not yet attached, and a
 * filter input doubles as inline-create — typing a value not in the vocabulary
 * reveals an explicit "Create 'X'" button (3c-1 guardrail: creating a new
 * controlled value is a deliberate click, never an accidental free-text commit).
 */
function TagEditor({
  field,
  values,
  vocabulary,
  busy,
  onAdd,
  onCreate,
  onRemove,
}: {
  field: string
  values: string[] | undefined
  vocabulary: string[]
  busy: boolean
  onAdd: (value: string) => void
  /** Register a brand-new taxonomy value, then attach it (register-first). */
  onCreate: (value: string) => void
  onRemove: (value: string) => void
}) {
  const [draft, setDraft] = useState('')
  const attached = values ?? []
  // Offer only registered values not already attached.
  const available = vocabulary.filter((v) => !attached.includes(v))
  const typed = draft.trim()
  // Inline-create is offered only for a typed value that is genuinely new
  // (not already registered, not already attached). An exact registered match
  // is attached through the select, never re-created.
  const isNew =
    typed.length > 0 &&
    !vocabulary.includes(typed) &&
    !attached.includes(typed)
  // Filter the select's options by the draft (case-insensitive substring) so the
  // input is also an autocomplete over existing values.
  const filtered = typed
    ? available.filter((v) => v.toLowerCase().includes(typed.toLowerCase()))
    : available
  return (
    <div className="flex flex-col gap-1.5">
      <div className="flex flex-wrap gap-1">
        {attached.length === 0 && <span className="text-stone-400">—</span>}
        {attached.map((v) => (
          <span
            key={v}
            className="inline-flex items-center gap-1 rounded-md bg-stone-200 px-2 py-0.5 text-xs text-stone-700"
          >
            {v}
            <button
              type="button"
              aria-label={`Remove ${field} ${v}`}
              title={`Remove ${v}`}
              disabled={busy}
              onClick={() => onRemove(v)}
              className="text-stone-400 transition-colors hover:text-rose-600 disabled:opacity-50"
            >
              ×
            </button>
          </span>
        ))}
      </div>
      <input
        type="text"
        aria-label={`Filter or create ${field}`}
        placeholder={`Filter or create ${field}…`}
        value={draft}
        disabled={busy}
        onChange={(e) => setDraft(e.target.value)}
        className={SELECT_CLASS}
      />
      {filtered.length > 0 && (
        <select
          aria-label={`Add ${field}`}
          className={SELECT_CLASS}
          value=""
          disabled={busy}
          onChange={(e) => {
            const v = e.target.value
            if (v) {
              onAdd(v)
              setDraft('')
            }
            e.target.value = '' // reset to the placeholder so the same value can re-add later
          }}
        >
          <option value="">+ add {field}…</option>
          {filtered.map((v) => (
            <option key={v} value={v}>
              {v}
            </option>
          ))}
        </select>
      )}
      {isNew && (
        <button
          type="button"
          disabled={busy}
          onClick={() => {
            onCreate(typed)
            setDraft('')
          }}
          className="self-start rounded-md border border-accent-300 bg-accent-50 px-2.5 py-1 text-xs font-medium text-accent-700 shadow-sm transition-colors hover:bg-accent-100 disabled:opacity-50"
        >
          + Create “{typed}”
        </button>
      )}
    </div>
  )
}

/** Writable Projects field: chips for each linked project (× to unlink) plus a
 * dropdown to link a registered project not yet attached (3c-1). Unlike the
 * topics/methods/data tag editor, projects are never inline-created here — a new
 * project carries a path binding, so it is created via the TopBar + dialog. */
function ProjectEditor({
  attached,
  registered,
  busy,
  onLink,
  onUnlink,
}: {
  attached: string[]
  registered: string[]
  busy: boolean
  onLink: (project: string) => void
  onUnlink: (project: string) => void
}) {
  const available = registered.filter((p) => !attached.includes(p))
  return (
    <div className="flex flex-col gap-1.5">
      <div className="flex flex-wrap gap-1">
        {attached.length === 0 && <span className="text-stone-400">—</span>}
        {attached.map((p) => (
          <span
            key={p}
            className="inline-flex items-center gap-1 rounded-md bg-stone-200 px-2 py-0.5 text-xs text-stone-700"
          >
            {p}
            <button
              type="button"
              aria-label={`Unlink project ${p}`}
              title={`Unlink ${p}`}
              disabled={busy}
              onClick={() => onUnlink(p)}
              className="text-stone-400 transition-colors hover:text-rose-600 disabled:opacity-50"
            >
              ×
            </button>
          </span>
        ))}
      </div>
      {available.length > 0 && (
        <select
          aria-label="Link project"
          className={SELECT_CLASS}
          value=""
          disabled={busy}
          onChange={(e) => {
            const v = e.target.value
            if (v) onLink(v)
            e.target.value = ''
          }}
        >
          <option value="">+ link project…</option>
          {available.map((p) => (
            <option key={p} value={p}>
              {p}
            </option>
          ))}
        </select>
      )}
    </div>
  )
}

function Relations({
  paper,
  onOpenPaper,
}: {
  paper: PaperMeta
  onOpenPaper: (id: string) => void
}) {
  const groups: Array<[string, string[] | undefined]> = [
    ['related', paper.related],
    ['extends', paper.extends],
    ['extended-by', paper['extended-by']],
    ['contradicts', paper.contradicts],
    ['contradicted-by', paper['contradicted-by']],
  ]
  const nonEmpty = groups.filter(([, ids]) => ids && ids.length > 0)
  if (nonEmpty.length === 0) return <span className="text-stone-400">—</span>
  return (
    <div className="space-y-1">
      {nonEmpty.map(([rel, ids]) => (
        <div key={rel}>
          <span className="text-xs text-stone-500">{rel}: </span>
          {ids!.map((id) => (
            <button
              key={id}
              onClick={() => onOpenPaper(id)}
              className="mr-1 text-xs text-accent-600 transition-colors hover:underline"
            >
              {id}
            </button>
          ))}
        </div>
      ))}
    </div>
  )
}

/** Confirm dialog for the guarded unread (problem 5). Clearing read-date
 * reverses an immutable-by-default stamp, so it sits behind a default-No confirm
 * (Cancel is autofocused); when a revisit record exists the body spells out that
 * it is dropped too (the date-ordering rule forbids a revisit without a first
 * read). Mirrors the macOS-style modal shell used by the TopBar project dialog. */
function UnreadConfirm({
  readDate,
  lastRevisited,
  busy,
  onCancel,
  onConfirm,
}: {
  readDate: string | null
  lastRevisited: string | null
  busy: boolean
  onCancel: () => void
  onConfirm: () => void
}) {
  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/30 backdrop-blur-sm"
      onClick={busy ? undefined : onCancel}
    >
      <div
        onClick={(e) => e.stopPropagation()}
        onKeyDown={(e) => {
          if (e.key === 'Escape') onCancel()
        }}
        className="w-[22rem] animate-grow-in rounded-2xl bg-white p-5 shadow-xl ring-1 ring-stone-200"
      >
        <h2 className="text-sm font-semibold text-stone-900">Mark as unread?</h2>
        <p className="mt-1.5 text-xs leading-relaxed text-stone-600">
          This clears the first-read date
          {readDate ? ` (${readDate})` : ''}, returning this paper to unread.
        </p>
        {lastRevisited != null && (
          <p className="mt-2 rounded-md border border-amber-300 bg-amber-50 px-2.5 py-1.5 text-xs leading-relaxed text-amber-700">
            The last revisit ({lastRevisited}) will also be cleared — a revisit
            cannot exist without a first read. This cannot be undone.
          </p>
        )}
        <div className="mt-5 flex justify-end gap-2">
          <button
            autoFocus
            onClick={onCancel}
            disabled={busy}
            className="rounded-lg px-3 py-1.5 text-xs text-stone-600 transition-colors hover:bg-stone-100 disabled:opacity-40"
          >
            Cancel
          </button>
          <button
            onClick={onConfirm}
            disabled={busy}
            className="rounded-lg bg-rose-500 px-3 py-1.5 text-xs font-medium text-white transition-colors hover:bg-rose-600 disabled:opacity-60"
          >
            Mark unread
          </button>
        </div>
      </div>
    </div>
  )
}

/** Curation cockpit with structured write controls (Phase 3b).
 *
 * Dropdowns (status/priority/type), editable tag chips (topics/methods/data),
 * and mutually-exclusive read/revisit buttons all dispatch to the invariant #16
 * second-class write endpoints (the server runs the `lit` command backend). On
 * success the parent re-fetches via `onChanged`; on error the backend's raw
 * message is toasted via `notify`. Relations / code-clones stay read-only (3c).
 * Collapses to a narrow strip via an animated width, mirroring BrowsePanel.
 */
export default function Cockpit({
  paper,
  loading,
  collapsed,
  onToggle,
  onOpenPaper,
  vaultPath,
  taxonomy,
  projects,
  fixedEnums,
  onChanged,
  onVocabChanged,
  notify,
}: Props) {
  const [copied, setCopied] = useState<string | null>(null)
  // Caveats from the last Cite (unverified abbreviation, missing fields, ...).
  // Persisted until the selected paper changes so the user actually reads them.
  const [citeWarn, setCiteWarn] = useState<string[] | null>(null)
  // A structured write is in flight — disables the write controls so a second
  // click can't race the first (the backend serialises, but a double-fire would
  // toast a confusing intermediate state).
  const [writing, setWriting] = useState(false)
  // Unread-confirm dialog (problem 5): clearing read-date reverses an
  // immutable-by-default stamp, so it sits behind a default-No confirm that also
  // warns the revisit record is dropped. Only reachable when readDate != null.
  const [showUnread, setShowUnread] = useState(false)

  // The currently-shown paper id, refreshed synchronously each render and read
  // inside the async handlers to drop a response that lands after the user has
  // already moved to a different paper.
  const shownId = useRef<string | undefined>(paper?.id)
  shownId.current = paper?.id

  // Selecting a different paper clears stale copy/cite feedback.
  useEffect(() => {
    setCopied(null)
    setCiteWarn(null)
    setShowUnread(false)
  }, [paper?.id])

  // Per-paper copy actions live here (the selected-paper context), not the top
  // bar. `ID` pastes into CLI commands / metadata / filenames; `path` is the
  // paper-folder path on the vault host — from it you reach pdf / metadata /
  // notes, or hand it straight to a terminal or an agent.
  async function doCopy(form: string, value: string) {
    try {
      await navigator.clipboard.writeText(value)
      setCopied(form)
      setTimeout(() => setCopied(null), 1200)
    } catch {
      /* no clipboard in some sandboxes — silently ignore */
    }
  }

  // Cite fetches the server-formatted ACS citation (one formatting path,
  // invariant #16), copies the clean text, and surfaces any caveats so a
  // wrong-looking abbreviation is never copied silently.
  async function doCite() {
    if (!paper) return
    const id = paper.id
    try {
      const { text, warnings } = await fetchCite(id)
      // The selection may have moved on while the request was in flight — drop
      // the stale result rather than copy it / flash its warnings under another
      // paper.
      if (shownId.current !== id) return
      await navigator.clipboard.writeText(text)
      setCopied('citation')
      setCiteWarn(warnings.length ? warnings : null)
      setTimeout(() => setCopied(null), 1200)
    } catch {
      /* no clipboard / fetch failure — silently ignore */
    }
  }

  // Run a structured write, then refresh on success or toast the backend's raw
  // message on failure. `writing` gates the controls for the duration. The
  // backend serialises writes; the parent's onChanged re-fetches the cockpit.
  // `vocabChanged` additionally refreshes the shared taxonomy/projects caches
  // (a new taxonomy value or a project link/unlink that the dropdowns reflect).
  async function runWrite(fn: () => Promise<unknown>, vocabChanged = false) {
    setWriting(true)
    try {
      await fn()
      onChanged()
      if (vocabChanged) onVocabChanged()
    } catch (err) {
      notify(err instanceof Error ? err.message : String(err))
    } finally {
      setWriting(false)
    }
  }

  function setEnum(field: 'status' | 'priority' | 'type', next: string | null) {
    if (!paper) return
    const id = paper.id
    runWrite(() => putMetadata(id, { set: { [field]: next } }))
  }

  function addTag(field: 'topics' | 'methods' | 'data', value: string) {
    if (!paper) return
    const id = paper.id
    runWrite(() => putMetadata(id, { addTag: { [field]: [value] } }))
  }

  function removeTag(field: 'topics' | 'methods' | 'data', value: string) {
    if (!paper) return
    const id = paper.id
    runWrite(() => putMetadata(id, { rmTag: { [field]: [value] } }))
  }

  // Inline-create: register the new taxonomy value first (invariant #2), then
  // attach it via the existing addTag path — two steps, one button click. The
  // vocabulary refresh keeps the new value in autocomplete for other papers.
  function createTag(field: 'topics' | 'methods' | 'data', value: string) {
    if (!paper) return
    const id = paper.id
    runWrite(async () => {
      await addTaxonomyValue(field, value)
      await putMetadata(id, { addTag: { [field]: [value] } })
    }, true)
  }

  function linkProj(project: string) {
    if (!paper) return
    const id = paper.id
    runWrite(() => linkProject(id, project), true)
  }

  function unlinkProj(project: string) {
    if (!paper) return
    const id = paper.id
    runWrite(() => unlinkProject(id, project), true)
  }

  function markRead() {
    if (!paper) return
    const id = paper.id
    runWrite(() => postRead(id))
  }

  function logRevisit() {
    if (!paper) return
    const id = paper.id
    runWrite(() => postRevisit(id))
  }

  // Guarded unread (problem 5): clears read-date + last-revisited in one atomic
  // backend write. Gated behind UnreadConfirm; runWrite refreshes the list so
  // the paper rejoins the unread smart-lists.
  function doUnread() {
    if (!paper) return
    const id = paper.id
    setShowUnread(false)
    runWrite(() => postUnread(id))
  }

  const readDate = paper?.['read-date'] ?? null
  const lastRevisited = paper?.['last-revisited'] ?? null

  return (
    <div
      className={`relative flex shrink-0 overflow-hidden border-l border-stone-200 bg-stone-100 transition-[width] duration-300 ease-fluid ${
        collapsed ? 'w-9' : 'w-80'
      }`}
    >
      {/* Collapsed strip: just the expand handle, fading in once narrowed. */}
      <div
        className={`absolute inset-0 flex flex-col items-center pt-3 transition-opacity duration-200 ${
          collapsed ? 'opacity-100 delay-150' : 'pointer-events-none opacity-0'
        }`}
      >
        <button
          onClick={onToggle}
          title="Expand metadata"
          className="text-stone-500 transition-colors hover:text-stone-800"
        >
          ‹
        </button>
      </div>

      {/* Full inspector — fixed w-80 so it never reflows while the container
          width animates; cross-fades out when collapsed. */}
      <aside
        className={`h-full w-80 overflow-auto p-4 transition-opacity duration-200 ${
          collapsed ? 'pointer-events-none opacity-0' : 'opacity-100 delay-100'
        }`}
      >
        <div className="mb-3 flex items-center justify-between">
          <span className="text-[11px] font-semibold uppercase tracking-wider text-stone-500">
            Metadata
          </span>
          <button
            onClick={onToggle}
            title="Collapse metadata"
            className="text-stone-500 transition-colors hover:text-stone-800"
          >
            ›
          </button>
        </div>

        {loading && <div className="text-sm text-stone-500">Loading…</div>}
        {!loading && !paper && (
          <div className="text-sm text-stone-500">Select a paper.</div>
        )}

        {paper && (
          <div>
            <div className="mb-4">
              <div className="text-[15px] font-semibold leading-snug text-stone-900">
                {paper.title || paper.id}
              </div>
              <div className="mt-0.5 font-mono text-xs text-stone-500">
                {paper.id}
              </div>
              <div className="mt-2.5 flex flex-wrap items-center gap-1.5">
                <button
                  onClick={() => doCopy('ID', paper.id)}
                  title="Copy the paper id"
                  className="flex items-center gap-1 rounded-md border border-stone-300 bg-white px-2.5 py-1 text-xs font-medium text-stone-600 shadow-sm transition-colors hover:bg-stone-50 hover:text-stone-900"
                >
                  <span className="text-stone-400">⧉</span> Copy ID
                </button>
                <button
                  onClick={() =>
                    vaultPath && doCopy('path', `${vaultPath}/papers/${paper.id}`)
                  }
                  disabled={!vaultPath}
                  title={
                    vaultPath
                      ? 'Copy the paper folder path'
                      : 'Vault path unavailable'
                  }
                  className="flex items-center gap-1 rounded-md border border-stone-300 bg-white px-2.5 py-1 text-xs font-medium text-stone-600 shadow-sm transition-colors hover:bg-stone-50 hover:text-stone-900 disabled:opacity-50"
                >
                  <span className="text-stone-400">⧉</span> Copy path
                </button>
                <button
                  onClick={doCite}
                  title="Copy an ACS-style citation (journal abbrev. year, volume, pages)"
                  className="flex items-center gap-1 rounded-md border border-stone-300 bg-white px-2.5 py-1 text-xs font-medium text-stone-600 shadow-sm transition-colors hover:bg-stone-50 hover:text-stone-900"
                >
                  <span className="text-stone-400">❝</span> Cite
                </button>
                {copied && (
                  <span className="text-[11px] font-medium text-emerald-600">
                    ✓ copied {copied}
                  </span>
                )}
              </div>
              {citeWarn && (
                <div className="mt-2 rounded-md border border-amber-300 bg-amber-50 px-2.5 py-1.5 text-[11px] leading-relaxed text-amber-700">
                  <span className="font-semibold">Citation copied — verify:</span>
                  <ul className="mt-0.5 list-disc space-y-0.5 pl-4">
                    {citeWarn.map((w, i) => (
                      <li key={i}>{w}</li>
                    ))}
                  </ul>
                </div>
              )}
            </div>
            <Field label="Authors">
              {paper.authors && paper.authors.length > 0
                ? paper.authors.join('; ')
                : '—'}
            </Field>
            <Field label="Venue / Year">
              {[paper.journal, paper.year].filter(Boolean).join(' · ') || '—'}
            </Field>
            {paper.doi && (
              <Field label="DOI">
                <a
                  href={`https://doi.org/${paper.doi}`}
                  target="_blank"
                  rel="noreferrer"
                  className="text-accent-600 transition-colors hover:underline"
                >
                  {paper.doi}
                </a>
              </Field>
            )}

            <Field label="Status">
              {fixedEnums ? (
                <EnumSelect
                  field="status"
                  value={paper.status}
                  options={fixedEnums.status.values}
                  allowsNone={fixedEnums.status.allowsNone}
                  disabled={writing}
                  onPick={(next) => setEnum('status', next)}
                />
              ) : (
                <span className="text-stone-400">{paper.status || '—'}</span>
              )}
            </Field>
            <Field label="Priority">
              {fixedEnums ? (
                <EnumSelect
                  field="priority"
                  value={paper.priority}
                  options={fixedEnums.priority.values}
                  allowsNone={fixedEnums.priority.allowsNone}
                  disabled={writing}
                  onPick={(next) => setEnum('priority', next)}
                />
              ) : (
                <span className="text-stone-400">{paper.priority || '—'}</span>
              )}
            </Field>
            <Field label="Type">
              {fixedEnums ? (
                <EnumSelect
                  field="type"
                  value={paper.type}
                  options={fixedEnums.type.values}
                  allowsNone={fixedEnums.type.allowsNone}
                  disabled={writing}
                  onPick={(next) => setEnum('type', next)}
                />
              ) : (
                <span className="text-stone-400">{paper.type || '—'}</span>
              )}
            </Field>

            <Field label="Read / Revisit">
              <div className="flex items-center gap-1.5">
                {readDate != null && (
                  <button
                    type="button"
                    aria-label="Mark as unread"
                    title="Mark as unread (undo the first-read stamp)"
                    disabled={writing}
                    onClick={() => setShowUnread(true)}
                    className="grid h-6 w-6 place-items-center rounded-md text-stone-300 transition-colors hover:bg-rose-50 hover:text-rose-500 disabled:opacity-40"
                  >
                    ↺
                  </button>
                )}
                <button
                  type="button"
                  disabled={writing || readDate != null}
                  onClick={markRead}
                  title={
                    readDate != null
                      ? `First read ${readDate}`
                      : 'Stamp the first-read date (today)'
                  }
                  className="rounded-md border border-stone-300 bg-white px-2.5 py-1 text-xs font-medium text-stone-600 shadow-sm transition-colors hover:bg-stone-50 hover:text-stone-900 disabled:opacity-50"
                >
                  {readDate != null ? `Read · ${readDate}` : 'Mark read'}
                </button>
                <button
                  type="button"
                  disabled={writing || readDate == null}
                  onClick={logRevisit}
                  title={
                    readDate == null
                      ? '先标记已读 (mark as read first)'
                      : 'Stamp a return visit (today)'
                  }
                  className="rounded-md border border-stone-300 bg-white px-2.5 py-1 text-xs font-medium text-stone-600 shadow-sm transition-colors hover:bg-stone-50 hover:text-stone-900 disabled:opacity-50"
                >
                  Log revisit
                </button>
              </div>
              {lastRevisited != null && (
                <div className="mt-1 text-[11px] text-stone-500">
                  last revisited {lastRevisited}
                </div>
              )}
              {showUnread && (
                <UnreadConfirm
                  readDate={readDate}
                  lastRevisited={lastRevisited}
                  busy={writing}
                  onCancel={() => setShowUnread(false)}
                  onConfirm={doUnread}
                />
              )}
            </Field>

            <Field label="Topics">
              <TagEditor
                field="topics"
                values={paper.topics}
                vocabulary={taxonomy?.topics ?? []}
                busy={writing}
                onAdd={(v) => addTag('topics', v)}
                onCreate={(v) => createTag('topics', v)}
                onRemove={(v) => removeTag('topics', v)}
              />
            </Field>
            <Field label="Methods">
              <TagEditor
                field="methods"
                values={paper.methods}
                vocabulary={taxonomy?.methods ?? []}
                busy={writing}
                onAdd={(v) => addTag('methods', v)}
                onCreate={(v) => createTag('methods', v)}
                onRemove={(v) => removeTag('methods', v)}
              />
            </Field>
            <Field label="Data">
              <TagEditor
                field="data"
                values={paper.data}
                vocabulary={taxonomy?.data ?? []}
                busy={writing}
                onAdd={(v) => addTag('data', v)}
                onCreate={(v) => createTag('data', v)}
                onRemove={(v) => removeTag('data', v)}
              />
            </Field>
            <Field label="Projects">
              <ProjectEditor
                attached={paper.projects ?? []}
                registered={projects.map((p) => p.name)}
                busy={writing}
                onLink={linkProj}
                onUnlink={unlinkProj}
              />
            </Field>
            <Field label="Relations">
              <Relations paper={paper} onOpenPaper={onOpenPaper} />
            </Field>
            <Field label="Code-clones">
              <Chips values={paper['code-clones']} />
            </Field>
          </div>
        )}
      </aside>
    </div>
  )
}
