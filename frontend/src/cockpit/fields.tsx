import { useState } from 'react'
import { ExternalLinkIcon } from '../ui/icons'

/** A read-only chip group (relations / code-clones stay read-only in 3b). */
export function Chips({
  values,
  suffix,
}: {
  values: string[] | undefined
  /** Optional trailing detail, appended as `value · suffix`. Projects use it
   * for the per-project grade (ADR-025); an ungraded link returns null and the
   * chip reads exactly as it always did. Read-only: this panel is the trash
   * inspector, where nothing is editable. */
  suffix?: (value: string) => string | null
}) {
  if (!values || values.length === 0) return <span className="text-stone-400">—</span>
  return (
    <div className="flex flex-wrap gap-1">
      {values.map((v) => {
        const extra = suffix?.(v)
        return (
          <span
            key={v}
            className="rounded-md bg-stone-200 px-2 py-0.5 text-xs text-stone-700"
          >
            {extra ? `${v} · ${extra}` : v}
          </span>
        )
      })}
    </div>
  )
}

/** Code-clone chips with a dangling-link marker. A name in `missing` (server-
 * resolved: codes/<name>/ is gone, the same criterion lit health-check uses for
 * invariant #12) renders amber + ⚠ "missing", so a link to a deleted codebase
 * never reads as live (e.g. after a restore that did not re-clone). The link is
 * kept by design — re-clone via `lit code add` or drop it via `lit code unlink`.
 * With `vaultPath`, a live chip copies the clone's folder path, like Copy path
 * does for the paper; the chip itself flashes ✓, since the copied note by the
 * Copy buttons is often scrolled out of view down here. A clone with an entry
 * in `upstream` gets a ↗ beside it that opens that page in a new tab. */
export function CodeCloneChips({
  values,
  missing,
  vaultPath = null,
  upstream,
}: {
  values: string[] | undefined
  missing: string[] | undefined
  vaultPath?: string | null
  upstream?: Record<string, string>
}) {
  const [copied, setCopied] = useState<string | null>(null)
  if (!values || values.length === 0)
    return <span className="text-stone-400">—</span>
  const gone = new Set(missing ?? [])
  async function copy(name: string) {
    try {
      await navigator.clipboard.writeText(`${vaultPath}/codes/${name}`)
      setCopied(name)
      setTimeout(() => setCopied(null), 1200)
    } catch {
      /* no clipboard in some sandboxes — silently ignore */
    }
  }
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
        ) : vaultPath ? (
          <span key={v} className="inline-flex items-center gap-0.5">
            <button
              type="button"
              onClick={() => copy(v)}
              title={`Copy the clone's folder path (codes/${v})`}
              className={
                copied === v
                  ? 'rounded-md bg-emerald-100 px-2 py-0.5 text-xs text-emerald-700'
                  : 'rounded-md bg-stone-200 px-2 py-0.5 text-xs text-stone-700 transition-colors hover:bg-stone-300 hover:text-stone-900'
              }
            >
              {copied === v ? `✓ ${v}` : v}
            </button>
            {upstream?.[v] && (
              <a
                href={upstream[v]}
                target="_blank"
                rel="noreferrer"
                title={upstream[v]}
                aria-label={`Open ${v} upstream: ${upstream[v]}`}
                className="rounded p-0.5 text-stone-400 transition-colors hover:text-accent-600"
              >
                <ExternalLinkIcon className="h-[13px] w-[13px]" />
              </a>
            )}
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
export const AUTHOR_HEAD = 3
export const AUTHOR_TAIL = 3

export function formatAuthors(authors: string[]): string {
  if (authors.length <= AUTHOR_HEAD + AUTHOR_TAIL) return authors.join('; ')
  return [
    ...authors.slice(0, AUTHOR_HEAD),
    '…',
    ...authors.slice(-AUTHOR_TAIL),
  ].join('; ')
}

/** A labelled row of the inspector. `count` goes on the label of a list-valued
 * field (authors, relations, …) and stays off while the list is empty — the
 * row's own "—" already says so. */
export function Field({
  label,
  count,
  children,
}: {
  label: string
  count?: number
  children: React.ReactNode
}) {
  return (
    <div className="mb-3.5">
      <div className="mb-1 text-[11px] font-semibold uppercase tracking-wider text-stone-600">
        {label}
        {count != null && count > 0 && (
          <span className="font-normal tabular-nums text-stone-400"> · {count}</span>
        )}
      </div>
      <div className="text-sm text-stone-800">{children}</div>
    </div>
  )
}

const SELECT_CLASS =
  'w-full rounded-md border border-stone-300 bg-white px-2 py-1 text-sm text-stone-800 ' +
  'shadow-sm transition-colors hover:bg-stone-50 focus:outline-none focus:ring-1 ' +
  'focus:ring-accent-500 disabled:opacity-50'

/** A single fixed-enum dropdown (status / type), and the per-project grade
 * picker in the Projects panel. Options come from `fixedEnums` for the former
 * and from PROJECT_GRADES for the latter; the unset option is offered only
 * when `allowsNone`.
 *
 * `compact` shrinks it to sit at the right edge of a project row: auto width so
 * it takes only what the current letter needs, leaving the rest of the 320px
 * aside to the project name. */
export function EnumSelect({
  field,
  value,
  options,
  allowsNone,
  disabled,
  compact,
  selectRef,
  onPick,
}: {
  field: string
  value: string | null | undefined
  options: string[]
  allowsNone: boolean
  disabled: boolean
  compact?: boolean
  selectRef?: React.Ref<HTMLSelectElement>
  onPick: (next: string | null) => void
}) {
  // Empty string is the sentinel for "— (unset)"; a real value never is.
  const current = value ?? ''
  return (
    <select
      ref={selectRef}
      aria-label={field}
      className={
        compact
          ? SELECT_CLASS.replace('w-full', 'w-auto').replace('px-2', 'px-1.5') +
            ' text-xs'
          : SELECT_CLASS
      }
      value={current}
      disabled={disabled}
      onChange={(e) => onPick(e.target.value === '' ? null : e.target.value)}
    >
      {allowsNone && <option value="">{compact ? '–' : '— (unset)'}</option>}
      {!allowsNone && current === '' && (
        // A select whose stored value is absent, on a field that offers no
        // unset option. Without this the browser falls back to displaying the
        // FIRST option, so a linked-but-ungraded project would read as an "A"
        // that is not in the metadata — the panel would lie about the grade.
        // Disabled: it can be left, never chosen. The GUI repairs the
        // ungraded state (ADR-025 decision 5); it never creates one.
        <option value="" disabled>
          {compact ? '–' : '— (none)'}
        </option>
      )}
      {options.map((opt) => (
        <option key={opt} value={opt}>
          {opt}
        </option>
      ))}
    </select>
  )
}
