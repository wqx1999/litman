/** UI text: reader area — the PDF and markdown readers, the tab strip, the
 * keyboard cheat sheet, What's New. See ../index.ts for the rules. */
export const reader = {
  // The keyboard-shortcut overlay (`?`). Keycap labels (Ctrl / Alt / Shift /
  // Esc / Enter / single keys) are not here: they match the printed key.
  cheatSheet: {
    title: 'Keyboard shortcuts',
    // The UI-language switch in the header (ui/LangSwitch.tsx). The two
    // language names are not here: each is written in its own language in
    // every UI (LANG_NAMES in ../index.ts).
    lang: {
      // Screen-reader name of the three-way switch.
      label: 'Interface language',
      auto: 'Auto',
      // Hover title of "Auto"; `system` is what the system language resolves to.
      autoTitle: (system: 'en' | 'zh') =>
        `Follow the system language (now ${system === 'zh' ? 'Chinese' : 'English'})`,
      // Toast when the choice could not be stored on the server.
      saveFailed: 'Language setting not saved. It reverts when the page reloads.',
    },
    // Between two alternative chords for one action: "V or Esc".
    or: 'or',
    // Muted tags after an action: where the shortcut works.
    scope: {
      pdf: 'PDF',
      note: 'Note',
      tool: 'Tool',
      search: 'Search',
    },
    display: {
      title: 'Display & panels',
      focusMode: 'Focus mode (hide side panels)',
      theme: 'Toggle light / dark theme',
      leftPanel: 'Toggle the left panel',
      cockpit: 'Toggle the right cockpit',
      refresh: 'Refresh from disk',
      cheatSheet: 'Toggle this cheat sheet',
    },
    agent: {
      title: 'Agent',
      // The keycap shows `~`; "1" is the key next to it.
      note: 'The key left of "1", with or without Shift. Plain launch opens setup when the default is not ready.',
      launch: 'Launch the default AI agent',
      manage: 'Manage agents / change default',
    },
    papers: {
      title: 'Papers list & search',
      note: 'Move through the middle list; Enter opens the PDF.',
      next: 'Next paper',
      previous: 'Previous paper',
      pin: 'Pin / unpin the selected paper',
      open: 'Open the selected paper',
      openNotes: 'Open its notes',
      openDiscussion: 'Open its discussion',
      focusSearch: 'Focus search',
      clearSearch: 'Clear search',
    },
    tabs: {
      title: 'Tabs',
      note: 'Drag a tab to reorder. Right-click the bar to close many.',
      previous: 'Previous tab',
      next: 'Next tab',
      jump: 'Jump to tab 1–9',
    },
    pdf: {
      title: 'PDF tools',
      // "Cursor" is the toolbar's name for the no-tool mode (pdfView.tool.none).
      note: 'Only while a PDF tab is active. Tools switch freely in any order. Undo needs a tool on — not Cursor.',
      cursor: 'Cursor (select / exit tool)',
      saveNote: 'Save the note and close it',
      highlight: 'Highlight',
      textNote: 'Text note',
      draw: 'Draw (ink)',
      undo: 'Undo the last annotation change',
      redo: 'Redo it',
      back: 'Back to where you jumped from',
      forward: 'Forward again',
    },
    curation: {
      title: 'Curation — selected paper',
      note: 'Hold Alt. Acts on the selected paper; does nothing when none is selected.',
      markRead: 'Mark read (repeat presses are ignored)',
      markUnread: 'Mark unread (confirm)',
      // "deep-read" is the reading-status value (common.status).
      promote: 'Promote (deep-read)',
      drop: 'Drop (confirm)',
      tagEditor: 'Open the tag editor',
      copyPath: 'Copy paper path',
      copyId: 'Copy paper id',
    },
    system: {
      title: 'System (unchanged)',
      save: 'Save the current tab',
      zoom: 'PDF zoom',
      focusSearch: 'Focus search',
    },
  },

  // The post-update card. The bullets come from the package, not from here.
  whatsNew: {
    // Small caps line above the title.
    updated: 'Updated',
    // Card title and its dialog label; `version` is e.g. "1.3.12".
    title: (version: string) => `What's new in litman ${version}`,
    noHighlights: 'No highlights recorded for this version.',
    changelog: 'Read the full changelog →',
  },

  // The PDF tab: toolbar, annotation popover, load states.
  pdfView: {
    // The four toolbar tools: button label and its hover title.
    tool: {
      none: {
        label: 'Cursor',
        title: 'Select text / follow links (double-click an annotation to edit)',
      },
      highlight: { label: 'Highlight', title: 'Highlight text' },
      freetext: { label: 'Text', title: 'Add a text note' },
      ink: { label: 'Draw', title: 'Freehand ink' },
    },
    zoomOut: 'Zoom out',
    zoomIn: 'Zoom in',
    // The editable zoom-% box: its screen-reader label and hover title.
    zoomPercentLabel: 'Zoom percentage',
    zoomPercentTitle: 'Type a zoom %, then Enter',
    save: 'Save',
    saving: 'Saving…',
    // Shown on the Save button for a moment after a save lands.
    saved: 'Saved ✓',
    saveTitle: 'Save annotations into the PDF (⌘/Ctrl+S)',
    noChanges: 'No unsaved changes',
    // Toolbar, right end: the document's page count.
    pageCount: (n: number) => (n === 1 ? '1 page' : `${n} pages`),
    loadFailed: 'Failed to load PDF.',
    // The server has no paper.pdf for this paper (e.g. not yet synced to this
    // computer).
    notOnThisComputer: 'No PDF for this paper on this computer.',
    loading: 'Loading PDF…',
    // The note box in the popover beside a selected highlight / drawing.
    notePlaceholder: 'Add a note…',
    deleteAnnotation: 'Delete annotation',
  },

  // Colour / size swatches for an annotation tool (toolbar and popover).
  paramSwatches: {
    // Hover title of each colour dot.
    color: {
      yellow: 'Yellow',
      green: 'Green',
      blue: 'Blue',
      pink: 'Pink',
      red: 'Red',
      black: 'Black',
      orange: 'Orange',
    },
    // Hover titles; the number is pdf.js's own size value.
    fontSize: (size: number) => `Font size ${size}`,
    thickness: (width: number) => `Thickness ${width}`,
  },

  // The pill at the bottom of the PDF after a citation jump.
  jumpBackPill: {
    label: (page: number) => `Back to page ${page}`,
    // Keep "Alt+←": it is the key to press.
    title: (page: number) => `Back to page ${page} (Alt+←)`,
    // The pill's visible text; "p." abbreviates "page".
    text: (page: number) => `Back to p.${page}`,
  },

  // A notes.md / discussion.md tab. `file` is the file name, "notes.md" or
  // "discussion.md", and stays as-is.
  mdView: {
    header: {
      notes: '📝 Notes',
      discussion: '💬 Discussion',
    },
    // Header, while the unsaved draft's rendered preview is showing.
    previewingDraft: 'Unsaved draft — previewing',
    edit: 'Edit',
    preview: 'Preview',
    save: 'Save',
    saving: 'Saving…',
    // Header hints, right end, when not editing.
    readOnly: 'read-only (trash)',
    unavailable: 'unavailable',
    hintStart: 'double-click to start writing',
    hintEdit: 'double-click to edit',
    saveFailed: (file: string) => `Couldn't save ${file} — your edit is kept; try again.`,
    loadFailed: (file: string) => `Couldn't load ${file}.`,
    loadFailedFix: 'The file is unchanged. Reopen this tab once it is fixed.',
    missingTrashed: (file: string) => `No ${file} for this trashed paper.`,
    missing: (file: string) => `No ${file} for this paper yet — double-click to start writing.`,
  },

  // The tab strip and the empty document area under it.
  tabArea: {
    noTabs: 'Open a paper from the list.',
    closeTab: 'Close tab',
    // Right-click menu on the tab strip.
    closeOthers: 'Close other tabs',
    closeAll: 'Close all tabs',
    noDocument: 'No document open.',
  },

  // Closing a tab with unsaved changes. {label} is the tab's label (a paper id,
  // or "<id> · notes"), shown in bold.
  saveDialog: {
    pdf: {
      title: 'Save annotations?',
      body: '{label} has unsaved annotations. Save before closing?',
    },
    md: {
      title: 'Save edits?',
      body: '{label} has unsaved edits. Save before closing?',
    },
    discard: "Don't save",
    save: 'Save',
    saving: 'Saving…',
  },

  // Confirm before moving a paper to the trash (the tab's trash icon). The
  // lists are paper ids / repo names / project names.
  removePaper: {
    dialogLabel: 'Remove paper from library',
    title: (id: string) => `Remove “${id}” from the library?`,
    // "🗑 Trash" is the app's Trash view.
    body: 'The paper moves to the trash; you can restore it from 🗑 Trash.',
    previewFailed: 'Could not load the impact preview.',
    checking: 'Checking what this affects…',
    nothingLinks: 'Nothing else links to this paper.',
    // Heading over the list of links the removal breaks (one line each below).
    alsoBreaks: 'This also breaks',
    references: (papers: string[]) =>
      `Drops it from the relations of ${papers.length === 1 ? '1 paper' : `${papers.length} papers`}: ${papers.join(', ')}`,
    reposRemoved: (repos: string[]) =>
      `Deletes ${repos.length === 1 ? '1 cloned repo' : `${repos.length} cloned repos`} no other paper uses: ${repos.join(', ')}`,
    reposUnbound: (repos: string[]) =>
      `Unbinds ${repos.length === 1 ? '1 repo' : `${repos.length} repos`} (kept — still used elsewhere): ${repos.join(', ')}`,
    reposKept: (repos: string[]) =>
      `Unbinds ${repos.length === 1 ? '1 repo' : `${repos.length} repos`} (kept — no copy to clone it again): ${repos.join(', ')}`,
    reposEdited: (repos: string[]) =>
      `Unbinds ${repos.length === 1 ? '1 repo' : `${repos.length} repos`} (kept — has your own changes): ${repos.join(', ')}`,
    reposUnchecked: (repos: string[]) =>
      `Unbinds ${repos.length === 1 ? '1 repo' : `${repos.length} repos`} (kept — could not check for changes): ${repos.join(', ')}`,
    projects: (projects: string[]) =>
      `Unlinks from ${projects.length === 1 ? '1 project' : `${projects.length} projects`}: ${projects.join(', ')}`,
    // "(deleted)" is the literal tag written into those notes; keep it as-is.
    notes: (n: number) =>
      `Tags ${n === 1 ? '1 referencing note' : `${n} referencing notes`} with “(deleted)”`,
    removing: 'Removing…',
    remove: 'Remove to trash',
  },

  // Toast when a link to another site could not be handed to the browser.
  externalLinks: {
    noAnswer: 'litman did not answer — opening the link in this window instead',
  },

  // The year field's format hint (add-paper card, metadata editor).
  year: {
    hint: '4 digits',
  },
}
