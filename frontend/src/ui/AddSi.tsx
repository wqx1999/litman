import { useEffect, useRef, useState } from 'react'
import { createPortal } from 'react-dom'
import {
  ApiError,
  confirmSi,
  fetchSiRequest,
  removeSiRow,
  SiApiError,
  uploadSiFile,
  type SiRequestSeed,
  type SiRow,
  type SiSnapshot,
} from '../api'
import { formatBytes } from './bytes'
import { useEscapeLayer } from './escapeStack'
import { modalBackdropProps, useModalCardFocus } from './modalShell'

/** The SI window (task-supplementary-info): one paper's supplementary files,
 * dropped in one by one, checked by the server, added together on Done.
 *
 * Opened by App, never by a click in here: either `lit add --si <paper>` is
 * waiting (the poll hands its request over) or the cockpit's Add SI made one.
 * Two states. First an "Open website" link to the article page — a plain new
 * tab, no server call, since the server may run on another machine than the
 * browser — and then, once the link was followed or something was dropped,
 * the drop area and the rows.
 *
 * Every drop uploads straight away, one file after the other in drop order
 * (the order is what numbers the files when no publisher number decides it).
 * Each row then shows the server's verdict: ✓ counts, ? only when "add
 * anyway" is ticked, ✕ never. Nothing reaches the library before Done, and
 * Cancel / Esc throws every upload away (the server deletes the stash).
 *
 * The whole backdrop takes drops, not just the drop area: a file let go a
 * little off target should still land here, never in the browser. The drag
 * events then bubble on to App's DropZone, which is parked while this window
 * is up (`accepting` false) and so only keeps the browser from opening the
 * file. */

// Over this a dropped file waits for a click before uploading (D13): the
// browser knows the size before sending a byte. Core's LARGE_FILE_BYTES.
const LARGE_FILE_BYTES = 100 * 1024 * 1024

const FOLDER_MESSAGE = "Folders can't be added — drop the files inside."

// For this long after the window popped on its own, Esc does not cancel it:
// the key may have been meant for whatever the person was doing a moment before.
const ESC_GRACE_MS = 1000

type RowState = 'queued' | 'uploading' | 'large' | 'done' | 'failed' | 'removing'

/** One line of the window. `fid` and `server` arrive with the upload's answer;
 * `file` is held only until then. */
interface LocalRow {
  key: string
  name: string
  bytes: number
  state: RowState
  file?: File
  fid?: string
  server?: SiRow
  error?: string
  /** Removed while its upload was on the wire: shown as removing, taken out
   * on the server once the upload answers (see `removeRow`). */
  discard?: boolean
}

const isOpenStatus = (s: SiSnapshot['status']) => s === 'pending' || s === 'confirming'

/** The rows after the server's word on them.
 *
 * A row the server holds updates in place (a 409 on Done re-checks rows); one
 * it no longer holds goes (another page took it out, or Done found its upload
 * swept). Rows only the server knows — another page's drops, or the ones that
 * were there when this window opened — join at the end, except while an
 * upload is on the wire: its row may already be on the server before the
 * answer names it here, and adding it then would show the file twice. */
function mergeRows(
  local: LocalRow[],
  server: SiRow[],
  removed: ReadonlySet<string>,
): LocalRow[] {
  const byId = new Map(server.map((s) => [s.id, s]))
  const out: LocalRow[] = []
  for (const row of local) {
    if (row.fid === undefined) {
      out.push(row)
      continue
    }
    const s = byId.get(row.fid)
    if (s) {
      const state = row.state === 'removing' ? 'removing' : 'done'
      out.push({ ...row, name: s.name, bytes: s.bytes, server: s, state })
    }
  }
  if (local.some((r) => r.state === 'uploading')) return out
  const known = new Set(out.map((r) => r.fid))
  for (const s of server) {
    if (known.has(s.id) || removed.has(s.id)) continue
    out.push({ key: s.id, name: s.name, bytes: s.bytes, state: 'done', fid: s.id, server: s })
  }
  return out
}

export default function AddSi({
  seed,
  autoPopped = false,
  refreshRef,
  onAdded,
  onEnded,
  onCancel,
  onGone,
}: {
  seed: SiRequestSeed
  /** Popped by the poll, not opened by a click: Esc gets a short grace. */
  autoPopped?: boolean
  /** App's poll calls this once a tick while the window is up. */
  refreshRef: React.RefObject<(() => void) | null>
  /** Done landed: App closes the window and says so. */
  onAdded: (snapshot: SiSnapshot) => void
  /** The request ended somewhere else (another page pressed Done or Cancel):
   * close without a word. */
  onEnded: (id: string) => void
  /** Cancel or Esc. App closes the window and tells the server. */
  onCancel: (id: string) => void
  /** The server no longer knows this request (the GUI restarted, it expired,
   * or the paper left the library) — App may replace this window. */
  onGone: (id: string) => void
}) {
  const rid = seed.id
  const link = seed.link
  const [phase, setPhase] = useState<'intro' | 'files'>('intro')
  const [rows, setRows] = useState<LocalRow[]>([])
  // "add anyway" ticks, by server row id. Only ? rows can carry one.
  const [ticked, setTicked] = useState<ReadonlySet<string>>(() => new Set())
  const [busy, setBusy] = useState(false)
  // Read by the handlers, not the render: Done followed by Esc in the same
  // task runs Esc's handler before any re-render, so the state alone would
  // still say "idle" and the Esc would cancel what Done is adding.
  const busyRef = useRef<'done' | 'cancel' | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [gone, setGone] = useState<string | null>(null)
  const goneRef = useRef(false)
  const [dragOver, setDragOver] = useState(false)
  const depth = useRef(0)
  const nextKey = useRef(0)
  // Server rows this window took out: a poll answering before the DELETE did
  // must not bring them back.
  const removed = useRef(new Set<string>())
  // Rows removed mid-upload (by key), until their upload answers.
  const discarding = useRef(new Set<string>())
  const uploads = useRef(new Map<string, AbortController>())
  // Row DELETEs on the wire. Done waits for them: it adds every ✓ the server
  // still holds, including one the person has just taken out.
  const [deleting, setDeleting] = useState(0)
  // Bumped by every change made here to the rows. A snapshot asked for before
  // the latest one is stale: it may lack a row that has just come back from
  // its upload, and merging it would drop that row from view while Done would
  // still add it.
  const rowsVersion = useRef(0)
  const touch = () => {
    rowsVersion.current += 1
  }
  // A Done whose outcome is not known yet (on the wire, or its answer lost).
  // An `added` seen meanwhile is this page's own Done landing, not another
  // page's, and gets the toast and the refresh.
  const settling = useRef(false)
  const alive = useRef(true)
  const openedAt = useRef(Date.now())

  // Before the card takes focus (effects run in order): where focus was, to
  // hand it back when the window goes.
  useEffect(() => {
    const before = document.activeElement
    return () => {
      if (before instanceof HTMLElement && before.isConnected) before.focus()
    }
  }, [])
  const cardFocus = useModalCardFocus()

  const markGone = (message: string) => {
    goneRef.current = true
    setGone(message)
    onGone(rid)
  }

  const applySnapshot = (snap: SiSnapshot, stale = false) => {
    if (snap.status === 'added' && settling.current) {
      onAdded(snap)
      return
    }
    if (!isOpenStatus(snap.status)) {
      onEnded(rid)
      return
    }
    if (snap.status === 'pending') settling.current = false
    if (stale) return
    setRows((prev) => mergeRows(prev, snap.rows, removed.current))
    // A tick stands only on a row still waiting for one.
    const unverified = new Set(
      snap.rows.filter((r) => r.verdict === 'unverified').map((r) => r.id),
    )
    setTicked((prev) => new Set([...prev].filter((fid) => unverified.has(fid))))
    if (snap.rows.length > 0) setPhase('files')
  }

  // The request as the server has it, and whether the rows changed here
  // since it was asked for (then only its status counts).
  const fetchSnapshot = (): Promise<[SiSnapshot, boolean]> => {
    const asked = rowsVersion.current
    return fetchSiRequest(rid).then((snap) => [snap, asked !== rowsVersion.current])
  }

  // On open: the rows the request already has (rejoined after a reload, or a
  // CLI rerun while this page was away).
  useEffect(() => {
    alive.current = true
    fetchSnapshot()
      .then(([snap, stale]) => {
        if (alive.current && !busyRef.current) applySnapshot(snap, stale)
      })
      .catch((err) => {
        if (alive.current && err instanceof ApiError && err.status === 404) {
          markGone(err.message)
        }
      })
    const inFlight = uploads.current
    return () => {
      alive.current = false
      for (const c of inFlight.values()) c.abort()
    }
    // Once per window; App remounts it (key = request id) for another request.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  // The poll's hook. Quiet on failure: the next tick asks again, and only a
  // 404 (the request is gone) changes what the window can do.
  const refresh = () => {
    if (goneRef.current || busyRef.current) return
    fetchSnapshot()
      .then(([snap, stale]) => {
        if (alive.current && !goneRef.current && !busyRef.current) applySnapshot(snap, stale)
      })
      .catch((err) => {
        if (!alive.current || busyRef.current) return
        if (err instanceof ApiError && err.status === 404) markGone(err.message)
      })
  }
  useEffect(() => {
    refreshRef.current = refresh
  })
  useEffect(
    () => () => {
      refreshRef.current = null
    },
    [refreshRef],
  )

  // One upload at a time, oldest drop first.
  useEffect(() => {
    if (gone || busy) return
    if (rows.some((r) => r.state === 'uploading')) return
    const next = rows.find((r) => r.state === 'queued')
    if (next) startUpload(next)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [rows, gone, busy])

  function startUpload(row: LocalRow) {
    const ctl = new AbortController()
    uploads.current.set(row.key, ctl)
    setRows((prev) =>
      prev.map((r) => (r.key === row.key ? { ...r, state: 'uploading' } : r)),
    )
    uploadSiFile(rid, row.file!, ctl.signal)
      .then(({ row: srow }) => {
        if (!alive.current) return
        touch()
        if (discarding.current.delete(row.key)) {
          deleteServerRow(row.key, srow)
          return
        }
        setRows((prev) =>
          prev.map((r) =>
            r.key === row.key
              ? { ...r, state: 'done', file: undefined, fid: srow.id, server: srow }
              : r,
          ),
        )
      })
      .catch((err) => {
        if (!alive.current || ctl.signal.aborted) return
        touch()
        if (discarding.current.delete(row.key)) {
          setRows((prev) => prev.filter((r) => r.key !== row.key))
          return
        }
        const message = err instanceof Error ? err.message : String(err)
        const lost = err instanceof ApiError && err.status === 404
        setRows((prev) =>
          prev.map((r) =>
            r.key === row.key
              ? { ...r, state: 'failed', file: undefined, error: lost ? undefined : message }
              : r,
          ),
        )
        if (lost) markGone(message)
      })
      .finally(() => {
        uploads.current.delete(row.key)
      })
  }

  function takeDrop(dt: DataTransfer) {
    if (goneRef.current || busyRef.current) return
    const items = Array.from(dt.items).filter((it) => it.kind === 'file')
    // getAsFile / webkitGetAsEntry only answer during the drop event itself.
    const dropped: Array<[File | null, FileSystemEntry | null]> =
      items.length > 0
        ? items.map((it) => [it.getAsFile(), it.webkitGetAsEntry?.() ?? null])
        : Array.from(dt.files).map((f) => [f, null])
    const picked: LocalRow[] = []
    for (const [file, entry] of dropped) {
      if (!file) continue
      const key = `drop-${nextKey.current++}`
      if (entry?.isDirectory) {
        picked.push({ key, name: file.name, bytes: 0, state: 'failed', error: FOLDER_MESSAGE })
      } else {
        picked.push({
          key,
          name: file.name,
          bytes: file.size,
          file,
          state: file.size > LARGE_FILE_BYTES ? 'large' : 'queued',
        })
      }
    }
    if (picked.length === 0) return
    setError(null)
    touch()
    setRows((prev) => [...prev, ...picked])
    setPhase('files')
  }

  // The row's ×. A row still uploading is not aborted: the server may have
  // added it already, and an abort would leave a ✓ there that nobody sees and
  // Done would add. It shows as removing and is taken out once its upload
  // answers.
  function removeRow(row: LocalRow) {
    touch()
    if (row.state === 'uploading') {
      discarding.current.add(row.key)
      setRows((prev) => prev.map((r) => (r.key === row.key ? { ...r, discard: true } : r)))
      return
    }
    if (row.fid === undefined || row.server === undefined) {
      setRows((prev) => prev.filter((r) => r.key !== row.key))
      return
    }
    deleteServerRow(row.key, row.server)
  }

  // Take a row out on the server. It stays, as "Removing…", until the DELETE
  // answers; if that fails it is back as it was, since the server still has it.
  function deleteServerRow(key: string, srow: SiRow) {
    const fid = srow.id
    removed.current.add(fid)
    setRows((prev) =>
      prev.map((r) =>
        r.key === key
          ? { ...r, state: 'removing', discard: undefined, file: undefined, fid, server: srow }
          : r,
      ),
    )
    setTicked((prev) => new Set([...prev].filter((t) => t !== fid)))
    setDeleting((n) => n + 1)
    removeSiRow(rid, fid)
      .then(() => {
        if (!alive.current) return
        touch()
        setRows((prev) => prev.filter((r) => r.key !== key))
      })
      .catch((err) => {
        if (!alive.current) return
        removed.current.delete(fid)
        touch()
        setRows((prev) => prev.map((r) => (r.key === key ? { ...r, state: 'done' } : r)))
        const message = err instanceof Error ? err.message : String(err)
        if (err instanceof ApiError && err.status === 404) markGone(message)
        else setError(message)
      })
      .finally(() => {
        if (alive.current) setDeleting((n) => n - 1)
      })
  }

  function uploadLarge(row: LocalRow) {
    touch()
    setRows((prev) => prev.map((r) => (r.key === row.key ? { ...r, state: 'queued' } : r)))
  }

  function toggleTick(fid: string) {
    setTicked((prev) => {
      const next = new Set(prev)
      if (!next.delete(fid)) next.add(fid)
      return next
    })
  }

  const addable = rows.filter((r) => r.state === 'done' && r.server)
  const okCount = addable.filter((r) => r.server!.verdict === 'ok').length
  const addAnyway = addable
    .filter((r) => r.server!.verdict === 'unverified' && ticked.has(r.fid!))
    .map((r) => r.fid!)
  // A large file waiting for its Upload click counts too: Done would leave it
  // behind without a word.
  const pending =
    deleting > 0 ||
    rows.some(
      (r) =>
        r.state === 'uploading' ||
        r.state === 'queued' ||
        r.state === 'large' ||
        r.state === 'removing',
    )
  const canDone = !busy && gone === null && !pending && okCount + addAnyway.length > 0

  async function done() {
    if (!canDone || busyRef.current) return
    busyRef.current = 'done'
    settling.current = true
    setBusy(true)
    setError(null)
    try {
      const snap = await confirmSi(rid, addAnyway)
      if (!alive.current) return
      if (snap.status === 'added') {
        onAdded(snap)
        return
      }
      settling.current = false
      applySnapshot(snap)
    } catch (err) {
      if (!alive.current) return
      // An answer from the server means this Done did not land. No answer (the
      // connection dropped) leaves it open: the next snapshot says.
      if (err instanceof ApiError) settling.current = false
      const message = err instanceof Error ? err.message : String(err)
      if (err instanceof ApiError && err.status === 404) {
        markGone(message)
      } else {
        setError(message)
        // A conflict brings the rows as the server now has them; any other
        // failure leaves them unknown, so ask.
        if (err instanceof SiApiError && err.request) applySnapshot(err.request)
        else
          void fetchSnapshot()
            .then(([snap, stale]) => {
              if (alive.current) applySnapshot(snap, stale)
            })
            .catch(() => {})
      }
    }
    busyRef.current = null
    setBusy(false)
  }

  function cancel() {
    if (busyRef.current) return
    busyRef.current = 'cancel'
    for (const c of uploads.current.values()) c.abort()
    onCancel(rid)
  }
  // Esc is Cancel, except in the first moment of a window that popped on its
  // own. It is consumed either way (the layer does that), so it never reaches
  // the host.
  function escape() {
    if (autoPopped && Date.now() - openedAt.current < ESC_GRACE_MS) return
    cancel()
  }
  // Held, not acting, while Done is on the wire: the files may be landing in
  // the library right now, and Esc must neither undo that nor leak to the host.
  useEscapeLayer(true, busy ? null : escape)

  const hasFiles = (dt: DataTransfer | null): boolean =>
    dt !== null && Array.from(dt.types).includes('Files')
  // A drag that ends without a drop on the backdrop (let go over a toast, Esc
  // mid-drag) never sends the matching dragleave.
  useEffect(() => {
    const reset = () => {
      depth.current = 0
      setDragOver(false)
    }
    window.addEventListener('dragend', reset)
    window.addEventListener('drop', reset)
    return () => {
      window.removeEventListener('dragend', reset)
      window.removeEventListener('drop', reset)
    }
  }, [])

  const intro = phase === 'intro'
  const pageLink = (className: string) =>
    link && (
      <a
        href={link}
        target="_blank"
        rel="noreferrer"
        onClick={() => setPhase('files')}
        className={className}
      >
        Open website ↗
      </a>
    )

  return createPortal(
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/30 backdrop-blur-sm"
      {...modalBackdropProps}
      onDragEnter={(e) => {
        if (!hasFiles(e.dataTransfer)) return
        e.preventDefault()
        depth.current += 1
        setDragOver(true)
      }}
      onDragOver={(e) => {
        if (!hasFiles(e.dataTransfer)) return
        e.preventDefault()
        e.dataTransfer.dropEffect = 'copy'
      }}
      onDragLeave={(e) => {
        if (!hasFiles(e.dataTransfer)) return
        depth.current = Math.max(0, depth.current - 1)
        if (depth.current === 0) setDragOver(false)
      }}
      onDrop={(e) => {
        if (!hasFiles(e.dataTransfer)) return
        e.preventDefault()
        depth.current = 0
        setDragOver(false)
        takeDrop(e.dataTransfer)
      }}
    >
      <div
        {...cardFocus}
        onClick={(e) => e.stopPropagation()}
        role="dialog"
        aria-label="Supplementary files"
        className={`flex max-h-[85vh] w-[28rem] max-w-[94vw] animate-grow-in flex-col rounded-2xl bg-white p-6 shadow-xl transition-shadow duration-300 ease-fluid focus:outline-none ${
          intro && dragOver ? 'ring-2 ring-accent-500' : 'ring-1 ring-stone-200'
        }`}
      >
        <div className="text-[11px] font-semibold uppercase tracking-wider text-accent-600">
          Supplementary files
        </div>
        <h2
          className="mt-0.5 truncate text-sm font-semibold text-stone-900"
          title={seed.title}
        >
          {seed.title}
        </h2>

        {intro ? (
          <div className="mt-4">
            {link ? (
              <>
                {pageLink(
                  'inline-flex rounded-lg px-2.5 py-1.5 text-xs font-medium text-accent-600 ring-1 ring-stone-200 transition duration-200 ease-fluid hover:bg-stone-50',
                )}
                <p className="mt-2 text-xs leading-5 text-stone-600">
                  Download the SI there, then drop the files here.
                </p>
              </>
            ) : (
              <p className="text-xs leading-5 text-stone-600">
                No link on record — drop the files you downloaded.
              </p>
            )}
          </div>
        ) : (
          <div className="mt-4 flex min-h-0 animate-grow-in flex-col">
            {/* The drag-in ingest overlay's card (ui/DropZone), sitting in
                this one: same rounding, hairline and two-line label, so the
                two drop targets read as one family. */}
            <div
              className={`rounded-2xl px-8 py-5 text-center transition duration-300 ease-fluid ${
                dragOver
                  ? 'bg-accent-50 ring-2 ring-accent-500'
                  : 'bg-white ring-1 ring-stone-200'
              }`}
            >
              <div className="text-sm font-semibold text-stone-900">Drop SI files here</div>
              <div className="mt-1 text-xs text-stone-600">
                Checked on drop, added when you press Done
              </div>
            </div>
            {rows.length > 0 && (
              <ul className="-mx-2 mt-3 min-h-0 space-y-0.5 overflow-y-auto">
                {rows.map((row) => (
                  <RowLine
                    key={row.key}
                    row={row}
                    ticked={row.fid !== undefined && ticked.has(row.fid)}
                    disabled={busy}
                    onTick={toggleTick}
                    onUpload={uploadLarge}
                    onRemove={removeRow}
                  />
                ))}
              </ul>
            )}
          </div>
        )}

        {(gone ?? error) && (
          <p className="mt-3 whitespace-pre-wrap text-xs leading-5 text-rose-600 dark:text-rose-400">
            {gone ?? error}
          </p>
        )}

        <div className="mt-4 flex items-center justify-between gap-3">
          <div className="min-w-0">
            {!intro &&
              pageLink(
                'text-xs font-medium text-accent-600 transition-colors hover:underline',
              )}
          </div>
          <div className="flex items-center gap-2">
            <button
              type="button"
              onClick={cancel}
              disabled={busy}
              className="rounded-lg px-3 py-1.5 text-sm font-medium text-stone-600 transition duration-200 ease-fluid hover:bg-stone-100 hover:text-stone-800 disabled:opacity-40 disabled:hover:bg-transparent"
            >
              Cancel
            </button>
            {!intro && (
              <button
                type="button"
                onClick={() => void done()}
                disabled={!canDone}
                className="rounded-lg bg-accent-500 px-3 py-1.5 text-sm font-medium text-white transition duration-200 ease-fluid hover:bg-accent-600 disabled:opacity-40 disabled:hover:bg-accent-500"
              >
                {busy ? 'Adding…' : 'Done'}
              </button>
            )}
          </div>
        </div>
      </div>
    </div>,
    document.body,
  )
}

const MARK_TONE = {
  ok: 'text-emerald-700 dark:text-emerald-400',
  unverified: 'text-amber-700 dark:text-amber-400',
  rejected: 'text-rose-600 dark:text-rose-400',
} as const

/** One row: mark, name, size or progress, ×; under it what needs saying —
 * the reason for a ? or ✕, the large-file question, a failure. A ✓ keeps its
 * reason on hover: it counts, and the line stays short. */
function RowLine({
  row,
  ticked,
  disabled,
  onTick,
  onUpload,
  onRemove,
}: {
  row: LocalRow
  ticked: boolean
  disabled: boolean
  onTick: (fid: string) => void
  onUpload: (row: LocalRow) => void
  onRemove: (row: LocalRow) => void
}) {
  const s = row.server
  // A failed upload was never checked: no mark, like a large file not sent yet.
  const verdict = row.state === 'failed' ? undefined : s?.verdict
  const mark = verdict === 'ok' ? '✓' : verdict === 'unverified' ? '?' : verdict ? '✕' : ''
  const removing = row.state === 'removing' || row.discard === true
  const status = removing
    ? 'Removing…'
    : row.state === 'queued'
      ? 'Waiting…'
      : row.state === 'uploading'
        ? 'Checking…'
        : s && s.verdict !== 'rejected'
          ? formatBytes(s.bytes)
          : null
  const okReason = s?.verdict === 'ok' ? s.reason : undefined

  let detail: React.ReactNode = null
  if (row.state === 'large') {
    detail = (
      <>
        <span className="min-w-0 flex-1">Large file ({formatBytes(row.bytes)}) — upload?</span>
        <button
          type="button"
          disabled={disabled}
          onClick={() => onUpload(row)}
          className="shrink-0 rounded-md px-2 py-0.5 text-[11px] font-medium text-accent-600 ring-1 ring-stone-200 transition duration-200 ease-fluid hover:bg-stone-50 disabled:opacity-40"
        >
          Upload
        </button>
      </>
    )
  } else if (row.state === 'failed') {
    detail = row.error ? <span className="min-w-0 flex-1">{row.error}</span> : null
  } else if (s?.verdict === 'unverified') {
    detail = (
      <>
        <span className="min-w-0 flex-1">{s.reason}</span>
        <label className="inline-flex shrink-0 cursor-pointer items-center gap-1 text-stone-700">
          <input
            type="checkbox"
            checked={ticked}
            disabled={disabled}
            onChange={() => onTick(s.id)}
            className="h-3 w-3 accent-accent-500"
          />
          add anyway
        </label>
      </>
    )
  } else if (s?.verdict === 'rejected') {
    detail = <span className="min-w-0 flex-1">{s.reason}</span>
  }

  return (
    <li className="animate-grow-in rounded-lg px-2 py-1.5" title={okReason}>
      <div className="flex items-center gap-2">
        <span
          aria-hidden
          className={`w-3 shrink-0 text-center text-xs font-semibold ${
            verdict ? MARK_TONE[verdict] : ''
          }`}
        >
          {mark}
        </span>
        {/* The name keeps its own tooltip (it may be cut short), so a ✓'s
            reason has to ride along there too. */}
        <span
          className="min-w-0 flex-1 truncate text-xs text-stone-800"
          title={okReason ? `${row.name}\n${okReason}` : row.name}
        >
          {row.name}
        </span>
        {status && (
          <span className="shrink-0 text-[11px] tabular-nums text-stone-600">{status}</span>
        )}
        <button
          type="button"
          aria-label={`Remove ${row.name}`}
          title="Remove"
          disabled={disabled || removing}
          onClick={() => onRemove(row)}
          className="grid h-5 w-5 shrink-0 place-items-center rounded-md text-sm leading-none text-stone-400 transition-colors hover:bg-stone-100 hover:text-rose-600 disabled:opacity-40"
        >
          ×
        </button>
      </div>
      {detail && (
        <div className="mt-0.5 flex items-center gap-2 pl-5 pr-7 text-[11px] leading-4 text-stone-600">
          {detail}
        </div>
      )}
    </li>
  )
}
