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

// External links leave for a NEW window so a click in notes can never navigate
// the app window away (the SPA is the application). Wikilink anchors
// (data-paper, href="#") stay in-app via the delegated click handler below.
// Registered once at module scope; the hook runs after attribute sanitization,
// so the attributes it adds survive (DOMPurify's own documented recipe).
DOMPurify.addHook('afterSanitizeAttributes', (node) => {
  if (node.tagName !== 'A' || node.hasAttribute('data-paper')) return
  const href = node.getAttribute('href') ?? ''
  if (/^https?:\/\//i.test(href)) {
    node.setAttribute('target', '_blank')
    node.setAttribute('rel', 'noopener noreferrer')
  }
})

function renderMarkdown(src: string): string {
  const raw = marked.parse(wikilinksToAnchors(src)) as string
  return DOMPurify.sanitize(raw, PURIFY_CONFIG)
}

/** Markdown view: render by default, double-click to edit, Cmd/Ctrl+S to save.
 *
 * An open edit session has two shapes — the textarea, and a preview that renders
 * the draft. Both keep the draft; the editor offers no way to throw it away.
 * Discarding is one path only: close the tab and answer "Don't save" in the
 * shared dialog, the same route a PDF tab's unsaved annotations take.
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
  // Load failed with a described server error (e.g. the file on disk is not
  // UTF-8). Kept separate from `missing`: the file EXISTS, so the tab must
  // explain instead of offering "start writing" (whose save would overwrite
  // the original).
  const [loadError, setLoadError] = useState<string | null>(null)
  const [saving, setSaving] = useState(false)
  // Previewing the draft: the edit session is still open, but the rendered view
  // is showing instead of the textarea. Only meaningful while editing — see
  // `previewingDraft` below, which is the guarded form used everywhere.
  const [previewing, setPreviewing] = useState(false)
  const contentRef = useRef<HTMLDivElement>(null)
  const textareaRef = useRef<HTMLTextAreaElement>(null)
  // [selectionStart, selectionEnd] parked while a preview is up; null otherwise.
  // A tuple, not a bare number: an array is always truthy, so a caret sitting at
  // offset 0 restores like any other. A `number | null` ref would read 0 as
  // "nothing stored" and silently jump to the end of the document instead.
  const caretRef = useRef<[number, number] | null>(null)
  // Where the preview was scrolled to. Deliberately NOT mdScrollPositions: that
  // map is the READING position, restored when the tab is next opened, and a
  // draft is a different document once it diverges from the file. Keeping them
  // apart is what lets a second Preview return to the passage being checked
  // without a draft scroll deciding where the tab reopens.
  const previewScrollRef = useRef(0)

  // Edit mode + the draft now come from App (controlled): a present draftEntry
  // means this tab is mid-edit. The draft survives this view's unmount because
  // it lives in App, not here. Read-only (trash) tabs never edit, so editing is
  // pinned false regardless of any stray draft.
  const editing = !readOnly && draftEntry !== undefined
  const draft = draftEntry?.draft ?? ''
  const savedText = draftEntry?.savedText ?? ''
  const previewingDraft = editing && previewing

  // The render source: the DRAFT while previewing it, the on-disk text otherwise.
  // Derived before the memo on purpose — outside preview this is `text` itself,
  // so a keystroke (which changes `draft`, not `text`) cannot re-run marked +
  // DOMPurify. Feeding `draft` straight into the memo deps would re-parse the
  // whole document on every character typed.
  const previewSrc = previewingDraft ? draft : text
  const html = useMemo(
    () => (previewSrc === null ? '' : renderMarkdown(previewSrc)),
    [previewSrc],
  )

  useEffect(() => {
    let cancelled = false
    setLoaded(false)
    setLoadError(null)
    // Read-only (trash) tabs address the file by the trash entry_name, which the
    // host passes as `paperId`; live tabs use the per-paper read endpoints.
    const load = readOnly
      ? doc === 'notes'
        ? fetchTrashNotes
        : fetchTrashDiscussion
      : doc === 'notes'
        ? fetchNotes
        : fetchDiscussion
    load(paperId)
      .then((loadedText) => {
        if (cancelled) return
        setText(loadedText)
        setLoaded(true)
      })
      .catch((err: unknown) => {
        if (cancelled) return
        // Deliberately NOT setLoaded(true): `loaded` gates enterEdit, and
        // editing a file the server could not read would overwrite it with a
        // blank draft on save. Unreadable ⇒ not editable until fixed on disk.
        setLoadError(err instanceof Error ? err.message : String(err))
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
    load(paperId)
      .then((fresh) => {
        if (cancelled) return
        setText(fresh)
        // A resync can also RECOVER a previously unreadable file (the user
        // fixed it on disk): clear the error and unlock the tab.
        setLoaded(true)
        setLoadError(null)
      })
      .catch((err: unknown) => {
        if (cancelled) return
        setLoadError(err instanceof Error ? err.message : String(err))
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

  // The preview's own scroll restore. The effect above cannot serve this: it is
  // once-per-mount and skipped while editing, whereas the preview mounts and
  // unmounts repeatedly inside one session (Edit ⇄ Preview), and without this
  // every trip back lands at the top of the document.
  useLayoutEffect(() => {
    if (!previewingDraft) return
    const el = contentRef.current
    if (!el) return
    el.scrollTop = previewScrollRef.current
  }, [previewingDraft])

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
    // Already mid-session (we are previewing the draft): just swap the preview
    // back for the textarea. Re-seeding here would be destructive — onBeginEdit
    // overwrites the draft with the on-disk text AND resets savedText, so the
    // edit would be lost and the tab would additionally be marked clean, which
    // silently disarms the close prompt.
    if (editing) {
      setPreviewing(false)
      return
    }
    // notes.md / discussion.md are create-or-overwrite, so editing is allowed
    // even when the file is absent (text === null) — a first edit starts blank
    // and the save creates the file. `lit add` scaffolds both, so absence now
    // only means a paper older than the scaffold (health-check --fix backfills).
    // Wait for the fetch to settle (loaded) so an existing file seeds the
    // textarea from its real content, not a transient null.
    if (!loaded) return
    onBeginEdit(tabKey, text ?? '')
  }, [readOnly, editing, loaded, text, onBeginEdit, tabKey])

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

  // Leave the textarea WITHOUT ending the edit session: the draft is kept and
  // rendered, so this is a preview, not a discard. Nothing in the editor throws
  // work away — the only route to that is closing the tab and answering "Don't
  // save" in the shared dialog.
  //
  // An untouched draft is the exception: previewing it would show exactly what
  // was already on screen, so the session just ends. That exit must stay free of
  // a write — saving an unchanged file would bump its mtime, register as an
  // external change on the next resync, and (for notes) re-insert the server's
  // wikilink reminder, all for an edit the user never made.
  const showPreview = useCallback(() => {
    if (draft === savedText) {
      onEndEdit(tabKey)
      return
    }
    // Where the caret was when the preview took the textarea away. Preview is
    // for "check how this section renders, then carry on writing", and coming
    // back to the end of a long document breaks exactly that loop.
    const ta = textareaRef.current
    caretRef.current = ta ? [ta.selectionStart, ta.selectionEnd] : null
    setPreviewing(true)
  }, [draft, savedText, onEndEdit, tabKey])

  // `previewing` only means anything inside a session. Clear it whenever one
  // ends by any route (a save here, or App dropping the draft), so the next
  // double-click opens the textarea instead of a stale preview — and drop the
  // caret and scroll with it, or they would be restored into the NEXT session's
  // document, which may be a different length entirely.
  useEffect(() => {
    if (!editing) {
      setPreviewing(false)
      caretRef.current = null
      previewScrollRef.current = 0
    }
  }, [editing])

  // Cmd/Ctrl+S saves the SESSION, not the textarea. Bound on the window (capture
  // phase, mirroring PdfView's ⌘S) rather than on the textarea's onKeyDown: the
  // preview has no textarea, and the cheat sheet promises Ctrl+S saves the
  // current tab whatever is on screen. Only the active tab is mounted, so this
  // and PdfView's binding are never live at the same time.
  //
  // Esc is deliberately NOT bound. It used to discard the whole draft — a
  // reflex key wired to an unrecoverable action. It now falls through to the
  // global dispatcher, whose PDF branch is guarded by `!editing`, so inside the
  // editor it does nothing beyond closing the cheat sheet / What's New.
  useEffect(() => {
    if (!editing) return
    const onKey = (e: KeyboardEvent) => {
      if ((e.ctrlKey || e.metaKey) && (e.key === 's' || e.key === 'S')) {
        e.preventDefault()
        e.stopPropagation()
        void save()
      }
    }
    window.addEventListener('keydown', onKey, { capture: true })
    return () => window.removeEventListener('keydown', onKey, { capture: true })
  }, [editing, save])

  // Focus the textarea when edit mode opens, cursor at the end — or back where
  // it was if a preview is what took it away. Also runs on the way back from a
  // preview, which remounts the textarea without `editing` ever changing:
  // without the preview dep the box would come back unfocused.
  useEffect(() => {
    if (!editing || previewingDraft) return
    const ta = textareaRef.current
    if (!ta) return
    const at = caretRef.current
    ta.focus()
    if (at) ta.setSelectionRange(at[0], at[1])
    else ta.setSelectionRange(ta.value.length, ta.value.length)
    caretRef.current = null
  }, [editing, previewingDraft])

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
          // Two shapes for one open session. Previewing looks just like the
          // ordinary rendered view, so it has to SAY the draft is unsaved —
          // otherwise the close prompt later comes as a surprise.
          <div className="flex items-center gap-2">
            {previewingDraft && draft !== savedText && (
              <span className="font-mono text-xs font-normal text-amber-600 dark:text-amber-400">
                Unsaved draft — previewing
              </span>
            )}
            <button
              onClick={previewingDraft ? enterEdit : showPreview}
              disabled={saving}
              className="rounded-lg px-2.5 py-1 text-xs font-normal text-stone-600 transition-colors hover:bg-stone-200 disabled:opacity-40"
            >
              {previewingDraft ? 'Edit' : 'Preview'}
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
            {loadError
              ? 'unavailable'
              : missing
                ? 'double-click to start writing'
                : 'double-click to edit'}
          </span>
        )}
      </div>
      {/* An open session MUST be tested before `missing`, in BOTH of its shapes:
          entering edit on an absent file leaves `text` null (the draft lives in
          App, not `text`), so a `missing`-first chain would keep the placeholder
          mounted while the header showed edit controls. The textarea half of
          that was the e8d39d6 fix; `!previewingDraft` on the `missing` branch
          below is the same bug for the preview half — without it, previewing the
          first draft of a paper that has no notes.md yet shows "No notes.md"
          instead of what was just written. */}
      {editing && !previewingDraft ? (
        <textarea
          ref={textareaRef}
          value={draft}
          onChange={(e) => onDraftChange(tabKey, e.target.value)}
          spellCheck={false}
          className="min-h-0 w-full flex-1 resize-none bg-white p-8 font-mono text-sm leading-relaxed text-stone-800 outline-none"
        />
      ) : loadError ? (
        // The file exists but the server could not serve it (non-UTF-8 or
        // unreadable on disk). No edit affordance on purpose: `loaded` stays
        // false, so a double-click cannot open a blank draft whose save would
        // overwrite the original.
        <div className="flex-1 overflow-auto p-8 text-sm leading-relaxed text-stone-500">
          <p className="font-medium text-stone-600">
            Couldn't load {doc}.md
          </p>
          <p className="mt-2 max-w-prose">{loadError}</p>
          <p className="mt-2 max-w-prose text-stone-400">
            Fix the file on disk, then reopen this tab — litman never
            overwrites it.
          </p>
        </div>
      ) : missing && !previewingDraft ? (
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
            const top = e.currentTarget.scrollTop
            // A preview scroll is a position in the DRAFT, so it goes to the
            // draft's own ref. Only a scroll of the file itself updates the
            // reading position this tab reopens at.
            if (previewingDraft) previewScrollRef.current = top
            else if (tabKey) mdScrollPositions.set(tabKey, top)
          }}
          dangerouslySetInnerHTML={{ __html: html }}
        />
      )}
    </div>
  )
}
