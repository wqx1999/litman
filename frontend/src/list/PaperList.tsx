import type { IndexPaper } from '../types'

interface Props {
  papers: IndexPaper[]
  loading: boolean
  selectedId: string | null
  onOpenPdf: (id: string) => void
  onOpenDoc: (id: string, doc: 'notes' | 'discussion') => void
}

export default function PaperList({
  papers,
  loading,
  selectedId,
  onOpenPdf,
  onOpenDoc,
}: Props) {
  return (
    <div className="flex w-80 shrink-0 flex-col overflow-auto border-r border-stone-200 bg-white">
      {loading && <div className="p-3 text-sm text-stone-500">Loading…</div>}
      {!loading && papers.length === 0 && (
        <div className="p-3 text-sm text-stone-400">No papers.</div>
      )}
      {papers.map((p) => (
        <div
          key={p.id}
          className={`border-b border-stone-100 px-3 py-2 ${
            p.id === selectedId ? 'bg-stone-100' : 'hover:bg-stone-50'
          }`}
        >
          <button
            onClick={() => onOpenPdf(p.id)}
            className="block w-full text-left"
          >
            <div className="truncate text-sm text-stone-800">
              {p.title || p.id}
            </div>
            <div className="mt-0.5 flex items-center gap-2 text-xs text-stone-500">
              <span className="font-mono">{p.id}</span>
              {p.year != null && <span>· {p.year}</span>}
              {p.status && <span>· {p.status}</span>}
            </div>
          </button>
          <div className="mt-1 flex gap-2 text-xs">
            <button
              onClick={() => onOpenDoc(p.id, 'notes')}
              className="text-stone-500 underline decoration-stone-300 hover:text-stone-800"
            >
              notes
            </button>
            <button
              onClick={() => onOpenDoc(p.id, 'discussion')}
              className="text-stone-500 underline decoration-stone-300 hover:text-stone-800"
            >
              discussion
            </button>
          </div>
        </div>
      ))}
    </div>
  )
}
