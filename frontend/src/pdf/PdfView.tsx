import { useCallback, useEffect, useLayoutEffect, useRef, useState } from 'react'
import * as pdfjsLib from 'pdfjs-dist'
import { AnnotationEditorType, AnnotationEditorParamsType } from 'pdfjs-dist'
import type { PDFDocumentProxy } from 'pdfjs-dist'
import { EventBus, PDFLinkService, PDFViewer } from 'pdfjs-dist/web/pdf_viewer.mjs'
import workerUrl from 'pdfjs-dist/build/pdf.worker.min.mjs?url'
// pdf_viewer ships the text / annotation / annotation-editor layer CSS; import
// it so vite bundles the rules the viewer's DOM relies on.
import 'pdfjs-dist/web/pdf_viewer.css'
// Must load AFTER pdf_viewer.css — hides pdf.js's broken floating editor
// toolbar + native comment chrome so our own React param bar is the only
// annotation chrome.
import './pdf-editor-overrides.css'
import { CommentManager } from './comment-manager'
import ParamSwatches, { type ParamType } from './ParamSwatches'
import { pdfUrl, putPdfAnnotations } from '../api'

// pdf.js needs its worker registered once, before any document is parsed. The
// `?url` import gives vite the hashed worker path under assets/ at build time.
pdfjsLib.GlobalWorkerOptions.workerSrc = workerUrl

// UIManagers whose `addCommands` we've wrapped for dirty-tracking (see captureUI).
// A WeakSet so a destroyed document's manager is GC'd without leaking here.
const wrappedManagers = new WeakSet<object>()

// Per-tab reading position (scroll offset + zoom), kept for the session so that
// switching away from a PDF tab and back returns to where you were reading
// instead of snapping to page 1 — TabArea mounts only the active tab, so every
// switch unmounts/reloads this view (scrollTop resets, scale resets to default).
// Keyed by the tab key (`pdf:<paperId>`); scrollTop is in scaled pixels, so the
// scale is stored alongside and re-applied before the offset is restored, or the
// offset would land at the wrong place. Memory-only (cleared on a full reload),
// mirroring the app's other session-only state (the activity log); also resumes
// the position when the same paper is reopened later in the session.
const viewPositions = new Map<string, { scrollTop: number; scale: number }>()

// The PDF-tool modes the keyboard shortcuts can switch to, mirroring EditMode
// below. Exposed on the handle so the global shortcut dispatcher (V/H/T/D/Esc)
// can drive the active PDF tab without reaching into its internals — it calls
// the same `selectMode` the toolbar buttons do (one tool-switch path, no second
// way to mutate editor state). 'none' is Cursor / Esc.
export type PdfEditMode = 'none' | ParamType

/** Handle the parent (App) uses to flush / inspect a mounted PDF tab when the
 *  user closes it, and to drive its annotation tool from the global keyboard
 *  shortcuts. Lets the close path show a Save / Don't-save / Cancel prompt
 *  instead of a silent write (invariant #16: annotations embed into paper.pdf). */
export interface PdfHandle {
  /** Are there annotation edits not yet embedded into the PDF? */
  isDirty(): boolean
  /** saveDocument() + PUT the bytes; resolves when paper.pdf is overwritten. */
  flush(): Promise<void>
  /** Drop pending edits so the unmount teardown does not save them. */
  discard(): void
  /** Switch the active annotation tool (Cursor/Highlight/Text/Draw) from the
   *  global keyboard shortcuts — routes to the same `selectMode` the toolbar
   *  buttons use, so "load the tool" stays one path. */
  setEditMode(mode: PdfEditMode): void
}

interface Props {
  paperId: string
  /** This view's tab key, used by the parent's close-time save registry. */
  tabKey?: string
  /** Register/unregister this view's flush handle with the parent. */
  onRegister?: (key: string, handle: PdfHandle | null) => void
  /** Read-only variant (trash view): render + zoom + read-only note hover, but
   * hide every write affordance (annotation tools, Save, the edit popover) and
   * never register a flush handle. A trashed PDF is immutable. */
  readOnly?: boolean
  /** Override the document URL (the trash view serves from /api/trash/.../pdf).
   * Defaults to the live paper's /api/paper/{id}/pdf. */
  pdfSrc?: string
}

const MIN_SCALE = 0.5
const MAX_SCALE = 3.0
const DEFAULT_SCALE = 1.3
const ZOOM_STEP = 0.2
const WHEEL_STEP = 0.1

// Clamp to [MIN, MAX] and snap to 0.1 so button/wheel zoom lands on tidy steps.
// The % input bypasses the snap so the field can hold any integer percentage.
const clampScale = (s: number) =>
  Math.min(MAX_SCALE, Math.max(MIN_SCALE, Math.round(s * 10) / 10))

// The three annotation editors this build exposes, plus a cursor/select mode.
// "Cursor" maps to pdf.js's NONE mode (the reading default, like Adobe / Zotero):
// the editor layer drops its pointer-events so native text selection (copy /
// paste) works on the textLayer beneath, and the annotation layer keeps its
// pointer-events so hyperlinks stay clickable (in pdf.mjs's updateMode the NONE
// branch is the only one that does BOTH; POPUP — the old Cursor — blocked text
// selection AND links). The cost — you cannot single-click an existing
// annotation to select it — is covered by pdf.js's built-in double-click-to-edit
// path: AnnotationElement._editOnDoubleClick dispatches `switchannotationeditormode`
// (wired below), so double-clicking a saved highlight / note / ink enters its
// editor and selects it, and the popover's recolour / delete / note still reach
// it. The other three enter the matching creation editor. Numeric values come
// from pdf.js's own enum (v5: NONE=0, FREETEXT=3, HIGHLIGHT=9, INK=15) — never
// hard-coded, so a pdf.js bump can't silently point a button at the wrong mode.
type EditMode = 'none' | ParamType
const EDITOR_TYPE: Record<EditMode, number> = {
  none: AnnotationEditorType.NONE,
  highlight: AnnotationEditorType.HIGHLIGHT,
  freetext: AnnotationEditorType.FREETEXT,
  ink: AnnotationEditorType.INK,
}
// Reverse of EDITOR_TYPE: maps pdf.js's numeric mode back to our EditMode union
// so the toolbar's active-tool highlight can re-sync when pdf.js changes the
// mode on its own (e.g. a double-click entering an editor).
const MODE_TO_EDIT: Record<number, EditMode> = {
  [AnnotationEditorType.NONE]: 'none',
  [AnnotationEditorType.HIGHLIGHT]: 'highlight',
  [AnnotationEditorType.FREETEXT]: 'freetext',
  [AnnotationEditorType.INK]: 'ink',
}
const EDIT_MODES: { mode: EditMode; label: string; title: string }[] = [
  { mode: 'none', label: 'Cursor', title: 'Select text / follow links (double-click an annotation to edit)' },
  { mode: 'highlight', label: 'Highlight', title: 'Highlight text' },
  { mode: 'freetext', label: 'Text', title: 'Add a text note' },
  { mode: 'ink', label: 'Draw', title: 'Freehand ink' },
]

// pdf.js's highlight editor is NOT self-sufficient: its UIManager.getNonHCMColorName()
// does `highlightColorNames.get(color)` with NO null guard, and that map is null
// unless the viewer is given a highlight palette. Omitting it makes EVERY highlight
// create — and every entry into highlight mode (which rebuilds existing highlights) —
// throw "Cannot read properties of null (reading 'get')", which silently corrupts the
// editor. The option is a comma-separated `name=#hex` STRING (pdf.js splits it
// internally). The hex values mirror HL_SWATCHES in ParamSwatches.tsx so our colour
// bar values resolve to a known name (used for the annotation's aria label).
const HIGHLIGHT_COLORS =
  'yellow=#FFFF98,green=#53FFBC,blue=#80EBFF,pink=#FFCBE6,red=#FF4F5F,' +
  'yellow_HCM=#FFFFCC,green_HCM=#53FFBC,blue_HCM=#80EBFF,pink_HCM=#F6B8FF,red_HCM=#C50043'

const PARAM = AnnotationEditorParamsType

// The slivers of pdf.js's editor we touch on the SELECTED annotation. `comment`
// is asymmetric (getter returns an object, setter takes a string / null), and
// `canAddComment` is true for highlight + ink, false for FreeText (a text
// annotation IS the note) — so pdf.js itself enforces "text can't carry a note".
interface SelectedEditor {
  canAddComment?: boolean
  get comment(): { text: string | null } | null
  set comment(value: string | null)
}
// Opaque handle for a pdf.js editor — we only hand it straight back to
// `setSelected`, never read its fields, so an unnamed object type is enough.
type AnnotationEditorHandle = object
// One undo/redo command pair handed to the UIManager (used for note edits and,
// internally by pdf.js, for every annotation mutation — see captureUI).
type EditorCommand = { cmd: () => void; undo: () => void; mustExec: boolean }
type EditorUIManager = {
  delete(): void
  commitOrRemove(): void
  addCommands(params: EditorCommand): void
  // Look up an editor instance by its DOM id (= editor.div.id, prefix
  // `pdfjs_internal_editor_`; pdf.mjs sets `div.setAttribute("id", this.id)`).
  // Used to read a hovered annotation's note for the read-only hover tooltip —
  // UIManager.getEditor(id) returns #allEditors.get(id).
  getEditor?(id: string): SelectedEditor | undefined
  // Ends the current freehand drawing session and returns the editor it
  // finalized (null if there was no open session or the stroke was empty).
  currentLayer?: {
    endDrawingSession(stop: boolean): AnnotationEditorHandle | null
  } | null
  firstSelectedEditor?: SelectedEditor | null
  setSelected?(editor: AnnotationEditorHandle): void
}

function TrashIcon() {
  // Inline SVG (not a CSS mask) so the glyph always renders regardless of which
  // image assets the pdf.js distribution ships.
  return (
    <svg viewBox="0 0 16 16" className="h-3.5 w-3.5" fill="currentColor" aria-hidden>
      <path d="M6 1h4a1 1 0 0 1 1 1v1h3v2H2V3h3V2a1 1 0 0 1 1-1zm-2.5 5h9l-.8 8.2a1 1 0 0 1-1 .8H5.3a1 1 0 0 1-1-.8L3.5 6z" />
    </svg>
  )
}

/** pdf.js render via the higher-level `pdf_viewer` (text + annotation +
 * annotation-editor layers).
 *
 * We drive pdf.js's `PDFViewer` and enable three built-in editors — Highlight /
 * FreeText / Ink — plus a Cursor (NONE) mode for reading: native text selection
 * (copy/paste) and clickable hyperlinks, with double-click-to-edit reaching
 * existing annotations. The editor's own floating toolbar + native
 * comment chrome are hidden (see pdf-editor-overrides.css); instead two surfaces
 * drive the editors through the documented `switchannotationeditorparams` event
 * (colour / size / thickness) and the UIManager (delete, note command):
 *   - the TOP toolbar shows the active creation tool's default params (set the pen
 *     before you draw), and
 *   - a floating popover, anchored next to the SELECTED annotation (Adobe-style),
 *     recolours / resizes it, edits its note inline, or deletes it.
 *
 * Notes (comments): Highlight + Ink can carry a note (edited inline in the popover
 * and embedded into the PDF by pdf.js's comment machinery via `editor.comment`);
 * FreeText cannot (canAddComment=false — the text IS the note). A CommentManager
 * is still wired (its mere presence stops FreeText auto-rendering its text as a
 * stray hover popup), but we never use its dialog — note capture is inline.
 *
 * Annotation persistence (invariant #16): edits embed into the PDF via
 * `doc.saveDocument()`. The user saves explicitly (Save button / ⌘-Ctrl+S) with
 * visible state; the parent also intercepts tab close to prompt Save / Don't save
 * (via the registered PdfHandle), and a best-effort flush runs on plain unmount
 * (tab switch) unless `discard()` suppressed it. `dirtyRef` (driven by the wrapped
 * `addCommands`, cleared on save) gates every flush so an untouched / already-saved
 * PDF is never re-written. */
export default function PdfView({
  paperId,
  tabKey,
  onRegister,
  readOnly = false,
  pdfSrc,
}: Props) {
  // The absolutely-positioned scroll container pdf_viewer attaches to.
  const containerRef = useRef<HTMLDivElement>(null)
  // The inner `.pdfViewer` div pdf_viewer fills with pages.
  const viewerElRef = useRef<HTMLDivElement>(null)
  // The relative+isolate wrapper the floating popover positions itself within.
  const pdfWrapRef = useRef<HTMLDivElement>(null)
  // The floating editor popover (positioned imperatively by the rAF loop).
  const popoverRef = useRef<HTMLDivElement>(null)
  // The read-only note tooltip shown while hovering a commented annotation
  // (positioned imperatively by a layout effect; pointer-events:none).
  const hoverNoteRef = useRef<HTMLDivElement>(null)
  const viewerRef = useRef<PDFViewer | null>(null)
  const eventBusRef = useRef<EventBus | null>(null)
  // The AnnotationEditorUIManager, captured from the editing-state event source
  // (it dispatches with `source: this`). Used for programmatic delete, note
  // commands, and finalizing pending editors before save.
  const uiManagerRef = useRef<EditorUIManager | null>(null)
  const docRef = useRef<PDFDocumentProxy | null>(null)
  // Captured for the unmount flush so the cleanup closure does not depend on a
  // possibly-stale prop.
  const paperIdRef = useRef(paperId)
  paperIdRef.current = paperId
  // Set true by the wrapped addCommands on any edit; cleared after a save lands.
  // The single source of truth for "has unsaved annotation edits".
  const dirtyRef = useRef(false)
  // Set by discard(): the unmount teardown must not save when the user chose
  // "Don't save" at the close prompt.
  const discardRef = useRef(false)
  // Re-entrancy guard so a note command's editing-state dispatch can't recurse
  // back into commitNote while it is mid-flight.
  const committingNoteRef = useRef(false)
  // The editor whose note the popover textarea is currently bound to. We commit
  // to THIS editor (not the live selection) so a note survives even if the click
  // that ends editing also clears the pdf.js selection.
  const lastNoteEditorRef = useRef<SelectedEditor | null>(null)
  // Stable indirections so the load effect's cleanup / event handlers can call
  // the latest commit callbacks without listing them as effect deps.
  const commitNoteRef = useRef<() => void>(() => {})
  const commitPendingRef = useRef<() => void>(() => {})
  // Guards saveNow against a double-fire (button click racing ⌘/Ctrl+S).
  const savingRef = useRef(false)
  // The id of the annotation the hover tooltip currently shows (null = hidden).
  // A ref so the high-frequency mouseover handler can short-circuit re-entry on
  // the same annotation without a state read.
  const hoverIdRef = useRef<string | null>(null)

  const [error, setError] = useState<string | null>(null)
  const [pageCount, setPageCount] = useState(0)
  const [scale, setScale] = useState(DEFAULT_SCALE)
  const [editMode, setEditMode] = useState<EditMode>('none')
  // Draft string while the user is typing in the % box; null = show live scale.
  const [pctDraft, setPctDraft] = useState<string | null>(null)
  // True when an annotation editor is selected — gates the floating popover.
  const [hasSelection, setHasSelection] = useState(false)
  // Type of the currently-selected annotation (inferred from the params pdf.js
  // broadcasts on selection). Drives the popover's controls; null when nothing
  // is selected.
  const [selectedType, setSelectedType] = useState<ParamType | null>(null)
  // True when there are unsaved annotation edits (drives the Save button).
  const [dirty, setDirty] = useState(false)
  // True while a save (saveDocument + PUT) is in flight (button → "Saving…").
  const [saving, setSaving] = useState(false)
  // Brief positive confirmation after an explicit save ("Saved ✓").
  const [savedFlash, setSavedFlash] = useState(false)
  // The selected highlight/ink's note text, edited inline in the popover.
  const [noteDraft, setNoteDraft] = useState('')
  // The hovered annotation's note (read-only tooltip). ax/ay/atop are the
  // annotation's left / bottom / top relative to the PDF wrapper; the layout
  // effect measures the tooltip and clamps it inside the wrapper.
  const [hoverNote, setHoverNote] = useState<{
    text: string
    ax: number
    ay: number
    atop: number
  } | null>(null)

  // Per-tool params shown in the contextual controls. Persist across papers so
  // the user's last colour/size sticks. Defaults match pdf.js's first-use feel.
  const [hlColor, setHlColor] = useState('#FFFF98')
  const [textColor, setTextColor] = useState('#1A1A1A')
  const [textSize, setTextSize] = useState(18)
  const [inkColor, setInkColor] = useState('#1A1A1A')
  const [inkWidth, setInkWidth] = useState(6)

  // zoomBy reads the latest scale without re-binding the wheel/key listeners on
  // every zoom; a ref mirrors the state for that.
  const scaleRef = useRef(scale)
  scaleRef.current = scale
  // Mirror of noteDraft so the commit callbacks (stable identity) read fresh text.
  const noteDraftRef = useRef(noteDraft)
  noteDraftRef.current = noteDraft
  // True while restoring a saved scroll offset on load: gates the scroll listener
  // so pdf_viewer's own load-time scroll-to-top doesn't overwrite the saved
  // position in viewPositions before we've reapplied it.
  const restoringRef = useRef(false)

  // Which annotation type the colour/size actions target: the selected editor in
  // selection mode (the popover is showing), else the active creation tool.
  const actionType: ParamType | null = hasSelection
    ? selectedType
    : editMode !== 'none'
      ? editMode
      : null
  // The creation tool's params show in the top toolbar only while a tool is
  // active AND nothing is selected (a selection hands editing to the popover).
  const toolType: ParamType | null =
    !hasSelection && editMode !== 'none' ? editMode : null
  const colorFor = (t: ParamType) =>
    t === 'highlight' ? hlColor : t === 'freetext' ? textColor : inkColor
  // Highlight + Ink can carry a note; FreeText cannot (it IS the note).
  const canNoteFor = (t: ParamType) => t === 'highlight' || t === 'ink'

  // Push a clamped scale to the live viewer and mirror it into state (so the %
  // box and button disabled-states track it). The single seam every zoom funnels
  // through.
  const applyScale = useCallback((next: number) => {
    const clamped = Math.min(MAX_SCALE, Math.max(MIN_SCALE, next))
    const viewer = viewerRef.current
    if (viewer) viewer.currentScale = clamped
    setScale(clamped)
  }, [])

  const zoomBy = useCallback(
    (delta: number) => applyScale(clampScale(scaleRef.current + delta)),
    [applyScale],
  )
  const resetZoom = useCallback(() => applyScale(DEFAULT_SCALE), [applyScale])

  // Commit a typed percentage from the toolbar input: parse, clamp, apply.
  const commitPct = useCallback(
    (raw: string) => {
      setPctDraft(null)
      const n = parseInt(raw, 10)
      if (Number.isFinite(n) && n > 0) applyScale(n / 100)
    },
    [applyScale],
  )

  // Set an annotation-editor parameter (colour / size / thickness). pdf.js v5
  // has no PDFViewer setter for this — the UIManager subscribes to this event
  // and routes to the selected editor, or to the type's default for new ones.
  const setParam = useCallback((type: number, value: unknown) => {
    eventBusRef.current?.dispatch('switchannotationeditorparams', {
      source: viewerRef.current,
      type,
      value,
    })
  }, [])

  const selectMode = useCallback(
    (mode: EditMode) => {
      const viewer = viewerRef.current
      if (!viewer) return
      viewer.annotationEditorMode = { mode: EDITOR_TYPE[mode] }
      setEditMode(mode)
      // Re-assert the toolbar's stored params so newly-created annotations match
      // the visible swatch/size rather than pdf.js's internal default.
      if (mode === 'highlight') {
        setParam(PARAM.HIGHLIGHT_COLOR, hlColor)
      } else if (mode === 'freetext') {
        setParam(PARAM.FREETEXT_COLOR, textColor)
        setParam(PARAM.FREETEXT_SIZE, textSize)
      } else if (mode === 'ink') {
        setParam(PARAM.INK_COLOR, inkColor)
        setParam(PARAM.INK_THICKNESS, inkWidth)
      }
    },
    [setParam, hlColor, textColor, textSize, inkColor, inkWidth],
  )

  // Apply a colour to the contextual target: the selected editor when the popover
  // is showing, else the active tool's default for the next annotation. pdf.js
  // routes the param to the selected editor automatically when one is selected;
  // we mirror the value into the matching swatch state so the bar reflects it.
  const pickColor = useCallback(
    (hex: string) => {
      if (actionType === 'highlight') {
        setHlColor(hex)
        setParam(PARAM.HIGHLIGHT_COLOR, hex)
      } else if (actionType === 'freetext') {
        setTextColor(hex)
        setParam(PARAM.FREETEXT_COLOR, hex)
      } else if (actionType === 'ink') {
        setInkColor(hex)
        setParam(PARAM.INK_COLOR, hex)
      }
    },
    [actionType, setParam],
  )

  const pickTextSize = useCallback(
    (v: number) => {
      setTextSize(v)
      setParam(PARAM.FREETEXT_SIZE, v)
    },
    [setParam],
  )
  const pickInkWidth = useCallback(
    (v: number) => {
      setInkWidth(v)
      setParam(PARAM.INK_THICKNESS, v)
    },
    [setParam],
  )

  const deleteSelected = useCallback(() => {
    uiManagerRef.current?.delete()
  }, [])

  // Embed the popover textarea's draft into the note of the editor it is bound to
  // (lastNoteEditorRef), through the UIManager command stack (gives undo/redo and,
  // via the wrapped addCommands, marks the doc dirty). A no-op when the text is
  // unchanged. Targets lastNoteEditorRef rather than the live selection so the
  // note still commits if the click that ended editing also cleared the selection.
  const commitNote = useCallback(() => {
    if (committingNoteRef.current) return
    const ui = uiManagerRef.current
    const ed = lastNoteEditorRef.current
    if (!ui || !ed || !ed.canAddComment) return
    const prev = ed.comment?.text ?? null
    const draft = noteDraftRef.current
    const next = draft.trim() === '' ? null : draft
    if (next === prev) return
    committingNoteRef.current = true
    try {
      ui.addCommands({
        cmd: () => {
          ed.comment = next
        },
        undo: () => {
          ed.comment = prev
        },
        mustExec: true,
      })
    } finally {
      committingNoteRef.current = false
    }
  }, [])
  commitNoteRef.current = commitNote

  // Finalize in-progress edits before a save: flush a pending note draft, commit
  // the active FreeText, and end any open Ink drawing session. Without this, an
  // editor still being edited at save time is not yet in the annotationStorage
  // that saveDocument() serializes, so its annotation silently fails to embed.
  const commitPending = useCallback(() => {
    const ui = uiManagerRef.current
    if (!ui) return
    try {
      commitNote()
      ui.commitOrRemove()
      ui.currentLayer?.endDrawingSession(false)
    } catch (err) {
      console.error('Failed to commit pending annotation:', err)
    }
  }, [commitNote])
  commitPendingRef.current = commitPending

  // Embed pending edits into the PDF and overwrite paper.pdf (invariant #16).
  // Stable callback (reads refs) so the registration effect does not churn. Used
  // by the close-prompt (parent awaits) and wrapped by saveNow for the toolbar.
  const flush = useCallback(async () => {
    commitPending()
    const doc = docRef.current
    const id = paperIdRef.current
    if (!dirtyRef.current || !doc) return
    const bytes = await doc.saveDocument()
    await putPdfAnnotations(id, bytes)
    // Clear ONLY after the write lands. If saveDocument()/PUT throws, dirtyRef
    // stays true so a later save retries instead of silently dropping the edit.
    dirtyRef.current = false
    setDirty(false)
  }, [commitPending])

  // Explicit user save (Save button / ⌘-Ctrl+S): flush with visible state +
  // a brief "Saved ✓" confirmation. savingRef guards a double-fire.
  const saveNow = useCallback(async () => {
    if (savingRef.current || !dirtyRef.current) return
    savingRef.current = true
    setSaving(true)
    try {
      await flush()
      setSavedFlash(true)
    } catch (err) {
      console.error('Failed to save annotations:', err)
    } finally {
      savingRef.current = false
      setSaving(false)
    }
  }, [flush])

  // Hide the read-only hover-note tooltip. Defined up here (not with the other
  // hover logic below) so the wheel-zoom effect can list it as a dep without a
  // temporal-dead-zone error on a later const.
  const clearHover = useCallback(() => {
    if (hoverIdRef.current !== null) {
      hoverIdRef.current = null
      setHoverNote(null)
    }
  }, [])

  // Clear the "Saved ✓" flash after a moment.
  useEffect(() => {
    if (!savedFlash) return
    const t = setTimeout(() => setSavedFlash(false), 1600)
    return () => clearTimeout(t)
  }, [savedFlash])

  // Register this view's flush handle with the parent so closing the tab can
  // prompt Save / Don't save instead of writing silently. Skipped in read-only
  // (trash) mode: there is no write path, so no flush handle to expose.
  useEffect(() => {
    if (readOnly || !onRegister || !tabKey) return
    onRegister(tabKey, {
      isDirty: () => dirtyRef.current,
      flush,
      discard: () => {
        discardRef.current = true
        dirtyRef.current = false
        setDirty(false)
      },
      // Drive the active tool from the global V/H/T/D/Esc shortcuts through the
      // exact toolbar path (no second way to set editor state). EditMode and
      // PdfEditMode are the same union, so the cast is a label, not a widening.
      setEditMode: (mode) => selectMode(mode),
    })
    return () => onRegister(tabKey, null)
  }, [tabKey, onRegister, flush, selectMode, readOnly])

  // Build the viewer and load the document when the paper changes.
  useEffect(() => {
    const container = containerRef.current
    const viewerEl = viewerElRef.current
    if (!container || !viewerEl) return

    // Restore this tab's last reading position (scroll + zoom) if we have one;
    // otherwise open at the default scale, top of page 1.
    const savedView = tabKey ? viewPositions.get(tabKey) : undefined
    const initialScale = savedView?.scale ?? DEFAULT_SCALE
    // Suppress scroll-position saves until the restore reapplies the offset, so
    // pdf_viewer's load-time scroll-to-top can't clobber the saved value.
    restoringRef.current = !!savedView

    setError(null)
    setPageCount(0)
    setScale(initialScale)
    setEditMode('none')
    setHasSelection(false)
    setSelectedType(null)
    setNoteDraft('')
    setDirty(false)
    setSaving(false)
    setSavedFlash(false)
    dirtyRef.current = false
    discardRef.current = false
    lastNoteEditorRef.current = null

    const eventBus = new EventBus()
    eventBusRef.current = eventBus
    const linkService = new PDFLinkService({ eventBus })
    // Supplies pdf.js's comment contract; its presence switches FreeText off the
    // legacy "render my text as a hover popup" path. We capture notes inline, so
    // its dialog is never invoked (openDialog returns undefined).
    const commentManager = new CommentManager({
      openDialog: () => Promise.resolve(undefined),
    })
    const viewer = new PDFViewer({
      container,
      viewer: viewerEl,
      eventBus,
      linkService,
      // NONE is our Cursor (reading) default: native text selection + clickable
      // hyperlinks. The Highlight / Text / Draw toolbar buttons switch into the
      // creation editors; double-clicking a saved annotation switches into its
      // editor via the switchannotationeditormode listener wired below.
      annotationEditorMode: AnnotationEditorType.NONE,
      // Required for the highlight editor — see HIGHLIGHT_COLORS above.
      annotationEditorHighlightColors: HIGHLIGHT_COLORS,
      // Not in pdfjs-dist's PDFViewerOptions type (it's a full-viewer-only
      // option), so widen the type to attach it.
      commentManager,
    } as ConstructorParameters<typeof PDFViewer>[0] & {
      commentManager: CommentManager
    })
    linkService.setViewer(viewer)
    viewerRef.current = viewer

    // Capture the UIManager and, once per manager, wrap `addCommands` so EVERY
    // annotation mutation (create / move / recolour / delete / note) flips dirty.
    // Selection changes do NOT go through addCommands, so dirty stays put when the
    // user merely clicks around — and stays clean after a save even though pdf.js
    // keeps the (still-undoable) command on its stack. This is the only reliable
    // "edited since last save" signal pdf.js exposes.
    const captureUI = (ui?: EditorUIManager) => {
      if (!ui) return
      uiManagerRef.current = ui
      if (!wrappedManagers.has(ui)) {
        wrappedManagers.add(ui)
        const orig = ui.addCommands.bind(ui)
        ui.addCommands = (params: EditorCommand) => {
          dirtyRef.current = true
          setDirty(true)
          return orig(params)
        }
      }
    }

    // Load the selected editor's note into the popover textarea when the selection
    // moves to a different editor (or clears). Flushes the PREVIOUS editor's draft
    // first so switching/deselecting never drops an in-progress note.
    const syncNoteDraft = (ui?: EditorUIManager) => {
      const ed = ui?.firstSelectedEditor ?? null
      if (ed === lastNoteEditorRef.current) return
      commitNoteRef.current()
      lastNoteEditorRef.current = ed
      setNoteDraft(ed?.comment?.text ?? '')
    }

    // Restore the saved scroll offset (and scale). Event-driven, NOT polled:
    // reading scrollHeight every frame forces a synchronous reflow, and doing
    // that during pdf.js's heaviest initial layout/render contends for the main
    // thread and visibly slows the open. Instead we set the offset at two fixed
    // points, each O(1):
    //   - pagesinit: page geometry is established, so set scale then an early
    //     offset set (one rAF) — usually enough and avoids a visible top→pos jump.
    //   - pagesloaded: all pages are laid out (scroll range final), so re-set in
    //     case the early attempt clamped, plus one rAF re-assert in case
    //     pdf_viewer's own page-1 scroll lands just after us. Scroll-saving
    //     reopens (restoringRef) only after that final reapply.
    const applyOffset = (last: boolean) => {
      if (cancelled || !savedView) return
      container.scrollTop = savedView.scrollTop
      requestAnimationFrame(() => {
        if (cancelled) return
        container.scrollTop = savedView.scrollTop
        if (last) restoringRef.current = false
      })
    }
    const onPagesInit = () => {
      viewer.currentScale = initialScale
      requestAnimationFrame(() => applyOffset(false))
    }
    const onPagesLoaded = () => {
      if (savedView) applyOffset(true)
      else restoringRef.current = false
    }
    eventBus.on('pagesinit', onPagesInit)
    eventBus.on('pagesloaded', onPagesLoaded)

    // pdf.js's embedded PDFViewer does NOT itself act on `switchannotationeditormode`
    // (only the full viewer.html app wires that). We wire it so pdf.js's built-in
    // double-click-to-edit works: AnnotationElement._editOnDoubleClick dispatches
    // this event with the saved annotation's editor type + id, and we route it to
    // the viewer so it enters that editor and selects the annotation (the popover
    // then drives recolour / delete / note). Our own toolbar sets the mode
    // directly (selectMode), so this listener only matters for the dblclick path.
    //
    // Read-only (trash) mode SKIPS this: no write path may be reachable, so a
    // double-click must never enter an editor (red line ④). The viewer stays in
    // NONE; annotations still render (baked into paper.pdf) and stay selectable
    // as text / followable as links, just not editable.
    const onSwitchMode = (e: {
      mode?: number
      editId?: string | null
      mustEnterInEditMode?: boolean
    }) => {
      if (e.mode == null) return
      try {
        viewer.annotationEditorMode = {
          mode: e.mode,
          editId: e.editId ?? null,
          mustEnterInEditMode: e.mustEnterInEditMode ?? false,
        }
      } catch (err) {
        console.error('Failed to switch annotation editor mode:', err)
      }
    }
    if (!readOnly) {
      eventBus.on('switchannotationeditormode', onSwitchMode)
    }

    // Keep the React toolbar's active-tool highlight in sync when pdf.js changes
    // the mode on its own (e.g. a double-click entering an editor). selectMode
    // already sets editMode for toolbar-driven switches, so this is idempotent
    // there; an unknown mode (shouldn't happen) is ignored.
    const onModeChanged = (e: { mode?: number }) => {
      const mode = MODE_TO_EDIT[e.mode ?? -1]
      if (mode) setEditMode(mode)
    }
    if (!readOnly) {
      eventBus.on('annotationeditormodechanged', onModeChanged)
    }

    // editingstateschanged fires with the full editor state every time. Capture
    // the UIManager (= event source), track whether an editor is selected (gates
    // the popover), and sync the inline note. Dirty is NOT derived here — it is
    // owned by the wrapped addCommands (see captureUI).
    const onEditingStates = (e: {
      source?: EditorUIManager
      details?: { hasSelectedEditor?: boolean }
    }) => {
      captureUI(e.source)
      const selected = !!e.details?.hasSelectedEditor
      setHasSelection(selected)
      // Selection cleared → drop the inferred type so the popover hides.
      if (!selected) setSelectedType(null)
      syncNoteDraft(e.source)
    }
    eventBus.on('editingstateschanged', onEditingStates)

    // pdf.js broadcasts the active param values (e.g. when a different editor is
    // selected, or on mode entry) as [type, value] pairs; mirror them so the
    // contextual controls' highlighted swatch/size track the selected annotation.
    const onParamsChanged = (e: {
      source?: EditorUIManager
      details?: [number, unknown][]
    }) => {
      const details = e.details ?? []
      // annotationeditorparamschanged fires on every setSelected (the params
      // dispatch precedes the editing-state dispatch). Sync the note here too:
      // this is the path that catches a DIRECT annotation→annotation reselect,
      // where hasSelectedEditor stays true→true so editingstateschanged does NOT
      // re-fire.
      captureUI(e.source)
      syncNoteDraft(e.source)
      // Infer the selected annotation's type from which param kinds pdf.js
      // broadcasts (each editor exposes a distinct set). Drives the popover's
      // controls; ignored in tool modes (toolType uses editMode there).
      const types = details.map(([t]) => t)
      if (types.includes(PARAM.HIGHLIGHT_COLOR)) {
        setSelectedType('highlight')
      } else if (
        types.includes(PARAM.FREETEXT_COLOR) ||
        types.includes(PARAM.FREETEXT_SIZE)
      ) {
        setSelectedType('freetext')
      } else if (
        types.includes(PARAM.INK_COLOR) ||
        types.includes(PARAM.INK_THICKNESS)
      ) {
        setSelectedType('ink')
      }
      for (const [type, value] of details) {
        if (typeof value !== 'string' && typeof value !== 'number') continue
        switch (type) {
          case PARAM.HIGHLIGHT_COLOR:
            setHlColor(String(value))
            break
          case PARAM.FREETEXT_COLOR:
            setTextColor(String(value))
            break
          case PARAM.FREETEXT_SIZE:
            setTextSize(Number(value))
            break
          case PARAM.INK_COLOR:
            setInkColor(String(value))
            break
          case PARAM.INK_THICKNESS:
            setInkWidth(Number(value))
            break
        }
      }
    }
    eventBus.on('annotationeditorparamschanged', onParamsChanged)

    let cancelled = false
    const loadingTask = pdfjsLib.getDocument({
      url: pdfSrc ?? pdfUrl(paperId),
      // Scanned (image-only) PDFs decode their JBIG2 / CCITT-fax / JPEG2000
      // images in a WebAssembly module pdf.js fetches from this directory;
      // build.sh vendors pdf.js's wasm/ folder to the served root. Without it a
      // scanned page fetches <null>/jbig2.wasm, the image decodes to nothing,
      // and the page paints blank ("Dependent image isn't ready yet").
      wasmUrl: new URL('wasm/', document.baseURI).href,
    })
    loadingTask.promise
      .then((doc) => {
        if (cancelled) {
          void doc.destroy()
          return
        }
        docRef.current = doc
        setPageCount(doc.numPages)
        viewer.setDocument(doc)
        linkService.setDocument(doc)
      })
      .catch((err: unknown) => {
        if (!cancelled) {
          setError(err instanceof Error ? err.message : String(err))
        }
      })

    return () => {
      cancelled = true
      // Remember where the user was reading so a tab switch (which unmounts this
      // view) returns here instead of page 1. containerRef is still live during
      // cleanup; scaleRef holds the latest zoom. Saved together so the scaled
      // scrollTop restores against matching geometry. Skipped if a restore is
      // still in flight (switched away before it landed) so we don't overwrite
      // the saved offset with the transient load-time top.
      if (tabKey && containerRef.current && !restoringRef.current) {
        viewPositions.set(tabKey, {
          scrollTop: containerRef.current.scrollTop,
          scale: scaleRef.current,
        })
      }
      eventBus.off('pagesinit', onPagesInit)
      eventBus.off('pagesloaded', onPagesLoaded)
      eventBus.off('switchannotationeditormode', onSwitchMode)
      eventBus.off('annotationeditormodechanged', onModeChanged)
      eventBus.off('editingstateschanged', onEditingStates)
      eventBus.off('annotationeditorparamschanged', onParamsChanged)

      // Finalize any in-progress editor (and flush a pending inline note) so the
      // flush below embeds it. Events are already detached, so the resulting
      // state dispatch won't setState on the unmounting view; the wrapped
      // addCommands still flips dirtyRef (a ref) so the save below picks it up.
      commitPendingRef.current()
      // Capture the UIManager before the ref is nulled — teardown destroys it to
      // drop its leaked document-level listeners (see teardown comment).
      const uiManager = uiManagerRef.current
      eventBusRef.current = null
      uiManagerRef.current = null

      const doc = docRef.current
      const id = paperIdRef.current
      docRef.current = null
      viewerRef.current = null

      // Teardown destroys the worker transport. `doc.destroy()` IS
      // `loadingTask.destroy()` (the proxy just delegates to it), so one call
      // tears down the document, the loading task, and the worker. saveDocument()
      // runs ON that worker, so when flushing we must defer teardown until the
      // save resolves — an eager `loadingTask.destroy()` here was killing the
      // worker mid-save, rejecting saveDocument(), and silently dropping the
      // write (the "edits don't persist after reopen" bug).
      const teardown = () => {
        // Destroy the AnnotationEditorUIManager so its document-level listeners
        // (selectionchange, focus/blur, keyboard) are removed. We build a fresh
        // PDFViewer every mount but never tore the old one down, so each tab-reopen
        // leaked a UIManager: the stale one kept listening on `document` and, on the
        // next view's text selection, ran FIRST — reaching `selection.empty()` then
        // no-opping (its layers were gone) — so it emptied the selection before the
        // live manager could turn it into a highlight. Text highlighting thus died
        // after a save+reopen until a full page reload (which drops all document
        // listeners) brought it back; free-draw stayed fine (instance-local
        // textLayer pointerdown, not the global selectionchange path).
        // `viewer.setDocument(null)` throws in this pdf.js build, so call the
        // manager's own `destroy()` — what pdf.js's setDocument does internally:
        // aborts its AbortController, removing every `_signal`-bound listener. Safe
        // after saveDocument() (the dirty branch runs teardown in `.finally`) — it
        // tears down editor state, not the doc's already-serialized
        // annotationStorage.
        try {
          ;(uiManager as unknown as { destroy?: () => void } | null)?.destroy?.()
        } catch (err) {
          console.error('Failed to destroy PDF editor UIManager:', err)
        }
        if (doc) void doc.destroy()
        else void loadingTask.destroy()
      }

      // Best-effort flush on plain unmount (tab switch). When the user closed the
      // tab the parent already ran flush()/discard() before removing it, so dirty
      // is false here (or discard suppresses the save) and this is a no-op
      // teardown. The deliberate save path is the explicit Save button / prompt.
      //
      // Read-only (trash) mode never writes: no editor mode (NONE), so dirtyRef
      // can't flip — but gate explicitly so no putPdfAnnotations call is even
      // reachable against the trash entry_name (red line ④).
      if (!readOnly && dirtyRef.current && !discardRef.current && doc) {
        dirtyRef.current = false
        doc
          .saveDocument()
          .then((bytes) => putPdfAnnotations(id, bytes))
          .catch((err: unknown) => {
            console.error('Failed to embed PDF annotations:', err)
          })
          .finally(teardown)
      } else {
        teardown()
      }
    }
  }, [paperId, pdfSrc, readOnly, tabKey])

  // Ctrl/Cmd + wheel zooms the PDF instead of the browser page (also catches
  // trackpad pinch, which fires wheel events with ctrlKey set). Plain wheel is
  // left alone so it still scrolls the page stack. Bound manually with
  // passive:false because a React onWheel handler can't preventDefault reliably.
  useEffect(() => {
    const el = containerRef.current
    if (!el) return
    const onWheel = (e: WheelEvent) => {
      if (!(e.ctrlKey || e.metaKey)) return
      e.preventDefault()
      clearHover() // zoom shifts annotations; drop a now-misplaced hover tooltip
      zoomBy(e.deltaY < 0 ? WHEEL_STEP : -WHEEL_STEP)
    }
    el.addEventListener('wheel', onWheel, { passive: false })
    return () => el.removeEventListener('wheel', onWheel)
  }, [zoomBy, clearHover])

  // Ctrl/Cmd + (+ / - / 0), including the numpad, drives PDF zoom rather than
  // the browser's. Only one PdfView is mounted at a time (TabArea renders the
  // active tab only), so a window listener is unambiguous.
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (!(e.ctrlKey || e.metaKey)) return
      if (e.key === '+' || e.key === '=' || e.code === 'NumpadAdd') {
        e.preventDefault()
        zoomBy(ZOOM_STEP)
      } else if (e.key === '-' || e.key === '_' || e.code === 'NumpadSubtract') {
        e.preventDefault()
        zoomBy(-ZOOM_STEP)
      } else if (e.key === '0' || e.code === 'Numpad0') {
        e.preventDefault()
        resetZoom()
      }
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [zoomBy, resetZoom])

  // ⌘/Ctrl+S saves annotations into the PDF instead of the browser's "save page".
  // Bound in the CAPTURE phase so it beats both the browser default AND pdf.js's
  // editor-level ctrl+s (which only commits the active editor) — saveNow's
  // commitPending already does that commit before writing, so nothing is lost.
  //
  // NOT bound in read-only (trash) mode: there is no write path, so ⌘S must not
  // even attempt a saveDocument()/PUT against the trash entry_name (red line ④).
  // The browser's own "save page" is left alone there (no preventDefault).
  useEffect(() => {
    if (readOnly) return
    const onKey = (e: KeyboardEvent) => {
      if ((e.ctrlKey || e.metaKey) && (e.key === 's' || e.key === 'S')) {
        e.preventDefault()
        e.stopPropagation()
        void saveNow()
      }
    }
    window.addEventListener('keydown', onKey, { capture: true })
    return () => window.removeEventListener('keydown', onKey, { capture: true })
  }, [saveNow, readOnly])

  // In Draw (Ink) mode, finalize each stroke as soon as it's released and select
  // the resulting editor. pdf.js's Ink editor is multi-stroke
  // (supportMultipleDrawings = true): a stroke does NOT finalize an editor — the
  // drawing session stays open for more strokes until you switch tools or click
  // empty space, so the mark the user just drew is neither an editor nor selected
  // and the popover (gated on a selection) never appears. We end the session on
  // pointerup — deferred a tick so pdf.js's own pointerup records the stroke
  // geometry first — then setSelected the returned editor, which dispatches the
  // selection + params events our handlers consume (popping the editor popover on
  // the just-drawn mark). We stay in Ink mode, so consecutive marks are still one
  // stroke each; moving/resizing still uses Cursor.
  useEffect(() => {
    if (editMode !== 'ink') return
    const onPointerUp = () => {
      setTimeout(() => {
        const ui = uiManagerRef.current
        const editor = ui?.currentLayer?.endDrawingSession(false)
        if (editor) ui?.setSelected?.(editor)
      }, 0)
    }
    window.addEventListener('pointerup', onPointerUp)
    return () => window.removeEventListener('pointerup', onPointerUp)
  }, [editMode])

  // Anchor the floating popover next to the selected annotation (Adobe-style).
  // A rAF loop syncs the popover to the `.selectedEditor` element's live rect, so
  // it follows the object through scroll / zoom / drag with one mechanism. We
  // mutate the popover's style directly (not React state) to avoid a re-render
  // per frame; React still owns the popover's visibility + contents.
  useEffect(() => {
    if (!hasSelection) return
    let raf = 0
    const place = () => {
      raf = requestAnimationFrame(place)
      const wrap = pdfWrapRef.current
      const pop = popoverRef.current
      const sel = wrap?.querySelector<HTMLElement>('.selectedEditor')
      if (!wrap || !pop) return
      const wr = wrap.getBoundingClientRect()
      const sr = sel?.getBoundingClientRect()
      // Hide until the selected editor's element exists and is within view (it
      // can be absent for a tick after selection, or scrolled out of the page).
      if (!sr || sr.bottom < wr.top || sr.top > wr.bottom) {
        pop.style.visibility = 'hidden'
        return
      }
      const gap = 8
      const pw = pop.offsetWidth
      const ph = pop.offsetHeight
      let left = sr.left - wr.left
      let top = sr.bottom - wr.top + gap
      // Flip above the object if the popover would overflow the bottom edge.
      if (top + ph > wr.height - gap) top = sr.top - wr.top - ph - gap
      left = Math.max(gap, Math.min(left, wr.width - pw - gap))
      top = Math.max(gap, Math.min(top, wr.height - ph - gap))
      pop.style.left = `${left}px`
      pop.style.top = `${top}px`
      pop.style.visibility = 'visible'
    }
    raf = requestAnimationFrame(place)
    return () => cancelAnimationFrame(raf)
  }, [hasSelection])

  // --- Hover note tooltip ---------------------------------------------------
  // Hovering a commented highlight / ink annotation surfaces its note in a
  // read-only tooltip (no click needed), Adobe-style. We detect the hovered
  // annotation by event delegation on the wrapper: every editor's div carries
  // id `pdfjs_internal_editor_<n>` (= the UIManager's #allEditors key), so
  // `getEditor(div.id).comment` reads the note. A SELECTED annotation
  // (`.selectedEditor`) is skipped — the editable popover already shows its note.
  const handlePdfHover = useCallback(
    (e: React.MouseEvent<HTMLDivElement>) => {
      const target = e.target as HTMLElement
      const div = target.closest<HTMLElement>('[id^="pdfjs_internal_editor_"]')
      if (!div || div.classList.contains('selectedEditor')) {
        clearHover()
        return
      }
      if (div.id === hoverIdRef.current) return // already showing this one
      const ed = uiManagerRef.current?.getEditor?.(div.id)
      const text = ed?.comment?.text?.trim()
      if (!text) {
        // No note (or a FreeText, whose text is the annotation itself) → nothing
        // to surface; drop any tooltip left over from a neighbouring annotation.
        clearHover()
        return
      }
      const wrap = pdfWrapRef.current
      if (!wrap) return
      const wr = wrap.getBoundingClientRect()
      const dr = div.getBoundingClientRect()
      hoverIdRef.current = div.id
      setHoverNote({
        text,
        ax: dr.left - wr.left,
        ay: dr.bottom - wr.top,
        atop: dr.top - wr.top,
      })
    },
    [clearHover],
  )

  // Measure the tooltip and clamp it inside the wrapper (below the annotation,
  // flipped above if it would overflow the bottom). useLayoutEffect so the
  // position is set before paint — no visible jump from a default corner.
  useLayoutEffect(() => {
    const tip = hoverNoteRef.current
    const wrap = pdfWrapRef.current
    if (!hoverNote || !tip || !wrap) return
    const gap = 6
    const wr = wrap.getBoundingClientRect()
    const tw = tip.offsetWidth
    const th = tip.offsetHeight
    const left = Math.max(gap, Math.min(hoverNote.ax, wr.width - tw - gap))
    let top = hoverNote.ay + gap
    if (top + th > wr.height - gap) top = hoverNote.atop - th - gap
    top = Math.max(gap, Math.min(top, wr.height - th - gap))
    tip.style.left = `${left}px`
    tip.style.top = `${top}px`
    tip.style.visibility = 'visible'
  }, [hoverNote])

  // Scrolling the page stack moves annotations out from under the tooltip's
  // (wrapper-relative) anchor, so drop it on scroll; the next hover re-shows it.
  // Also persist the reading position on every scroll so a tab switch returns
  // here — saving live (not only in the unmount cleanup, where reading scrollTop
  // off a detaching node is less reliable) keeps the latest offset in the map.
  // The current zoom is stored alongside (scrollTop is scaled pixels), so a zoom
  // — which itself shifts scrollTop and fires this — is captured too.
  useEffect(() => {
    const el = containerRef.current
    if (!el) return
    const onScroll = () => {
      clearHover()
      if (tabKey && !restoringRef.current) {
        viewPositions.set(tabKey, {
          scrollTop: el.scrollTop,
          scale: scaleRef.current,
        })
      }
    }
    el.addEventListener('scroll', onScroll, { passive: true })
    return () => el.removeEventListener('scroll', onScroll)
  }, [clearHover, tabKey])

  const saveLabel = saving ? 'Saving…' : savedFlash && !dirty ? 'Saved ✓' : 'Save'

  return (
    <div className="flex h-full flex-col">
      <div className="flex shrink-0 items-center gap-2 border-b border-stone-200 bg-stone-100 px-3 py-1.5">
        <button
          onClick={() => zoomBy(-ZOOM_STEP)}
          disabled={scale <= MIN_SCALE}
          title="Zoom out"
          className="rounded-md px-2 py-0.5 text-sm text-stone-600 transition-colors hover:bg-stone-200 disabled:text-stone-300 disabled:hover:bg-transparent"
        >
          −
        </button>
        <div className="flex items-center gap-0.5">
          <input
            value={pctDraft ?? String(Math.round(scale * 100))}
            onChange={(e) =>
              setPctDraft(e.target.value.replace(/[^0-9]/g, '').slice(0, 3))
            }
            onFocus={(e) => {
              setPctDraft(String(Math.round(scale * 100)))
              e.currentTarget.select()
            }}
            onBlur={(e) => commitPct(e.currentTarget.value)}
            onKeyDown={(e) => {
              if (e.key === 'Enter') {
                e.currentTarget.blur()
              } else if (e.key === 'Escape') {
                setPctDraft(null)
                e.currentTarget.blur()
              }
            }}
            inputMode="numeric"
            aria-label="Zoom percentage"
            title="Type a zoom %, then Enter"
            className="w-9 rounded bg-transparent text-right text-xs tabular-nums text-stone-600 hover:bg-stone-200 focus:bg-white focus:outline-none focus:ring-1 focus:ring-stone-300"
          />
          <span className="text-xs text-stone-500">%</span>
        </div>
        <button
          onClick={() => zoomBy(ZOOM_STEP)}
          disabled={scale >= MAX_SCALE}
          title="Zoom in"
          className="rounded-md px-2 py-0.5 text-sm text-stone-600 transition-colors hover:bg-stone-200 disabled:text-stone-300 disabled:hover:bg-transparent"
        >
          +
        </button>

        {/* Annotation tools + Save are hidden in read-only (trash) mode — a
            trashed PDF is immutable, so only render + zoom + read-only hover. */}
        {!readOnly && (
          <>
            <div className="mx-1 h-4 w-px bg-stone-300" />

            <div className="flex items-center gap-1">
              {EDIT_MODES.map(({ mode, label, title }) => {
                const active = editMode === mode
                return (
                  <button
                    key={mode}
                    onClick={() => selectMode(mode)}
                    title={title}
                    aria-pressed={active}
                    className={
                      'rounded-md border px-2 py-0.5 text-xs transition-colors ' +
                      (active
                        ? 'border-accent-500 bg-accent-50 text-accent-700 ring-1 ring-accent-500'
                        : 'border-transparent text-stone-600 hover:bg-stone-200')
                    }
                  >
                    {label}
                  </button>
                )
              })}
            </div>

            {/* Creation-tool params: pick the colour / size for the NEXT annotation
                before you draw. Editing an EXISTING annotation happens in the
                floating popover, not here. */}
            {toolType && (
              <>
                <div className="mx-1 h-4 w-px bg-stone-300" />
                <div className="animate-grow-in">
                  <ParamSwatches
                    type={toolType}
                    color={colorFor(toolType)}
                    onColor={pickColor}
                    textSize={textSize}
                    onTextSize={pickTextSize}
                    inkWidth={inkWidth}
                    onInkWidth={pickInkWidth}
                  />
                </div>
              </>
            )}
          </>
        )}

        <div className="ml-auto flex items-center gap-2.5">
          {!readOnly && (
            <button
              onClick={() => void saveNow()}
              disabled={!dirty || saving}
              title={
                dirty
                  ? 'Save annotations into the PDF (⌘/Ctrl+S)'
                  : 'No unsaved changes'
              }
              className={
                'rounded-md px-2.5 py-0.5 text-xs font-medium transition-colors ' +
                (dirty && !saving
                  ? 'bg-accent-500 text-white shadow-sm hover:bg-accent-600'
                  : savedFlash
                    ? 'bg-emerald-50 text-emerald-600'
                    : 'bg-stone-200 text-stone-400')
              }
            >
              {saveLabel}
            </button>
          )}
          {pageCount > 0 && (
            <span className="text-xs text-stone-500">
              {pageCount} page{pageCount === 1 ? '' : 's'}
            </span>
          )}
        </div>
      </div>

      {/* pdf_viewer requires an absolutely-positioned, overflow:auto container
          with an inner `.pdfViewer` div it fills with pages. `absolute inset-0`
          gives it the positioned box; the relative wrapper bounds it. `isolate`
          forces a stacking context here so pdf.js's huge internal z-indexes
          (e.g. `.selectedEditor` at z-index:100000) — and our popover above them —
          stay contained and can't paint over the TopBar's search dropdown. */}
      <div
        ref={pdfWrapRef}
        onMouseOver={handlePdfHover}
        onMouseLeave={clearHover}
        className="relative isolate min-h-0 flex-1 bg-stone-200"
      >
        {error && (
          <div className="absolute inset-x-0 top-0 z-10 p-6 text-sm text-red-700">
            Failed to load PDF: {error}
          </div>
        )}
        {!error && pageCount === 0 && (
          <div className="absolute inset-x-0 top-0 z-10 p-6 text-sm text-stone-500">
            Loading PDF…
          </div>
        )}
        <div ref={containerRef} className="absolute inset-0 overflow-auto p-6">
          <div ref={viewerElRef} className="pdfViewer" />
        </div>

        {/* Floating editor popover, anchored next to the selected annotation by
            the rAF loop above. zIndex sits above pdf.js's .selectedEditor
            (100000) but stays trapped in this `isolate` wrapper. Pointer events
            are kept from reaching pdf.js so clicking the popover never deselects
            the annotation it edits. */}
        {!readOnly && hasSelection && selectedType && (
          <div
            ref={popoverRef}
            style={{ visibility: 'hidden', zIndex: 100001 }}
            onPointerDown={(e) => e.stopPropagation()}
            onPointerUp={(e) => e.stopPropagation()}
            className="absolute flex flex-col gap-2 rounded-xl border border-stone-200 bg-white/95 p-2.5 shadow-xl shadow-stone-900/10 backdrop-blur-sm"
          >
            <ParamSwatches
              type={selectedType}
              color={colorFor(selectedType)}
              onColor={pickColor}
              textSize={textSize}
              onTextSize={pickTextSize}
              inkWidth={inkWidth}
              onInkWidth={pickInkWidth}
            />
            {canNoteFor(selectedType) && (
              <textarea
                value={noteDraft}
                onChange={(e) => setNoteDraft(e.target.value)}
                onBlur={commitNote}
                // Keep keystrokes inside the note. pdf.js's window-level
                // Backspace/Delete shortcut deletes the SELECTED annotation; its
                // text-field guard only exempts <input>, so a Delete meant to fix
                // a typo here would wipe the highlight/ink. Stopping propagation
                // (its listener is on `window`, bubble phase) prevents that.
                onKeyDown={(e) => e.stopPropagation()}
                rows={2}
                placeholder="Add a note…"
                className="w-52 resize-none rounded-lg border border-stone-200 bg-stone-50 p-2 text-xs text-stone-800 placeholder:text-stone-400 focus:border-accent-400 focus:bg-white focus:outline-none focus:ring-1 focus:ring-accent-400"
              />
            )}
            <button
              onClick={deleteSelected}
              title="Delete annotation"
              aria-label="Delete annotation"
              className="flex items-center justify-center gap-1.5 rounded-lg px-2 py-1 text-xs text-red-600 transition-colors hover:bg-red-50"
            >
              <TrashIcon />
              Delete
            </button>
          </div>
        )}

        {/* Read-only hover-note tooltip, positioned by the layout effect above.
            pointer-events:none so the cursor "passes through" to the annotation
            underneath — otherwise hovering the tooltip itself would fire a
            mouseover with no editor target and flicker it closed. zIndex sits
            with the popover, trapped in this `isolate` wrapper. */}
        {hoverNote && (
          <div
            ref={hoverNoteRef}
            style={{ visibility: 'hidden', zIndex: 100001 }}
            // A dark tooltip in both themes. The `.dark` ramp would otherwise
            // invert bg-stone-800 → light and text-stone-50 → dark (a bright
            // tooltip); pin the dark elevated surface + light text in dark mode.
            className="pointer-events-none absolute max-w-xs whitespace-pre-wrap break-words rounded-lg bg-stone-800/95 px-2.5 py-1.5 text-xs leading-snug text-stone-50 shadow-lg shadow-stone-900/20 backdrop-blur-sm dark:bg-stone-50/95 dark:text-stone-900"
          >
            {hoverNote.text}
          </div>
        )}
      </div>
    </div>
  )
}
