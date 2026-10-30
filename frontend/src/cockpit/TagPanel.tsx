import { useRef, useState } from 'react'
import type { ProjectHealth } from '../projects'
import type { SimilarPair } from '../api'
import VocabPopover from '../ui/VocabPopover'
import { EnumSelect } from './fields'
import { useEscapeLayer } from '../ui/escapeStack'
import { modalBackdropProps } from '../ui/modalShell'

/** The per-project grade vocabulary (ADR-025). Not from `/api/fixed-enums`:
 * that endpoint serves whole-field enums, and this one is keyed per project, so
 * core exposes it as PROJECT_PRIORITY_VALUES rather than a dropdown whitelist.
 * Three letters that only change with a code release, so the list is written
 * out here. */
const PROJECT_GRADES = ['A', 'B', 'C']

/** One pill in the Tags group: the field name plus a count badge when the field
 * has values. Clicking toggles the field's value popover open/closed; `ref` is
 * what the popover lines up with. A filled (accent) pill means the field is
 * non-empty, so the collapsed group still reads at a glance which fields are
 * tagged (problem 1's glanceability cost). */
function TagPill({
  label,
  count,
  active,
  disabled,
  onClick,
  ref,
}: {
  label: string
  count: number
  active: boolean
  disabled: boolean
  onClick: () => void
  ref?: React.Ref<HTMLButtonElement>
}) {
  const filled = count > 0
  return (
    <button
      ref={ref}
      type="button"
      aria-label={`${label} (${count} selected)`}
      aria-haspopup="dialog"
      aria-expanded={active}
      disabled={disabled}
      onClick={onClick}
      className={`inline-flex items-center gap-1.5 rounded-md border px-2.5 py-1 text-xs font-medium shadow-sm transition-colors focus:outline-none focus:ring-1 focus:ring-accent-500 disabled:opacity-50 ${
        filled
          ? 'border-accent-200 bg-accent-50 text-accent-700 hover:bg-accent-100'
          : 'border-stone-300 bg-white text-stone-500 hover:bg-stone-50'
      } ${active ? 'ring-1 ring-accent-500' : ''}`}
    >
      <span>{label}</span>
      {filled && (
        <span className="grid h-4 min-w-4 place-items-center rounded-full bg-accent-200 px-1 text-[10px] font-bold text-accent-700">
          {count}
        </span>
      )}
    </button>
  )
}

/** One projects row of a TagPanel (topics / methods / data use the popover's
 * plain row).
 *
 * A div carrying a name button and a grade `<select>` — buttons cannot nest, so
 * the row itself is not one. The name wraps instead of truncating
 * (`break-words`, no `truncate`): project names are long and the popover is
 * narrow, and a name the user cannot read is worse than a taller row.
 * `items-start` + `ml-auto shrink-0`
 * keeps the dropdown pinned to the FIRST line while the name flows under it.
 *
 * The two link states differ deliberately (ADR-025 decision 5):
 *   - linked   → options A/B/C, no "–". Clearing a grade is not a GUI gesture;
 *                unlink is. The GUI only ever repairs the ungraded state that
 *                the CLI and the migration can create, never creates one.
 *   - unlinked → options –/A/B/C; picking a letter links AND grades in one
 *                write. Clicking the name opens the picker instead of linking,
 *                because requiring a grade at link time IS the gesture.
 *
 * An unlinked row whose project is unhealthy shows the badge and no dropdown:
 * the link would be refused anyway. A LINKED unhealthy row keeps its dropdown —
 * a grade is metadata and does not need the project directory to exist.
 */
function TagPanelRow({
  value,
  on,
  health,
  grade,
  busy,
  onToggle,
  onGrade,
}: {
  value: string
  on: boolean
  health: ProjectHealth | null
  grade: string | null
  busy: boolean
  onToggle: () => void
  onGrade: (value: string, grade: string) => void
}) {
  const selectRef = useRef<HTMLSelectElement>(null)
  const badge = health && (
    <span
      className={`ml-auto shrink-0 text-[10px] font-semibold uppercase tracking-wide ${
        health.tone === 'missing' ? 'text-rose-600' : 'text-amber-600'
      }`}
    >
      {health.badge}
    </span>
  )

  // Linking is refused for an unhealthy project, so an unlinked one gets no
  // picker to offer a write that cannot land.
  const gradable = on || health == null

  return (
    <div className="flex items-start gap-2 rounded-md px-2 py-1 text-sm transition-colors hover:bg-stone-100">
      <span
        className={`w-3 shrink-0 pt-0.5 ${on ? 'text-accent-600' : 'text-transparent'}`}
      >
        ✓
      </span>
      <button
        type="button"
        disabled={busy}
        title={
          health?.title ??
          (on ? `Unlink ${value}` : `Grade to link this paper to ${value}`)
        }
        onClick={() => {
          if (on) {
            onToggle()
            return
          }
          // Progressive enhancement: showPicker() is missing on older
          // browsers, and a focused select is still keyboard-operable without
          // it. The guard is a RUNTIME one — the typed DOM lib declares the
          // method, so `'showPicker' in el` would narrow the else branch to
          // `never` and hide the fallback from the compiler.
          const el = selectRef.current
          if (!el) return
          if (typeof el.showPicker === 'function') el.showPicker()
          else el.focus()
        }}
        className={`min-w-0 flex-1 whitespace-normal break-words text-left transition-colors disabled:opacity-50 ${
          health ? 'text-amber-700' : 'text-stone-700'
        }`}
      >
        {value}
      </button>
      {gradable ? (
        <div className="ml-auto shrink-0">
          <EnumSelect
            field={`priority-${value}`}
            value={grade}
            options={PROJECT_GRADES}
            // A linked row cannot go back to ungraded from here; an unlinked
            // one shows "–" because it has no grade yet.
            allowsNone={!on}
            disabled={busy}
            compact
            selectRef={selectRef}
            onPick={(next) => {
              if (next != null) onGrade(value, next)
            }}
          />
        </div>
      ) : (
        badge
      )}
    </div>
  )
}

/** The value popover for one tag field (problem 3), opened from its pill. The
 * input is search-only (never doubles as a create box); the list shows every
 * registered value with the number of papers using it (attached ones get a ✓,
 * click to add/remove); a contextual "Create" row appears under the list only
 * when the typed text matches no existing value (controlled-vocab fields), and
 * a value the server finds too close to registered ones is asked about in
 * CreateSimilarConfirm before anything is created; a "Manage…" footer enters
 * the dictionary editor.
 * `onCreate`/`onManage` are omitted for projects (a project is created/deleted
 * from the TopBar, never here), whose rows carry their grade picker. The panel
 * mounts fresh per open (keyed on field in TagGroup), so the draft is always
 * empty on open. */
function TagPanel({
  field,
  label,
  values,
  vocabulary,
  count,
  note,
  busy,
  onAdd,
  onRemove,
  grade,
  onGrade,
  onCreate,
  onManage,
  onClose,
  anchor,
}: {
  field: string
  label: string
  values: string[] | undefined
  vocabulary: string[]
  /** Papers in the library using `value`; absent = no count (projects). */
  count?: (value: string) => number
  /** Per-value health marker, for a vocabulary whose entries can go bad behind
   * litman's back. Only projects supply it (a project is bound to a folder, and
   * the folder can move); a taxonomy value is just a word and cannot rot. A
   * marked value stays listed and stays clickable — hiding it would be one more
   * thing happening without the user being told. The title says what picking it
   * means: a missing or taxonomy-only project refuses the link, a config-only
   * one links but lands the tag outside the taxonomy. */
  note?: (value: string) => ProjectHealth | null
  busy: boolean
  onAdd: (value: string) => void | Promise<void>
  onRemove: (value: string) => void
  /** See TagFieldConfig — projects only. */
  grade?: (value: string) => string | null
  onGrade?: (value: string, grade: string) => void
  /** Register-then-attach a brand-new taxonomy value (controlled-vocab fields).
   * Resolves to the registered values it was refused for looking like. */
  onCreate?: (value: string, force: boolean) => Promise<SimilarPair[] | null>
  /** Open the dictionary editor for this field (controlled-vocab fields). */
  onManage?: () => void
  onClose: () => void
  /** The pill this panel opened from. */
  anchor: () => HTMLElement | null
}) {
  // A create the server refused as too close to registered values, waiting on
  // CreateSimilarConfirm; `settle` resumes the popover's pending create with
  // whether the dialog ended in a write.
  const [refusal, setRefusal] = useState<{
    value: string
    pairs: SimilarPair[]
    settle: (done: boolean) => void
  } | null>(null)
  const attached = values ?? []
  const toggle = (v: string) => (attached.includes(v) ? onRemove(v) : onAdd(v))
  const items = vocabulary
    .slice()
    .sort((a, b) => a.localeCompare(b))
    .map((v) => ({ value: v, count: count ? count(v) : null, on: attached.includes(v) }))
  const create =
    onCreate &&
    (async (value: string): Promise<SimilarPair[] | null> => {
      const pairs = await onCreate(value, false)
      if (!pairs?.length) return null
      const done = await new Promise<boolean>((settle) => setRefusal({ value, pairs, settle }))
      setRefusal(null)
      return done ? null : pairs
    })
  return (
    <>
      <VocabPopover
        label={label}
        noun={field}
        values={items}
        anchor={anchor}
        align="right"
        busy={busy}
        taken={attached}
        onToggle={toggle}
        onCreate={create}
        onManage={onManage}
        onClose={onClose}
        renderRow={
          onGrade
            ? (item) => (
                <TagPanelRow
                  value={item.value}
                  on={item.on}
                  health={note?.(item.value) ?? null}
                  grade={grade?.(item.value) ?? null}
                  busy={busy}
                  onToggle={() => toggle(item.value)}
                  onGrade={onGrade}
                />
              )
            : undefined
        }
      />
      {refusal && onCreate && (
        <CreateSimilarConfirm
          field={field}
          value={refusal.value}
          pairs={refusal.pairs}
          taken={attached}
          busy={busy}
          onUse={async (v) => {
            await onAdd(v)
            refusal.settle(true)
          }}
          onCancel={() => refusal.settle(false)}
          onConfirm={async () => {
            await onCreate(refusal.value, true)
            refusal.settle(true)
          }}
        />
      )}
    </>
  )
}

/** Config for one field in the Tags group. */
type TagFieldConfig = {
  key: string
  label: string
  values: string[] | undefined
  vocabulary: string[]
  note?: (value: string) => ProjectHealth | null
  /** Papers in the library using `value` (controlled-vocab fields). */
  count?: (value: string) => number
  onAdd: (value: string) => void | Promise<void>
  onRemove: (value: string) => void
  onCreate?: (value: string, force: boolean) => Promise<SimilarPair[] | null>
  onManage?: () => void
  /** This paper's grade for `value`, or null when the link is ungraded.
   * Supplied ONLY by projects — a grade is a property of a paper↔project link,
   * and a topic/method/data tag has nothing to be graded against. */
  grade?: (value: string) => string | null
  /** Set the grade for `value`. On an already-linked project this is a
   * metadata write; on an unlinked one it links AND grades in one request. */
  onGrade?: (value: string, grade: string) => void
}

/** The "Tags" group (problem 1): collapses the four multi-value association
 * fields (topics/methods/data via TAXONOMY, projects via the registry) under one
 * header so they no longer stack as four full-width dropdowns. The body is a row
 * of pills; clicking a pill opens that field's TagPanel, a popover lined up with
 * the pill's right edge, one at a time. A click outside the panel and its pill
 * (or Esc) closes it. The open field is held by the parent (shared `openField`)
 * so it resets on paper change. */
export function TagGroup({
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
  const pills = useRef<Record<string, HTMLButtonElement | null>>({})
  const openCfg = fields.find((f) => f.key === openKey) ?? null

  return (
    <div className="mb-3.5">
      <div className="mb-1 text-[11px] font-semibold uppercase tracking-wider text-stone-600">
        Tags
      </div>
      <div className="flex flex-wrap gap-1.5">
        {fields.map((f) => (
          <TagPill
            key={f.key}
            ref={(el) => {
              pills.current[f.key] = el
            }}
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
          label={openCfg.label}
          values={openCfg.values}
          vocabulary={openCfg.vocabulary}
          count={openCfg.count}
          note={openCfg.note}
          busy={busy}
          onAdd={openCfg.onAdd}
          onRemove={openCfg.onRemove}
          grade={openCfg.grade}
          onGrade={openCfg.onGrade}
          onCreate={openCfg.onCreate}
          onManage={openCfg.onManage}
          onClose={() => onOpenKey(null)}
          anchor={() => pills.current[openCfg.key] ?? null}
        />
      )}
    </div>
  )
}

/** Default-No confirm for a new controlled-vocab value the server found too
 * close to registered ones (its 409). Every match is listed with its reason; a
 * click on one tags the paper with that existing value instead, and nothing is
 * created. "Create anyway" registers the value regardless and tags the paper
 * with it. Cancel and Esc close only this dialog: the value popover under it
 * keeps the typed text. The server's own message is written for the CLI (it
 * names --force) and is never shown. Same shell as DeleteValueConfirm, stacked
 * above the value popover; mid-write Esc is held, as in RemovePaperConfirm.
 * A dense vocabulary can return dozens of matches: the card stays inside the
 * window and only the list scrolls, so the title and both buttons stay in
 * reach. Matches already on the paper carry the popover's tick. */
function CreateSimilarConfirm({
  field,
  value,
  pairs,
  taken,
  busy,
  onUse,
  onCancel,
  onConfirm,
}: {
  field: string
  value: string
  pairs: SimilarPair[]
  /** Values already on the paper. */
  taken: readonly string[]
  busy: boolean
  onUse: (existing: string) => void
  onCancel: () => void
  onConfirm: () => void
}) {
  useEscapeLayer(true, busy ? null : onCancel)
  return (
    <div
      className="fixed inset-0 z-[60] flex items-center justify-center bg-black/30 backdrop-blur-sm"
      {...modalBackdropProps}
    >
      <div
        onClick={(e) => e.stopPropagation()}
        role="dialog"
        aria-modal="true"
        aria-label={`Create “${value}”?`}
        className="flex w-[22rem] animate-grow-in flex-col rounded-2xl bg-white p-5 shadow-xl ring-1 ring-stone-200"
        style={{ maxHeight: 'calc(100vh - 32px)' }}
      >
        <h2 className="text-sm font-semibold text-stone-900">Create “{value}”?</h2>
        <p className="mt-1.5 text-xs leading-relaxed text-stone-600">
          Similar {field} already exist:
        </p>
        <ul className="-mx-2 mt-2 min-h-0 overflow-y-auto overscroll-contain">
          {pairs.map(([v, why]) => (
            <li key={v}>
              <button
                type="button"
                title={`Use ${v}`}
                disabled={busy}
                onClick={() => onUse(v)}
                className="flex w-full items-center gap-2 rounded-md px-2 py-1 text-left text-xs text-stone-700 transition-colors hover:bg-stone-100 disabled:opacity-50"
              >
                <span
                  className={`w-3 shrink-0 ${taken.includes(v) ? 'text-accent-600' : 'text-transparent'}`}
                >
                  ✓
                </span>
                <span className="min-w-0 break-words">{v}</span>
                <span className="ml-auto shrink-0 text-[11px] text-stone-400">{why}</span>
              </button>
            </li>
          ))}
        </ul>
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
            className="rounded-lg bg-accent-500 px-3 py-1.5 text-xs font-medium text-white transition-colors hover:bg-accent-600 disabled:opacity-60"
          >
            Create anyway
          </button>
        </div>
      </div>
    </div>
  )
}
