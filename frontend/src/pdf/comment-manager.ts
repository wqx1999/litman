// Minimal CommentManager satisfying pdf.js v5.7's `commentManager` contract.
//
// pdf.js's component build (`pdf_viewer.mjs`) ships the editor-side comment
// machinery (the `Comment` class, the `editor.comment` setter, serializing a
// note into the saved PDF as a Popup annotation on `saveDocument()`), but NOT
// the app-level CommentManager that the full Firefox viewer wires up — that one
// lives in `web/app.js` and depends on a specific dialog DOM, an OverlayManager,
// l10n, and `images/*.svg` icons that don't resolve in an embedded build.
//
// So we supply our own CommentManager. It does only the part pdf.js can't:
// show a dialog to capture/edit/delete the note text. Everything else (button
// rendering, popup hover wiring, PDF serialization) pdf.js handles. We
// deliberately suppress pdf.js's native comment chrome (.annotationCommentButton
// / .commentPopup) via pdf-editor-overrides.css and instead drive add/edit from
// our own React toolbar (Note button → editor.editComment() → showDialog) so
// the UI matches the rest of the app.
//
// Two consequences worth noting:
//  1. Highlight + Ink editors set `canAddComment = true` (pdf.js base); FreeText
//     sets it `false`. So pdf.js itself enforces "text can't carry a note" —
//     exactly the desired behaviour (a text annotation IS the note).
//  2. With ANY commentManager present, FreeText's renderAnnotationElement uses
//     `popup: this.comment` (null when no note) instead of the legacy
//     `popup: {text: content}` fallback — which is what made text annotations
//     sprout a duplicate hover popup. Providing this manager fixes that.

/** Async UI seam the React layer implements: open the note modal and resolve
 *  with the new text, '' to delete the note, or `undefined` if cancelled. */
export interface CommentBridge {
  openDialog(initialText: string): Promise<string | undefined>
}

// The slivers of pdf.js's editor / UIManager we touch. Untyped in pdfjs-dist.
// `comment` reads back as an object but is written as a plain string ('' / text)
// or null — pdf.js's getter/setter are asymmetric, so we model both halves.
interface CommentableEditor {
  get comment(): { text: string | null } | null
  set comment(value: string | null)
}
interface CommentUIManager {
  addCommands(params: { cmd: () => void; undo: () => void; mustExec: boolean }): void
  setSelected(editor: CommentableEditor): void
}

export class CommentManager {
  #bridge: CommentBridge
  // pdf.js reads `commentManager.dialogElement` for aria wiring on the editor's
  // comment button; give it a real (detached) node so that never dereferences null.
  #dialogElement: HTMLElement

  constructor(bridge: CommentBridge) {
    this.#bridge = bridge
    this.#dialogElement = document.createElement('div')
    this.#dialogElement.id = 'litman-comment-dialog-anchor'
  }

  get dialogElement(): HTMLElement {
    return this.#dialogElement
  }

  // The only method that does real work. pdf.js calls it as
  // `showDialog(uiManager, editor, posX, posY, options)`; we ignore position and
  // center our own modal. Persisting the note through `editor.comment = …` inside
  // `uiManager.addCommands` (a) applies it, (b) marks the doc dirty via the
  // editingstateschanged the command emits (so our flush saves it), and (c) gives
  // undo/redo for free.
  async showDialog(uiManager: CommentUIManager, editor: CommentableEditor): Promise<void> {
    const prev = editor.comment?.text ?? null
    const result = await this.#bridge.openDialog(prev ?? '')
    if (result === undefined) return // cancelled — no change
    const next = result.trim() === '' ? null : result
    if (next === prev) return
    uiManager.addCommands({
      cmd: () => {
        editor.comment = next
      },
      undo: () => {
        editor.comment = prev
      },
      mustExec: true,
    })
    // Clicking the modal's Save button (outside the PDF) deselects the editor;
    // re-select it so the toolbar's Note icon reflects the new note immediately
    // (re-dispatches hasSelectedEditor + the editor's params).
    uiManager.setSelected(editor)
  }

  // --- Sidebar / native-popup surface: we render neither, so these are no-ops.
  //     Declared param-less so TS strict (noUnusedParameters) is satisfied while
  //     still accepting pdf.js's extra call arguments (JS ignores them). ---
  setSidebarUiManager(): void {}
  showSidebar(): void {}
  hideSidebar(): void {}
  removeComments(): void {}
  selectComment(): void {}
  addComment(): void {}
  updateComment(): void {}
  updatePopupColor(): void {}
  toggleCommentPopup(): void {}
  destroyPopup(): void {}
  makeCommentColor(): string | null {
    return null
  }
  destroy(): void {}
}
