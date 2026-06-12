import type { Tab } from '../types'
import PdfView from '../pdf/PdfView'
import type { PdfHandle } from '../pdf/PdfView'
import MdView from '../md/MdView'
import type { MdDraft } from '../md/MdView'

interface Props {
  tabs: Tab[]
  activeKey: string | null
  onActivate: (key: string) => void
  onClose: (key: string) => void
  onOpenPaper: (id: string) => void
  /** Register/unregister a PDF tab's flush handle for the close-time prompt. */
  onRegisterPdf: (key: string, handle: PdfHandle | null) => void
  /** Surface a transient message (e.g. a failed md save) to the host. */
  onNotify: (message: string) => void
  /** A notes/discussion tab (by key) to scroll to + highlight a query in, set
   * when the doc was opened from a search hit. */
  mdJump: { key: string; query: string } | null
  /** The active md tab's lifted edit session (App-owned), or undefined when the
   * active tab is not an md tab or is not being edited. */
  mdDraft?: MdDraft
  /** Forwarded md edit-session callbacks (App owns the per-tab draft map). */
  onMdBeginEdit: (tabKey: string, seed: string) => void
  onMdDraftChange: (tabKey: string, draft: string) => void
  onMdEndEdit: (tabKey: string) => void
}

export default function TabArea({
  tabs,
  activeKey,
  onActivate,
  onClose,
  onOpenPaper,
  onRegisterPdf,
  onNotify,
  mdJump,
  mdDraft,
  onMdBeginEdit,
  onMdDraftChange,
  onMdEndEdit,
}: Props) {
  const active = tabs.find((t) => t.key === activeKey) ?? null

  return (
    <section className="flex min-w-0 flex-1 flex-col bg-white">
      <div className="flex items-stretch gap-1 overflow-x-auto border-b border-stone-200 bg-stone-100 px-2 pt-1.5">
        {tabs.length === 0 && (
          <div className="px-2 py-2 text-xs text-stone-400">
            Open a paper from the list.
          </div>
        )}
        {tabs.map((t) => {
          const isActive = t.key === activeKey
          return (
            <div
              key={t.key}
              className={`group flex shrink-0 animate-grow-in items-center gap-2 rounded-t-lg border border-b-0 px-3 py-1.5 text-sm transition-colors ${
                isActive
                  ? 'border-stone-200 bg-white text-stone-900'
                  : 'border-transparent text-stone-500 hover:bg-stone-200/70'
              }`}
            >
              <button onClick={() => onActivate(t.key)} className="max-w-48 truncate">
                {t.label}
              </button>
              <button
                onClick={() => onClose(t.key)}
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
            No document open.
          </div>
        )}
        {active && active.kind === 'pdf' && (
          <PdfView
            key={active.key}
            paperId={active.paperId}
            tabKey={active.key}
            onRegister={onRegisterPdf}
          />
        )}
        {active && active.kind !== 'pdf' && (
          <MdView
            key={active.key}
            paperId={active.paperId}
            doc={active.kind}
            tabKey={active.key}
            onOpenPaper={onOpenPaper}
            onNotify={onNotify}
            highlightQuery={
              mdJump && mdJump.key === active.key ? mdJump.query : undefined
            }
            draftEntry={mdDraft}
            onBeginEdit={onMdBeginEdit}
            onDraftChange={onMdDraftChange}
            onEndEdit={onMdEndEdit}
          />
        )}
      </div>
    </section>
  )
}
