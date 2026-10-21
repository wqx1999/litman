import { siFileUrl } from '../api'
import { formatBytes } from '../ui/bytes'
import { useEscapeLayer } from '../ui/escapeStack'
import { modalBackdropProps } from '../ui/modalShell'

/** One recorded SI file, as far as the cockpit needs it. */
export interface SiRecord {
  file: string
  original: string | null
  bytes: number | null
}

// core/supplementary.py `_SI_NAME_RE`: si-01.pdf, si-02.tar.gz, si-100.xlsx.
const SI_NAME = /^si-(\d{2,})((?:\.[a-z0-9]+){0,2})$/

/** The paper's SI records, read the way core's `list_supplementary` reads
 * them: a list of mappings with a valid, unrepeated `file`; anything else in
 * the field is left out (`lit health-check` is what reports it). */
export function parseSupplementary(value: unknown): SiRecord[] {
  if (!Array.isArray(value)) return []
  const out: SiRecord[] = []
  const seen = new Set<string>()
  for (const entry of value) {
    if (entry === null || typeof entry !== 'object' || Array.isArray(entry)) continue
    const record = entry as Record<string, unknown>
    const file = record.file
    if (typeof file !== 'string' || !SI_NAME.test(file) || seen.has(file)) continue
    seen.add(file)
    const { original, bytes } = record
    out.push({
      file,
      original: typeof original === 'string' && original ? original : null,
      bytes: typeof bytes === 'number' && Number.isFinite(bytes) && bytes >= 0 ? bytes : null,
    })
  }
  return out
}

const chipLabel = (r: SiRecord) =>
  r.bytes === null ? r.file : `${r.file} · ${formatBytes(r.bytes)}`

const CHIP = 'rounded-md bg-stone-200 px-2 py-0.5 text-xs text-stone-700'

/** The Supplementary row: one chip per SI file, hover for the name it was
 * downloaded under.
 *
 * Editable, each chip opens the file in a new tab (the browser's own viewer
 * for a PDF, a download under the original name for anything else) and has a
 * × at its end; "+ Add SI" at the end of the row opens the SI window. Read-only
 * (the trash inspector) the chips are plain: no route serves a trashed
 * paper's SI, so a link there would only lead to a 404. */
export function SupplementaryChips({
  paperId,
  value,
  readOnly = false,
  busy = false,
  onRemove,
  onAdd,
}: {
  paperId: string
  /** The paper's raw `supplementary` field. */
  value: unknown
  readOnly?: boolean
  /** A write is in flight — the × and Add SI stop taking clicks. */
  busy?: boolean
  onRemove?: (record: SiRecord) => void
  onAdd?: () => void
}) {
  const records = parseSupplementary(value)
  if (readOnly) {
    if (records.length === 0) return <span className="text-stone-400">—</span>
    return (
      <div className="flex flex-wrap gap-1">
        {records.map((r) => (
          <span key={r.file} title={r.original ?? undefined} className={CHIP}>
            {chipLabel(r)}
          </span>
        ))}
      </div>
    )
  }
  return (
    <div className="flex flex-wrap items-center gap-1">
      {records.map((r) => (
        // One chip holding two controls side by side: the link and the ×. The
        // × is never inside the link (a button in a link is invalid and its
        // click would open the file too). Its slot at the chip's end is always
        // there and only fades in on hover, so the label never moves under the
        // pointer — which is how a click meant to open the file lands on the
        // delete — and chips sit as close as the code-clone ones.
        <span
          key={r.file}
          className="group/si inline-flex items-stretch rounded-md bg-stone-200 text-xs text-stone-700 transition-colors hover:bg-stone-300"
        >
          <a
            href={siFileUrl(paperId, r.file)}
            target="_blank"
            rel="noreferrer"
            title={r.original ?? undefined}
            className="rounded-l-md py-0.5 pl-2 transition-colors hover:text-stone-900"
          >
            {chipLabel(r)}
          </a>
          <button
            type="button"
            aria-label={`Remove ${r.file}`}
            title="Remove"
            disabled={busy}
            onClick={() => onRemove?.(r)}
            className="grid w-4 place-items-center rounded-r-md leading-none text-stone-500 opacity-0 transition-opacity hover:text-rose-600 focus-visible:opacity-100 group-hover/si:opacity-100 disabled:opacity-0"
          >
            ×
          </button>
        </span>
      ))}
      <button
        type="button"
        disabled={busy}
        onClick={onAdd}
        className="inline-flex items-center gap-1 rounded-md px-2 py-0.5 text-xs font-medium text-accent-700 transition-colors hover:bg-accent-50 disabled:opacity-50"
      >
        <span aria-hidden className="text-accent-600">
          +
        </span>
        Add SI
      </button>
    </div>
  )
}

/** Confirm before an SI file goes to the trash — the relation-remove confirm's
 * shape. It stays up while the move runs, holding Escape without acting on
 * it. The file can come back (`lit add --si` with its path in the trash), so
 * no "cannot be undone". */
export function SiRemoveConfirm({
  record,
  busy,
  onCancel,
  onConfirm,
}: {
  record: SiRecord
  busy: boolean
  onCancel: () => void
  onConfirm: () => void
}) {
  useEscapeLayer(true, busy ? null : onCancel)
  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/30 backdrop-blur-sm"
      {...modalBackdropProps}
    >
      <div
        onClick={(e) => e.stopPropagation()}
        role="dialog"
        aria-label="Move this SI file to trash?"
        className="w-[22rem] animate-grow-in rounded-2xl bg-white p-5 shadow-xl ring-1 ring-stone-200"
      >
        <h2 className="text-sm font-semibold text-stone-900">
          Move this SI file to trash?
        </h2>
        <p className="mt-1.5 font-mono text-xs leading-relaxed text-stone-600">
          {record.file}
        </p>
        {record.original && (
          <p className="truncate text-xs leading-relaxed text-stone-600" title={record.original}>
            {record.original}
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
            Move to trash
          </button>
        </div>
      </div>
    </div>
  )
}
