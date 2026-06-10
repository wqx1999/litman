import type { Tab } from '../types'
import PdfView from '../pdf/PdfView'
import MdView from '../md/MdView'

interface Props {
  tabs: Tab[]
  activeKey: string | null
  onActivate: (key: string) => void
  onClose: (key: string) => void
  onOpenPaper: (id: string) => void
}

export default function TabArea({
  tabs,
  activeKey,
  onActivate,
  onClose,
  onOpenPaper,
}: Props) {
  const active = tabs.find((t) => t.key === activeKey) ?? null

  return (
    <section className="flex min-w-0 flex-1 flex-col">
      <div className="flex items-stretch gap-px overflow-x-auto border-b border-stone-200 bg-stone-100">
        {tabs.length === 0 && (
          <div className="px-3 py-2 text-xs text-stone-400">
            Open a paper from the list.
          </div>
        )}
        {tabs.map((t) => (
          <div
            key={t.key}
            className={`flex items-center gap-2 border-r border-stone-200 px-3 py-1.5 text-sm ${
              t.key === activeKey
                ? 'bg-white text-stone-900'
                : 'bg-stone-100 text-stone-600 hover:bg-stone-200'
            }`}
          >
            <button onClick={() => onActivate(t.key)} className="max-w-48 truncate">
              {t.label}
            </button>
            <button
              onClick={() => onClose(t.key)}
              title="Close tab"
              className="text-stone-400 hover:text-stone-700"
            >
              ×
            </button>
          </div>
        ))}
      </div>

      <div className="min-h-0 flex-1">
        {active === null && (
          <div className="flex h-full items-center justify-center text-sm text-stone-400">
            No document open.
          </div>
        )}
        {active && active.kind === 'pdf' && (
          <PdfView key={active.key} paperId={active.paperId} />
        )}
        {active && active.kind !== 'pdf' && (
          <MdView
            key={active.key}
            paperId={active.paperId}
            doc={active.kind}
            onOpenPaper={onOpenPaper}
          />
        )}
      </div>
    </section>
  )
}
