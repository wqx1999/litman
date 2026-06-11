import { useEffect, useState } from 'react'
import { marked } from 'marked'
import { fetchDiscussion, fetchNotes } from '../api'

interface Props {
  paperId: string
  doc: 'notes' | 'discussion'
  /** Called when a [[paper-id]] wikilink is clicked. */
  onOpenPaper: (id: string) => void
}

// [[paper-id]] → a marker anchor we delegate-click below. Done on the raw
// markdown (before marked) so the link text renders normally; the data-paper
// attribute survives marked's HTML passthrough for our click handler.
const WIKILINK = /\[\[([^\]]+)\]\]/g

function wikilinksToAnchors(src: string): string {
  return src.replace(WIKILINK, (_m, id: string) => {
    const safe = id.trim()
    return `<a href="#" data-paper="${safe}" class="text-accent-600 no-underline hover:underline">${safe}</a>`
  })
}

/** Read-only markdown render (Phase 1 — editing/saving is Phase 3). */
export default function MdView({ paperId, doc, onOpenPaper }: Props) {
  const [html, setHtml] = useState<string>('')
  const [missing, setMissing] = useState(false)

  useEffect(() => {
    let cancelled = false
    const load = doc === 'notes' ? fetchNotes : fetchDiscussion
    load(paperId).then((text) => {
      if (cancelled) return
      if (text === null) {
        setMissing(true)
        setHtml('')
        return
      }
      setMissing(false)
      setHtml(marked.parse(wikilinksToAnchors(text)) as string)
    })
    return () => {
      cancelled = true
    }
  }, [paperId, doc])

  function handleClick(e: React.MouseEvent<HTMLDivElement>) {
    const target = e.target as HTMLElement
    const anchor = target.closest('a[data-paper]')
    if (anchor) {
      e.preventDefault()
      const id = anchor.getAttribute('data-paper')
      if (id) onOpenPaper(id)
    }
  }

  const header =
    doc === 'notes' ? (
      <span className="text-stone-700">📝 Notes</span>
    ) : (
      <span className="text-stone-700">💬 Discussion</span>
    )

  return (
    <div className="flex h-full flex-col bg-white">
      <div className="shrink-0 border-b border-stone-200 bg-stone-100 px-6 py-2 text-sm font-semibold">
        {header}
        <span className="ml-2 font-mono text-xs font-normal text-stone-500">
          {paperId}
        </span>
      </div>
      {missing ? (
        <div className="flex-1 overflow-auto p-6 text-sm text-stone-500">
          No {doc}.md for this paper yet.
        </div>
      ) : (
        <div
          className="prose-litman mx-auto min-h-0 w-full max-w-3xl flex-1 overflow-auto p-8"
          onClick={handleClick}
          dangerouslySetInnerHTML={{ __html: html }}
        />
      )}
    </div>
  )
}
