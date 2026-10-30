import { useState } from 'react'
import HelpHint from '../ui/HelpHint'
import { IconTrash, PencilIcon } from '../ui/icons'
import { useEscapeLayer } from '../ui/escapeStack'
import {
  modalBackdropProps,
  useModalCardFocus,
} from '../ui/modalShell'

/** Dictionary editor for one controlled-vocab field (problem 2). Lists every
 * registered value with its in-use count (from the loaded INDEX) and a delete
 * affordance; deletion routes through a default-No confirm into the
 * `lit taxonomy rm` backend (atomic dictionary + reference rewrite, invariant
 * #2). The macOS-style modal shell mirrors UnreadConfirm / NewProjectDialog. */
export function ManageDialog({
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
  // Is one of the nested dialogs on screen? Deliberately WITHOUT `busy`: a write
  // finishing is no reason to grab focus back.
  const childOpen = pending != null || renaming != null
  // Focus the card on mount so Escape reaches the handler below — this is a
  // blocking dialog, so nothing else is listening — and again whenever a nested
  // dialog closes, which drops focus to <body> (see useModalCardFocus).
  const cardFocus = useModalCardFocus(childOpen)
  const sorted = vocabulary.slice().sort((a, b) => a.localeCompare(b))
  // While a delete confirm or rename dialog is open (or a write is in flight),
  // gate the list's controls so a keyboard-tab onto a row behind the nested
  // dialog can't swap the target out from under it.
  const blocked = busy || childOpen
  // The two nested dialogs used to be hand-enumerated here so Escape would not
  // close this panel out from under one of them; each is a layer of its own now
  // and the stack only ever hands the key to the top (ui/escapeStack).
  useEscapeLayer(true, onClose)
  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/30 backdrop-blur-sm"
      {...modalBackdropProps}
    >
      <div
        {...cardFocus}
        onClick={(e) => e.stopPropagation()}
        className="flex max-h-[70vh] w-[24rem] animate-grow-in focus:outline-none flex-col rounded-2xl bg-white p-5 shadow-xl ring-1 ring-stone-200"
      >
        <h2 className="text-sm font-semibold text-stone-900">
          Manage {field}
          {sorted.length > 0 && (
            <span className="font-normal tabular-nums text-stone-400"> · {sorted.length}</span>
          )}
          <HelpHint text="Rename fixes a typo or merges two values everywhere they're used. Delete untags a value from every paper." />
        </h2>
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
                  <PencilIcon className="h-3.5 w-3.5" />
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
 * dialog (z-[60]); Escape cancels and the backdrop swallows clicks so
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

  useEscapeLayer(true, onCancel)
  return (
    <div
      className="fixed inset-0 z-[60] flex items-center justify-center bg-black/30 backdrop-blur-sm"
      {...modalBackdropProps}
    >
      <div
        onClick={(e) => e.stopPropagation()}
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
          className="mt-3 w-full rounded-md border border-stone-300 bg-white px-2.5 py-1.5 text-sm text-stone-800 shadow-sm focus:outline-none focus:ring-1 focus:ring-accent-500 disabled:opacity-50"
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
  useEscapeLayer(true, onCancel)
  return (
    <div
      className="fixed inset-0 z-[60] flex items-center justify-center bg-black/30 backdrop-blur-sm"
      {...modalBackdropProps}
    >
      <div
        onClick={(e) => e.stopPropagation()}
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
