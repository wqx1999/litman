import { useEffect, useMemo, useRef, useState } from 'react'
import type { FixedEnums, IndexPaper, PaperMeta, ProjectEntry, Taxonomy } from '../types'
import { projectHealth } from '../projects'

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
  /** status → dropped via the existing confirm — same as the Status dropdown +
   *  the cockpit's own write guard surfacing the backend confirm. */
  triggerDrop(): void
  /** Open the Tags group (Topics pill) so the user can pick a value (no write). */
  openTags(): void
  /** Copy the paper-folder path — same as the Copy path button. */
  copyPath(): void
  /** Copy the paper id — same as the Copy ID button. */
  copyId(): void
}
import {
  ApiError,
  TaxonomySimilarError,
  addTaxonomyValue,
  deleteTaxonomyValue,
  fetchCite,
  linkProject,
  postRead,
  postRevisit,
  postUnread,
  putMetadata,
  removeSiFile,
  renameTaxonomyValue,
  unlinkProject,
} from '../api'
import type { SimilarPair } from '../api'
import type { ToastVariant } from '../ui/Toast'
import {
  parseSupplementary,
  SiRemoveConfirm,
  SupplementaryChips,
  type SiRecord,
} from './Supplementary'
import {
  AUTHOR_HEAD,
  AUTHOR_TAIL,
  Chips,
  CodeCloneChips,
  EnumSelect,
  Field,
  formatAuthors,
} from './fields'
import { TagGroup } from './TagPanel'
import { ManageDialog } from './ManageVocab'
import {
  REVERSE_TO_FORWARD,
  RelationRemoveConfirm,
  Relations,
  relationCount,
} from './Relations'
import type { RelField } from './Relations'
import MetadataEditDialog from './MetadataEditDialog'
import { useEscapeLayer } from '../ui/escapeStack'
import { modalBackdropProps } from '../ui/modalShell'

interface Props {
  paper: PaperMeta | null
  loading: boolean
  /** Why the selected paper could not be loaded — the server's own sentence
   * (a broken metadata.yaml names the file and the fix). Shown in place of the
   * empty state, which would otherwise read as "nothing is selected". */
  loadError?: string | null
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
  /** status/type whitelists for the dropdowns. */
  fixedEnums?: FixedEnums | null
  /** Called after a successful structured write so the parent re-fetches the
   * cockpit paper AND the left list (status/read-date move smart-list members). */
  onChanged?: () => void
  /** Called after a metadata save that set a new title: the backend rewrote
   * the heading of the paper's notes.md in the same write, so the parent
   * re-reads the open md tab and counts that change as the GUI's own. */
  onNotesRetitled?: (paperId: string) => void
  /** Called after a write that changes the shared vocabulary (a new taxonomy
   * value or project link/unlink) so the parent re-fetches /api/taxonomy +
   * /api/projects, keeping the dropdowns/autocomplete current (3c-1). */
  onVocabChanged?: () => void
  /** Toast a message (used to surface the backend's raw error verbatim). */
  notify?: (msg: string, variant?: ToastVariant) => void
  /** Add SI: open the SI window for this paper (App hosts the one window). */
  onAddSi?: (paperId: string) => void
  /** 'desktop': a plain click on an SI chip opens the file with the default
   * app of the machine the server runs on; 'none': the chip is a plain link
   * into the browser (GET /api/capabilities `open_files`). */
  openFiles?: 'desktop' | 'none'
  /** Register/unregister the imperative handle the global keyboard shortcuts
   * (Phase 4) drive curation through. Null on unmount. */
  onRegisterHandle?: (handle: CockpitHandle | null) => void
  /** Report whether any cockpit-owned modal (a Tags field panel, the Manage
   * dictionary dialog, the Unread confirm, the Drop confirm, or the relation-
   * remove confirm) is open, so the shortcut dispatcher's modal guard can
   * suppress global keys while one is up. */
  onModalState?: (open: boolean) => void
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
  useEscapeLayer(true, onCancel)
  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/30 backdrop-blur-sm"
      {...modalBackdropProps}
    >
      <div
        onClick={(e) => e.stopPropagation()}
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
 * a guard (AC B5 ②: ⌥D must confirm). On confirm it calls the SAME
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
  useEscapeLayer(true, onCancel)
  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/30 backdrop-blur-sm"
      {...modalBackdropProps}
    >
      <div
        onClick={(e) => e.stopPropagation()}
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
 * Dropdowns (status/type), the collapsed Tags group (topics/methods/
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
          <span className="text-[11px] font-semibold uppercase tracking-wider text-stone-600">
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
            <Field label="Authors" count={paper.authors?.length}>
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
              <Chips values={paper.projects} suffix={(v) => paper[`priority-${v}`] ?? null} />
            </Field>
            <Field label="Relations" count={relationCount(paper)}>
              <Relations key={paper.id} paper={paper} onOpenPaper={onOpenPaper} />
            </Field>
            <Field label="Code-clones" count={paper['code-clones']?.length}>
              <CodeCloneChips
                values={paper['code-clones']}
                missing={paper['code-clones-missing']}
              />
            </Field>
            <Field
              label="Supplementary"
              count={parseSupplementary(paper.supplementary).length}
            >
              <SupplementaryChips paperId={paper.id} value={paper.supplementary} readOnly />
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
  loadError = null,
  collapsed,
  onToggle,
  onOpenPaper,
  vaultPath = null,
  taxonomy = null,
  projects = [],
  allPapers = [],
  fixedEnums = null,
  onChanged = () => {},
  onNotesRetitled = () => {},
  onVocabChanged = () => {},
  notify = () => {},
  onRegisterHandle = () => {},
  onModalState = () => {},
  onAddSi = () => {},
  openFiles = 'none',
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
  // needs the confirm). On confirm it calls the existing setEnum write path.
  const [showDrop, setShowDrop] = useState(false)
  // The relation the user asked to remove, or null. `me` is the paper the ask
  // was made ON, frozen at click time: the confirm can outlive the selection
  // (the user clicks another paper in the left list with it up), and reading
  // `paper.id` at confirm time would then delete the relation from whichever
  // paper is showing now.
  const [pendingRel, setPendingRel] = useState<{
    me: string
    rel: RelField
    id: string
  } | null>(null)
  // The SI file asked to be deleted, frozen with its paper like `pendingRel`
  // above.
  const [pendingSi, setPendingSi] = useState<{ me: string; record: SiRecord } | null>(
    null,
  )
  // The currently-open pill in the Tags group (topics/methods/data/projects), or
  // null. One field at a time: opening one collapses the others (problem 1/3).
  const [openField, setOpenField] = useState<string | null>(null)
  // The controlled-vocab field whose Manage dialog is open (problem 2), or null.
  const [manageField, setManageField] = useState<
    'topics' | 'methods' | 'data' | null
  >(null)
  // The metadata edit dialog (title/year/journal/authors…) is open.
  const [editingMeta, setEditingMeta] = useState(false)

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
    setPendingRel(null)
    setPendingSi(null)
    setOpenField(null)
    setManageField(null)
    setEditingMeta(false)
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

  function setEnum(field: 'status' | 'type', next: string | null) {
    if (!paper) return
    const id = paper.id
    runWrite(() => putMetadata(id, { set: { [field]: next } }))
  }

  function addTag(field: 'topics' | 'methods' | 'data', value: string) {
    if (!paper) return
    const id = paper.id
    return runWrite(() => putMetadata(id, { addTag: { [field]: [value] } }))
  }

  function removeTag(field: 'topics' | 'methods' | 'data', value: string) {
    if (!paper) return
    const id = paper.id
    runWrite(() => putMetadata(id, { rmTag: { [field]: [value] } }))
  }

  // Remove the confirmed relation through the same rmTag write path the tag
  // chips use. The other paper's paired edge goes in the SAME transaction
  // server-side (ADR-012), so this is one request, never "delete here then
  // delete there". A reverse row flips the originator (see REVERSE_TO_FORWARD);
  // everything else writes the field it is standing on.
  function doRemoveRelation() {
    if (!pendingRel) return
    const { me, rel, id: other } = pendingRel
    setPendingRel(null)
    if (rel === 'extended-by' || rel === 'contradicted-by') {
      // 🔴 The flip is load-bearing, and nothing in the frontend can catch its
      // loss: "simplifying" this to putMetadata(me, {rmTag: {[rel]: [other]}})
      // type-checks, builds, and leaves the whole Python suite green — it only
      // shows up at runtime as a 400 toast. The contract it depends on is
      // pinned server-side by
      // tests/server/test_server_structured.py::test_put_metadata_rm_reverse_field_400,
      // which is the test to read before touching this branch.
      runWrite(async () => {
        try {
          await putMetadata(other, {
            rmTag: { [REVERSE_TO_FORWARD[rel]]: [me] },
          })
        } catch (err) {
          // A 404 is the server finding no paper `other` on disk — deleted
          // outside litman, lost by a sync, or in the trash — so there is no
          // forward edge left to rewrite. dropDangling removes this side
          // alone, the per-edge form of `lit health-check --fix`. Only the
          // server can tell: allPapers is INDEX.json, which can be stale in
          // either direction. It refuses an id still in the library (there,
          // just unreadable); that corner then shows the flip's own error, as
          // it always did.
          if (!(err instanceof ApiError && err.status === 404)) throw err
          try {
            await putMetadata(me, { dropDangling: { [rel]: [other] } })
          } catch (refused) {
            throw refused instanceof ApiError && refused.status === 400
              ? err
              : refused
          }
        }
      })
    } else {
      runWrite(() => putMetadata(me, { rmTag: { [rel]: [other] } }))
    }
  }

  // Delete the confirmed SI file through the `lit rm --si` backend. The
  // confirm stays up until the delete answers.
  async function doRemoveSi() {
    if (!pendingSi) return
    const { me, record } = pendingSi
    await runWrite(async () => {
      const { warnings } = await removeSiFile(me, record.file)
      if (warnings.length) notify(`Deleted ${record.file} · ${warnings.join(' · ')}`, 'warning')
      else notify(`Deleted ${record.file}`, 'success')
    })
    setPendingSi(null)
  }

  // Inline-create: register the new taxonomy value first (invariant #2), then
  // attach it via the existing addTag path — two steps, one button click. The
  // vocabulary refresh keeps the new value in autocomplete for other papers.
  //
  // A value too close to a registered one comes back as the list of those
  // values, for the panel's confirm to ask about; the server's sentence
  // (written for the CLI, it names --force) is never toasted. `force` is the
  // confirm's "Create anyway".
  async function createTag(
    field: 'topics' | 'methods' | 'data',
    value: string,
    force: boolean,
  ): Promise<SimilarPair[] | null> {
    if (!paper) return null
    const id = paper.id
    setWriting(true)
    try {
      await addTaxonomyValue(field, value, force)
    } catch (err) {
      setWriting(false)
      // A 409 says the vocabulary on disk has a value like this one, which this
      // page may not have loaded yet (made by the CLI or an agent since):
      // refresh it, so a match used from the confirm shows up ticked in the
      // popover. Not awaited: the confirm lists the server's matches.
      const refused = err instanceof ApiError && err.status === 409
      if (refused) onVocabChanged()
      if (err instanceof TaxonomySimilarError) return err.similar
      // Without the matches there is nothing to ask about, and the server's
      // sentence is still the CLI's.
      notify(
        refused
          ? `“${value}” looks like an existing value. Look for it in the list.`
          : err instanceof Error
            ? err.message
            : String(err),
      )
      return null
    }
    setWriting(false)
    await runWrite(() => putMetadata(id, { addTag: { [field]: [value] } }), true)
    return null
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
  // the Tags panels' per-value counts, the Manage list's "used by N" and the
  // delete confirm's wording. Counted once per INDEX, not once per row.
  const usage = useMemo(() => {
    const out = {
      topics: new Map<string, number>(),
      methods: new Map<string, number>(),
      data: new Map<string, number>(),
    }
    for (const p of allPapers) {
      for (const f of ['topics', 'methods', 'data'] as const) {
        for (const v of new Set(p[f] ?? [])) out[f].set(v, (out[f].get(v) ?? 0) + 1)
      }
    }
    return out
  }, [allPapers])
  function countValue(field: 'topics' | 'methods' | 'data', value: string): number {
    return usage[field].get(value) ?? 0
  }

  /** Link + grade in ONE request (ADR-025 decision 5): the panel's unlinked
   * row only ever links by picking a letter, so there is no ungraded link for
   * the GUI to create. `vocabChanged` stays true — membership moved. */
  function linkProj(project: string, grade?: string) {
    if (!paper) return
    const id = paper.id
    runWrite(() => linkProject(id, project, undefined, grade), true)
  }

  /** Regrade an ALREADY-linked project: metadata only, so no membership change
   * and no vocab refresh. Core refuses a project the paper is not in, which is
   * why the caller must never route an unlinked row here. */
  function gradeProj(project: string, grade: string) {
    if (!paper) return
    const id = paper.id
    runWrite(() => putMetadata(id, { set: { [`priority-${project}`]: grade } }))
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
  // the preceding confirm (DropConfirm), not a second write path.
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
    // so toast a subtle hint rather than no-op silently (a mis-key stays
    // visible) — no write.
    triggerUnread: () => {
      if (readDate != null) setShowUnread(true)
      else notify('Not marked read yet')
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
  // suppresses global keys while a confirm / dictionary / tag panel is open —
  // including the relation-remove confirm, whose × is reachable straight from
  // the panel, so a stray ⌥D under it would stack a second dialog.
  const modalOpen =
    showUnread ||
    showDrop ||
    pendingRel != null ||
    pendingSi != null ||
    manageField != null ||
    openField != null
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
          <span className="text-[11px] font-semibold uppercase tracking-wider text-stone-600">
            Metadata
          </span>
          <div className="flex items-center gap-2.5">
            {/* Edit lives on the section header, not among the Copy/Cite pills
                below: as a fourth pill it wrapped to a line of its own on a
                narrow panel and read as a button bolted on for one feature.
                Here it is scoped to exactly what it edits — the dialog covers
                title / year / journal / authors / doi, which IS this section
                (the tags underneath edit in place and are none of its
                business) — at the same visual weight as the collapse chevron.

                Always visible, never hover-only: the reason this exists is to
                let someone fix the `Unknown` rows health-check reports, and an
                entry point you have to discover by hovering is one they will
                not find. This header is a plain div, so a button nests here
                fine (unlike the list row's dot). */}
            {paper && (
              <button
                onClick={() => setEditingMeta(true)}
                disabled={writing}
                title="Edit title, year, journal, authors and other bibliographic fields"
                aria-label="Edit metadata"
                className="text-sm leading-none text-stone-400 transition-colors hover:text-accent-600 disabled:opacity-40"
              >
                ✎
              </button>
            )}
            <button
              onClick={onToggle}
              title="Collapse metadata"
              className="text-stone-500 transition-colors hover:text-stone-800"
            >
              ›
            </button>
          </div>
        </div>

        {loading && <div className="text-sm text-stone-500">Loading…</div>}
        {!loading && !paper && loadError && (
          <div className="break-words rounded-md border border-red-300 bg-red-50 px-2.5 py-1.5 text-xs leading-relaxed text-red-700 dark:border-red-800 dark:bg-red-950/40 dark:text-red-300">
            {loadError}
          </div>
        )}
        {!loading && !paper && !loadError && (
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
            <Field label="Authors" count={paper.authors?.length}>
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
                      ? 'Mark as read first'
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
                  count: (v) => countValue('topics', v),
                  onAdd: (v) => addTag('topics', v),
                  onRemove: (v) => removeTag('topics', v),
                  onCreate: (v, force) => createTag('topics', v, force),
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
                  count: (v) => countValue('methods', v),
                  onAdd: (v) => addTag('methods', v),
                  onRemove: (v) => removeTag('methods', v),
                  onCreate: (v, force) => createTag('methods', v, force),
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
                  count: (v) => countValue('data', v),
                  onAdd: (v) => addTag('data', v),
                  onRemove: (v) => removeTag('data', v),
                  onCreate: (v, force) => createTag('data', v, force),
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
                  // A project whose folder has moved (or is half-registered)
                  // misbehaves on link — refused, or quietly deepening the
                  // split — so say so in the list, not on the click.
                  note: (name: string) => {
                    const p = projects.find((x) => x.name === name)
                    return p ? projectHealth(p.status) : null
                  },
                  onAdd: linkProj,
                  onRemove: unlinkProj,
                  grade: (name: string) => paper[`priority-${name}`] ?? null,
                  // One entry point, two writes: an already-linked project is a
                  // metadata edit, an unlinked one is link+grade in a single
                  // request. Deciding here (not in the row) keeps the row
                  // ignorant of the API shape.
                  onGrade: (name: string, next: string) =>
                    (paper.projects ?? []).includes(name)
                      ? gradeProj(name, next)
                      : linkProj(name, next),
                },
              ]}
            />
            <Field label="Relations" count={relationCount(paper)}>
              <Relations
                key={paper.id}
                paper={paper}
                onOpenPaper={onOpenPaper}
                onRemove={(rel, id) => setPendingRel({ me: paper.id, rel, id })}
                busy={writing}
              />
            </Field>
            <Field label="Code-clones" count={paper['code-clones']?.length}>
              <CodeCloneChips
                values={paper['code-clones']}
                missing={paper['code-clones-missing']}
                vaultPath={vaultPath}
                upstream={paper['code-clones-upstream']}
              />
            </Field>
            <Field
              label="Supplementary"
              count={parseSupplementary(paper.supplementary).length}
            >
              <SupplementaryChips
                paperId={paper.id}
                value={paper.supplementary}
                busy={writing}
                openFiles={openFiles}
                notify={notify}
                onRemove={(record) => setPendingSi({ me: paper.id, record })}
                onAdd={() => onAddSi(paper.id)}
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

      {pendingRel && (
        <RelationRemoveConfirm
          rel={pendingRel.rel}
          id={pendingRel.id}
          busy={writing}
          onCancel={() => setPendingRel(null)}
          onConfirm={doRemoveRelation}
        />
      )}

      {pendingSi && (
        <SiRemoveConfirm
          paperId={pendingSi.me}
          record={pendingSi.record}
          busy={writing}
          onCancel={() => setPendingSi(null)}
          onConfirm={() => void doRemoveSi()}
        />
      )}

      {editingMeta && paper && (
        <MetadataEditDialog
          // Remount per paper: the dialog seeds its draft state from the paper
          // it opened on, and must never carry a draft across a selection swap.
          key={paper.id}
          paper={paper}
          onSave={async (body) => {
            // Deliberately NOT runWrite: on failure the dialog shows the
            // backend's message inline and stays open (runWrite would toast
            // and swallow), and on success onChanged re-fetches the cockpit.
            await putMetadata(paper.id, body)
            onChanged()
            // A cleared title (null) leaves notes.md as it was.
            if (typeof body.set?.title === 'string') onNotesRetitled(paper.id)
          }}
          onClose={() => setEditingMeta(false)}
        />
      )}
    </div>
  )
}
