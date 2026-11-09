import { useEffect, useState } from 'react'
import { createPortal } from 'react-dom'
import type {
  VaultEntry,
  VaultsPayload,
} from '../types'
import { listDir } from '../api'
import type { FsAnchor } from '../api'
import { IconTrash, anchorIcon } from '../ui/icons'
import { getT, useT } from '../i18n'
import { errorLine } from '../apiErrors'
import { rich } from '../i18n/rich'
import HelpHint from '../ui/HelpHint'
import PathField, { describeLocation } from '../ui/PathField'
import { useEscapeLayer } from '../ui/escapeStack'
import {
  modalBackdropProps,
  useModalCardFocus,
} from '../ui/modalShell'

/** The vault manager (vault-manager slice): lists every registered vault with an
 * active marker + path, unregisters a non-served one through a default-No confirm
 * into the `lit vault remove` backend (registry entry only — the directory on
 * disk is kept), and registers an EXISTING vault dir via RegisterVaultDialog.
 * Mirrors ProjectManager's portaled, auto-theming modal shell (no `dark:`
 * variants — the inverted `stone` ramp + `.dark .bg-white` handle dark mode).
 *
 * The served vault's row (`v.served`) has Unregister disabled with a "switch
 * first" tooltip — a front-of-house mirror of the server's 409 guard (a GUI-only
 * user who unregistered the served vault and closed the browser would be locked
 * out, since `lit gui` needs an active vault to boot). */
export default function VaultManager({
  vaults,
  onRegisterVault,
  onCreateVault,
  onUnregisterVault,
  onRelocateVault,
  onClose,
}: {
  vaults: VaultsPayload | null
  onRegisterVault: (name: string, path: string, setActive: boolean) => Promise<void>
  onCreateVault: (parentDir: string, name: string, setActive: boolean) => Promise<void>
  onUnregisterVault: (name: string) => Promise<void>
  onRelocateVault: (name: string, path: string) => Promise<void>
  onClose: () => void
}) {
  const t = useT()
  const m = t.topbar.vaultManager
  const [pendingUnregister, setPendingUnregister] = useState<string | null>(null)
  // The missing vault whose new home the user is locating (Locate → path input).
  const [pendingLocate, setPendingLocate] = useState<VaultEntry | null>(null)
  const [showRegister, setShowRegister] = useState(false)
  const [showCreate, setShowCreate] = useState(false)
  const [busy, setBusy] = useState(false)

  // Is one of the nested dialogs on screen? Deliberately WITHOUT `busy`: a write
  // finishing is no reason to grab focus back.
  const childOpen =
    pendingUnregister != null || pendingLocate != null || showRegister || showCreate
  // Focus the card on mount so Escape reaches the handler below — this is a
  // blocking dialog, so nothing else is listening — and again whenever a nested
  // dialog closes, which drops focus to <body> (see useModalCardFocus).
  const cardFocus = useModalCardFocus(childOpen)

  async function doUnregister(name: string) {
    setBusy(true)
    try {
      await onUnregisterVault(name)
    } finally {
      setBusy(false)
      setPendingUnregister(null)
    }
  }

  const entries = vaults?.vaults ?? []
  const sorted = entries.slice().sort((a, b) => a.name.localeCompare(b.name))
  const blocked = busy || childOpen

  // This panel used to hand-enumerate its four nested dialogs here so Escape
  // would not close it out from under one of them. It no longer has to: each
  // nested dialog registers a layer of its own above this one, and the stack
  // only ever hands Escape to the top (ui/escapeStack). A fifth nested dialog
  // needs no change here.
  useEscapeLayer(true, onClose)
  return createPortal(
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/30 backdrop-blur-sm"
      {...modalBackdropProps}
    >
      <div
        {...cardFocus}
        onClick={(e) => e.stopPropagation()}
        className="flex max-h-[70vh] w-[30rem] animate-grow-in focus:outline-none flex-col rounded-2xl bg-white p-5 shadow-xl ring-1 ring-stone-200"
      >
        <h2 className="text-sm font-semibold text-stone-900">
          {m.title}
          {sorted.length > 0 && (
            <span className="font-normal tabular-nums text-stone-400"> · {sorted.length}</span>
          )}
          <HelpHint text={m.help} />
        </h2>
        <div className="mt-3 min-h-0 flex-1 overflow-y-auto rounded-lg border border-stone-200">
          {sorted.length === 0 && (
            <div className="px-3 py-4 text-center text-xs text-stone-400">
              {m.empty}
            </div>
          )}
          {sorted.map((v) => (
            <div
              key={v.name}
              className="flex items-center justify-between gap-2 border-b border-stone-100 px-3 py-2 last:border-b-0"
            >
              <div className="min-w-0">
                <div className="flex items-center gap-1.5">
                  <span className="truncate text-sm font-medium text-stone-800">
                    {v.name}
                  </span>
                  {v.active && (
                    <span className="shrink-0 rounded-full bg-accent-100 px-1.5 py-0.5 text-[10px] font-semibold uppercase tracking-wide text-accent-700">
                      {m.active}
                    </span>
                  )}
                  {!v.exists && (
                    <span
                      title={m.missingTitle}
                      className="shrink-0 rounded-full bg-rose-100 px-1.5 py-0.5 text-[10px] font-semibold uppercase tracking-wide text-rose-700"
                    >
                      {m.missing}
                    </span>
                  )}
                </div>
                <div
                  className={`truncate font-mono text-[11px] ${
                    v.exists ? 'text-stone-400' : 'text-rose-400 line-through'
                  }`}
                >
                  {v.path}
                </div>
              </div>
              <div className="flex shrink-0 items-center gap-2">
                {!v.exists && (
                  // A moved vault's only way home: point the registry at its new
                  // folder. Rendered on missing rows only — including the served
                  // one (v.served), whose Unregister stays disabled, so Locate is
                  // that user's sole escape from the 410.
                  <button
                    type="button"
                    aria-label={m.locateLabel(v.name)}
                    title={m.locateTitle(v.name)}
                    disabled={blocked}
                    onClick={() => setPendingLocate(v)}
                    className="rounded-md border border-accent-200 bg-accent-50 px-2 py-0.5 text-[11px] font-medium text-accent-700 transition-colors hover:bg-accent-100 disabled:opacity-40"
                  >
                    {m.locate}
                  </button>
                )}
                <button
                  type="button"
                  aria-label={m.unregisterLabel(v.name)}
                  title={
                    v.served
                      ? m.unregisterServedTitle
                      : m.unregisterTitle(v.name)
                  }
                  disabled={blocked || v.served}
                  onClick={() => setPendingUnregister(v.name)}
                  className="grid h-6 w-6 place-items-center rounded-md text-stone-300 transition-colors hover:bg-rose-50 hover:text-rose-500 disabled:opacity-40 disabled:hover:bg-transparent disabled:hover:text-stone-300"
                >
                  <IconTrash />
                </button>
              </div>
            </div>
          ))}
        </div>
        <div className="mt-4 flex items-center justify-between gap-2">
          <div className="flex items-center gap-2">
            <button
              type="button"
              disabled={blocked}
              onClick={() => setShowCreate(true)}
              className="rounded-lg bg-accent-500 px-3 py-1.5 text-xs font-medium text-white shadow-sm transition-colors hover:bg-accent-600 disabled:opacity-50"
            >
              {m.newVault}
            </button>
            <button
              type="button"
              disabled={blocked}
              onClick={() => setShowRegister(true)}
              className="rounded-lg border border-accent-200 bg-accent-50 px-3 py-1.5 text-xs font-medium text-accent-700 shadow-sm transition-colors hover:bg-accent-100 disabled:opacity-50"
            >
              {m.registerExisting}
            </button>
          </div>
          <button
            type="button"
            disabled={blocked}
            onClick={onClose}
            className="rounded-lg px-3 py-1.5 text-xs text-stone-600 transition-colors hover:bg-stone-100 disabled:opacity-40"
          >
            {t.common.done}
          </button>
        </div>
      </div>
      {pendingUnregister != null && (
        <UnregisterVaultConfirm
          name={pendingUnregister}
          busy={busy}
          onCancel={() => setPendingUnregister(null)}
          onConfirm={() => doUnregister(pendingUnregister)}
        />
      )}
      {pendingLocate != null && (
        <LocateVaultDialog
          vault={pendingLocate}
          onRelocateVault={onRelocateVault}
          onCancel={() => setPendingLocate(null)}
          onLocated={() => setPendingLocate(null)}
        />
      )}
      {showRegister && (
        <RegisterVaultDialog
          onRegisterVault={onRegisterVault}
          onClose={() => setShowRegister(false)}
          onRegistered={(setActive) => {
            setShowRegister(false)
            // When the user asked to set-active, App is already opening the
            // SwitchVaultDialog (via switchVault) — close this panel so that
            // confirm isn't stacked behind it.
            if (setActive) onClose()
          }}
        />
      )}
      {showCreate && (
        <CreateVaultDialog
          suggestedName={vaults?.suggestedName}
          onCreateVault={onCreateVault}
          onClose={() => setShowCreate(false)}
          onCreated={(setActive) => {
            setShowCreate(false)
            if (setActive) onClose() // same reason as above: don't stack the confirm
          }}
        />
      )}
    </div>,
    document.body,
  )
}

/** Default-No confirm for unregistering a vault (vault-manager slice). The body
 * states that ONLY the registry entry is removed and the on-disk directory is
 * kept; Cancel is autofocused, the destructive button is rose, and the backdrop
 * swallows clicks so none reach the manager under it. Mirrors
 * DeleteProjectConfirm. */
function UnregisterVaultConfirm({
  name,
  busy,
  onCancel,
  onConfirm,
}: {
  name: string
  busy: boolean
  onCancel: () => void
  onConfirm: () => void
}) {
  useEscapeLayer(true, onCancel)
  const t = useT()
  const u = t.topbar.unregisterVault
  return (
    <div
      className="fixed inset-0 z-[60] flex items-center justify-center bg-black/30 backdrop-blur-sm"
      {...modalBackdropProps}
    >
      <div
        onClick={(e) => e.stopPropagation()}
        className="w-[24rem] animate-grow-in rounded-2xl bg-white p-5 shadow-xl ring-1 ring-stone-200"
      >
        <h2 className="text-sm font-semibold text-stone-900">
          {u.title(name)}
        </h2>
        <p className="mt-1.5 text-xs leading-relaxed text-stone-600">
          {rich(u.body, {
            name,
            keep: <span className="font-semibold">{u.keepsFolder}</span>,
          })}
        </p>
        <div className="mt-5 flex justify-end gap-2">
          <button
            autoFocus
            onClick={onCancel}
            disabled={busy}
            className="rounded-lg px-3 py-1.5 text-xs text-stone-600 transition-colors hover:bg-stone-100 disabled:opacity-40"
          >
            {t.common.cancel}
          </button>
          <button
            onClick={onConfirm}
            disabled={busy}
            className="rounded-lg bg-rose-500 px-3 py-1.5 text-xs font-medium text-white transition-colors hover:bg-rose-600 disabled:opacity-60"
          >
            {busy ? u.unregistering : u.unregister}
          </button>
        </div>
      </div>
    </div>
  )
}

/** Locate-a-moved-vault modal (vault-manager slice). For a vault whose folder
 * moved on disk (row shows "missing"): the user pastes its new location and the
 * `lit vault set-path` backend re-points the registry in place. When the vault
 * is the active/served one the server repoints itself too, healing a live 410
 * with no restart — no forced rename, no zombie entry. The new path must resolve
 * server-side to an existing directory holding a lit-config.yaml, so a bad path
 * surfaces the backend's verbatim VaultRegistryError INLINE and the dialog stays
 * open for correction (mirrors RegisterVaultDialog / SetProjectPathDialog).
 * Escape cancels; the backdrop stops click-through so dismissing keeps the
 * manager open. */
function LocateVaultDialog({
  vault,
  onRelocateVault,
  onCancel,
  onLocated,
}: {
  vault: VaultEntry
  onRelocateVault: (name: string, path: string) => Promise<void>
  onCancel: () => void
  onLocated: () => void
}) {
  const t = useT()
  const l = t.topbar.locateVault
  const [path, setPath] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const trimmed = path.trim()
  const canSubmit = trimmed.length > 0 && !busy

  async function submit() {
    if (!canSubmit) return
    setBusy(true)
    setError(null)
    try {
      await onRelocateVault(vault.name, trimmed)
      onLocated()
    } catch (err) {
      // The line for what the server refused, inline; keep the dialog open for
      // correction. The server's full text is in the GUI log.
      setError(errorLine(err, null, getT().errors.vault))
      setBusy(false)
    }
  }

  // Guarded like RegisterVaultDialog: this dialog is the error's only surface,
  // so closing it mid-request would lose a late failure.
  useEscapeLayer(true, busy ? null : onCancel)
  return (
    <div
      className="fixed inset-0 z-[60] flex items-center justify-center bg-black/30 backdrop-blur-sm"
      {...modalBackdropProps}
    >
      <div
        onClick={(e) => e.stopPropagation()}
        className="w-[26rem] animate-grow-in rounded-2xl bg-white p-5 shadow-xl ring-1 ring-stone-200"
      >
        <h2 className="text-sm font-semibold text-stone-900">
          {l.title(vault.name)}
        </h2>
        <p className="mt-1.5 text-xs leading-relaxed text-stone-500">
          {rich(l.body, {
            path: (
              <span className="font-mono text-[0.9em] text-rose-400 line-through">
                {vault.path}
              </span>
            ),
          })}
        </p>
        <label className="mt-4 flex flex-col gap-1">
          <span className="text-[11px] font-semibold uppercase tracking-wider text-stone-600">
            {l.pathLabel}
          </span>
          <PathField
            mode="vault-dir"
            value={path}
            onChange={setPath}
            disabled={busy}
            autoFocus
            placeholder="/home/you/literature_vault"
            onEnter={() => {
              if (canSubmit) submit()
            }}
          />
        </label>
        {error && (
          <p className="mt-3 rounded-md bg-rose-50 px-3 py-2 text-[11px] leading-relaxed text-rose-600">
            {error}
          </p>
        )}
        <div className="mt-5 flex justify-end gap-2">
          <button
            onClick={onCancel}
            disabled={busy}
            className="rounded-lg px-3 py-1.5 text-xs text-stone-600 transition-colors hover:bg-stone-100 disabled:opacity-40"
          >
            {t.common.cancel}
          </button>
          <button
            onClick={submit}
            disabled={!canSubmit}
            className="rounded-lg bg-accent-500 px-3 py-1.5 text-xs font-medium text-white transition-colors hover:bg-accent-600 disabled:opacity-60"
          >
            {busy ? l.locating : l.locate}
          </button>
        </div>
      </div>
    </div>
  )
}

/** Register-an-existing-vault modal (vault-manager slice). name + server-side
 * path + a "switch to it after registering" checkbox. Mirrors NewProjectDialog's
 * shell, but surfaces the backend's verbatim VaultRegistryError INLINE (not just
 * a toast) so the user can fix the path without losing the form — the path must
 * resolve server-side to an existing directory containing a lit-config.yaml. On
 * success `onRegistered(setActive)` closes the form; when setActive the parent
 * also closes so App's reused SwitchVaultDialog stands alone. Escape cancels;
 * the backdrop swallows clicks so none reach the manager under it. */
function RegisterVaultDialog({
  onRegisterVault,
  onClose,
  onRegistered,
}: {
  onRegisterVault: (name: string, path: string, setActive: boolean) => Promise<void>
  onClose: () => void
  onRegistered: (setActive: boolean) => void
}) {
  const t = useT()
  const r = t.topbar.registerVault
  const [name, setName] = useState('')
  const [path, setPath] = useState('')
  const [setActive, setSetActive] = useState(false)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const canSubmit = name.trim().length > 0 && path.trim().length > 0 && !busy

  async function submit() {
    if (!canSubmit) return
    setBusy(true)
    setError(null)
    try {
      await onRegisterVault(name.trim(), path.trim(), setActive)
      onRegistered(setActive)
    } catch (err) {
      // The line for what the server refused, inline; keep the dialog open for
      // correction. The server's full text is in the GUI log.
      setError(errorLine(err, null, getT().errors.vault))
      setBusy(false)
    }
  }

  const INPUT =
    'w-full rounded-md border border-stone-300 bg-white px-2.5 py-1.5 text-sm ' +
    'text-stone-800 shadow-sm focus:outline-none focus:ring-1 focus:ring-accent-500 ' +
    'disabled:opacity-50'

  // Guarded like Cancel: this dialog is the error's only surface, so closing it
  // mid-request would lose a late failure (setError on an unmounted component is
  // a no-op). The layer stays registered so Escape does not fall through to the
  // panel underneath either.
  useEscapeLayer(true, busy ? null : onClose)
  return (
    <div
      className="fixed inset-0 z-[60] flex items-center justify-center bg-black/30 backdrop-blur-sm"
      {...modalBackdropProps}
    >
      <div
        onClick={(e) => e.stopPropagation()}
        className="w-[26rem] animate-grow-in rounded-2xl bg-white p-5 shadow-xl ring-1 ring-stone-200"
      >
        <h2 className="text-sm font-semibold text-stone-900">
          {r.title}
        </h2>
        <p className="mt-1.5 text-xs leading-relaxed text-stone-500">
          {r.intro}
        </p>
        <div className="mt-4 flex flex-col gap-3">
          <label className="flex flex-col gap-1">
            <span className="text-[11px] font-semibold uppercase tracking-wider text-stone-600">
              {r.nameLabel}
            </span>
            <input
              autoFocus
              type="text"
              value={name}
              disabled={busy}
              placeholder="main"
              onChange={(e) => setName(e.target.value)}
              className={INPUT}
            />
          </label>
          <label className="flex flex-col gap-1">
            <span className="text-[11px] font-semibold uppercase tracking-wider text-stone-600">
              {r.pathLabel}
            </span>
            <PathField
              mode="vault-dir"
              value={path}
              onChange={setPath}
              disabled={busy}
              placeholder="/home/you/literature_vault"
              onEnter={submit}
            />
            <span className="text-[11px] text-stone-400">
              {r.pathHint}
            </span>
          </label>
          <label className="flex items-center gap-2 text-xs text-stone-600">
            <input
              type="checkbox"
              checked={setActive}
              disabled={busy}
              onChange={(e) => setSetActive(e.target.checked)}
              className="h-3.5 w-3.5 rounded border-stone-300 text-accent-500 focus:ring-accent-500"
            />
            {r.switchAfter}
          </label>
        </div>
        {error && (
          <p className="mt-3 rounded-md bg-rose-50 px-3 py-2 text-[11px] leading-relaxed text-rose-600">
            {error}
          </p>
        )}
        <div className="mt-5 flex justify-end gap-2">
          <button
            onClick={onClose}
            disabled={busy}
            className="rounded-lg px-3 py-1.5 text-xs text-stone-600 transition-colors hover:bg-stone-100 disabled:opacity-40"
          >
            {t.common.cancel}
          </button>
          <button
            onClick={submit}
            disabled={!canSubmit}
            className="rounded-lg bg-accent-500 px-3 py-1.5 text-xs font-medium text-white transition-colors hover:bg-accent-600 disabled:opacity-60"
          >
            {busy ? r.registering : r.register}
          </button>
        </div>
      </div>
    </div>
  )
}

/** Create a NEW vault: the `lit init` backend (POST /api/vaults/create), reachable
 * from the manager rather than only from the first-run welcome page. Sibling of
 * RegisterVaultDialog, and deliberately shaped like it — same shell, same inline
 * verbatim-400, same "switch to it after" checkbox reusing App's switchVault.
 *
 * The two differ in what the path field means, which is the whole point of having
 * two dialogs instead of one: Register takes the vault folder ITSELF (it must
 * already hold a lit-config.yaml), while Create takes the PARENT and makes the
 * folder under it. Getting that backwards is the easy mistake, so the field is
 * labelled "Location", the name is a separate field, and the composed
 * "place / name" is previewed in the card above — the same shape the welcome
 * page uses.
 *
 * One name, not two: the folder created on disk and the registry entry share it.
 * (`lit init --register-as` splits them; the GUI does not need to.) */
function CreateVaultDialog({
  suggestedName,
  onCreateVault,
  onClose,
  onCreated,
}: {
  /** The server's offer: the default name, or the first free `<default>_<n>`
   * once a library already has it (a second library would collide). */
  suggestedName?: string
  onCreateVault: (parentDir: string, name: string, setActive: boolean) => Promise<void>
  onClose: () => void
  onCreated: (setActive: boolean) => void
}) {
  const t = useT()
  const c = t.topbar.createVault
  const offered = suggestedName ?? 'literature_vault'
  const [parentDir, setParentDir] = useState('~')
  const [name, setName] = useState(offered)
  const [setActive, setSetActive] = useState(true)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  // Default the parent dir to the server's suggested start (Desktop → Documents
  // → Home), so a new library lands somewhere the user can actually see. The
  // anchors let the card name it ("Desktop"). Graceful fallback: on failure
  // the '~' default stands — never block the dialog on this read.
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
        /* keep the '~' default so the dialog always works */
      })
    return () => {
      cancelled = true
    }
  }, [])

  // The name starts as the server's free offer, and an empty name is no
  // blocker: an emptied field falls back server-side to that same offer
  // (createVault omits it), so only the Location is required — matching the
  // welcome page.
  const canSubmit = parentDir.trim().length > 0 && !busy

  async function submit() {
    if (!canSubmit) return
    setBusy(true)
    setError(null)
    try {
      await onCreateVault(parentDir.trim(), name.trim(), setActive)
      onCreated(setActive)
    } catch (err) {
      // The line for what the server refused (missing parent / target not
      // empty / name taken), inline; keep the dialog open so the user can
      // correct it.
      setError(errorLine(err, null, getT().errors.vault))
      setBusy(false)
    }
  }

  const loc = describeLocation(parentDir, anchors)

  const INPUT =
    'w-full rounded-md border border-stone-300 bg-white px-2.5 py-1.5 text-sm ' +
    'text-stone-800 shadow-sm focus:outline-none focus:ring-1 focus:ring-accent-500 ' +
    'disabled:opacity-50'

  // Same guard as RegisterVaultDialog, same reason: mid-write the layer holds
  // Escape without acting on it, so the key neither dismisses this dialog nor
  // reaches the panel underneath.
  useEscapeLayer(true, busy ? null : onClose)
  return (
    <div
      className="fixed inset-0 z-[60] flex items-center justify-center bg-black/30 backdrop-blur-sm"
      {...modalBackdropProps}
    >
      <div
        onClick={(e) => e.stopPropagation()}
        className="w-[26rem] animate-grow-in rounded-2xl bg-white p-5 shadow-xl ring-1 ring-stone-200"
      >
        <h2 className="text-sm font-semibold text-stone-900">{c.title}</h2>
        <p className="mt-1.5 text-xs leading-relaxed text-stone-500">
          {c.intro}
        </p>
        <div className="mt-4 flex flex-col gap-3">
          <div className="rounded-xl border border-stone-200 bg-stone-50 px-4 py-3">
            <div className="flex items-center gap-2 text-sm font-medium text-stone-800">
              {anchorIcon(loc.kind, 'h-5 w-5 shrink-0 text-stone-500')}
              <span className="min-w-0 truncate">
                {loc.label} <span className="text-stone-400">/</span>{' '}
                {name.trim() || offered}
              </span>
            </div>
          </div>
          <label className="flex flex-col gap-1">
            <span className="text-[11px] font-semibold uppercase tracking-wider text-stone-600">
              {c.locationLabel}
            </span>
            <PathField
              mode="parent-dir"
              value={parentDir}
              onChange={setParentDir}
              disabled={busy}
              placeholder="/home/you"
              onEnter={submit}
            />
            <span className="text-[11px] text-stone-400">
              {c.locationHint}
            </span>
          </label>
          <label className="flex flex-col gap-1">
            <span className="text-[11px] font-semibold uppercase tracking-wider text-stone-600">
              {c.nameLabel}
            </span>
            <input
              autoFocus
              type="text"
              value={name}
              disabled={busy}
              spellCheck={false}
              placeholder={offered}
              onChange={(e) => setName(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === 'Enter') submit()
              }}
              className={INPUT}
            />
          </label>
          <label className="flex items-center gap-2 text-xs text-stone-600">
            <input
              type="checkbox"
              checked={setActive}
              disabled={busy}
              onChange={(e) => setSetActive(e.target.checked)}
              className="h-3.5 w-3.5 rounded border-stone-300 text-accent-500 focus:ring-accent-500"
            />
            {c.switchAfter}
          </label>
        </div>
        {error && (
          <p className="mt-3 rounded-md bg-rose-50 px-3 py-2 text-[11px] leading-relaxed text-rose-600">
            {error}
          </p>
        )}
        <div className="mt-5 flex justify-end gap-2">
          <button
            onClick={onClose}
            disabled={busy}
            className="rounded-lg px-3 py-1.5 text-xs text-stone-600 transition-colors hover:bg-stone-100 disabled:opacity-40"
          >
            {t.common.cancel}
          </button>
          <button
            onClick={submit}
            disabled={!canSubmit}
            className="rounded-lg bg-accent-500 px-3 py-1.5 text-xs font-medium text-white transition-colors hover:bg-accent-600 disabled:opacity-60"
          >
            {busy ? c.creating : c.create}
          </button>
        </div>
      </div>
    </div>
  )
}
