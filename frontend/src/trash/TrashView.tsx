import { useEffect, useMemo, useRef, useState } from 'react'
import { fetchTrashMeta, trashPdfUrl } from '../api'
import type { PaperMeta, TrashEntry } from '../types'
import PdfView from '../pdf/PdfView'
import MdView from '../md/MdView'
import Cockpit from '../cockpit/Cockpit'

/** A read-only tab inside the trash view: a trashed paper's PDF or one of its
 * markdown docs. Addressed by the trash `entryName` (all trash endpoints key off
 * it), not the paper id (the paper is not in papers/ anymore). */
interface TrashTab {
  key: string
  kind: 'pdf' | 'notes' | 'discussion'
  entryName: string
  paperId: string
  label: string
}

function tabLabel(paperId: string, kind: TrashTab['kind']): string {
  return kind === 'pdf' ? paperId : `${paperId} · ${kind}`
}

interface Props {
  entries: TrashEntry[]
  loading: boolean
  /** Active vault's name — shown in the banner so the per-vault scope of the
   * trash stays visible (the vault switcher is hidden in trash mode). */
  vaultName: string
  /** Leave trash mode (back to the library). */
  onExit: () => void
  /** Restore one entry — App runs the POST + refreshes; resolves so the row can
   * clear its busy state. The entryName of the entry currently restoring is in
   * `restoringEntry`. */
  onRestore: (entry: TrashEntry) => void
  /** The entryName of the entry whose restore is in flight (gates its button). */
  restoringEntry: string | null
}

/** Full-screen "trash library" mode (Phase 4.9, B8). A persistent warm-amber
 * banner makes it unmistakably distinct from the normal library; rows show
 * deleted_at / title / id with a Restore button; opening a row opens a read-only
 * tab (PDF / md) with a "Trash" badge and a read-only inspector + Restore. The
 * id/title filter is client-side (the search endpoint excludes .trash/, and the
 * cap-100 trash is small enough to filter in the browser). */
export default function TrashView({
  entries,
  loading,
  vaultName,
  onExit,
  onRestore,
  restoringEntry,
}: Props) {
  const [filter, setFilter] = useState('')
  const [selected, setSelected] = useState<string | null>(null)
  const [tabs, setTabs] = useState<TrashTab[]>([])
  const [activeTab, setActiveTab] = useState<string | null>(null)
  // The selected entry's full trashed metadata (read from the trash endpoint),
  // for the read-only inspector. Null while loading or none selected.
  const [meta, setMeta] = useState<PaperMeta | null>(null)
  const [metaLoading, setMetaLoading] = useState(false)
  const [cockpitCollapsed, setCockpitCollapsed] = useState(false)

  // An entry restored elsewhere (or the vault switched) drops out of `entries`;
  // close any of its open tabs + clear the selection so the view never points at
  // a gone entry.
  useEffect(() => {
    const live = new Set(entries.map((e) => e.entryName))
    setTabs((prev) => {
      const next = prev.filter((t) => live.has(t.entryName))
      if (next.length !== prev.length) {
        setActiveTab((cur) =>
          cur && next.some((t) => t.key === cur)
            ? cur
            : next.length
              ? next[next.length - 1].key
              : null,
        )
      }
      return next
    })
    if (selected && !live.has(selected)) {
      setSelected(null)
      setMeta(null)
    }
  }, [entries, selected])

  const filtered = useMemo(() => {
    const q = filter.trim().toLowerCase()
    if (!q) return entries
    return entries.filter(
      (e) =>
        e.paperId.toLowerCase().includes(q) ||
        (e.title ?? '').toLowerCase().includes(q),
    )
  }, [entries, filter])

  const selectEntry = (entry: TrashEntry) => {
    setSelected(entry.entryName)
    setMetaLoading(true)
    fetchTrashMeta(entry.entryName)
      .then((m) => {
        if (selectedStillCurrent(entry.entryName)) setMeta(m)
      })
      .catch(() => {
        if (selectedStillCurrent(entry.entryName)) setMeta(null)
      })
      .finally(() => {
        if (selectedStillCurrent(entry.entryName)) setMetaLoading(false)
      })
  }
  // The selection may move while the meta fetch is in flight — a ref mirror of
  // `selected` lets the .then drop a stale response (state would be stale).
  const selectedRef = useRef(selected)
  selectedRef.current = selected
  function selectedStillCurrent(entryName: string): boolean {
    return selectedRef.current === entryName
  }

  const openTab = (entry: TrashEntry, kind: TrashTab['kind']) => {
    const key = `${kind}:${entry.entryName}`
    setTabs((prev) =>
      prev.some((t) => t.key === key)
        ? prev
        : [
            ...prev,
            {
              key,
              kind,
              entryName: entry.entryName,
              paperId: entry.paperId,
              label: tabLabel(entry.paperId, kind),
            },
          ],
    )
    setActiveTab(key)
    selectEntry(entry)
  }

  const closeTab = (key: string) => {
    setTabs((prev) => {
      const next = prev.filter((t) => t.key !== key)
      setActiveTab((cur) =>
        cur === key ? (next.length ? next[next.length - 1].key : null) : cur,
      )
      return next
    })
  }

  const active = tabs.find((t) => t.key === activeTab) ?? null
  const selectedEntry = entries.find((e) => e.entryName === selected) ?? null

  return (
    // `min-w-0 flex-1` so this fills the App's flex row (the normal 3-pane each
    // sizes itself; this single wrapper would otherwise shrink to content width
    // and leave the rest of the row blank).
    <div className="flex h-full min-w-0 flex-1 flex-col">
      {/* Persistent warm banner — the deliberate amber exception to the cool-gray
          palette, pinned for dark mode so it stays readable. */}
      <div className="flex shrink-0 items-center gap-3 border-b border-amber-200 bg-amber-100 px-4 py-2 text-sm text-amber-800 dark:border-amber-800 dark:bg-amber-950/60 dark:text-amber-200">
        <button
          onClick={onExit}
          className="rounded-md px-2 py-1 text-xs font-medium text-amber-800 transition-colors hover:bg-amber-200 dark:text-amber-200 dark:hover:bg-amber-900/60"
        >
          ← Back to library
        </button>
        <span className="font-medium">
          {/* Cap is TRASH_MAX_ENTRIES = 100 (core/trash.py, ADR-011): hardcoded
              by design, no flag — safe to mirror as a literal. Past 100 the
              oldest entries are permanently evicted (ring eviction). */}
          🗑 Trash{vaultName ? ` · ${vaultName}` : ''} · {entries.length} / 100 ·
          restores back here (oldest auto-removed past 100)
        </span>
      </div>

      <div className="flex min-h-0 flex-1">
        {/* Left: filterable list of trashed entries. */}
        <div className="flex w-80 shrink-0 flex-col border-r border-stone-200 bg-stone-100">
          <div className="shrink-0 border-b border-stone-200 p-2.5">
            <input
              type="text"
              value={filter}
              onChange={(e) => setFilter(e.target.value)}
              placeholder="Filter by id or title…"
              aria-label="Filter trash"
              className="w-full rounded-md border border-stone-300 bg-white px-2 py-1 text-sm text-stone-800 shadow-sm focus:border-accent-500 focus:outline-none focus:ring-2 focus:ring-accent-500/25"
            />
          </div>
          <div className="min-h-0 flex-1 overflow-auto bg-white py-1">
            {loading && (
              <div className="p-3 text-sm text-stone-500">Loading…</div>
            )}
            {!loading && filtered.length === 0 && (
              <div className="p-3 text-sm text-stone-400">
                {entries.length === 0 ? 'Trash is empty.' : 'No match.'}
              </div>
            )}
            {filtered.map((e) => {
              const isSel = e.entryName === selected
              const busy = restoringEntry === e.entryName
              return (
                <div
                  key={e.entryName}
                  className={`mx-2 my-0.5 rounded-xl ring-1 transition-colors ${
                    isSel
                      ? 'bg-accent-50 ring-accent-200/70'
                      : 'ring-transparent hover:bg-stone-200/50'
                  }`}
                >
                  <button
                    onClick={() => selectEntry(e)}
                    title={e.title ?? e.paperId}
                    className="block w-full px-2.5 py-1.5 text-left"
                  >
                    <div className="text-[11px] text-stone-400">
                      {e.deletedAt}
                    </div>
                    <div className="truncate text-sm text-stone-800">
                      {e.title || e.paperId}
                    </div>
                    <div className="truncate font-mono text-xs text-stone-500">
                      {e.paperId}
                    </div>
                  </button>
                  <div className="flex items-center gap-1.5 px-2.5 pb-2 pt-0.5">
                    <button
                      onClick={() => openTab(e, 'pdf')}
                      className="rounded-lg bg-white px-2 py-0.5 text-xs text-stone-700 shadow-sm ring-1 ring-stone-200 transition-colors hover:text-accent-700 hover:ring-accent-300"
                    >
                      📄 PDF
                    </button>
                    <button
                      onClick={() => openTab(e, 'notes')}
                      className="rounded-lg bg-white px-2 py-0.5 text-xs text-stone-700 shadow-sm ring-1 ring-stone-200 transition-colors hover:text-accent-700 hover:ring-accent-300"
                    >
                      📝 notes
                    </button>
                    <button
                      onClick={() => onRestore(e)}
                      disabled={busy}
                      className="ml-auto rounded-lg bg-accent-500 px-2.5 py-0.5 text-xs font-medium text-white shadow-sm transition-colors hover:bg-accent-600 disabled:opacity-50"
                    >
                      {busy ? 'Restoring…' : '↩ Restore'}
                    </button>
                  </div>
                </div>
              )
            })}
          </div>
        </div>

        {/* Center: read-only tabs (PDF / md) with a "Trash" badge. */}
        <section className="flex min-w-0 flex-1 flex-col bg-white">
          <div className="flex items-stretch gap-1 overflow-x-auto border-b border-stone-200 bg-stone-100 px-2 pt-1.5">
            {tabs.length === 0 && (
              <div className="px-2 py-2 text-xs text-stone-400">
                Open a trashed paper from the list (read-only).
              </div>
            )}
            {tabs.map((t) => {
              const isActive = t.key === activeTab
              return (
                <div
                  key={t.key}
                  className={`group flex shrink-0 animate-grow-in items-center gap-2 rounded-t-lg border border-b-0 px-3 py-1.5 text-sm transition-colors ${
                    isActive
                      ? 'border-stone-200 bg-white text-stone-900'
                      : 'border-transparent text-stone-500 hover:bg-stone-200/70'
                  }`}
                >
                  <button
                    onClick={() => {
                      setActiveTab(t.key)
                      const ent = entries.find(
                        (e) => e.entryName === t.entryName,
                      )
                      if (ent) selectEntry(ent)
                    }}
                    className="flex max-w-56 items-center gap-1.5 truncate"
                  >
                    <span className="shrink-0 rounded bg-amber-100 px-1 py-px text-[10px] font-medium text-amber-700 dark:bg-amber-900/70 dark:text-amber-200">
                      Trash
                    </span>
                    <span className="truncate">{t.label}</span>
                  </button>
                  <button
                    onClick={() => closeTab(t.key)}
                    title="Close tab"
                    className={`rounded p-0.5 leading-none text-stone-400 transition-colors hover:bg-stone-300 hover:text-stone-700 ${
                      isActive ? '' : 'opacity-0 group-hover:opacity-100'
                    }`}
                  >
                    ×
                  </button>
                </div>
              )
            })}
          </div>

          <div className="min-h-0 flex-1">
            {active === null && (
              <div className="flex h-full items-center justify-center text-sm text-stone-400">
                Select a trashed paper to preview it (read-only).
              </div>
            )}
            {active && active.kind === 'pdf' && (
              <PdfView
                key={active.key}
                paperId={active.entryName}
                readOnly
                pdfSrc={trashPdfUrl(active.entryName)}
              />
            )}
            {active && active.kind !== 'pdf' && (
              <MdView
                key={active.key}
                paperId={active.entryName}
                doc={active.kind}
                tabKey={active.key}
                readOnly
                onOpenPaper={() => {}}
                onBeginEdit={() => {}}
                onDraftChange={() => {}}
                onEndEdit={() => {}}
              />
            )}
          </div>
        </section>

        {/* Right: read-only inspector + Restore for the selected entry. */}
        <Cockpit
          readOnly
          paper={meta}
          loading={metaLoading}
          collapsed={cockpitCollapsed}
          onToggle={() => setCockpitCollapsed((c) => !c)}
          onOpenPaper={() => {}}
          onRestore={selectedEntry ? () => onRestore(selectedEntry) : undefined}
          restoring={selectedEntry != null && restoringEntry === selectedEntry.entryName}
          orphanRepoCount={selectedEntry?.orphanRepoCount ?? 0}
        />
      </div>
    </div>
  )
}
