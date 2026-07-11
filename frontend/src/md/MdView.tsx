import {
  useCallback,
  useEffect,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
} from 'react'
import { marked } from 'marked'
import DOMPurify from 'dompurify'
import {
  fetchDiscussion,
  fetchNotes,
  fetchTrashDiscussion,
  fetchTrashNotes,
  putDiscussion,
  putNotes,
} from '../api'

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
  /** Read-only variant (trash view): render the md from the trash endpoints
   * (addressed by `paperId` carrying the trash entry_name) and never enter edit
   * mode (no double-click, no Cmd+S save). Edit callbacks are unused here. */
  readOnly?: boolean
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
  /** Bumped by the host on a resync-from-disk so this view re-reads its file
   * (e.g. a CLI/agent rewrote notes.md). Skipped while editing — the draft is
   * the source of truth then. */
  reloadToken?: number
  /** Begin editing: App records {draft: seed, savedText: seed}. */
  onBeginEdit: (tabKey: string, seed: string) => void
  /** A keystroke in the textarea: App updates the lifted draft. */
  onDraftChange: (tabKey: string, draft: string) => void
  /** A successful save: the edit session ends; App drops the entry. */
  onEndEdit: (tabKey: string) => void
  /** A successful save bumped this paper's notes/discussion mtime. App advances
   * its doc-mtime baseline so the next resync diff does NOT mislabel the user's
   * own GUI edit as an external "notes updated" change (D2). */
  onSaved?: (paperId: string, doc: 'notes' | 'discussion') => void
}

// Per-tab scroll position for the rendered markdown, kept for the session so a
// tab switch (which unmounts this view — TabArea mounts only the active tab)
// returns to where you were reading instead of the top. Keyed by tab key;
// memory-only (cleared on a full reload), mirroring the PDF view's viewPositions.
const mdScrollPositions = new Map<string, number>()

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
  readOnly = false,
  onOpenPaper,
  highlightQuery,
  onNotify,
  draftEntry,
  reloadToken = 0,
  onBeginEdit,
  onDraftChange,
  onEndEdit,
  onSaved,
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
  // it lives in App, not here. Read-only (trash) tabs never edit, so editing is
  // pinned false regardless of any stray draft.
  const editing = !readOnly && draftEntry !== undefined
  const draft = draftEntry?.draft ?? ''

  const html = useMemo(
    () => (text === null ? '' : renderMarkdown(text)),
    [text],
  )

  useEffect(() => {
    let cancelled = false
    setLoaded(false)
    // Read-only (trash) tabs address the file by the trash entry_name, which the
    // host passes as `paperId`; live tabs use the per-paper read endpoints.
    const load = readOnly
      ? doc === 'notes'
        ? fetchTrashNotes
        : fetchTrashDiscussion
      : doc === 'notes'
        ? fetchNotes
        : fetchDiscussion
    load(paperId).then((loadedText) => {
      if (cancelled) return
      setText(loadedText)
      setLoaded(true)
    })
    return () => {
      cancelled = true
    }
  }, [paperId, doc, readOnly])

  // Re-read the file when the host bumps reloadToken (a resync-from-disk: the CLI
  // or an agent rewrote this doc). The MOUNT run is skipped (firstReloadRef) — the
  // load effect above already fetched then — so a tab opened while reloadToken is
  // already > 0 (a focus resync happened earlier) does NOT double-fetch + double-
  // parse. Subsequent bumps refetch. Skipped while editing (the App-owned draft is
  // the source of truth) and in read-only/trash mode (a trashed file is immutable).
  // Only `text` is touched, never the draft, so an in-progress GUI edit is safe.
  const firstReloadRef = useRef(true)
  useEffect(() => {
    if (firstReloadRef.current) {
      firstReloadRef.current = false
      return
    }
    if (editing || readOnly) return
    let cancelled = false
    const load = doc === 'notes' ? fetchNotes : fetchDiscussion
    load(paperId).then((fresh) => {
      if (!cancelled) setText(fresh)
    })
    return () => {
      cancelled = true
    }
    // Only reloadToken drives this refetch; paperId/doc/editing/readOnly are read
    // for the current value but must not themselves re-trigger it (the mount
    // effect owns paperId/doc loads; entering edit must not reload).
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [reloadToken])

  // Restore this tab's saved scroll position once the rendered markdown is on
  // screen (a tab switch unmounts this view, so without this it reopens at the
  // top). useLayoutEffect so the offset is set before paint — no visible jump.
  // Once per mount (restoredRef). Skipped when a search opened this doc — the
  // highlight effect below owns the scroll then (jump to the first match).
  const restoredRef = useRef(false)
  useLayoutEffect(() => {
    if (restoredRef.current || editing || !loaded || text === null) return
    if (highlightQuery?.trim()) return
    const el = contentRef.current
    if (!el) return
    restoredRef.current = true
    const saved = tabKey ? mdScrollPositions.get(tabKey) : undefined
    if (saved) el.scrollTop = saved
  }, [loaded, text, editing, highlightQuery, tabKey])

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
    // Read-only (trash) tabs never edit.
    if (readOnly) return
    // notes.md / discussion.md are create-or-overwrite, so editing is allowed
    // even when the file is absent (text === null) — a first edit starts blank
    // and the save creates the file. `lit add` scaffolds both, so absence now
    // only means a paper older than the scaffold (health-check --fix backfills).
    // Wait for the fetch to settle (loaded) so an existing file seeds the
    // textarea from its real content, not a transient null.
    if (!loaded) return
    onBeginEdit(tabKey, text ?? '')
  }, [readOnly, loaded, text, onBeginEdit, tabKey])

  const save = useCallback(async () => {
    if (saving) return
    setSaving(true)
    const put = doc === 'notes' ? putNotes : putDiscussion
    try {
      await put(paperId, draft)
      // Suppress this GUI write from the next resync diff (D2): advance App's
      // doc-mtime baseline so the bumped file mtime is not read as an external
      // edit. Done before the reload so a slow reload can't race a resync.
      onSaved?.(paperId, doc)
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
  }, [doc, draft, paperId, saving, onNotify, onEndEdit, onSaved, tabKey])

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
        ) : readOnly ? (
          <span className="font-mono text-xs font-normal text-stone-400">
            read-only (trash)
          </span>
        ) : (
          <span className="font-mono text-xs font-normal text-stone-400">
            {missing ? 'double-click to start writing' : 'double-click to edit'}
          </span>
        )}
      </div>
      {/* `editing` MUST be tested before `missing`: entering edit on an absent
          file leaves `text` null (the draft lives in App, not `text`), so a
          `missing`-first chain would keep the placeholder mounted and the
          textarea would never appear — the Save/Cancel header would show
          (driven by `editing`) while the body stayed un-editable. */}
      {editing ? (
        <textarea
          ref={textareaRef}
          value={draft}
          onChange={(e) => onDraftChange(tabKey, e.target.value)}
          onKeyDown={onTextareaKeyDown}
          spellCheck={false}
          className="min-h-0 w-full flex-1 resize-none bg-white p-8 font-mono text-sm leading-relaxed text-stone-800 outline-none"
        />
      ) : missing ? (
        <div
          className={`flex-1 overflow-auto p-8 text-sm text-stone-400 ${
            readOnly ? '' : 'cursor-text'
          }`}
          onDoubleClick={enterEdit}
        >
          {readOnly
            ? `No ${doc}.md for this trashed paper.`
            : `No ${doc}.md for this paper yet — double-click to start writing.`}
        </div>
      ) : (
        <div
          ref={contentRef}
          className={`prose-litman mx-auto min-h-0 w-full max-w-3xl flex-1 overflow-auto p-8 ${
            readOnly ? '' : 'cursor-text'
          }`}
          onClick={handleClick}
          onDoubleClick={enterEdit}
          onScroll={(e) => {
            // Remember the reading position for this tab so a switch returns here.
            if (tabKey) mdScrollPositions.set(tabKey, e.currentTarget.scrollTop)
          }}
          dangerouslySetInnerHTML={{ __html: html }}
        />
      )}
    </div>
  )
}
