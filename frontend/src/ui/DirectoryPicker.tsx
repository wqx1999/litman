import { Fragment, useEffect, useRef, useState } from 'react'
import { createPortal } from 'react-dom'

import { listDir, mkdir } from '../api'
import type { FsListing } from '../api'
import {
  anchorIcon,
  ArrowUpIcon,
  CheckIcon,
  ChevronRightIcon,
  FolderIcon,
  PencilIcon,
  PlusIcon,
} from './icons'
import { breadcrumbs } from './path'

/** What "Select this folder" is allowed to return:
 *  - `existing-dir` / `parent-dir` — any folder that exists (the name is typed
 *    separately for parent-dir); Select is always enabled.
 *  - `vault-dir` — only a folder that is itself a litman library; Select is
 *    gated on the listing's own `is_vault`. */
export type PickerMode = 'existing-dir' | 'vault-dir' | 'parent-dir'

/** An in-app directory browser over the server's filesystem (GET /api/fs/list).
 *
 * It browses the machine the server runs on — the same disk the backend writes
 * to — so it works identically for a local `lit gui` and one reached over an SSH
 * tunnel (a browser's native file picker can never hand back an absolute path).
 *
 * Layering (spec §3.2): portaled to document.body at z-[70] so it sits above its
 * parent dialog (z-[60]). All clicks stopPropagation and Escape stopPropagation +
 * closes only the picker, so dismissing it never closes the dialog underneath.
 * Selecting a folder fills the field and closes the picker, leaving the dialog
 * open. It lists subdirectories, never files. Its only write is the "＋ New
 * folder" button (existing-dir / parent-dir), which POSTs to /api/fs/mkdir.
 */
export default function DirectoryPicker({
  initial,
  mode,
  onPick,
  onCancel,
}: {
  /** Where to open: a valid directory starts there, otherwise the picker falls
   * back to the server's suggested start (Desktop → Documents → Home). */
  initial: string
  mode: PickerMode
  /** Called with the chosen absolute directory; the host closes the picker. */
  onPick: (path: string) => void
  onCancel: () => void
}) {
  const [listing, setListing] = useState<FsListing | null>(null)
  const [address, setAddress] = useState(initial)
  const [error, setError] = useState<string | null>(null)
  const [loading, setLoading] = useState(true)
  const [showHidden, setShowHidden] = useState(false)
  // The address bar is clickable breadcrumbs when idle, an editable input when
  // active. It OPENS as breadcrumbs (macOS-clean, and one Escape closes the
  // picker); the expert's paste stays one click away — clicking the strip (or
  // the pencil) turns it into a focused, select-all input, the same single click
  // as focusing a text field. Blur / Esc / a navigation drop it back.
  const [editingAddress, setEditingAddress] = useState(false)
  // null = the "＋ New folder" button is shown; a string (even '') = the inline
  // name input is open. Only rendered in existing-dir / parent-dir.
  const [newName, setNewName] = useState<string | null>(null)
  const addressRef = useRef<HTMLInputElement>(null)
  const newNameRef = useRef<HTMLInputElement>(null)
  // Monotonic request id so a slow listDir()/mkdir() that resolves after a newer
  // navigation can't clobber the newer listing (spec priority #6). Shared by
  // go(), the mount effect, and submitNewFolder().
  const reqRef = useRef(0)

  const canCreateFolder = mode === 'existing-dir' || mode === 'parent-dir'
  const creatingFolder = newName !== null

  // Latest values for the capture-phase Escape handler, which is bound once and
  // must not close over stale state (mutating a ref during render is the
  // documented pattern for "current value inside a long-lived listener").
  const creatingRef = useRef(false)
  const editingRef = useRef(false)
  const pathRef = useRef(initial)
  creatingRef.current = creatingFolder
  editingRef.current = editingAddress
  pathRef.current = listing?.path ?? address

  // Focus (+ select) the address input whenever it becomes the active editor:
  // on open (preserving today's paste-ready focus) and on every click-to-edit
  // (spec §3.3 "auto-focus + select-all"). Selecting means a paste replaces the
  // whole path rather than inserting into it.
  useEffect(() => {
    if (editingAddress) {
      const el = addressRef.current
      el?.focus()
      el?.select()
    }
  }, [editingAddress])

  // Focus the new-folder name input the moment it opens.
  useEffect(() => {
    if (creatingFolder) newNameRef.current?.focus()
  }, [creatingFolder])

  // Escape, from any focus state, with the sub-input layering the spec's red
  // line requires: a live new-folder name box or an address edit box swallows
  // Esc to cancel ITSELF, leaving the picker (and the dialog under it) open;
  // only with no active sub-input does Esc close the picker. A capture-phase
  // document listener runs before React's handlers and stopPropagation()s, so
  // the Escape also never reaches the parent dialog's onKeyDown.
  useEffect(() => {
    function onKey(e: KeyboardEvent) {
      if (e.key !== 'Escape') return
      e.stopPropagation()
      if (creatingRef.current) {
        setNewName(null)
        return
      }
      if (editingRef.current) {
        setEditingAddress(false)
        setAddress(pathRef.current) // discard a half-typed path
        return
      }
      onCancel()
    }
    document.addEventListener('keydown', onKey, true)
    return () => document.removeEventListener('keydown', onKey, true)
  }, [onCancel])

  // A user navigation (anchor / row / breadcrumb / address paste / up): keep the
  // current listing on failure and surface the backend detail inline — a bad
  // address paste must not blank the picker.
  async function go(target: string | undefined, hidden = showHidden) {
    const req = ++reqRef.current
    setLoading(true)
    try {
      const l = await listDir(target, hidden)
      if (req !== reqRef.current) return // superseded by a newer navigation
      setListing(l)
      setAddress(l.path)
      setError(null)
    } catch (e) {
      if (req !== reqRef.current) return
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      if (req === reqRef.current) setLoading(false)
    }
  }

  // Open: try `initial`; a bad path degrades to the suggested start so the
  // picker never opens blank (spec §3.2). Mount-only.
  useEffect(() => {
    const req = ++reqRef.current
    let cancelled = false
    const fresh = () => !cancelled && req === reqRef.current
    ;(async () => {
      setLoading(true)
      try {
        const l = await listDir(initial || undefined, false)
        if (fresh()) {
          setListing(l)
          setAddress(l.path)
          setError(null)
        }
      } catch {
        try {
          const l = await listDir(undefined, false)
          if (fresh()) {
            setListing(l)
            setAddress(l.path)
            setError(null)
          }
        } catch (e) {
          if (fresh()) setError(e instanceof Error ? e.message : String(e))
        }
      } finally {
        if (fresh()) setLoading(false)
      }
    })()
    return () => {
      cancelled = true
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  function toggleHidden(next: boolean) {
    setShowHidden(next)
    if (listing) go(listing.path, next)
  }

  // Switch the address bar into its editable-input mode (spec §3.3). Reset the
  // text to the folder currently shown, and close any open new-folder box so
  // only one sub-input is ever active (keeps the Esc layering unambiguous).
  function enterAddressEdit() {
    setNewName(null)
    setAddress(listing?.path ?? address)
    setEditingAddress(true)
  }

  function startNewFolder() {
    setEditingAddress(false) // one sub-input at a time
    setError(null)
    setNewName('')
  }

  // Create the folder under the current directory, then treat the returned
  // listing as one navigation — landing INSIDE the new folder — guarded by the
  // same reqRef monotonic id as go() so a slow mkdir can't clobber a newer nav.
  async function submitNewFolder() {
    const name = (newName ?? '').trim()
    if (!name || !listing) return
    const req = ++reqRef.current
    setLoading(true)
    try {
      const l = await mkdir(listing.path, name)
      if (req !== reqRef.current) return
      setListing(l)
      setAddress(l.path)
      setNewName(null)
      setError(null)
    } catch (e) {
      if (req !== reqRef.current) return
      setError(e instanceof Error ? e.message : String(e)) // picker stays open
    } finally {
      if (req === reqRef.current) setLoading(false)
    }
  }

  const entries = listing?.entries ?? []
  const isVault = listing?.is_vault ?? false
  const parent = listing?.parent ?? null
  const vaultBlocked = mode === 'vault-dir' && !isVault
  const selectDisabled = loading || !listing || vaultBlocked
  const crumbs = breadcrumbs(listing?.path ?? address)

  const CHIP =
    'inline-flex items-center gap-1.5 rounded-full border border-stone-300 bg-white ' +
    'py-1 pl-2 pr-2.5 text-xs text-stone-600 shadow-sm transition-colors hover:bg-stone-50 ' +
    'disabled:opacity-40'
  const ADDRESS_INPUT =
    'w-full min-w-0 flex-1 rounded-md border border-stone-300 bg-white px-2.5 py-1.5 ' +
    'font-mono text-xs text-stone-800 shadow-sm focus:outline-none focus:ring-1 focus:ring-accent-400'

  return createPortal(
    <div
      className="fixed inset-0 z-[70] flex items-center justify-center bg-black/40 backdrop-blur-sm"
      onClick={(e) => {
        // Close only the picker; stopPropagation keeps the click from bubbling
        // (React portals bubble to the React parent) to the dialog underneath.
        e.stopPropagation()
        onCancel()
      }}
    >
      <div
        onClick={(e) => e.stopPropagation()}
        className="flex max-h-[80vh] w-[34rem] max-w-[92vw] animate-grow-in flex-col rounded-2xl bg-white p-5 shadow-xl ring-1 ring-stone-200"
      >
        <div className="flex items-center justify-between gap-3">
          <h2 className="text-sm font-semibold text-stone-900">Choose a folder</h2>
          <label className="flex select-none items-center gap-1.5 text-[11px] text-stone-500">
            <input
              type="checkbox"
              checked={showHidden}
              onChange={(e) => toggleHidden(e.target.checked)}
              className="h-3.5 w-3.5 rounded border-stone-300 text-accent-500 focus:ring-accent-400"
            />
            Show hidden
          </label>
        </div>

        {listing && listing.anchors.length > 0 && (
          <div className="mt-3 flex flex-wrap gap-1.5">
            {listing.anchors.map((a) => (
              <button
                key={a.path}
                type="button"
                onClick={() => go(a.path)}
                className={CHIP}
              >
                {anchorIcon(a.label, 'h-4 w-4 text-stone-400')}
                {a.label}
              </button>
            ))}
          </div>
        )}

        <div className="mt-3 flex items-center gap-1.5">
          <button
            type="button"
            aria-label="Up one level"
            title="Up one level"
            disabled={!parent}
            onClick={() => parent && go(parent)}
            className="grid h-8 w-8 shrink-0 place-items-center rounded-md border border-stone-300 bg-white text-stone-600 shadow-sm transition-colors hover:bg-stone-50 disabled:opacity-40"
          >
            <ArrowUpIcon className="h-4 w-4" />
          </button>
          {editingAddress ? (
            <input
              ref={addressRef}
              type="text"
              value={address}
              spellCheck={false}
              onChange={(e) => setAddress(e.target.value)}
              onBlur={() => setEditingAddress(false)}
              onKeyDown={(e) => {
                if (e.key === 'Enter') {
                  e.stopPropagation()
                  go(address.trim() || undefined)
                  setEditingAddress(false)
                }
              }}
              className={ADDRESS_INPUT}
            />
          ) : (
            // Breadcrumbs: each segment navigates to that ancestor; clicking the
            // blank strip (or the pencil) flips back to the editable input, so an
            // expert paste is the same single click as focusing the box today.
            <div
              onClick={enterAddressEdit}
              title="Click to edit the path"
              className="flex min-w-0 flex-1 cursor-text items-center gap-0.5 overflow-x-auto rounded-md border border-stone-300 bg-white px-2 py-1.5 shadow-sm"
            >
              {crumbs.map((c, i) => (
                <Fragment key={c.path}>
                  {i > 0 && (
                    <ChevronRightIcon className="h-3 w-3 shrink-0 text-stone-400" />
                  )}
                  <button
                    type="button"
                    onClick={(e) => {
                      e.stopPropagation()
                      go(c.path)
                    }}
                    className="shrink-0 truncate rounded px-1 py-0.5 font-mono text-xs text-stone-600 transition-colors hover:bg-stone-100 hover:text-stone-900"
                  >
                    {c.label}
                  </button>
                </Fragment>
              ))}
              <button
                type="button"
                aria-label="Edit path"
                title="Edit path"
                onClick={(e) => {
                  e.stopPropagation()
                  enterAddressEdit()
                }}
                className="ml-auto shrink-0 rounded p-1 text-stone-400 transition-colors hover:bg-stone-100 hover:text-stone-600"
              >
                <PencilIcon className="h-3.5 w-3.5" />
              </button>
            </div>
          )}
        </div>

        {canCreateFolder && (
          <div className="mt-3">
            {creatingFolder ? (
              <div className="flex items-center gap-1.5">
                <input
                  ref={newNameRef}
                  type="text"
                  value={newName ?? ''}
                  spellCheck={false}
                  placeholder="New folder name"
                  onChange={(e) => setNewName(e.target.value)}
                  onKeyDown={(e) => {
                    if (e.key === 'Enter') {
                      e.stopPropagation()
                      submitNewFolder()
                    }
                  }}
                  className="w-full min-w-0 flex-1 rounded-md border border-stone-300 bg-white px-2.5 py-1.5 text-xs text-stone-800 shadow-sm focus:outline-none focus:ring-1 focus:ring-accent-400"
                />
                <button
                  type="button"
                  disabled={!(newName ?? '').trim()}
                  onClick={submitNewFolder}
                  className="shrink-0 rounded-md border border-stone-300 bg-white px-2.5 py-1.5 text-xs font-medium text-stone-700 shadow-sm transition-colors hover:bg-stone-50 disabled:opacity-50"
                >
                  Create
                </button>
                <button
                  type="button"
                  onClick={() => setNewName(null)}
                  className="shrink-0 rounded-md px-2 py-1.5 text-xs text-stone-500 transition-colors hover:bg-stone-100"
                >
                  Cancel
                </button>
              </div>
            ) : (
              <button
                type="button"
                onClick={startNewFolder}
                className="inline-flex items-center gap-1.5 rounded-md border border-stone-300 py-1 pl-2 pr-2.5 text-xs text-stone-500 transition-colors hover:border-stone-400 hover:bg-stone-50 hover:text-stone-700"
              >
                <PlusIcon className="h-3.5 w-3.5 text-stone-400" />
                New folder
              </button>
            )}
          </div>
        )}

        {error && (
          <p className="mt-3 rounded-md bg-rose-50 px-3 py-2 text-[11px] leading-relaxed text-rose-600">
            {error}
          </p>
        )}

        <div className="mt-3 min-h-[8rem] flex-1 overflow-y-auto rounded-lg border border-stone-200">
          {loading && !listing ? (
            <div className="px-3 py-6 text-center text-xs text-stone-400">Loading…</div>
          ) : listing?.denied ? (
            <div className="px-3 py-6 text-center text-xs text-stone-400">
              Can’t open this folder.
            </div>
          ) : entries.length === 0 ? (
            <div className="px-3 py-6 text-center text-xs text-stone-400">
              No subfolders here.
            </div>
          ) : (
            entries.map((entry) => (
              <button
                key={entry.path}
                type="button"
                onClick={() => go(entry.path)}
                className="flex w-full items-center gap-2.5 border-b border-stone-100 px-3 py-2 text-left transition-colors last:border-b-0 hover:bg-stone-100"
              >
                <FolderIcon className="h-[18px] w-[18px] shrink-0 text-stone-400" />
                <span className="min-w-0 flex-1 truncate text-sm text-stone-800">
                  {entry.name}
                </span>
                {entry.is_vault && (
                  <span className="flex shrink-0 items-center gap-1 rounded-full bg-emerald-100 px-1.5 py-0.5 text-[10px] font-semibold uppercase tracking-wide text-emerald-700">
                    <CheckIcon className="h-3 w-3 text-emerald-600" />
                    litman library
                  </span>
                )}
              </button>
            ))
          )}
        </div>

        <div className="mt-4 flex items-center justify-between gap-3">
          <span className="min-w-0 flex-1 truncate text-[11px] text-stone-400">
            {vaultBlocked ? 'This folder isn’t a litman library.' : ''}
          </span>
          <div className="flex shrink-0 items-center gap-2">
            <button
              type="button"
              onClick={onCancel}
              className="rounded-lg px-3 py-1.5 text-xs text-stone-600 transition-colors hover:bg-stone-100"
            >
              Cancel
            </button>
            <button
              type="button"
              disabled={selectDisabled}
              onClick={() => listing && onPick(listing.path)}
              className="rounded-lg bg-accent-500 px-3 py-1.5 text-xs font-medium text-white transition-colors hover:bg-accent-600 disabled:opacity-50"
            >
              Select this folder
            </button>
          </div>
        </div>
      </div>
    </div>,
    document.body,
  )
}
