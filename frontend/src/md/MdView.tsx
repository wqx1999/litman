import { useEffect, useRef, useState } from 'react'
import { marked } from 'marked'
import { fetchDiscussion, fetchNotes } from '../api'

interface Props {
  paperId: string
  doc: 'notes' | 'discussion'
  /** Called when a [[paper-id]] wikilink is clicked. */
  onOpenPaper: (id: string) => void
  /** When set (the doc was opened from a search hit), scroll to and highlight
   * every occurrence of this query in the rendered markdown. */
  highlightQuery?: string
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
export default function MdView({ paperId, doc, onOpenPaper, highlightQuery }: Props) {
  const [html, setHtml] = useState<string>('')
  const [missing, setMissing] = useState(false)
  const contentRef = useRef<HTMLDivElement>(null)

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

  // After the markdown renders, mark every occurrence of the search query and
  // scroll the first into view (a search hit opened this doc). Runs again when
  // the query or rendered html changes; unwraps prior marks first so re-jumping
  // doesn't stack them.
  useEffect(() => {
    const root = contentRef.current
    if (!root) return
    root.querySelectorAll('mark[data-search]').forEach((m) => {
      const parent = m.parentNode
      if (!parent) return
      while (m.firstChild) parent.insertBefore(m.firstChild, m)
      parent.removeChild(m)
      parent.normalize()
    })
    const q = highlightQuery?.trim().toLowerCase()
    if (!q) return
    // Collect matching text nodes first (a full walk), then mutate — mutating
    // mid-walk would invalidate the TreeWalker.
    const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT)
    const matches: Text[] = []
    for (let n = walker.nextNode(); n; n = walker.nextNode()) {
      if ((n.nodeValue ?? '').toLowerCase().includes(q)) matches.push(n as Text)
    }
    let first: HTMLElement | null = null
    for (const node of matches) {
      const idx = (node.nodeValue ?? '').toLowerCase().indexOf(q)
      if (idx < 0) continue
      const range = document.createRange()
      range.setStart(node, idx)
      range.setEnd(node, idx + q.length)
      const mark = document.createElement('mark')
      mark.dataset.search = '1'
      mark.className = 'rounded-sm bg-amber-200 px-0.5 text-stone-900'
      try {
        range.surroundContents(mark)
      } catch {
        continue // match straddled element boundaries — skip it
      }
      if (!first) first = mark
    }
    first?.scrollIntoView({ block: 'center', behavior: 'smooth' })
  }, [html, highlightQuery])

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
          ref={contentRef}
          className="prose-litman mx-auto min-h-0 w-full max-w-3xl flex-1 overflow-auto p-8"
          onClick={handleClick}
          dangerouslySetInnerHTML={{ __html: html }}
        />
      )}
    </div>
  )
}
