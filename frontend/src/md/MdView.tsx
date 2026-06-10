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
    return `<a href="#" data-paper="${safe}" class="text-stone-700 underline decoration-stone-400">${safe}</a>`
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

  if (missing) {
    return (
      <div className="h-full overflow-auto bg-stone-50 p-6 text-sm text-stone-500">
        No {doc}.md for this paper yet.
      </div>
    )
  }

  return (
    <div
      className="prose-litman h-full overflow-auto bg-stone-50 p-6"
      onClick={handleClick}
      dangerouslySetInnerHTML={{ __html: html }}
    />
  )
}
