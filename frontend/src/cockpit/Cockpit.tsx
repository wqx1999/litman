import { useEffect, useRef, useState } from 'react'
import type { FixedEnums, IndexPaper, PaperMeta, ProjectEntry, Taxonomy } from '../types'

/** Imperative handle the global keyboard shortcuts (⌥R/⌥⇧R/⌥P/⌥D/⌥T/⌥C/⌥⇧C)
 * use to drive the cockpit's existing curation actions on the selected paper.
 * Every method routes to the SAME local handler the cockpit's own buttons call
 * (markRead / doUnread / setEnum / setOpenField / doCopy) so confirms, onChanged
 * refresh, and the invariant #16 write path stay untouched — the shortcut is a
 * second trigger, never a second write path. ⌥-combos act only when a paper is
 * selected; when none is, App no-ops + toasts before ever reaching the handle. */
export interface CockpitHandle {
  /** Stamp first-read (idempotent) — same as the Mark read button. */
  triggerRead(): void
  /** Open the default-No unread confirm — same as the ↺ undo button. */
  triggerUnread(): void
  /** status → promoted — same as picking it in the Status dropdown. */
  triggerPromote(): void
  /** status → dropped via the existing二次确认 — same as the Status dropdown +
   *  the cockpit's own write guard surfacing the backend confirm. */
  triggerDrop(): void
  /** Open the Tags母栏 (Topics pill) so the user can pick a value (no write). */
  openTags(): void
  /** Copy the paper-folder path — same as the Copy path button. */
  copyPath(): void
  /** Copy the paper id — same as the Copy ID button. */
  copyId(): void
}
import {
  addTaxonomyValue,
  deleteTaxonomyValue,
  fetchCite,
  linkProject,
  postRead,
  postRevisit,
  postUnread,
  putMetadata,
  renameTaxonomyValue,
  unlinkProject,
} from '../api'

interface Props {
  paper: PaperMeta | null
  loading: boolean
  collapsed: boolean
  onToggle: () => void
  onOpenPaper: (id: string) => void
  /** Read-only variant (trash view): render the bibliographic header + read-only
   * fields with NO write controls (no dropdowns, tag editing, read/revisit). A
   * Restore action replaces the curation surface. Every write-related prop below
   * (taxonomy/projects/fixedEnums/onChanged/...) is unused in this mode. */
  readOnly?: boolean
  /** Restore the trashed paper (read-only mode only). */
  onRestore?: () => void
  /** A restore is in flight — disables the Restore button. */
  restoring?: boolean
  /** Repos a restore would surface for CLI re-clone (read-only mode), shown as a
   * hint under the Restore button. */
  orphanRepoCount?: number

  // --- Write-mode props (required by App; unused/omitted in read-only mode) ---
  /** Active vault's filesystem path (server-side), for the copy-path action. */
  vaultPath?: string | null
  /** TAXONOMY controlled vocabulary — the add-chip affordance offers existing
   * values plus an explicit inline-create (3c-1). */
  taxonomy?: Taxonomy | null
  /** Registered projects (name/path/status) backing the link dropdown (3c-1). */
  projects?: ProjectEntry[]
  /** Full INDEX projection — backs the Manage dialog's per-value in-use count
   * ("used by N", problem 2) without an extra round-trip. */
  allPapers?: IndexPaper[]
  /** status/priority/type whitelists for the dropdowns. */
  fixedEnums?: FixedEnums | null
  /** Called after a successful structured write so the parent re-fetches the
   * cockpit paper AND the left list (status/read-date move smart-list members). */
  onChanged?: () => void
  /** Called after a write that changes the shared vocabulary (a new taxonomy
   * value or project link/unlink) so the parent re-fetches /api/taxonomy +
   * /api/projects, keeping the dropdowns/autocomplete current (3c-1). */
  onVocabChanged?: () => void
  /** Toast a message (used to surface the backend's raw error verbatim). */
  notify?: (msg: string) => void
  /** Register/unregister the imperative handle the global keyboard shortcuts
   * (Phase 4) drive curation through. Null on unmount. */
  onRegisterHandle?: (handle: CockpitHandle | null) => void
  /** Report whether any cockpit-owned modal (a Tags field panel, the Manage
   * dictionary dialog, the Unread confirm, or the Drop confirm) is open, so the
   * shortcut dispatcher's modal guard can suppress global keys while one is up. */
  onModalState?: (open: boolean) => void
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

/** Code-clone chips with a dangling-link marker. A name in `missing` (server-
 * resolved: codes/<name>/ is gone, the same criterion lit health-check uses for
 * invariant #12) renders amber + ⚠ "missing", so a link to a deleted codebase
 * never reads as live (e.g. after a restore that did not re-clone). The link is
 * kept by design — re-clone via `lit code add` or drop it via `lit code unlink`. */
function CodeCloneChips({
  values,
  missing,
}: {
  values: string[] | undefined
  missing: string[] | undefined
}) {
  if (!values || values.length === 0)
    return <span className="text-stone-400">—</span>
  const gone = new Set(missing ?? [])
  return (
    <div className="flex flex-wrap gap-1">
      {values.map((v) =>
        gone.has(v) ? (
          <span
            key={v}
            title={`codes/${v}/ is missing — the clone was deleted. Re-clone it (lit code add) or drop the link (lit code unlink); lit health-check reports it.`}
            className="inline-flex items-center gap-1 rounded-md bg-amber-100 px-2 py-0.5 text-xs text-amber-700 ring-1 ring-amber-200 dark:bg-amber-900/50 dark:text-amber-200 dark:ring-amber-800"
          >
            <span aria-hidden>⚠</span>
            {v}
            <span className="text-[10px] font-medium uppercase tracking-wide opacity-80">
              missing
            </span>
          </span>
        ) : (
          <span
            key={v}
            className="rounded-md bg-stone-200 px-2 py-0.5 text-xs text-stone-700"
          >
            {v}
          </span>
        ),
      )}
    </div>
  )
}

// Long author lists bloat the metadata panel, so cap the rendered names at six:
// the first three and the last three, joined by an ellipsis. The full list is
// kept on a `title` tooltip (see the Authors field) so nothing is truly hidden.
const AUTHOR_HEAD = 3
const AUTHOR_TAIL = 3

function formatAuthors(authors: string[]): string {
  if (authors.length <= AUTHOR_HEAD + AUTHOR_TAIL) return authors.join('; ')
  return [
    ...authors.slice(0, AUTHOR_HEAD),
    '…',
    ...authors.slice(-AUTHOR_TAIL),
  ].join('; ')
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

/** One pill in the Tags group: the field name plus a count badge when the field
 * has values. Clicking toggles the field's inline editor panel open/closed. A
 * filled (accent) pill means the field is non-empty, so the collapsed group still
 * reads at a glance which fields are tagged (problem 1's glanceability cost). */
function TagPill({
  label,
  count,
  active,
  disabled,
  onClick,
}: {
  label: string
  count: number
  active: boolean
  disabled: boolean
  onClick: () => void
}) {
  const filled = count > 0
  return (
    <button
      type="button"
      aria-label={`${label} (${count} selected)`}
      aria-expanded={active}
      disabled={disabled}
      onClick={onClick}
      className={`inline-flex items-center gap-1.5 rounded-md border px-2.5 py-1 text-xs font-medium shadow-sm transition-colors focus:outline-none focus:ring-1 focus:ring-accent-400 disabled:opacity-50 ${
        filled
          ? 'border-accent-300 bg-accent-50 text-accent-700 hover:bg-accent-100'
          : 'border-stone-300 bg-white text-stone-500 hover:bg-stone-50'
      } ${active ? 'ring-1 ring-accent-400' : ''}`}
    >
      <span>{label}</span>
      {filled && (
        <span className="grid h-4 min-w-4 place-items-center rounded-full bg-accent-200 px-1 text-[10px] font-bold text-accent-800">
          {count}
        </span>
      )}
    </button>
  )
}

/** The inline editor panel for one tag field — a combobox (problem 3). The input
 * is search-only (never doubles as a create box); the list shows every registered
 * value (attached ones get a ✓, click to add/remove); a contextual "Create" row
 * appears as the last list entry only when the typed text matches no existing
 * value (controlled-vocab fields); a "Manage…" footer enters the dictionary
 * editor. `onCreate`/`onManage` are omitted for projects (a project is
 * created/deleted from the TopBar, never here). The panel mounts fresh per open
 * (keyed on field in TagGroup), so the draft is always empty on open. */
function TagPanel({
  field,
  values,
  vocabulary,
  busy,
  onAdd,
  onRemove,
  onCreate,
  onManage,
  onClose,
}: {
  field: string
  values: string[] | undefined
  vocabulary: string[]
  busy: boolean
  onAdd: (value: string) => void
  onRemove: (value: string) => void
  /** Register-then-attach a brand-new taxonomy value (controlled-vocab fields). */
  onCreate?: (value: string) => void
  /** Open the dictionary editor for this field (controlled-vocab fields). */
  onManage?: () => void
  onClose: () => void
}) {
  const [draft, setDraft] = useState('')
  const attached = values ?? []
  const typed = draft.trim()
  const lower = typed.toLowerCase()
  // Inline-create is offered only on a controlled-vocab field, for a genuinely
  // new value: non-empty, matching no registered value and nothing attached
  // (case-insensitive, so "Test" won't offer to re-create "test").
  const isNew =
    onCreate != null &&
    typed.length > 0 &&
    !vocabulary.some((v) => v.toLowerCase() === lower) &&
    !attached.some((v) => v.toLowerCase() === lower)
  // The list shows every registered value, filtered by the draft (case-insensitive
  // substring) so the search box doubles as autocomplete.
  const filtered = (
    typed ? vocabulary.filter((v) => v.toLowerCase().includes(lower)) : vocabulary
  )
    .slice()
    .sort((a, b) => a.localeCompare(b))

  return (
    <div
      onKeyDown={(e) => {
        if (e.key === 'Escape') onClose()
      }}
      className="mt-1.5 rounded-lg border border-stone-200 bg-white p-1.5 shadow-md"
    >
      <input
        type="text"
        autoFocus
        aria-label={`Search ${field}`}
        placeholder={`Search ${field}…`}
        value={draft}
        disabled={busy}
        onChange={(e) => setDraft(e.target.value)}
        className="mb-1 w-full rounded-md border border-stone-300 bg-white px-2 py-1 text-sm text-stone-800 shadow-sm focus:outline-none focus:ring-1 focus:ring-accent-400 disabled:opacity-50"
      />
      <div className="max-h-48 overflow-y-auto">
        {filtered.length === 0 && !isNew && (
          <div className="px-2 py-1.5 text-xs text-stone-400">
            {vocabulary.length === 0 ? 'No values yet.' : 'No match.'}
          </div>
        )}
        {filtered.map((v) => {
          const on = attached.includes(v)
          return (
            <button
              key={v}
              type="button"
              disabled={busy}
              onClick={() => (on ? onRemove(v) : onAdd(v))}
              className="flex w-full items-center gap-2 rounded-md px-2 py-1 text-left text-sm text-stone-700 transition-colors hover:bg-stone-100 disabled:opacity-50"
            >
              <span
                className={`w-3 shrink-0 ${on ? 'text-accent-600' : 'text-transparent'}`}
              >
                ✓
              </span>
              <span className="truncate">{v}</span>
            </button>
          )
        })}
        {isNew && (
          <button
            type="button"
            disabled={busy}
            onClick={() => {
              onCreate!(typed)
              setDraft('')
            }}
            className="flex w-full items-center gap-2 rounded-md px-2 py-1 text-left text-sm text-accent-700 transition-colors hover:bg-accent-50 disabled:opacity-50"
          >
            <span className="w-3 shrink-0 text-accent-600">+</span>
            <span className="truncate">Create “{typed}”</span>
          </button>
        )}
      </div>
      {onManage && (
        <div className="mt-1 border-t border-stone-100 pt-1">
          <button
            type="button"
            disabled={busy}
            onClick={onManage}
            className="flex w-full items-center gap-1.5 rounded-md px-2 py-1 text-left text-xs text-stone-500 transition-colors hover:bg-stone-100 hover:text-stone-700 disabled:opacity-50"
          >
            <span className="text-stone-400">⚙</span> Manage {field}…
          </button>
        </div>
      )}
    </div>
  )
}

/** Config for one field in the Tags group. */
type TagFieldConfig = {
  key: string
  label: string
  values: string[] | undefined
  vocabulary: string[]
  onAdd: (value: string) => void
  onRemove: (value: string) => void
  onCreate?: (value: string) => void
  onManage?: () => void
}

/** The "Tags" group (problem 1): collapses the four multi-value association
 * fields (topics/methods/data via TAXONOMY, projects via the registry) under one
 * header so they no longer stack as four full-width dropdowns. The body is a row
 * of pills; clicking a pill opens that field's TagPanel inline below the row, one
 * at a time. A click outside the group (or Esc) closes the open panel. The open
 * field is held by the parent (shared `openField`) so it resets on paper change. */
function TagGroup({
  fields,
  openKey,
  onOpenKey,
  busy,
}: {
  fields: TagFieldConfig[]
  openKey: string | null
  onOpenKey: (key: string | null) => void
  busy: boolean
}) {
  const rootRef = useRef<HTMLDivElement>(null)
  useEffect(() => {
    if (openKey == null) return
    function onDown(e: MouseEvent) {
      if (rootRef.current && !rootRef.current.contains(e.target as Node)) onOpenKey(null)
    }
    document.addEventListener('mousedown', onDown)
    return () => document.removeEventListener('mousedown', onDown)
  }, [openKey, onOpenKey])

  const openCfg = fields.find((f) => f.key === openKey) ?? null

  return (
    <div ref={rootRef} className="mb-3.5">
      <div className="mb-1 text-[11px] font-semibold uppercase tracking-wider text-stone-500">
        Tags
      </div>
      <div className="flex flex-wrap gap-1.5">
        {fields.map((f) => (
          <TagPill
            key={f.key}
            label={f.label}
            count={(f.values ?? []).length}
            active={openKey === f.key}
            disabled={busy}
            onClick={() => onOpenKey(openKey === f.key ? null : f.key)}
          />
        ))}
      </div>
      {openCfg && (
        <TagPanel
          key={openCfg.key}
          field={openCfg.key}
          values={openCfg.values}
          vocabulary={openCfg.vocabulary}
          busy={busy}
          onAdd={openCfg.onAdd}
          onRemove={openCfg.onRemove}
          onCreate={openCfg.onCreate}
          onManage={openCfg.onManage}
          onClose={() => onOpenKey(null)}
        />
      )}
    </div>
  )
}

function IconTrash() {
  return (
    <svg
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth={1.8}
      strokeLinecap="round"
      strokeLinejoin="round"
      className="h-3.5 w-3.5"
      aria-hidden
    >
      <path d="M4 7h16M9 7V5a1 1 0 0 1 1-1h4a1 1 0 0 1 1 1v2M6 7l1 13a2 2 0 0 0 2 2h6a2 2 0 0 0 2-2l1-13" />
    </svg>
  )
}

function IconPencil() {
  return (
    <svg
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth={1.8}
      strokeLinecap="round"
      strokeLinejoin="round"
      className="h-3.5 w-3.5"
      aria-hidden
    >
      <path d="M12 20h9M16.5 3.5a2.12 2.12 0 0 1 3 3L7 19l-4 1 1-4Z" />
    </svg>
  )
}

/** Dictionary editor for one controlled-vocab field (problem 2). Lists every
 * registered value with its in-use count (from the loaded INDEX) and a delete
 * affordance; deletion routes through a default-No confirm into the
 * `lit taxonomy rm` backend (atomic dictionary + reference rewrite, invariant
 * #2). The macOS-style modal shell mirrors UnreadConfirm / NewProjectDialog. */
function ManageDialog({
  field,
  vocabulary,
  countFor,
  busy,
  onDelete,
  onRename,
  onClose,
}: {
  field: string
  vocabulary: string[]
  countFor: (value: string) => number
  busy: boolean
  onDelete: (value: string) => Promise<void>
  onRename: (old: string, next: string) => Promise<void>
  onClose: () => void
}) {
  // The value awaiting delete confirmation, or being renamed (null = list view).
  const [pending, setPending] = useState<string | null>(null)
  const [renaming, setRenaming] = useState<string | null>(null)
  const sorted = vocabulary.slice().sort((a, b) => a.localeCompare(b))
  // While a delete confirm or rename dialog is open (or a write is in flight),
  // gate the list's controls so a keyboard-tab onto a row behind the nested
  // dialog can't swap the target out from under it.
  const blocked = busy || pending != null || renaming != null
  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/30 backdrop-blur-sm"
      onClick={blocked ? undefined : onClose}
    >
      <div
        onClick={(e) => e.stopPropagation()}
        onKeyDown={(e) => {
          if (e.key === 'Escape' && !pending && !renaming) onClose()
        }}
        className="flex max-h-[70vh] w-[24rem] animate-grow-in flex-col rounded-2xl bg-white p-5 shadow-xl ring-1 ring-stone-200"
      >
        <h2 className="text-sm font-semibold text-stone-900">Manage {field}</h2>
        <p className="mt-1.5 text-xs leading-relaxed text-stone-500">
          Rename a value to fix a typo or merge wording — it updates everywhere
          it is used. Deleting a value removes it from the {field} vocabulary and
          untags it from every paper. Both cannot be undone.
        </p>
        <div className="mt-3 min-h-0 flex-1 overflow-y-auto rounded-lg border border-stone-200">
          {sorted.length === 0 && (
            <div className="px-3 py-4 text-center text-xs text-stone-400">
              No values yet.
            </div>
          )}
          {sorted.map((v) => (
            <div
              key={v}
              className="flex items-center justify-between gap-2 border-b border-stone-100 px-3 py-1.5 last:border-b-0"
            >
              <span className="truncate text-sm text-stone-800">{v}</span>
              <div className="flex shrink-0 items-center gap-2">
                <span className="text-[11px] text-stone-400">
                  used by {countFor(v)}
                </span>
                <button
                  type="button"
                  aria-label={`Rename ${field} ${v}`}
                  title={`Rename “${v}”`}
                  disabled={blocked}
                  onClick={() => setRenaming(v)}
                  className="grid h-6 w-6 place-items-center rounded-md text-stone-300 transition-colors hover:bg-accent-50 hover:text-accent-600 disabled:opacity-40"
                >
                  <IconPencil />
                </button>
                <button
                  type="button"
                  aria-label={`Delete ${field} ${v}`}
                  title={`Delete “${v}”`}
                  disabled={blocked}
                  onClick={() => setPending(v)}
                  className="grid h-6 w-6 place-items-center rounded-md text-stone-300 transition-colors hover:bg-rose-50 hover:text-rose-500 disabled:opacity-40"
                >
                  <IconTrash />
                </button>
              </div>
            </div>
          ))}
        </div>
        <div className="mt-4 flex justify-end">
          <button
            onClick={onClose}
            disabled={blocked}
            className="rounded-lg px-3 py-1.5 text-xs text-stone-600 transition-colors hover:bg-stone-100 disabled:opacity-40"
          >
            Done
          </button>
        </div>
      </div>
      {pending != null && (
        <DeleteValueConfirm
          field={field}
          value={pending}
          count={countFor(pending)}
          busy={busy}
          onCancel={() => setPending(null)}
          onConfirm={async () => {
            await onDelete(pending)
            setPending(null)
          }}
        />
      )}
      {renaming != null && (
        <RenameValueDialog
          field={field}
          value={renaming}
          count={countFor(renaming)}
          existing={vocabulary}
          busy={busy}
          onCancel={() => setRenaming(null)}
          onConfirm={async (next) => {
            await onRename(renaming, next)
            setRenaming(null)
          }}
        />
      )}
    </div>
  )
}

/** Rename a controlled-vocab value (prefilled with the current value). Mirrors
 * DeleteValueConfirm's shell; the input autofocuses and selects so a typo fix is
 * one keystroke. A blank or unchanged value, or one that collides with another
 * registered value (case-sensitive, matching the backend), disables Save — the
 * collision hint nudges toward delete/merge instead. Sits above the Manage
 * dialog (z-[60]); Escape cancels and the backdrop stops click-through so
 * dismissing it keeps the Manage dialog open. */
function RenameValueDialog({
  field,
  value,
  count,
  existing,
  busy,
  onCancel,
  onConfirm,
}: {
  field: string
  value: string
  count: number
  existing: string[]
  busy: boolean
  onCancel: () => void
  onConfirm: (next: string) => void
}) {
  const [next, setNext] = useState(value)
  const trimmed = next.trim()
  const collides = trimmed !== value && existing.includes(trimmed)
  const canSubmit = trimmed.length > 0 && trimmed !== value && !collides && !busy

  return (
    <div
      className="fixed inset-0 z-[60] flex items-center justify-center bg-black/30 backdrop-blur-sm"
      onClick={
        busy
          ? undefined
          : (e) => {
              e.stopPropagation()
              onCancel()
            }
      }
    >
      <div
        onClick={(e) => e.stopPropagation()}
        onKeyDown={(e) => {
          if (e.key === 'Escape') onCancel()
        }}
        className="w-[22rem] animate-grow-in rounded-2xl bg-white p-5 shadow-xl ring-1 ring-stone-200"
      >
        <h2 className="text-sm font-semibold text-stone-900">Rename “{value}”?</h2>
        <p className="mt-1.5 text-xs leading-relaxed text-stone-600">
          Updates the {field} vocabulary
          {count > 0
            ? ` and re-tags ${count} paper${count === 1 ? '' : 's'}`
            : ''}
          . This cannot be undone.
        </p>
        <input
          autoFocus
          type="text"
          value={next}
          disabled={busy}
          onChange={(e) => setNext(e.target.value)}
          onFocus={(e) => e.target.select()}
          onKeyDown={(e) => {
            if (e.key === 'Enter' && canSubmit) onConfirm(trimmed)
          }}
          className="mt-3 w-full rounded-md border border-stone-300 bg-white px-2.5 py-1.5 text-sm text-stone-800 shadow-sm focus:outline-none focus:ring-1 focus:ring-accent-400 disabled:opacity-50"
        />
        {collides && (
          <p className="mt-1.5 text-[11px] text-rose-500">
            “{trimmed}” already exists — delete one instead of renaming onto it.
          </p>
        )}
        <div className="mt-5 flex justify-end gap-2">
          <button
            onClick={onCancel}
            disabled={busy}
            className="rounded-lg px-3 py-1.5 text-xs text-stone-600 transition-colors hover:bg-stone-100 disabled:opacity-40"
          >
            Cancel
          </button>
          <button
            onClick={() => onConfirm(trimmed)}
            disabled={!canSubmit}
            className="rounded-lg bg-accent-500 px-3 py-1.5 text-xs font-medium text-white transition-colors hover:bg-accent-600 disabled:opacity-60"
          >
            {busy ? 'Renaming…' : 'Rename'}
          </button>
        </div>
      </div>
    </div>
  )
}

/** Default-No confirm for deleting a controlled-vocab value (problem 2). The
 * body states whether the value is still in use (untags N papers) or orphaned;
 * Cancel is autofocused and the destructive button is rose, mirroring
 * UnreadConfirm. Sits above the Manage dialog (z-[60]). */
function DeleteValueConfirm({
  field,
  value,
  count,
  busy,
  onCancel,
  onConfirm,
}: {
  field: string
  value: string
  count: number
  busy: boolean
  onCancel: () => void
  onConfirm: () => void
}) {
  return (
    <div
      className="fixed inset-0 z-[60] flex items-center justify-center bg-black/30 backdrop-blur-sm"
      onClick={
        busy
          ? undefined
          : (e) => {
              // Stop the click bubbling to the Manage dialog's backdrop (this
              // confirm renders inside it) — otherwise dismissing the confirm
              // would also tear down the whole Manage dialog in one click.
              e.stopPropagation()
              onCancel()
            }
      }
    >
      <div
        onClick={(e) => e.stopPropagation()}
        onKeyDown={(e) => {
          if (e.key === 'Escape') onCancel()
        }}
        className="w-[22rem] animate-grow-in rounded-2xl bg-white p-5 shadow-xl ring-1 ring-stone-200"
      >
        <h2 className="text-sm font-semibold text-stone-900">Delete “{value}”?</h2>
        <p className="mt-1.5 text-xs leading-relaxed text-stone-600">
          This removes “{value}” from the {field} vocabulary
          {count > 0
            ? ` and untags it from ${count} paper${count === 1 ? '' : 's'}. This cannot be undone.`
            : '. It is not attached to any paper.'}
        </p>
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
            Delete
          </button>
        </div>
      </div>
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

/** Default-No confirm for dropping a paper via the ⌥D shortcut (Phase 4). The
 * cockpit's Status dropdown drops without a prompt, but a one-keystroke ⌥D needs
 * a guard (AC B5 ②: "⌥D 二次确认"). On confirm it calls the SAME
 * `setEnum('status','dropped')` write path the dropdown uses, so this adds a
 * confirmation step, not a second write path (invariant #16). Mirrors the macOS
 * modal shell of UnreadConfirm; Cancel autofocused, destructive button rose. */
function DropConfirm({
  title,
  busy,
  onCancel,
  onConfirm,
}: {
  title: string
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
        <h2 className="text-sm font-semibold text-stone-900">Drop this paper?</h2>
        <p className="mt-1.5 text-xs leading-relaxed text-stone-600">
          Sets the status of “{title}” to dropped, removing it from the active
          reading lists. You can restore it later by changing the status back.
        </p>
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
            Drop
          </button>
        </div>
      </div>
    </div>
  )
}

/** Curation cockpit with structured write controls (Phase 3b/3c/3d).
 *
 * Dropdowns (status/priority/type), the collapsed Tags group (topics/methods/
 * data/projects pills), and mutually-exclusive read/revisit buttons all dispatch
 * to the invariant #16
 * second-class write endpoints (the server runs the `lit` command backend). On
 * success the parent re-fetches via `onChanged`; on error the backend's raw
 * message is toasted via `notify`. Relations / code-clones stay read-only.
 * Collapses to a narrow strip via an animated width, mirroring BrowsePanel.
 */
export default function Cockpit(props: Props) {
  // The trash view shows the same panel chrome but a read-only inspector with a
  // Restore action — no write controls, no imperative handle, no modal state.
  // Split into its own component so the live cockpit's write hooks never run for
  // a trashed paper (invariant #16: no write path reachable in read-only mode).
  if (props.readOnly) {
    return (
      <ReadOnlyCockpit
        paper={props.paper}
        loading={props.loading}
        collapsed={props.collapsed}
        onToggle={props.onToggle}
        onOpenPaper={props.onOpenPaper}
        onRestore={props.onRestore}
        restoring={props.restoring ?? false}
        orphanRepoCount={props.orphanRepoCount ?? 0}
      />
    )
  }
  return <WriteCockpit {...props} />
}

/** Read-only inspector for the trash view: bibliographic header, read-only
 * metadata fields, relations / code-clones (read-only), and a Restore button.
 * No dropdowns, tag editor, read/revisit, copy/cite — a trashed paper is not
 * curated. Shares BrowsePanel/Cockpit's animated collapse chrome. */
function ReadOnlyCockpit({
  paper,
  loading,
  collapsed,
  onToggle,
  onOpenPaper,
  onRestore,
  restoring,
  orphanRepoCount,
}: {
  paper: PaperMeta | null
  loading: boolean
  collapsed: boolean
  onToggle: () => void
  onOpenPaper: (id: string) => void
  onRestore?: () => void
  restoring: boolean
  orphanRepoCount: number
}) {
  return (
    <div
      className={`relative flex shrink-0 overflow-hidden border-l border-stone-200 bg-stone-100 transition-[width] duration-300 ease-fluid ${
        collapsed ? 'w-9' : 'w-80'
      }`}
    >
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

      <aside
        className={`h-full w-80 overflow-auto p-4 transition-opacity duration-200 ${
          collapsed ? 'pointer-events-none opacity-0' : 'opacity-100 delay-100'
        }`}
      >
        <div className="mb-3 flex items-center justify-between">
          <span className="text-[11px] font-semibold uppercase tracking-wider text-stone-500">
            Trashed paper
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
          <div className="text-sm text-stone-500">Select a trashed paper.</div>
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
              <div className="mt-3">
                <button
                  onClick={onRestore}
                  disabled={restoring || !onRestore}
                  className="w-full rounded-lg bg-accent-500 px-3 py-2 text-sm font-medium text-white shadow-sm transition-colors hover:bg-accent-600 disabled:opacity-50"
                >
                  {restoring ? 'Restoring…' : '↩ Restore to library'}
                </button>
                {orphanRepoCount > 0 && (
                  <div className="mt-1.5 rounded-md border border-amber-300 bg-amber-50 px-2.5 py-1.5 text-[11px] leading-relaxed text-amber-700 dark:border-amber-700 dark:bg-amber-950/40 dark:text-amber-300">
                    {orphanRepoCount} hard-deleted{' '}
                    {orphanRepoCount === 1 ? 'repo needs' : 'repos need'} re-clone
                    in the CLI after restore (lit trash restore / lit
                    health-check).
                  </div>
                )}
              </div>
            </div>
            <Field label="Authors">
              {paper.authors && paper.authors.length > 0 ? (
                <span
                  title={
                    paper.authors.length > AUTHOR_HEAD + AUTHOR_TAIL
                      ? paper.authors.join('; ')
                      : undefined
                  }
                >
                  {formatAuthors(paper.authors)}
                </span>
              ) : (
                '—'
              )}
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
              <span className="text-stone-700">{paper.status || '—'}</span>
            </Field>
            <Field label="Priority">
              <span className="text-stone-700">{paper.priority || '—'}</span>
            </Field>
            <Field label="Type">
              <span className="text-stone-700">{paper.type || '—'}</span>
            </Field>
            <Field label="Read-date">
              <span className="text-stone-700">{paper['read-date'] || '—'}</span>
            </Field>
            <Field label="Topics">
              <Chips values={paper.topics} />
            </Field>
            <Field label="Methods">
              <Chips values={paper.methods} />
            </Field>
            <Field label="Data">
              <Chips values={paper.data} />
            </Field>
            <Field label="Projects">
              <Chips values={paper.projects} />
            </Field>
            <Field label="Relations">
              <Relations paper={paper} onOpenPaper={onOpenPaper} />
            </Field>
            <Field label="Code-clones">
              <CodeCloneChips
                values={paper['code-clones']}
                missing={paper['code-clones-missing']}
              />
            </Field>
          </div>
        )}
      </aside>
    </div>
  )
}

function WriteCockpit({
  paper,
  loading,
  collapsed,
  onToggle,
  onOpenPaper,
  vaultPath = null,
  taxonomy = null,
  projects = [],
  allPapers = [],
  fixedEnums = null,
  onChanged = () => {},
  onVocabChanged = () => {},
  notify = () => {},
  onRegisterHandle = () => {},
  onModalState = () => {},
}: Props) {
  const [copied, setCopied] = useState<string | null>(null)
  // Caveats from the last Cite (unverified abbreviation, missing fields, ...).
  // Auto-dismissed after a few seconds (long enough to read a couple of lines)
  // so it behaves like a transient toast, not a banner that sticks until the
  // paper changes. `citeWarnTimer` lets a re-Cite reset the countdown instead
  // of letting an older timer clear the fresh warning early.
  const [citeWarn, setCiteWarn] = useState<string[] | null>(null)
  const citeWarnTimer = useRef<ReturnType<typeof setTimeout> | null>(null)
  // A structured write is in flight — disables the write controls so a second
  // click can't race the first (the backend serialises, but a double-fire would
  // toast a confusing intermediate state).
  const [writing, setWriting] = useState(false)
  // Unread-confirm dialog (problem 5): clearing read-date reverses an
  // immutable-by-default stamp, so it sits behind a default-No confirm that also
  // warns the revisit record is dropped. Only reachable when readDate != null.
  const [showUnread, setShowUnread] = useState(false)
  // Drop-confirm dialog (Phase 4): the ⌥D shortcut routes here so a one-keystroke
  // drop is guarded (the Status dropdown drops without a prompt; the shortcut
  // needs the二次确认). On confirm it calls the existing setEnum write path.
  const [showDrop, setShowDrop] = useState(false)
  // The currently-open pill in the Tags group (topics/methods/data/projects), or
  // null. One field at a time: opening one collapses the others (problem 1/3).
  const [openField, setOpenField] = useState<string | null>(null)
  // The controlled-vocab field whose Manage dialog is open (problem 2), or null.
  const [manageField, setManageField] = useState<
    'topics' | 'methods' | 'data' | null
  >(null)

  // The currently-shown paper id, refreshed synchronously each render and read
  // inside the async handlers to drop a response that lands after the user has
  // already moved to a different paper.
  const shownId = useRef<string | undefined>(paper?.id)
  shownId.current = paper?.id

  // Selecting a different paper clears stale copy/cite feedback and collapses any
  // open dropdown / dialog (they pertain to the previous paper).
  useEffect(() => {
    setCopied(null)
    setCiteWarn(null)
    if (citeWarnTimer.current) clearTimeout(citeWarnTimer.current)
    setShowUnread(false)
    setShowDrop(false)
    setOpenField(null)
    setManageField(null)
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
      // Restart the warning countdown each Cite so a fresh banner gets its full
      // dwell time and an in-flight older timer can't clear it early.
      if (citeWarnTimer.current) clearTimeout(citeWarnTimer.current)
      if (warnings.length) {
        citeWarnTimer.current = setTimeout(() => setCiteWarn(null), 8000)
      }
    } catch {
      /* no clipboard / fetch failure — silently ignore */
    }
  }

  // Run a structured write, then refresh on success or toast the backend's raw
  // message on failure. `writing` gates the controls for the duration. The
  // backend serialises writes; the parent's onChanged re-fetches the cockpit.
  // `vocabChanged` additionally refreshes the shared taxonomy/projects caches
  // (a new/removed taxonomy value or a project link/unlink the dropdowns
  // reflect). Returns the promise so a caller (e.g. delete-confirm) can await
  // completion before dismissing its dialog.
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

  // Delete a controlled-vocab value through the `lit taxonomy rm` backend
  // (problem 2). vocabChanged=true so the Manage list + every field dropdown
  // refresh; onChanged additionally re-pulls the INDEX so "used by N" and the
  // selected paper's own tags reflect the cascade. Returned so the confirm
  // dialog can await it before closing.
  function deleteValue(
    field: 'topics' | 'methods' | 'data',
    value: string,
  ): Promise<void> {
    return runWrite(() => deleteTaxonomyValue(field, value), true)
  }

  // Rename a controlled-vocab value through the `lit taxonomy rename` backend.
  // vocabChanged=true so the Manage list + every field dropdown refresh; the
  // INDEX re-pull keeps "used by N" and the selected paper's own tags in sync
  // with the cascade. Returned so the rename dialog can await it before closing.
  function renameValue(
    field: 'topics' | 'methods' | 'data',
    old: string,
    next: string,
  ): Promise<void> {
    return runWrite(() => renameTaxonomyValue(field, old, next), true)
  }

  // In-use count for a value, read off the loaded INDEX (no round-trip). Backs
  // the Manage list's "used by N" and the delete confirm's wording.
  function countValue(
    field: 'topics' | 'methods' | 'data',
    value: string,
  ): number {
    return allPapers.filter((p) => (p[field] ?? []).includes(value)).length
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

  // Confirmed drop (Phase 4, ⌥D path): routes through the existing
  // setEnum('status','dropped') write, so the only addition over the dropdown is
  // the preceding二次确认 (DropConfirm), not a second write path.
  function doDrop() {
    setShowDrop(false)
    setEnum('status', 'dropped')
  }

  const readDate = paper?.['read-date'] ?? null
  const lastRevisited = paper?.['last-revisited'] ?? null

  // --- Keyboard-shortcut handle (Phase 4) ----------------------------------
  // The global dispatcher drives curation through the SAME local handlers the
  // cockpit's own buttons call (no second write path, invariant #16). The
  // handlers are recreated each render (they close over the live `paper`), so we
  // mirror the dispatch table into a ref and register a STABLE handle that reads
  // it — registering the live closures would re-fire onRegisterHandle every
  // render. App already no-ops + toasts when no paper is selected, so the
  // ⌥-actions here can assume a selection (they also guard internally).
  const actionsRef = useRef<CockpitHandle>({
    triggerRead: () => {},
    triggerUnread: () => {},
    triggerPromote: () => {},
    triggerDrop: () => {},
    openTags: () => {},
    copyPath: () => {},
    copyId: () => {},
  })
  actionsRef.current = {
    triggerRead: markRead,
    // ↺ undo only makes sense once read; mirror the button's gate (it is hidden
    // until readDate != null). On an unread paper, undo-of-a-non-action is inert,
    // so toast a subtle hint rather than no-op silently (误触看得见) — no write.
    triggerUnread: () => {
      if (readDate != null) setShowUnread(true)
      else notify('尚未标记已读')
    },
    // `lit promote` is sugar for status=deep-read (commands/promote.py); mirror
    // that exact value through the existing setEnum write rather than invent a
    // "promoted" status the fixed-enum whitelist (deep-read/skim/inbox/dropped)
    // would reject.
    triggerPromote: () => setEnum('status', 'deep-read'),
    triggerDrop: () => setShowDrop(true),
    openTags: () => setOpenField('topics'),
    copyPath: () => {
      if (paper && vaultPath) doCopy('path', `${vaultPath}/papers/${paper.id}`)
    },
    copyId: () => {
      if (paper) doCopy('ID', paper.id)
    },
  }
  useEffect(() => {
    const stable: CockpitHandle = {
      triggerRead: () => actionsRef.current.triggerRead(),
      triggerUnread: () => actionsRef.current.triggerUnread(),
      triggerPromote: () => actionsRef.current.triggerPromote(),
      triggerDrop: () => actionsRef.current.triggerDrop(),
      openTags: () => actionsRef.current.openTags(),
      copyPath: () => actionsRef.current.copyPath(),
      copyId: () => actionsRef.current.copyId(),
    }
    onRegisterHandle(stable)
    return () => onRegisterHandle(null)
  }, [onRegisterHandle])

  // Report cockpit-owned modal state up so the shortcut dispatcher's modal guard
  // suppresses global keys while a confirm / dictionary / tag panel is open.
  const modalOpen =
    showUnread || showDrop || manageField != null || openField != null
  useEffect(() => {
    onModalState(modalOpen)
  }, [modalOpen, onModalState])

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
              {paper.authors && paper.authors.length > 0 ? (
                <span
                  title={
                    paper.authors.length > AUTHOR_HEAD + AUTHOR_TAIL
                      ? paper.authors.join('; ')
                      : undefined
                  }
                >
                  {formatAuthors(paper.authors)}
                </span>
              ) : (
                '—'
              )}
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

            <TagGroup
              openKey={openField}
              onOpenKey={setOpenField}
              busy={writing}
              fields={[
                {
                  key: 'topics',
                  label: 'Topics',
                  values: paper.topics,
                  vocabulary: taxonomy?.topics ?? [],
                  onAdd: (v) => addTag('topics', v),
                  onRemove: (v) => removeTag('topics', v),
                  onCreate: (v) => createTag('topics', v),
                  onManage: () => {
                    setOpenField(null)
                    setManageField('topics')
                  },
                },
                {
                  key: 'methods',
                  label: 'Methods',
                  values: paper.methods,
                  vocabulary: taxonomy?.methods ?? [],
                  onAdd: (v) => addTag('methods', v),
                  onRemove: (v) => removeTag('methods', v),
                  onCreate: (v) => createTag('methods', v),
                  onManage: () => {
                    setOpenField(null)
                    setManageField('methods')
                  },
                },
                {
                  key: 'data',
                  label: 'Data',
                  values: paper.data,
                  vocabulary: taxonomy?.data ?? [],
                  onAdd: (v) => addTag('data', v),
                  onRemove: (v) => removeTag('data', v),
                  onCreate: (v) => createTag('data', v),
                  onManage: () => {
                    setOpenField(null)
                    setManageField('data')
                  },
                },
                {
                  key: 'projects',
                  label: 'Projects',
                  values: paper.projects,
                  vocabulary: projects.map((p) => p.name),
                  onAdd: linkProj,
                  onRemove: unlinkProj,
                },
              ]}
            />
            <Field label="Relations">
              <Relations paper={paper} onOpenPaper={onOpenPaper} />
            </Field>
            <Field label="Code-clones">
              <CodeCloneChips
                values={paper['code-clones']}
                missing={paper['code-clones-missing']}
              />
            </Field>
          </div>
        )}
      </aside>

      {manageField && (
        <ManageDialog
          field={manageField}
          vocabulary={taxonomy?.[manageField] ?? []}
          countFor={(v) => countValue(manageField, v)}
          busy={writing}
          onDelete={(v) => deleteValue(manageField, v)}
          onRename={(old, next) => renameValue(manageField, old, next)}
          onClose={() => setManageField(null)}
        />
      )}

      {showDrop && paper && (
        <DropConfirm
          title={paper.title || paper.id}
          busy={writing}
          onCancel={() => setShowDrop(false)}
          onConfirm={doDrop}
        />
      )}
    </div>
  )
}
