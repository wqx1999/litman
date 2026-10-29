import { useState } from 'react'
import { createPortal } from 'react-dom'
import type {
  IndexPaper,
  ProjectEntry,
} from '../types'
import { projectHealth } from '../projects'
import {
  createProject,
  deleteProject,
  renameProject,
  setProjectPath,
} from '../api'
import type { ToastVariant } from '../ui/Toast'
import { IconMove, IconTrash, PencilIcon } from '../ui/icons'
import HelpHint from '../ui/HelpHint'
import PathField from '../ui/PathField'
import { useEscapeLayer } from '../ui/escapeStack'
import {
  modalBackdropProps,
  useModalCardFocus,
} from '../ui/modalShell'

/** The global Projects manager (P4): lists every registered project with its
 * in-use count, removes one through a default-No confirm into the `lit project
 * rm` backend (cascade: unlink papers + tear down reflib links, directory kept),
 * and registers a new one via NewProjectDialog. Project create/delete are
 * global (not per-paper), so they live here rather than in the Cockpit — the
 * Cockpit's project dropdown only links/unlinks the selected paper. macOS-style
 * modal shell shared with the rest of the app.
 *
 * Portaled to document.body: TopBar's <header> carries `backdrop-blur-md`, and
 * a backdrop-filter establishes a containing block for `position: fixed`
 * descendants. Rendered inline, this modal's `fixed inset-0` would resolve
 * against the ~44px-tall header instead of the viewport, centering (and
 * clipping) the dialog on the header's midline. The portal lifts the whole
 * subtree (this manager + its nested confirm/new dialogs) out of that
 * containing block so `fixed` is viewport-relative again. */
export default function ProjectManager({
  projects,
  allPapers,
  onChanged,
  onClose,
  notify,
}: {
  projects: ProjectEntry[]
  allPapers: IndexPaper[]
  onChanged: () => void
  onClose: () => void
  notify: (
    msg: string,
    variant?: ToastVariant,
    opts?: { sticky?: boolean },
  ) => void
}) {
  // The project awaiting delete confirmation, being renamed, or having its path
  // re-pointed; the new-project dialog toggle; and whether a write is in flight
  // (all gate the list controls).
  const [pendingDelete, setPendingDelete] = useState<string | null>(null)
  const [pendingRename, setPendingRename] = useState<ProjectEntry | null>(null)
  const [pendingSetPath, setPendingSetPath] = useState<ProjectEntry | null>(null)
  const [showNew, setShowNew] = useState(false)
  const [busy, setBusy] = useState(false)

  // Is one of the nested dialogs on screen? Deliberately WITHOUT `busy`: a write
  // finishing is no reason to grab focus back.
  const childOpen =
    pendingDelete != null || pendingRename != null || pendingSetPath != null || showNew
  // Focus the card on mount so Escape reaches the handler below — this is a
  // blocking dialog, so nothing else is listening — and again whenever a nested
  // dialog closes, which drops focus to <body> (see useModalCardFocus).
  const cardFocus = useModalCardFocus(childOpen)

  // Papers linked to a project, off the loaded INDEX (no round-trip).
  function countFor(name: string): number {
    return allPapers.filter((p) => (p.projects ?? []).includes(name)).length
  }

  async function doDelete(name: string) {
    setBusy(true)
    try {
      const res = await deleteProject(name)
      const left = res.foldersLeft
      if (left.length) {
        // The project is out of the config, so nothing looks at these folders
        // again: this is the one chance to say so — keep it up until read.
        const where = left.map((f) => `${f.path} (${f.reason})`).join(' · ')
        notify(
          `Removed project “${name}” · could not clean up ${where} — delete ` +
            `${left.length === 1 ? 'its' : 'their'} links by hand`,
          'warning',
          { sticky: true },
        )
      } else {
        notify(`Removed project “${name}”.`, 'success')
      }
      onChanged()
    } catch (err) {
      notify(err instanceof Error ? err.message : String(err), 'error')
    } finally {
      setBusy(false)
      setPendingDelete(null)
    }
  }

  async function doRename(oldName: string, newName: string) {
    setBusy(true)
    try {
      await renameProject(oldName, newName)
      notify(`Renamed project to “${newName}”.`, 'success')
      onChanged()
      setPendingRename(null)
    } catch (err) {
      notify(err instanceof Error ? err.message : String(err), 'error')
      // Keep the dialog open so the user can fix the name.
    } finally {
      setBusy(false)
    }
  }

  async function doSetPath(name: string, path: string) {
    setBusy(true)
    try {
      const res = await setProjectPath(name, path)
      notify(
        res.changed
          ? `“${name}” now points at ${res.path}. If the folder wasn't moved with its links, rebuild views.`
          : `“${name}” already points there.`,
        res.changed ? 'success' : 'info',
      )
      onChanged()
      setPendingSetPath(null)
    } catch (err) {
      notify(err instanceof Error ? err.message : String(err), 'error')
      // Keep the dialog open so the user can fix the path.
    } finally {
      setBusy(false)
    }
  }

  const sorted = projects.slice().sort((a, b) => a.name.localeCompare(b.name))
  const blocked = busy || childOpen

  // The four nested dialogs used to be hand-enumerated here so Escape would not
  // close the panel out from under one of them — see VaultManager for why that
  // is gone: each nested dialog is a layer above this one now (ui/escapeStack).
  useEscapeLayer(true, onClose)
  return createPortal(
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/30 backdrop-blur-sm"
      {...modalBackdropProps}
    >
      <div
        {...cardFocus}
        onClick={(e) => e.stopPropagation()}
        className="flex max-h-[70vh] w-[28rem] animate-grow-in focus:outline-none flex-col rounded-2xl bg-white p-5 shadow-xl ring-1 ring-stone-200"
      >
        <h2 className="text-sm font-semibold text-stone-900">
          Projects
          {sorted.length > 0 && (
            <span className="font-normal tabular-nums text-stone-400"> · {sorted.length}</span>
          )}
          <HelpHint text="Register, rename or re-point your projects. Removing one never deletes its folder." />
        </h2>
        <div className="mt-3 min-h-0 flex-1 overflow-y-auto rounded-lg border border-stone-200">
          {sorted.length === 0 && (
            <div className="px-3 py-4 text-center text-xs text-stone-400">
              No projects registered.
            </div>
          )}
          {sorted.map((p) => {
            const health = projectHealth(p.status)
            return (
            <div
              key={p.name}
              className="flex items-center justify-between gap-2 border-b border-stone-100 px-3 py-2 last:border-b-0"
            >
              <div className="min-w-0">
                <div className="flex items-center gap-1.5">
                  <span className="truncate text-sm font-medium text-stone-800">
                    {p.name}
                  </span>
                  {health && (
                    <span
                      title={health.title}
                      className={`shrink-0 rounded-full px-1.5 py-0.5 text-[10px] font-semibold uppercase tracking-wide ${
                        health.tone === 'missing'
                          ? 'bg-rose-100 text-rose-700'
                          : 'bg-amber-100 text-amber-700'
                      }`}
                    >
                      {health.badge}
                    </span>
                  )}
                </div>
                <div
                  className={`truncate font-mono text-[11px] ${
                    health?.tone === 'missing'
                      ? 'text-rose-400 line-through'
                      : 'text-stone-400'
                  }`}
                >
                  {p.path || 'no folder set'}
                </div>
              </div>
              <div className="flex shrink-0 items-center gap-2">
                <span className="text-[11px] text-stone-400">
                  {countFor(p.name)} papers
                </span>
                <button
                  type="button"
                  aria-label={`Rename project ${p.name}`}
                  title={`Rename project “${p.name}”`}
                  disabled={blocked}
                  onClick={() => setPendingRename(p)}
                  className="grid h-6 w-6 place-items-center rounded-md text-stone-300 transition-colors hover:bg-accent-50 hover:text-accent-600 disabled:opacity-40"
                >
                  <PencilIcon className="h-3.5 w-3.5" />
                </button>
                <button
                  type="button"
                  aria-label={`Change path for project ${p.name}`}
                  title={`Re-point “${p.name}” at a new folder`}
                  disabled={blocked}
                  onClick={() => setPendingSetPath(p)}
                  className="grid h-6 w-6 place-items-center rounded-md text-stone-300 transition-colors hover:bg-accent-50 hover:text-accent-600 disabled:opacity-40"
                >
                  <IconMove />
                </button>
                <button
                  type="button"
                  aria-label={`Delete project ${p.name}`}
                  title={`Delete project “${p.name}”`}
                  disabled={blocked}
                  onClick={() => setPendingDelete(p.name)}
                  className="grid h-6 w-6 place-items-center rounded-md text-stone-300 transition-colors hover:bg-rose-50 hover:text-rose-500 disabled:opacity-40"
                >
                  <IconTrash />
                </button>
              </div>
            </div>
            )
          })}
        </div>
        <div className="mt-4 flex justify-between">
          <button
            type="button"
            disabled={blocked}
            onClick={() => setShowNew(true)}
            className="rounded-lg border border-accent-200 bg-accent-50 px-3 py-1.5 text-xs font-medium text-accent-700 shadow-sm transition-colors hover:bg-accent-100 disabled:opacity-50"
          >
            + New project
          </button>
          <button
            type="button"
            disabled={blocked}
            onClick={onClose}
            className="rounded-lg px-3 py-1.5 text-xs text-stone-600 transition-colors hover:bg-stone-100 disabled:opacity-40"
          >
            Done
          </button>
        </div>
      </div>
      {pendingDelete != null && (
        <DeleteProjectConfirm
          name={pendingDelete}
          count={countFor(pendingDelete)}
          busy={busy}
          onCancel={() => setPendingDelete(null)}
          onConfirm={() => doDelete(pendingDelete)}
        />
      )}
      {pendingRename != null && (
        <RenameProjectDialog
          project={pendingRename}
          count={countFor(pendingRename.name)}
          existing={projects.map((p) => p.name)}
          busy={busy}
          onCancel={() => setPendingRename(null)}
          onConfirm={(next) => doRename(pendingRename.name, next)}
        />
      )}
      {pendingSetPath != null && (
        <SetProjectPathDialog
          project={pendingSetPath}
          busy={busy}
          onCancel={() => setPendingSetPath(null)}
          onConfirm={(path) => doSetPath(pendingSetPath.name, path)}
        />
      )}
      {showNew && (
        <NewProjectDialog
          onClose={() => setShowNew(false)}
          onCreated={() => {
            setShowNew(false)
            onChanged()
          }}
          notify={notify}
        />
      )}
    </div>,
    document.body,
  )
}

/** Default-No confirm for unregistering a project (P4). The body states the
 * cascade (unlinks N papers, drops reflib links) and that the on-disk directory
 * is kept; Cancel is autofocused, the destructive button is rose, and the
 * backdrop swallows clicks so a stray one never reaches the manager under it. */
function DeleteProjectConfirm({
  name,
  count,
  busy,
  onCancel,
  onConfirm,
}: {
  name: string
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
        className="w-[24rem] animate-grow-in rounded-2xl bg-white p-5 shadow-xl ring-1 ring-stone-200"
      >
        <h2 className="text-sm font-semibold text-stone-900">
          Delete project “{name}”?
        </h2>
        <p className="mt-1.5 text-xs leading-relaxed text-stone-600">
          This unregisters “{name}”
          {count > 0
            ? ` and unlinks it from ${count} paper${count === 1 ? '' : 's'}`
            : ''}{' '}
          and removes its reflib links + REFERENCES.md. The project folder on
          disk is kept. This cannot be undone.
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

/** Rename a project (prefilled with the current name). Mirrors NewProjectDialog's
 * shell; the input autofocuses + selects. A blank/unchanged name, or one that
 * collides with another registered project, disables Save. The path is carried
 * over unchanged — the backend renames across both truth sources + every paper.
 * Escape cancels; the backdrop swallows clicks so a stray one never reaches the
 * ProjectManager open. */
function RenameProjectDialog({
  project,
  count,
  existing,
  busy,
  onCancel,
  onConfirm,
}: {
  project: ProjectEntry
  count: number
  existing: string[]
  busy: boolean
  onCancel: () => void
  onConfirm: (next: string) => void
}) {
  const [next, setNext] = useState(project.name)
  const trimmed = next.trim()
  const collides = trimmed !== project.name && existing.includes(trimmed)
  const canSubmit =
    trimmed.length > 0 && trimmed !== project.name && !collides && !busy

  const INPUT =
    'w-full rounded-md border border-stone-300 bg-white px-2.5 py-1.5 text-sm ' +
    'text-stone-800 shadow-sm focus:outline-none focus:ring-1 focus:ring-accent-500 ' +
    'disabled:opacity-50'

  useEscapeLayer(true, onCancel)
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
          Rename “{project.name}”?
        </h2>
        <p className="mt-1.5 text-xs leading-relaxed text-stone-500">
          Renames it everywhere
          {count > 0
            ? ` — re-tags ${count} paper${count === 1 ? '' : 's'}`
            : ''}
          . The folder on disk and its path are unchanged.
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
          className={`${INPUT} mt-3`}
        />
        {collides && (
          <p className="mt-1.5 text-[11px] text-rose-500">
            “{trimmed}” already exists — pick another name.
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

/** Re-point a project's on-disk path (set-path). For when the folder was moved
 * manually and the registry needs to follow. Prefilled with the current path;
 * the new path must be absolute + already exist + be a directory server-side, so
 * a bad path surfaces the backend's TaxonomyError verbatim and the dialog stays
 * open for correction. Copy makes clear this does NOT move the folder. Escape
 * cancels; the backdrop swallows clicks so none reach the manager under it. */
function SetProjectPathDialog({
  project,
  busy,
  onCancel,
  onConfirm,
}: {
  project: ProjectEntry
  busy: boolean
  onCancel: () => void
  onConfirm: (path: string) => void
}) {
  const [path, setPath] = useState(project.path ?? '')
  const trimmed = path.trim()
  const canSubmit = trimmed.length > 0 && !busy

  useEscapeLayer(true, onCancel)
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
          Path for “{project.name}”
        </h2>
        <p className="mt-1.5 text-xs leading-relaxed text-stone-500">
          Points the registry at where the folder lives now. This does NOT move
          the folder — move it yourself first, then paste the new location here.
        </p>
        <label className="mt-4 flex flex-col gap-1">
          <span className="text-[11px] font-semibold uppercase tracking-wider text-stone-600">
            Absolute path
          </span>
          <PathField
            mode="existing-dir"
            value={path}
            onChange={setPath}
            disabled={busy}
            autoFocus
            selectOnFocus
            placeholder="/work/you/Project/pepforge"
            onEnter={() => {
              if (canSubmit) onConfirm(trimmed)
            }}
          />
          <span className="text-[11px] text-stone-400">
            the folder itself, must already exist
          </span>
        </label>
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
            {busy ? 'Saving…' : 'Save path'}
          </button>
        </div>
      </div>
    </div>
  )
}

/** Register-a-new-project modal (name + absolute path). Mirrors SaveDialog's
 * shell; the path must be absolute + already exist + be a directory server-side
 * (A7; a relative path is rejected, not resolved against the server cwd), so a
 * bad path surfaces the backend's TaxonomyError verbatim via `notify` and the
 * dialog stays open for correction. Escape cancels. The backdrop swallows
 * click-through so, when opened from the ProjectManager, dismissing it does not
 * also close the manager. */
function NewProjectDialog({
  onClose,
  onCreated,
  notify,
}: {
  onClose: () => void
  onCreated: () => void
  notify: (msg: string, variant?: ToastVariant) => void
}) {
  const [name, setName] = useState('')
  const [path, setPath] = useState('')
  const [busy, setBusy] = useState(false)

  const canSubmit = name.trim().length > 0 && path.trim().length > 0 && !busy

  async function submit() {
    if (!canSubmit) return
    setBusy(true)
    try {
      await createProject(name.trim(), path.trim())
      notify(`Linked project “${name.trim()}”.`, 'success')
      onCreated()
    } catch (err) {
      notify(err instanceof Error ? err.message : String(err), 'error')
      setBusy(false) // keep the dialog open so the user can fix the path
    }
  }

  const INPUT =
    'w-full rounded-md border border-stone-300 bg-white px-2.5 py-1.5 text-sm ' +
    'text-stone-800 shadow-sm focus:outline-none focus:ring-1 focus:ring-accent-500 ' +
    'disabled:opacity-50'

  useEscapeLayer(true, onClose)
  return (
    <div
      className="fixed inset-0 z-[60] flex items-center justify-center bg-black/30 backdrop-blur-sm"
      {...modalBackdropProps}
    >
      <div
        onClick={(e) => e.stopPropagation()}
        className="w-[24rem] animate-grow-in rounded-2xl bg-white p-5 shadow-xl ring-1 ring-stone-200"
      >
        <h2 className="text-sm font-semibold text-stone-900">New project</h2>
        <p className="mt-1.5 text-xs leading-relaxed text-stone-500">
          Links a name to an existing folder (nothing is created).
        </p>
        <div className="mt-4 flex flex-col gap-3">
          <label className="flex flex-col gap-1">
            <span className="text-[11px] font-semibold uppercase tracking-wider text-stone-600">
              Name
            </span>
            <input
              autoFocus
              type="text"
              value={name}
              disabled={busy}
              placeholder="pepforge"
              onChange={(e) => setName(e.target.value)}
              className={INPUT}
            />
          </label>
          <label className="flex flex-col gap-1">
            <span className="text-[11px] font-semibold uppercase tracking-wider text-stone-600">
              Absolute path
            </span>
            <PathField
              mode="existing-dir"
              value={path}
              onChange={setPath}
              disabled={busy}
              placeholder="/work/you/Project/pepforge"
              onEnter={submit}
            />
            <span className="text-[11px] text-stone-400">
              the folder itself, must exist
            </span>
          </label>
        </div>
        <div className="mt-5 flex justify-end gap-2">
          <button
            onClick={onClose}
            disabled={busy}
            className="rounded-lg px-3 py-1.5 text-xs text-stone-600 transition-colors hover:bg-stone-100 disabled:opacity-40"
          >
            Cancel
          </button>
          <button
            onClick={submit}
            disabled={!canSubmit}
            className="rounded-lg bg-accent-500 px-3 py-1.5 text-xs font-medium text-white transition-colors hover:bg-accent-600 disabled:opacity-60"
          >
            {busy ? 'Creating…' : 'Create'}
          </button>
        </div>
      </div>
    </div>
  )
}
