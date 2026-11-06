import { useEffect, useRef, useState } from 'react'
import type { Messages } from '../i18n'
import { useT } from '../i18n'
import type { SyncStatus } from '../types'
import { withCode, withoutCode } from '../ui/codeSpans'
import { useEscapeLayer } from '../ui/escapeStack'
import { IconCloud } from '../ui/icons'

/** The cloud sync's button, right after the library selector: a cloud whose
 * corner dot says how the auto sync stands, and a click opens a small popover
 * with one line saying what happened, "Sync now" and, after conflicts or code
 * repos left behind, the way to the activity log.
 * A library without `sync:` gets the bare cloud, whose popover says it is
 * off. Not drawn without a served library. */

type Look = 'ok' | 'running' | 'conflicts' | 'hollow' | 'error' | 'idle'

// The badge dot, in the style of the agent button's.
const DOT: Record<Look, string> = {
  ok: 'bg-emerald-500',
  running: 'bg-accent-500 animate-pulse',
  conflicts: 'bg-amber-500',
  hollow: 'border-[1.5px] border-stone-400 bg-stone-50',
  error: 'bg-rose-500',
  idle: 'bg-stone-400',
}

function look(status: SyncStatus): Look {
  switch (status.state) {
    case 'ok':
      return 'ok'
    case 'running':
      return 'running'
    case 'conflicts':
      return 'conflicts'
    case 'offline':
    case 'not-initialised':
      return 'hollow'
    case 'error':
      return 'error'
    default:
      return 'idle'
  }
}

/** "3 min ago", from an ISO time. */
function ago(t: Messages['sync'], iso: string | null, now: number): string | null {
  if (!iso) return null
  const then = Date.parse(iso)
  if (Number.isNaN(then)) return null
  const s = Math.max(0, Math.round((now - then) / 1000))
  if (s < 60) return t.ago.now
  if (s < 3600) return t.ago.minutes(Math.floor(s / 60))
  if (s < 86400) return t.ago.hours(Math.floor(s / 3600))
  return t.ago.days(Math.floor(s / 86400))
}

function clock(iso: string | null): string | null {
  if (!iso) return null
  const d = new Date(iso)
  if (Number.isNaN(d.getTime())) return null
  return `${String(d.getHours()).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')}`
}

/** The rclone remote's name, `gdrive` of `gdrive:litman-vault`. */
function remoteName(remote: string | null): string | null {
  return remote ? remote.split(':', 1)[0] || null : null
}

/** Why the auto sync stopped, as the popover says it; null when the cause
 * has no line of its own. The toast after a stop says the same. */
export function stopCause(
  t: Messages['sync'],
  reason: string | null | undefined,
  remote: string | null,
): string | null {
  switch (reason) {
    case 'auth':
      return t.line.auth(remoteName(remote))
    case 'safety-stop':
      return t.line.safetyStop
    case 'choice':
      return t.line.choice
    default:
      return null
  }
}

export interface SyncView {
  /** The popover's line. */
  line: string
  syncNow: boolean
  viewLog: boolean
}

/** What the popover shows for `status` (`null`: no `sync:` set up;
 * `undefined`: not known yet). Only what happened: the details are for
 * `lit sync status`. */
export function describeSync(
  t: Messages['sync'],
  status: SyncStatus | null | undefined,
  now: number,
): SyncView {
  const bare = { syncNow: false, viewLog: false }
  if (status === undefined) return { ...bare, line: t.line.checking }
  if (status === null) return { ...bare, line: t.button.notConfigured }
  const notice = status.notice?.kind === 'synced' ? status.notice : null
  const when = ago(t, status.lastOkAt, now)
  switch (status.state) {
    case 'running':
      return {
        ...bare,
        line:
          status.reason === 'busy'
            ? t.line.busy(clock(status.holder?.since ?? null))
            : t.line.running,
      }
    case 'ok': {
      // Code repos the last sync left behind: the log button shows which.
      const behind = notice?.reposNotRestored?.length ?? 0
      if (behind) return { line: t.line.okRepos(when ?? t.ago.now, behind), syncNow: true, viewLog: true }
      return { ...bare, line: when ? t.line.ok(when) : t.line.okUnknown, syncNow: true }
    }
    case 'conflicts': {
      const n = (notice?.conflicts?.length ?? 0) + (notice?.deletedFolders?.length ?? 0)
      return { line: t.line.conflicts(when ?? t.ago.now, n), syncNow: true, viewLog: true }
    }
    case 'offline':
      return { ...bare, line: t.line.offline(clock(status.nextTryAt)), syncNow: true }
    case 'not-initialised':
      return { ...bare, line: t.line.notInitialised }
    case 'error':
      return {
        ...bare,
        line: stopCause(t, status.reason, status.remote) ?? t.line.other,
        syncNow: true,
      }
    default:
      return { ...bare, line: t.line.idle, syncNow: true }
  }
}

interface Props {
  /** `null`: the library has no `sync:` set up; `undefined`: not known yet. */
  status: SyncStatus | null | undefined
  onSyncNow: () => void
  onOpenLog: () => void
}

export default function SyncButton({ status, onSyncNow, onOpenLog }: Props) {
  const t = useT().sync
  const [open, setOpen] = useState(false)
  const root = useRef<HTMLDivElement>(null)
  // Esc, or a press anywhere else, closes it (LangMenu's pattern). Not a
  // full-screen catcher: the header's backdrop blur would confine a fixed
  // one to the header itself.
  useEscapeLayer(open, () => setOpen(false))
  useEffect(() => {
    if (!open) return
    const onDown = (e: MouseEvent) => {
      if (!root.current?.contains(e.target as Node)) setOpen(false)
    }
    document.addEventListener('mousedown', onDown)
    return () => document.removeEventListener('mousedown', onDown)
  }, [open])
  const view = describeSync(t, status, Date.now())
  return (
    <div ref={root} className="relative shrink-0">
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        title={t.button.title}
        aria-label={`${t.button.title}: ${withoutCode(view.line)}`}
        aria-expanded={open}
        data-sync-state={status === null ? 'not-configured' : (status?.state ?? 'unknown')}
        className="relative grid h-8 w-8 place-items-center rounded-lg border border-stone-300 bg-white text-stone-500 shadow-sm transition duration-200 ease-fluid hover:bg-stone-50 hover:text-stone-700"
      >
        <IconCloud />
        {status && (
          <span
            aria-hidden="true"
            className={`absolute -right-0.5 -top-0.5 h-2.5 w-2.5 rounded-full ring-2 ring-stone-50 ${DOT[look(status)]}`}
          />
        )}
      </button>
      {open && (
        <div
          role="dialog"
          aria-label={t.button.title}
          data-sync-popover=""
          className="absolute left-0 top-full z-50 mt-2 w-72 rounded-xl border border-stone-200 bg-stone-50 p-3 shadow-lg shadow-stone-900/10"
        >
          <div className="text-sm leading-relaxed text-balance text-stone-800">{withCode(view.line)}</div>
          {(view.syncNow || view.viewLog) && (
            <div className="mt-2.5 flex items-center gap-2">
              {view.syncNow && (
                <button
                  type="button"
                  onClick={onSyncNow}
                  className="rounded-lg bg-accent-500 px-3 py-1.5 text-sm font-medium text-white transition duration-200 ease-fluid hover:bg-accent-600"
                >
                  {t.button.syncNow}
                </button>
              )}
              {view.viewLog && (
                <button
                  type="button"
                  onClick={() => {
                    setOpen(false)
                    onOpenLog()
                  }}
                  className="rounded-lg px-2.5 py-1.5 text-sm text-stone-600 transition duration-200 ease-fluid hover:bg-stone-200/70 hover:text-stone-800"
                >
                  {t.button.viewLog}
                </button>
              )}
            </div>
          )}
        </div>
      )}
    </div>
  )
}
