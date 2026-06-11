import { useCallback, useEffect, useRef, useState } from 'react'
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
import NoteDialog from './NoteDialog'
import { pdfUrl, putPdfAnnotations } from '../api'

// pdf.js needs its worker registered once, before any document is parsed. The
// `?url` import gives vite the hashed worker path under assets/ at build time.
pdfjsLib.GlobalWorkerOptions.workerSrc = workerUrl

/** Handle the parent (App) uses to flush / inspect a mounted PDF tab when the
 *  user closes it. Lets the close path show a Save / Don't-save / Cancel prompt
 *  instead of a silent write (invariant #16: annotations embed into paper.pdf). */
export interface PdfHandle {
  /** Are there annotation edits not yet embedded into the PDF? */
  isDirty(): boolean
  /** saveDocument() + PUT the bytes; resolves when paper.pdf is overwritten. */
  flush(): Promise<void>
  /** Drop pending edits so the unmount teardown does not save them. */
  discard(): void
}

interface Props {
  paperId: string
  /** This view's tab key, used by the parent's close-time save registry. */
  tabKey?: string
  /** Register/unregister this view's flush handle with the parent. */
  onRegister?: (key: string, handle: PdfHandle | null) => void
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
// "Cursor" maps to pdf.js's POPUP mode, NOT NONE: in POPUP the UIManager
// enables ALL existing editors (highlight/text/ink) so they can be selected,
// moved, recoloured and deleted, while a click on empty space creates nothing
// (pdf.mjs gates STAMP/POPUP/SIGNATURE out of createAndAddNewEditor). NONE
// would disable every editor — you could not touch an existing annotation. The
// other three enter the matching creation editor. Numeric values come from
// pdf.js's own enum (v5: FREETEXT=3, HIGHLIGHT=9, INK=15, POPUP=16) — never
// hard-coded, so a pdf.js bump can't silently point a button at the wrong mode.
type EditMode = 'none' | 'highlight' | 'freetext' | 'ink'
const EDITOR_TYPE: Record<EditMode, number> = {
  none: AnnotationEditorType.POPUP,
  highlight: AnnotationEditorType.HIGHLIGHT,
  freetext: AnnotationEditorType.FREETEXT,
  ink: AnnotationEditorType.INK,
}
const EDIT_MODES: { mode: EditMode; label: string; title: string }[] = [
  { mode: 'none', label: 'Cursor', title: 'Select / move / edit existing annotations' },
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
// internally). Keep the names/hexes in sync with HL_SWATCHES below so our colour
// bar values resolve to a known name (used for the annotation's aria label).
const HIGHLIGHT_COLORS =
  'yellow=#FFFF98,green=#53FFBC,blue=#80EBFF,pink=#FFCBE6,red=#FF4F5F,' +
  'yellow_HCM=#FFFFCC,green_HCM=#53FFBC,blue_HCM=#80EBFF,pink_HCM=#F6B8FF,red_HCM=#C50043'

// Built-in colour bars (per the user's request: a few fixed swatches, no full
// colour picker). Highlight uses pdf.js's translucent palette; text/ink use
// opaque ink colours. Each `hex` is what we feed to the matching *_COLOR param.
const HL_SWATCHES = [
  { name: 'Yellow', hex: '#FFFF98' },
  { name: 'Green', hex: '#53FFBC' },
  { name: 'Blue', hex: '#80EBFF' },
  { name: 'Pink', hex: '#FFCBE6' },
  { name: 'Red', hex: '#FF4F5F' },
]
const PEN_SWATCHES = [
  { name: 'Black', hex: '#1A1A1A' },
  { name: 'Red', hex: '#E03131' },
  { name: 'Blue', hex: '#1971C2' },
  { name: 'Green', hex: '#2F9E44' },
  { name: 'Orange', hex: '#E8590C' },
]
// FreeText font sizes (FREETEXT_SIZE param) — `ui` sizes the preview glyph.
const TEXT_SIZES = [
  { value: 12, ui: '11px' },
  { value: 18, ui: '14px' },
  { value: 28, ui: '18px' },
]
// Ink line widths (INK_THICKNESS param) — `dot` is the preview bar height.
const INK_WIDTHS = [
  { value: 2, dot: 2 },
  { value: 6, dot: 4 },
  { value: 12, dot: 7 },
]

const PARAM = AnnotationEditorParamsType

// The subset of pdf.js's AnnotationEditorUIManager we touch: programmatic
// delete, the finalize pair (commit active editor / end ink drawing session),
// and the currently-selected editor (for the Note button — `editComment()`
// opens the comment dialog; `hasComment`/`canAddComment` drive the button).
type SelectedEditor = {
  editComment(): void
  canAddComment?: boolean
  hasComment?: boolean
}
type EditorUIManager = {
  delete(): void
  commitOrRemove(): void
  currentLayer?: { endDrawingSession(stop: boolean): void } | null
  firstSelectedEditor?: SelectedEditor | null
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

function NoteIcon({ filled }: { filled: boolean }) {
  // Speech-bubble glyph. Filled when the selected annotation already carries a
  // note, outline when it doesn't (click to add). Inline SVG, same rationale as
  // TrashIcon.
  return (
    <svg
      viewBox="0 0 16 16"
      className="h-3.5 w-3.5"
      fill={filled ? 'currentColor' : 'none'}
      stroke="currentColor"
      strokeWidth="1.3"
      aria-hidden
    >
      <path
        d="M2 3.5A1.5 1.5 0 0 1 3.5 2h9A1.5 1.5 0 0 1 14 3.5v6A1.5 1.5 0 0 1 12.5 11H6l-3 3v-3H3.5A1.5 1.5 0 0 1 2 9.5v-6z"
        strokeLinejoin="round"
      />
    </svg>
  )
}

/** pdf.js render via the higher-level `pdf_viewer` (Phase 2 — text +
 * annotation + annotation-editor layers).
 *
 * We drive pdf.js's `PDFViewer` (text + annotation + annotation-editor layers)
 * and enable three built-in editors — Highlight / FreeText / Ink — plus a
 * Cursor (POPUP) mode that selects/moves/recolours/deletes existing annotations
 * of any type. The editor's own floating toolbar + native comment chrome are
 * hidden (see pdf-editor-overrides.css); instead our toolbar exposes contextual
 * controls and drives the editor through the documented
 * `switchannotationeditorparams` event (colour / size / thickness) and the
 * UIManager (delete). pdf.js v5 has no `annotationEditorParams` setter — the
 * eventBus is the supported path. The contextual bar is selection-aware: in
 * Cursor mode it mirrors the SELECTED annotation's type so the same colour /
 * size / delete / note controls apply to it.
 *
 * Notes (comments): Highlight + Ink annotations can carry a note via the Note
 * button, captured in our NoteDialog and embedded into the PDF by pdf.js's
 * comment machinery (a minimal CommentManager supplies just the dialog). FreeText
 * cannot carry a note (pdf.js sets canAddComment=false) — a text annotation IS
 * the note. Wiring a CommentManager also stops FreeText from auto-rendering its
 * text as a stray hover popup.
 *
 * Annotation persistence (invariant #16): edits embed into the PDF via
 * `doc.saveDocument()`. The parent intercepts tab close to prompt Save / Don't
 * save (via the registered PdfHandle); a silent flush still runs on plain
 * unmount (tab switch) unless `discard()` suppressed it. We never flush per
 * stroke. `dirtyRef` gates the flush so an untouched PDF is never re-written. */
export default function PdfView({ paperId, tabKey, onRegister }: Props) {
  // The absolutely-positioned scroll container pdf_viewer attaches to.
  const containerRef = useRef<HTMLDivElement>(null)
  // The inner `.pdfViewer` div pdf_viewer fills with pages.
  const viewerElRef = useRef<HTMLDivElement>(null)
  const viewerRef = useRef<PDFViewer | null>(null)
  const eventBusRef = useRef<EventBus | null>(null)
  // The AnnotationEditorUIManager, captured from editingstateschanged's source
  // (it dispatches with `source: this`). Used for programmatic delete and to
  // finalize pending editors before save, without reaching into pdf.js private
  // fields. `commitOrRemove` + `endDrawingSession` mirror pdf.js's own
  // `#beforeUnload` handler.
  const uiManagerRef = useRef<EditorUIManager | null>(null)
  const docRef = useRef<PDFDocumentProxy | null>(null)
  // Captured for the unmount flush so the cleanup closure does not depend on a
  // possibly-stale prop.
  const paperIdRef = useRef(paperId)
  paperIdRef.current = paperId
  // Set true once an annotation command lands; gates the one-shot flush.
  const dirtyRef = useRef(false)
  // Set by discard(): the unmount teardown must not save when the user chose
  // "Don't save" at the close prompt.
  const discardRef = useRef(false)

  const [error, setError] = useState<string | null>(null)
  const [pageCount, setPageCount] = useState(0)
  const [scale, setScale] = useState(DEFAULT_SCALE)
  const [editMode, setEditMode] = useState<EditMode>('none')
  // Draft string while the user is typing in the % box; null = show live scale.
  const [pctDraft, setPctDraft] = useState<string | null>(null)
  // True when an annotation editor is selected — gates Delete / Note buttons.
  const [hasSelection, setHasSelection] = useState(false)
  // Type of the currently-selected annotation (inferred from the params pdf.js
  // broadcasts on selection). In Cursor mode this drives which contextual
  // controls the toolbar shows; null when nothing is selected.
  const [selectedType, setSelectedType] = useState<EditMode | null>(null)
  // True when the selected highlight/ink already carries a note (fills the Note
  // icon so the user can tell at a glance).
  const [hasNote, setHasNote] = useState(false)
  // Open note modal request: holds the resolver the CommentManager awaits.
  const [noteDialog, setNoteDialog] = useState<{
    initial: string
    resolve: (value: string | undefined) => void
  } | null>(null)

  // Per-tool params shown in the contextual bar. Persist across papers so the
  // user's last colour/size sticks. Defaults match pdf.js's first-use feel.
  const [hlColor, setHlColor] = useState('#FFFF98')
  const [textColor, setTextColor] = useState('#1A1A1A')
  const [textSize, setTextSize] = useState(18)
  const [inkColor, setInkColor] = useState('#1A1A1A')
  const [inkWidth, setInkWidth] = useState(6)

  // zoomBy reads the latest scale without re-binding the wheel/key listeners on
  // every zoom; a ref mirrors the state for that.
  const scaleRef = useRef(scale)
  scaleRef.current = scale

  // Bridge the CommentManager (created once per document, inside the load
  // effect) to React state. The manager calls openNoteRef.current(initial) and
  // awaits the user's note; we resolve it from the NoteDialog handlers. A ref so
  // the manager closes over a stable function while always hitting fresh state.
  const openNoteRef = useRef<(initial: string) => Promise<string | undefined>>(
    () => Promise.resolve(undefined),
  )
  openNoteRef.current = (initial: string) =>
    new Promise<string | undefined>((resolve) =>
      setNoteDialog({ initial, resolve }),
    )

  // Which annotation type the contextual bar reflects: the active creation tool,
  // or (in Cursor mode) the selected annotation's type. null = no bar.
  const barType: EditMode | null =
    editMode !== 'none' ? editMode : hasSelection ? selectedType : null

  // Push a clamped scale to the live viewer and mirror it into state (so the %
  // box and button disabled-states track it). The single seam every zoom path
  // funnels through.
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

  // Commit a typed percentage from the toolbar input: parse, clamp, apply. The
  // input holds an integer percent so the field can land on any value (e.g.
  // 137%), unlike the 0.1-snapped button/wheel zoom.
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

  // Apply a colour to the contextual target: the selected editor in Cursor mode,
  // else the active tool's default for the next annotation. Keyed off barType so
  // recolouring a selected highlight in Cursor mode hits HIGHLIGHT_COLOR, etc.
  const pickColor = useCallback(
    (hex: string) => {
      if (barType === 'highlight') {
        setHlColor(hex)
        setParam(PARAM.HIGHLIGHT_COLOR, hex)
      } else if (barType === 'freetext') {
        setTextColor(hex)
        setParam(PARAM.FREETEXT_COLOR, hex)
      } else if (barType === 'ink') {
        setInkColor(hex)
        setParam(PARAM.INK_COLOR, hex)
      }
    },
    [barType, setParam],
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

  // Open the note dialog for the selected highlight/ink. editComment() runs
  // pdf.js's comment flow, which calls our CommentManager.showDialog →
  // openNoteRef → NoteDialog; saving routes back through addCommands (marks the
  // doc dirty so the close-flush embeds the note into the PDF).
  const openNote = useCallback(() => {
    uiManagerRef.current?.firstSelectedEditor?.editComment()
  }, [])

  // Finalize the in-progress editor before a save: commit the active FreeText
  // and end any open Ink drawing session. Without this, an editor still being
  // edited at save time is not yet in the annotationStorage that saveDocument()
  // serializes, so its annotation silently fails to embed (Highlight commits on
  // creation and was unaffected; FreeText/Ink were not). Mirrors pdf.js's own
  // #beforeUnload handler.
  const commitPending = useCallback(() => {
    const ui = uiManagerRef.current
    if (!ui) return
    try {
      ui.commitOrRemove()
      ui.currentLayer?.endDrawingSession(false)
    } catch (err) {
      console.error('Failed to commit pending annotation:', err)
    }
  }, [])

  // Embed pending edits into the PDF and overwrite paper.pdf (invariant #16).
  // Stable callback (reads refs) so the registration effect does not churn.
  const flush = useCallback(async () => {
    commitPending()
    const doc = docRef.current
    const id = paperIdRef.current
    if (!dirtyRef.current || !doc) return
    dirtyRef.current = false
    const bytes = await doc.saveDocument()
    await putPdfAnnotations(id, bytes)
  }, [commitPending])

  // Register this view's flush handle with the parent so closing the tab can
  // prompt Save / Don't save instead of writing silently.
  useEffect(() => {
    if (!onRegister || !tabKey) return
    onRegister(tabKey, {
      isDirty: () => dirtyRef.current,
      flush,
      discard: () => {
        discardRef.current = true
        dirtyRef.current = false
      },
    })
    return () => onRegister(tabKey, null)
  }, [tabKey, onRegister, flush])

  // Build the viewer and load the document when the paper changes.
  useEffect(() => {
    const container = containerRef.current
    const viewerEl = viewerElRef.current
    if (!container || !viewerEl) return

    setError(null)
    setPageCount(0)
    setScale(DEFAULT_SCALE)
    setEditMode('none')
    setHasSelection(false)
    setSelectedType(null)
    setHasNote(false)
    setNoteDialog(null)
    dirtyRef.current = false
    discardRef.current = false

    const eventBus = new EventBus()
    eventBusRef.current = eventBus
    const linkService = new PDFLinkService({ eventBus })
    // Supplies just the note-capture dialog; pdf.js does the rest (comment
    // buttons, embedding the note into the PDF on save). Its presence also
    // switches FreeText off the legacy "render my text as a hover popup" path.
    const commentManager = new CommentManager({
      openDialog: (initial) => openNoteRef.current(initial),
    })
    const viewer = new PDFViewer({
      container,
      viewer: viewerEl,
      eventBus,
      linkService,
      // Construct in NONE, then switch to POPUP (our Cursor default) once the
      // editor layer has rendered — see enterCursorMode below. Entering POPUP at
      // construction trips pdf.js's POPUP-mode comment-sidebar code, which maps
      // over `#editorTypes` before any layer has registered them (null → throw).
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

    // Apply the initial scale once pages are laid out (currentScale before
    // pagesinit is ignored by pdf_viewer).
    const onPagesInit = () => {
      viewer.currentScale = DEFAULT_SCALE
    }
    eventBus.on('pagesinit', onPagesInit)

    // Enter Cursor (POPUP) select mode only AFTER the first annotation-editor
    // layer has rendered: that's when the UIManager has registered its editor
    // types (AnnotationEditorLayer ctor → registerEditorTypes), which POPUP's
    // sidebar code needs. One-shot — detach after first fire.
    const enterCursorMode = () => {
      eventBus.off('annotationeditorlayerrendered', enterCursorMode)
      try {
        viewer.annotationEditorMode = { mode: AnnotationEditorType.POPUP }
      } catch (err) {
        console.error('Failed to enter select mode:', err)
      }
    }
    eventBus.on('annotationeditorlayerrendered', enterCursorMode)

    // editingstateschanged fires with the full editor state every time (pdf.js
    // merges into a persistent object). We capture the UIManager (= event
    // source) for programmatic delete, mirror the undo stack into dirty (a real
    // command grows it; mere text selection does not; undoing everything drains
    // it → false, so "add then fully undo" ends clean and the flush is a no-op),
    // and track whether an editor is selected to gate the Delete button.
    const onEditingStates = (e: {
      source?: EditorUIManager
      details?: { hasSomethingToUndo?: boolean; hasSelectedEditor?: boolean }
    }) => {
      const ui = e.source
      if (ui) uiManagerRef.current = ui
      dirtyRef.current = !!e.details?.hasSomethingToUndo
      const selected = !!e.details?.hasSelectedEditor
      setHasSelection(selected)
      // Selection cleared → drop the inferred type so the Cursor-mode bar hides.
      if (!selected) setSelectedType(null)
      // Reflect whether the selected highlight/ink already carries a note.
      setHasNote(selected && !!ui?.firstSelectedEditor?.hasComment)
    }
    eventBus.on('editingstateschanged', onEditingStates)

    // pdf.js broadcasts the active param values (e.g. when a different editor is
    // selected, or on mode entry) as [type, value] pairs; mirror them so the
    // toolbar's highlighted swatch/size tracks the selected annotation.
    const onParamsChanged = (e: { details?: [number, unknown][] }) => {
      const details = e.details ?? []
      // Infer the selected annotation's type from which param kinds pdf.js
      // broadcasts (each editor exposes a distinct set). Drives the Cursor-mode
      // contextual bar; ignored in tool modes (barType uses editMode there).
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
    const loadingTask = pdfjsLib.getDocument({ url: pdfUrl(paperId) })
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
      eventBus.off('pagesinit', onPagesInit)
      eventBus.off('annotationeditorlayerrendered', enterCursorMode)
      eventBus.off('editingstateschanged', onEditingStates)
      eventBus.off('annotationeditorparamschanged', onParamsChanged)

      // Finalize any in-progress editor so the flush below embeds it. Events are
      // already detached, so the resulting state dispatch won't setState on the
      // unmounting view.
      const ui = uiManagerRef.current
      try {
        ui?.commitOrRemove()
        ui?.currentLayer?.endDrawingSession(false)
      } catch (err) {
        console.error('Failed to commit pending annotation:', err)
      }
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
        if (doc) void doc.destroy()
        else void loadingTask.destroy()
      }

      // Silent flush on plain unmount (tab switch). When the user closed the tab
      // the parent already ran flush()/discard() before removing it, so dirty is
      // false here (or discard suppresses the save) and this is a no-op teardown.
      if (dirtyRef.current && !discardRef.current && doc) {
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
  }, [paperId])

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
      zoomBy(e.deltaY < 0 ? WHEEL_STEP : -WHEEL_STEP)
    }
    el.addEventListener('wheel', onWheel, { passive: false })
    return () => el.removeEventListener('wheel', onWheel)
  }, [zoomBy])

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

  const colorBar = barType === 'highlight' ? HL_SWATCHES : PEN_SWATCHES
  const activeColor =
    barType === 'highlight'
      ? hlColor
      : barType === 'freetext'
        ? textColor
        : inkColor
  // Highlight + Ink can carry a note; FreeText cannot (it IS the note).
  const canNote = barType === 'highlight' || barType === 'ink'

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

        {/* Contextual param bar — colours + (size | thickness) + note + delete.
            Shown while a creation tool is active, or (in Cursor mode) while an
            annotation is selected, mirroring that annotation's type. */}
        {barType && (
          <>
            <div className="mx-1 h-4 w-px bg-stone-300" />
            <div className="flex items-center gap-1.5 animate-grow-in">
              {colorBar.map((c) => {
                const active =
                  activeColor.toLowerCase() === c.hex.toLowerCase()
                return (
                  <button
                    key={c.hex}
                    onClick={() => pickColor(c.hex)}
                    title={c.name}
                    aria-label={c.name}
                    aria-pressed={active}
                    className={
                      'h-5 w-5 rounded-full transition ' +
                      (active
                        ? 'ring-2 ring-accent-500 ring-offset-1'
                        : 'ring-1 ring-stone-300 hover:scale-110')
                    }
                    style={{ backgroundColor: c.hex }}
                  />
                )
              })}

              {barType === 'freetext' && (
                <div className="ml-1 flex items-center gap-0.5">
                  {TEXT_SIZES.map((s) => (
                    <button
                      key={s.value}
                      onClick={() => pickTextSize(s.value)}
                      aria-pressed={textSize === s.value}
                      title={`Font size ${s.value}`}
                      className={
                        'flex h-6 w-6 items-center justify-center rounded-md font-semibold leading-none transition-colors ' +
                        (textSize === s.value
                          ? 'bg-accent-50 text-accent-700 ring-1 ring-accent-500'
                          : 'text-stone-600 hover:bg-stone-200')
                      }
                      style={{ fontSize: s.ui }}
                    >
                      A
                    </button>
                  ))}
                </div>
              )}

              {barType === 'ink' && (
                <div className="ml-1 flex items-center gap-0.5">
                  {INK_WIDTHS.map((w) => (
                    <button
                      key={w.value}
                      onClick={() => pickInkWidth(w.value)}
                      aria-pressed={inkWidth === w.value}
                      title={`Thickness ${w.value}`}
                      className={
                        'flex h-6 w-7 items-center justify-center rounded-md transition-colors ' +
                        (inkWidth === w.value
                          ? 'bg-accent-50 ring-1 ring-accent-500'
                          : 'hover:bg-stone-200')
                      }
                    >
                      <span
                        className="rounded-full bg-stone-600"
                        style={{ width: '14px', height: `${w.dot}px` }}
                      />
                    </button>
                  ))}
                </div>
              )}

              <div className="mx-0.5 h-4 w-px bg-stone-300" />
              {canNote && (
                <button
                  onClick={openNote}
                  disabled={!hasSelection}
                  title={
                    hasNote
                      ? 'Edit note on selected annotation'
                      : 'Add a note to selected annotation'
                  }
                  aria-label="Note on selected annotation"
                  aria-pressed={hasNote}
                  className={
                    'flex h-6 w-6 items-center justify-center rounded-md transition-colors disabled:text-stone-300 disabled:hover:bg-transparent ' +
                    (hasNote
                      ? 'text-accent-600 hover:bg-accent-50'
                      : 'text-stone-600 hover:bg-stone-200')
                  }
                >
                  <NoteIcon filled={hasNote} />
                </button>
              )}
              <button
                onClick={deleteSelected}
                disabled={!hasSelection}
                title="Delete selected annotation"
                aria-label="Delete selected annotation"
                className="flex h-6 w-6 items-center justify-center rounded-md text-stone-600 transition-colors hover:bg-red-50 hover:text-red-600 disabled:text-stone-300 disabled:hover:bg-transparent disabled:hover:text-stone-300"
              >
                <TrashIcon />
              </button>
            </div>
          </>
        )}

        {pageCount > 0 && (
          <span className="ml-auto text-xs text-stone-500">
            {pageCount} page{pageCount === 1 ? '' : 's'}
          </span>
        )}
      </div>

      {/* pdf_viewer requires an absolutely-positioned, overflow:auto container
          with an inner `.pdfViewer` div it fills with pages. `absolute inset-0`
          gives it the positioned box; the relative wrapper bounds it. */}
      <div className="relative min-h-0 flex-1 bg-stone-200">
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
      </div>

      {noteDialog && (
        <NoteDialog
          initialText={noteDialog.initial}
          onResolve={(value) => {
            noteDialog.resolve(value)
            setNoteDialog(null)
          }}
        />
      )}
    </div>
  )
}
