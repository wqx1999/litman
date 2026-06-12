import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { marked } from 'marked'
import DOMPurify from 'dompurify'
import { fetchDiscussion, fetchNotes, putDiscussion, putNotes } from '../api'

/** The edit session for one md tab, owned by App (keyed per tab) so it survives
 * this view's unmount on tab switch. Absent (undefined) = not editing. */
export interface MdDraft {
  draft: string
  /** The on-disk text when the edit began — App diffs against it for dirtiness
   * (the close prompt + page-unload guard), mirroring PdfHandle.isDirty(). */
  savedText: string
}

interface Props {
  paperId: string
  doc: 'notes' | 'discussion'
  /** This view's tab key, used to address its lifted edit session in App. */
  tabKey: string
  /** Called when a [[paper-id]] wikilink is clicked. The handler decides
   * whether the target exists (App holds the full paper list); a dangling
   * target shows a toast instead of opening a broken tab. */
  onOpenPaper: (id: string) => void
  /** When set (the doc was opened from a search hit), scroll to and highlight
   * every occurrence of this query in the rendered markdown. */
  highlightQuery?: string
  /** Transient message surfaced by the host (e.g. a failed save). */
  onNotify?: (message: string) => void
  /** The lifted edit session for this tab (App-owned), or undefined when not
   * editing. Controlled: a present value puts the view in edit mode. */
  draftEntry?: MdDraft
  /** Begin editing: App records {draft: seed, savedText: seed}. */
  onBeginEdit: (tabKey: string, seed: string) => void
  /** A keystroke in the textarea: App updates the lifted draft. */
  onDraftChange: (tabKey: string, draft: string) => void
  /** A successful save: the edit session ends; App drops the entry. */
  onEndEdit: (tabKey: string) => void
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

// DOMPurify drops unknown attributes by default, which would strip the
// data-paper hook that drives wikilink clicks (decision 5). Allow it back
// explicitly so sanitization keeps the wikilink graph intact while still
// neutralizing scripts / event handlers / javascript: URLs in authored md.
const PURIFY_CONFIG = { ADD_ATTR: ['data-paper'] }

function renderMarkdown(src: string): string {
  const raw = marked.parse(wikilinksToAnchors(src)) as string
  return DOMPurify.sanitize(raw, PURIFY_CONFIG)
}

/** Markdown view: render by default, double-click to edit, Cmd/Ctrl+S to save.
 *
 * The edit session (draft text) is LIFTED to App (keyed per tab) so switching
 * tabs mid-edit — which unmounts this view (TabArea renders one tab) — does not
 * lose the in-progress edit, and so App can warn on page-unload / prompt on a
 * dirty-tab close (mirroring the PDF tab's dirty-close protection). This view
 * still owns the on-disk `text` (the render source) and the in-flight `saving`
 * flag; `editing` is now derived from whether App holds a draft for this tab. */
export default function MdView({
  paperId,
  doc,
  tabKey,
  onOpenPaper,
  highlightQuery,
  onNotify,
  draftEntry,
  onBeginEdit,
  onDraftChange,
  onEndEdit,
}: Props) {
  // The current on-disk text (null = file absent / not loaded yet). The render
  // html is derived from it; edit mode seeds its textarea from it.
  const [text, setText] = useState<string | null>(null)
  const [loaded, setLoaded] = useState(false)
  const [saving, setSaving] = useState(false)
  const contentRef = useRef<HTMLDivElement>(null)
  const textareaRef = useRef<HTMLTextAreaElement>(null)

  // Edit mode + the draft now come from App (controlled): a present draftEntry
  // means this tab is mid-edit. The draft survives this view's unmount because
  // it lives in App, not here.
  const editing = draftEntry !== undefined
  const draft = draftEntry?.draft ?? ''

  const html = useMemo(
    () => (text === null ? '' : renderMarkdown(text)),
    [text],
  )

  useEffect(() => {
    let cancelled = false
    setLoaded(false)
    const load = doc === 'notes' ? fetchNotes : fetchDiscussion
    load(paperId).then((loadedText) => {
      if (cancelled) return
      setText(loadedText)
      setLoaded(true)
    })
    return () => {
      cancelled = true
    }
  }, [paperId, doc])

  // After the markdown renders, mark every occurrence of the search query and
  // scroll the first into view (a search hit opened this doc). Runs again when
  // the query or rendered html changes; unwraps prior marks first so re-jumping
  // doesn't stack them. Skipped while editing (the rendered div is unmounted).
  useEffect(() => {
    if (editing) return
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
  }, [html, highlightQuery, editing])

  function handleClick(e: React.MouseEvent<HTMLDivElement>) {
    const target = e.target as HTMLElement
    const anchor = target.closest('a[data-paper]')
    if (anchor) {
      e.preventDefault()
      const id = anchor.getAttribute('data-paper')
      if (id) onOpenPaper(id)
    }
  }

  const enterEdit = useCallback(() => {
    // notes.md / discussion.md are create-or-overwrite, so editing is allowed
    // even when the file is absent (text === null) — a first edit starts blank
    // and the save creates the file (lit add never scaffolds discussion.md).
    // Wait for the fetch to settle (loaded) so an existing file seeds the
    // textarea from its real content, not a transient null.
    if (!loaded) return
    onBeginEdit(tabKey, text ?? '')
  }, [loaded, text, onBeginEdit, tabKey])

  const save = useCallback(async () => {
    if (saving) return
    setSaving(true)
    const put = doc === 'notes' ? putNotes : putDiscussion
    try {
      await put(paperId, draft)
      // Re-fetch so the canonical on-disk text shows (notes gets the wikilink
      // reminder re-inserted server-side; reflecting that keeps a follow-up edit
      // from re-stripping it).
      const reload = doc === 'notes' ? fetchNotes : fetchDiscussion
      const fresh = await reload(paperId)
      setText(fresh ?? draft)
      // End the (App-owned) edit session — drops the draft so the tab is no
      // longer dirty and switch-back shows the rendered view.
      onEndEdit(tabKey)
    } catch {
      onNotify?.(`Couldn't save ${doc}.md — your edit is kept; try again.`)
    } finally {
      setSaving(false)
    }
  }, [doc, draft, paperId, saving, onNotify, onEndEdit, tabKey])

  const cancelEdit = useCallback(() => onEndEdit(tabKey), [onEndEdit, tabKey])

  // Cmd/Ctrl+S saves, Esc cancels — only while editing, and only when the
  // textarea has focus (it's the sole interactive element in edit mode).
  function onTextareaKeyDown(e: React.KeyboardEvent<HTMLTextAreaElement>) {
    if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === 's') {
      e.preventDefault()
      void save()
    } else if (e.key === 'Escape') {
      e.preventDefault()
      cancelEdit()
    }
  }

  // Focus the textarea when edit mode opens, cursor at the end.
  useEffect(() => {
    if (!editing) return
    const ta = textareaRef.current
    if (!ta) return
    ta.focus()
    ta.setSelectionRange(ta.value.length, ta.value.length)
  }, [editing])

  const missing = loaded && text === null

  const header =
    doc === 'notes' ? (
      <span className="text-stone-700">📝 Notes</span>
    ) : (
      <span className="text-stone-700">💬 Discussion</span>
    )

  return (
    <div className="flex h-full flex-col bg-white">
      <div className="flex shrink-0 items-center justify-between border-b border-stone-200 bg-stone-100 px-6 py-2 text-sm font-semibold">
        <div>
          {header}
          <span className="ml-2 font-mono text-xs font-normal text-stone-500">
            {paperId}
          </span>
        </div>
        {editing ? (
          <div className="flex items-center gap-2">
            <button
              onClick={cancelEdit}
              disabled={saving}
              className="rounded-lg px-2.5 py-1 text-xs font-normal text-stone-600 transition-colors hover:bg-stone-200 disabled:opacity-40"
            >
              Cancel
            </button>
            <button
              onClick={() => void save()}
              disabled={saving}
              className="rounded-lg bg-accent-500 px-2.5 py-1 text-xs font-medium text-white transition-colors hover:bg-accent-600 disabled:opacity-60"
            >
              {saving ? 'Saving…' : 'Save'}
            </button>
          </div>
        ) : (
          <span className="font-mono text-xs font-normal text-stone-400">
            {missing ? 'double-click to start writing' : 'double-click to edit'}
          </span>
        )}
      </div>
      {missing ? (
        <div
          className="flex-1 cursor-text overflow-auto p-8 text-sm text-stone-400"
          onDoubleClick={enterEdit}
        >
          No {doc}.md for this paper yet — double-click to start writing.
        </div>
      ) : editing ? (
        <textarea
          ref={textareaRef}
          value={draft}
          onChange={(e) => onDraftChange(tabKey, e.target.value)}
          onKeyDown={onTextareaKeyDown}
          spellCheck={false}
          className="min-h-0 w-full flex-1 resize-none bg-white p-8 font-mono text-sm leading-relaxed text-stone-800 outline-none"
        />
      ) : (
        <div
          ref={contentRef}
          className="prose-litman mx-auto min-h-0 w-full max-w-3xl flex-1 cursor-text overflow-auto p-8"
          onClick={handleClick}
          onDoubleClick={enterEdit}
          dangerouslySetInnerHTML={{ __html: html }}
        />
      )}
    </div>
  )
}
