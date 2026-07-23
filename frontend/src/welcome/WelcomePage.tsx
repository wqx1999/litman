import { useEffect, useState } from 'react'

import {
  createVault,
  listDir,
  putActiveVault,
  setVaultPath,
  unregisterVault,
} from '../api'
import type { FsAnchor } from '../api'
import type { VaultsPayload } from '../types'
import { anchorIcon } from '../ui/icons'
import PathField, { describeLocation } from '../ui/PathField'
import logoUrl from '../assets/logo.svg'

/** Full-screen first-run page shown when the server started with no vault to
 * serve (VaultsPayload.served === null): a fresh install, or an active registry
 * entry whose directory has moved. Creates a new library (POST /api/vaults/create)
 * or opens an already-registered one (PUT /api/vaults/active); on success the
 * parent re-fetches vaults, which flips `served` non-null and slides the GUI into
 * the normal three-column view with no reload.
 *
 * Copy stays task-focused: the "next step" after a library exists is the empty-
 * vault card in the normal view, which points at the agent button (ADR-021). This
 * page never suggests adding papers from the GUI. */
const INPUT =
  'w-full rounded-md border border-stone-300 bg-white px-3 py-2 text-sm ' +
  'text-stone-800 shadow-sm focus:outline-none focus:ring-1 focus:ring-accent-400 ' +
  'disabled:opacity-50'

export default function WelcomePage({
  vaults,
  onEnter,
  onRefresh,
}: {
  vaults: VaultsPayload | null
  /** Called after a successful create or open — the parent re-fetches vaults. */
  onEnter: () => void
  /** Re-fetch the vault list without leaving this page (after a Forget). */
  onRefresh: () => Promise<void>
}) {
  const [parentDir, setParentDir] = useState('~')
  const [name, setName] = useState('literature_vault')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  // The moved entry the user is locating (its new path is typed inline).
  const [locating, setLocating] = useState<string | null>(null)
  const [locatePath, setLocatePath] = useState('')
  // The moved entry the user is dropping from the registry (inline confirm).
  const [forgetting, setForgetting] = useState<string | null>(null)
  // Default the location to the server's suggested start (Desktop → Documents →
  // Home) so the first library lands somewhere the user can see; the anchors let
  // the card name it ("Desktop"). Graceful fallback: on failure the '~'
  // default stands — first-run boot must never break on this read.
  const [anchors, setAnchors] = useState<FsAnchor[]>([])
  useEffect(() => {
    let cancelled = false
    listDir()
      .then((l) => {
        if (!cancelled) {
          // Only replace the untouched '~' placeholder — never clobber a path
          // the user pasted while this async read was still in flight (red
          // line #1: don't disturb the expert flow).
          setParentDir((prev) => (prev === '~' ? l.path : prev))
          setAnchors(l.anchors)
        }
      })
      .catch(() => {
        /* keep the '~' default */
      })
    return () => {
      cancelled = true
    }
  }, [])

  const existing = vaults?.vaults ?? []
  const canCreate = parentDir.trim().length > 0 && !busy
  const loc = describeLocation(parentDir, anchors)

  async function create() {
    if (!canCreate) return
    setBusy(true)
    setError(null)
    try {
      await createVault(parentDir.trim(), name.trim() || undefined)
      onEnter() // unmounts this page once `served` flips
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
      setBusy(false)
    }
  }

  async function open(vaultName: string) {
    if (busy) return
    setBusy(true)
    setError(null)
    try {
      await putActiveVault(vaultName)
      onEnter()
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
      setBusy(false)
    }
  }

  // The moved-library recovery on the welcome page: point the registry at where
  // the vault lives now (`lit vault set-path`). When the entry is the active one
  // the server binds to the new path, so `onEnter` re-bootstraps and slides the
  // GUI straight into the library — the relaunch-after-move dead end, fixed.
  async function locate(vaultName: string) {
    if (busy) return
    const p = locatePath.trim()
    if (!p) return
    setBusy(true)
    setError(null)
    try {
      await setVaultPath(vaultName, p)
      onEnter()
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
      setBusy(false)
    }
  }

  // Drop a moved/lost entry from the registry (`lit vault remove`): the folder on
  // disk is never touched, so this only clears the dead pointer. Stays on this
  // page — onRefresh re-pulls the list with the entry gone.
  async function forget(vaultName: string) {
    if (busy) return
    setBusy(true)
    setError(null)
    try {
      await unregisterVault(vaultName)
      setForgetting(null)
      await onRefresh()
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="flex h-full w-full items-center justify-center overflow-y-auto bg-stone-100 px-4 py-10 text-stone-800 antialiased">
      <div className="w-full max-w-md animate-grow-in rounded-2xl bg-white p-8 shadow-xl ring-1 ring-stone-200">
        <div className="flex flex-col items-center text-center">
          <img src={logoUrl} alt="litman" className="h-12 w-auto select-none" />
          <h1 className="mt-4 text-lg font-semibold text-stone-900">
            Create your library
          </h1>
          <p className="mt-1.5 text-sm text-stone-500">
            litman keeps your papers in one folder on disk.
          </p>
        </div>

        <div className="mt-6 space-y-4">
          <div className="rounded-xl border border-stone-200 bg-stone-50 px-4 py-3">
            <div className="flex items-center gap-2 text-sm font-medium text-stone-800">
              {anchorIcon(loc.kind, 'h-5 w-5 shrink-0 text-stone-500')}
              <span className="min-w-0 truncate">
                {loc.label} <span className="text-stone-400">/</span>{' '}
                {name.trim() || 'literature_vault'}
              </span>
            </div>
          </div>
          <label className="block">
            <span className="text-xs font-medium text-stone-600">Location</span>
            <PathField
              mode="parent-dir"
              value={parentDir}
              onChange={setParentDir}
              disabled={busy}
              placeholder="/work/you"
              onEnter={create}
              className="mt-1"
            />
          </label>
          <label className="block">
            <span className="text-xs font-medium text-stone-600">Name</span>
            <input
              type="text"
              value={name}
              disabled={busy}
              spellCheck={false}
              onChange={(e) => setName(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === 'Enter') create()
              }}
              className={`${INPUT} mt-1`}
            />
          </label>
        </div>

        {error && (
          <p className="mt-4 rounded-lg bg-rose-50 px-3 py-2 text-xs leading-relaxed text-rose-600 ring-1 ring-rose-100">
            {error}
          </p>
        )}

        <button
          onClick={create}
          disabled={!canCreate}
          className="mt-6 w-full rounded-lg bg-accent-500 px-4 py-2.5 text-sm font-medium text-white shadow-sm transition-colors hover:bg-accent-600 disabled:opacity-50"
        >
          {busy ? 'Creating…' : 'Create library'}
        </button>

        {existing.length > 0 && (
          <div className="mt-7 border-t border-stone-200 pt-5">
            <p className="text-xs font-medium text-stone-500">
              Or open an existing library
            </p>
            <ul className="mt-2 space-y-1.5">
              {existing.map((v) =>
                v.exists ? (
                  <li key={v.name}>
                    <button
                      onClick={() => open(v.name)}
                      disabled={busy}
                      className="flex w-full items-center justify-between gap-3 rounded-lg border border-stone-200 bg-white px-3 py-2 text-left transition-colors hover:bg-stone-50 disabled:opacity-50"
                    >
                      <span className="min-w-0">
                        <span className="block text-sm font-medium text-stone-800">
                          {v.name}
                        </span>
                        <span className="block truncate font-mono text-[11px] text-stone-400">
                          {v.path}
                        </span>
                      </span>
                      <span className="shrink-0 text-xs font-medium text-accent-600">
                        Open
                      </span>
                    </button>
                  </li>
                ) : (
                  // Moved / missing: Open would 400 on the dead path. Offer Locate
                  // (re-point via `setVaultPath`) or Forget (drop the dead registry
                  // entry via `unregisterVault`), both inline — never delete on disk.
                  <li
                    key={v.name}
                    className="rounded-lg border border-stone-200 bg-white px-3 py-2"
                  >
                    <div className="flex items-center justify-between gap-3">
                      <span className="min-w-0">
                        <span className="flex items-center gap-1.5">
                          <span className="text-sm font-medium text-stone-800">
                            {v.name}
                          </span>
                          <span className="shrink-0 rounded-full bg-rose-100 px-1.5 py-0.5 text-[10px] font-semibold uppercase tracking-wide text-rose-700">
                            moved
                          </span>
                        </span>
                        <span className="block truncate font-mono text-[11px] text-rose-400 line-through">
                          {v.path}
                        </span>
                      </span>
                      {locating !== v.name && forgetting !== v.name && (
                        <span className="flex shrink-0 items-center gap-3">
                          <button
                            onClick={() => {
                              setLocating(v.name)
                              setLocatePath('')
                              setForgetting(null)
                              setError(null)
                            }}
                            disabled={busy}
                            className="text-xs font-medium text-accent-600 disabled:opacity-50"
                          >
                            Locate
                          </button>
                          <button
                            onClick={() => {
                              setForgetting(v.name)
                              setLocating(null)
                              setError(null)
                            }}
                            disabled={busy}
                            className="text-xs font-medium text-stone-400 transition-colors hover:text-rose-500 disabled:opacity-50"
                          >
                            Forget
                          </button>
                        </span>
                      )}
                    </div>
                    {locating === v.name && (
                      <div className="mt-2 flex items-center gap-2">
                        <PathField
                          mode="vault-dir"
                          value={locatePath}
                          onChange={setLocatePath}
                          disabled={busy}
                          autoFocus
                          placeholder="/new/path/to/literature_vault"
                          onEnter={() => locate(v.name)}
                          className="flex-1"
                        />
                        <button
                          onClick={() => locate(v.name)}
                          disabled={busy || locatePath.trim().length === 0}
                          className="shrink-0 rounded-md bg-accent-500 px-3 py-2 text-xs font-medium text-white transition-colors hover:bg-accent-600 disabled:opacity-50"
                        >
                          {busy ? '…' : 'Locate'}
                        </button>
                      </div>
                    )}
                    {forgetting === v.name && (
                      <div className="mt-2 flex items-center justify-between gap-2 rounded-md bg-rose-50 px-2.5 py-1.5 ring-1 ring-rose-100">
                        <span className="min-w-0 text-[11px] leading-snug text-rose-700">
                          Remove from the list? Nothing on disk is deleted.
                        </span>
                        <span className="flex shrink-0 items-center gap-1.5">
                          <button
                            onClick={() => setForgetting(null)}
                            disabled={busy}
                            className="rounded px-2 py-1 text-[11px] font-medium text-stone-600 transition-colors hover:bg-white disabled:opacity-50"
                          >
                            Cancel
                          </button>
                          <button
                            onClick={() => forget(v.name)}
                            disabled={busy}
                            className="rounded-md bg-rose-500 px-2.5 py-1 text-[11px] font-medium text-white transition-colors hover:bg-rose-600 disabled:opacity-50"
                          >
                            {busy ? '…' : 'Remove'}
                          </button>
                        </span>
                      </div>
                    )}
                  </li>
                ),
              )}
            </ul>
          </div>
        )}
      </div>
    </div>
  )
}
