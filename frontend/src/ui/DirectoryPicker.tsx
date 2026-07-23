import { useEffect, useRef, useState } from 'react'
import { createPortal } from 'react-dom'

import { listDir } from '../api'
import type { FsListing } from '../api'

/** What "Select this folder" is allowed to return:
 *  - `existing-dir` / `parent-dir` — any folder that exists (the name is typed
 *    separately for parent-dir); Select is always enabled.
 *  - `vault-dir` — only a folder that is itself a litman library; Select is
 *    gated on the listing's own `is_vault`. */
export type PickerMode = 'existing-dir' | 'vault-dir' | 'parent-dir'

/** Emoji per standard anchor label. Shared with PathField's create-vault card so
 * the same location reads the same way in the chip and on the card. */
export const ANCHOR_EMOJI: Record<string, string> = {
  Home: '🏠',
  Desktop: '🖥',
  Documents: '📄',
  Downloads: '⬇',
}

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
 * open. Read-only: it lists subdirectories, never files, and never writes.
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
  const addressRef = useRef<HTMLInputElement>(null)
  // Monotonic request id so a slow listDir() that resolves after a newer
  // navigation can't clobber the newer listing (spec priority #6). Shared by
  // go() and the mount effect.
  const reqRef = useRef(0)

  // Land focus in the address bar on open so an expert can immediately paste a
  // path and press Enter. (Escape is handled globally below, so it works no
  // matter where focus later lands.)
  useEffect(() => {
    addressRef.current?.focus()
  }, [])

  // Escape closes ONLY the picker, from any focus state — including after a
  // folder-row click unmounts the focused row and focus falls back to
  // document.body (AC B3). A capture-phase document listener runs before React's
  // root-container handlers and stopPropagation()s, so the Escape also never
  // reaches the parent dialog's onKeyDown. Safe because no dialog uses a
  // document-level Escape listener (the picker is the only global Escape while
  // it is open).
  useEffect(() => {
    function onKey(e: KeyboardEvent) {
      if (e.key === 'Escape') {
        e.stopPropagation()
        onCancel()
      }
    }
    document.addEventListener('keydown', onKey, true)
    return () => document.removeEventListener('keydown', onKey, true)
  }, [onCancel])

  // A user navigation (anchor / row / address paste / up): keep the current
  // listing on failure and surface the backend detail inline — a bad address
  // paste must not blank the picker.
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

  const entries = listing?.entries ?? []
  const isVault = listing?.is_vault ?? false
  const parent = listing?.parent ?? null
  const vaultBlocked = mode === 'vault-dir' && !isVault
  const selectDisabled = loading || !listing || vaultBlocked

  const CHIP =
    'rounded-full border border-stone-300 bg-white px-2.5 py-1 text-xs text-stone-600 ' +
    'shadow-sm transition-colors hover:bg-stone-50 disabled:opacity-40'

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
                {ANCHOR_EMOJI[a.label] ?? '📁'} {a.label}
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
            ↑
          </button>
          <input
            ref={addressRef}
            type="text"
            value={address}
            spellCheck={false}
            onChange={(e) => setAddress(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === 'Enter') {
                e.stopPropagation()
                go(address.trim() || undefined)
              }
            }}
            className="w-full min-w-0 flex-1 rounded-md border border-stone-300 bg-white px-2.5 py-1.5 font-mono text-xs text-stone-800 shadow-sm focus:outline-none focus:ring-1 focus:ring-accent-400"
          />
        </div>

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
                className="flex w-full items-center gap-2 border-b border-stone-100 px-3 py-2 text-left transition-colors last:border-b-0 hover:bg-stone-50"
              >
                <span className="shrink-0 text-stone-400">📁</span>
                <span className="min-w-0 flex-1 truncate text-sm text-stone-800">
                  {entry.name}
                </span>
                {entry.is_vault && (
                  <span className="shrink-0 rounded-full bg-emerald-100 px-1.5 py-0.5 text-[10px] font-semibold text-emerald-700">
                    ✓ litman library
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
