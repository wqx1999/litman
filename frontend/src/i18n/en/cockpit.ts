/** UI text: cockpit area — the right-hand metadata panel, its dialogs, and the
 * editors it shares with Add paper / the browse panel. See ../index.ts for the
 * rules. */
export const cockpit = {
  /** The panel itself (cockpit/Cockpit.tsx), live and trash-inspector. */
  panel: {
    // Hover titles of the ‹ / › handles that open and close the panel.
    expand: 'Expand metadata',
    collapse: 'Collapse metadata',
    // Section header over a live paper.
    heading: 'Metadata',
    // Section header in the trash view's read-only inspector.
    trashedHeading: 'Trashed paper',
    loading: 'Loading…',
    selectPaper: 'Select a paper.',
    selectTrashed: 'Select a trashed paper.',
    // The ✎ button on the section header.
    editTitle: 'Edit title, year, journal, authors and other bibliographic fields',
    editAria: 'Edit metadata',
    copyIdTitle: 'Copy the paper id',
    copyId: 'Copy ID',
    copyPathTitle: 'Copy the paper folder path',
    // Copy path is disabled: the server did not report the vault's path.
    noVaultPath: 'Vault path unavailable',
    copyPath: 'Copy path',
    citeTitle: 'Copy an ACS-style citation (journal abbrev. year, volume, pages)',
    cite: 'Cite',
    // Flashed beside the Copy ID / Copy path / Cite buttons for a moment.
    copied: {
      id: '✓ copied ID',
      path: '✓ copied path',
      citation: '✓ copied citation',
    },
    // Over the list of the server's caveats about the citation just copied.
    citeWarnHeading: 'Citation copied — verify:',
    // The trash inspector's Restore button.
    restoring: 'Restoring…',
    restore: '↩ Restore to library',
    // Under Restore: code clones of this paper that were deleted from disk.
    orphanRepos: (n: number) =>
      `${n} hard-deleted ${n === 1 ? 'repo needs' : 'repos need'} re-clone in the CLI after restore (lit trash restore / lit health-check).`,
    // Toast: the ⌥⇧R (unread) shortcut on a paper that was never marked read.
    notReadYet: 'Not marked read yet',
    // Toasts after an SI file was deleted. `warnings` is the server's own
    // text, already joined with " · ".
    siDeleted: (file: string) => `Deleted ${file}`,
    siDeletedWithWarnings: (file: string, warnings: string) => `Deleted ${file} · ${warnings}`,
    // Toast: the server refused a new tag as too close to an existing one but
    // sent no list of matches to show.
    looksLikeExisting: (value: string) =>
      `“${value}” looks like an existing value. Look for it in the list.`,
  },

  /** Row labels of the inspector (shown in small caps). DOI stays a literal. */
  fields: {
    authors: 'Authors',
    venueYear: 'Venue / Year',
    status: 'Status',
    type: 'Type',
    readDate: 'Read-date',
    readRevisit: 'Read / Revisit',
    tags: 'Tags',
    topics: 'Topics',
    methods: 'Methods',
    data: 'Data',
    projects: 'Projects',
    relations: 'Relations',
    codeClones: 'Code-clones',
    supplementary: 'Supplementary',
  },

  /** The Read / Revisit row. Dates are YYYY-MM-DD from metadata. */
  readRevisit: {
    unreadAria: 'Mark as unread',
    unreadTitle: 'Mark as unread (undo the first-read stamp)',
    // Title of the disabled Mark read button once the paper has been read.
    firstRead: (date: string) => `First read ${date}`,
    markReadTitle: 'Stamp the first-read date (today)',
    // The Mark read button's text once the paper has been read.
    readOn: (date: string) => `Read · ${date}`,
    markRead: 'Mark read',
    // Title of Log revisit while it is disabled.
    readFirst: 'Mark as read first',
    revisitTitle: 'Stamp a return visit (today)',
    logRevisit: 'Log revisit',
    lastRevisited: (date: string) => `last revisited ${date}`,
  },

  /** Confirm behind ↺ / ⌥⇧R: clears the first-read date. */
  unreadConfirm: {
    title: 'Mark as unread?',
    // `readDate` is null when the stamp has no date to name.
    body: (readDate: string | null) =>
      readDate
        ? `This clears the first-read date (${readDate}), returning this paper to unread.`
        : 'This clears the first-read date, returning this paper to unread.',
    revisitCleared: (date: string) =>
      `The last revisit (${date}) will also be cleared — a revisit cannot exist without a first read. This cannot be undone.`,
    confirm: 'Mark unread',
  },

  /** Confirm behind the ⌥D shortcut: sets status to `dropped`. */
  dropConfirm: {
    title: 'Drop this paper?',
    body: (title: string) =>
      `Sets the status of “${title}” to dropped. It stays in the lists, greyed out; change the status back to undo.`,
    confirm: 'Drop',
  },

  /** The status / type / project-grade dropdown (cockpit/fields.tsx). */
  enumSelect: {
    // The empty option of a field that may be unset.
    unset: '— (unset)',
    // Shown, disabled, when the stored value is absent on a field that offers
    // no empty option.
    none: '— (none)',
    // Screen-reader names of the dropdowns. English reuses the metadata key.
    statusAria: 'status',
    typeAria: 'type',
    gradeAria: (project: string) => `priority-${project}`,
  },

  /** The Code-clones row: one chip per cloned repo under codes/<name>/. */
  codeClones: {
    missingTitle: (name: string) =>
      `codes/${name}/ is missing — the clone was deleted. Re-clone it (lit code add) or drop the link (lit code unlink); lit health-check reports it.`,
    // Badge on a chip whose folder is gone (shown in small caps).
    missing: 'missing',
    copyTitle: (name: string) => `Copy the clone's folder path (codes/${name})`,
    upstreamAria: (name: string, url: string) => `Open ${name} upstream: ${url}`,
  },

  /** The Tags group: one pill per field, each opening a value popover. */
  tags: {
    // A field's name inside running text, passed as `noun` to the value
    // popover, Manage dialog and similar-value confirm below.
    nouns: {
      topics: 'topics',
      methods: 'methods',
      data: 'data',
      projects: 'projects',
    },
    // Screen-reader name of a pill; `label` is one of `fields` above.
    pillAria: (label: string, n: number) => `${label} (${n} selected)`,
    // Hover title of a project row's name in the Projects popover.
    unlink: (project: string) => `Unlink ${project}`,
    gradeToLink: (project: string) => `Grade to link this paper to ${project}`,
  },

  /** Confirm when the server finds a new tag too close to registered ones. */
  createSimilar: {
    // Heading, and the dialog's accessible name.
    title: (value: string) => `Create “${value}”?`,
    similarExist: (noun: string) => `Similar ${noun} already exist:`,
    // Title of one listed match; clicking it tags the paper with that value.
    use: (value: string) => `Use ${value}`,
    confirm: 'Create anyway',
  },

  /** Searchable value popover (ui/VocabPopover.tsx), shared with the browse
   * panel's filters. `noun` is a field's name in running text, lowercase. */
  vocabPopover: {
    searchAria: (noun: string) => `Search ${noun}`,
    searchPlaceholder: (noun: string) => `Search ${noun}…`,
    noValues: 'No values yet.',
    noMatch: 'No match.',
    // The row that registers the typed text as a new value.
    create: (value: string) => `Create “${value}”`,
    manage: (noun: string) => `Manage ${noun}…`,
    // Footer count while a search narrows the list.
    filtered: (shown: number, total: number) => `${shown} of ${total}`,
    // Footer count of the whole vocabulary.
    size: (n: number, noun: string) => `${n} ${n === 1 ? noun.replace(/s$/, '') : noun}`,
  },

  /** Dictionary editor for one controlled-vocab field (cockpit/ManageVocab.tsx). */
  manage: {
    title: (noun: string) => `Manage ${noun}`,
    help: "Rename fixes a typo everywhere the value is used. Delete untags a value from every paper.",
    empty: 'No values yet.',
    // Papers tagged with the value.
    usedBy: (n: number) => `used by ${n}`,
    renameAria: (noun: string, value: string) => `Rename ${noun} ${value}`,
    renameTitle: (value: string) => `Rename “${value}”`,
    deleteAria: (noun: string, value: string) => `Delete ${noun} ${value}`,
    deleteTitle: (value: string) => `Delete “${value}”`,
  },

  renameValue: {
    title: (value: string) => `Rename “${value}”?`,
    // `n` = papers carrying the value.
    body: (noun: string, n: number) =>
      n === 0
        ? `Updates the ${noun} vocabulary. This cannot be undone.`
        : n === 1
          ? `Updates the ${noun} vocabulary and re-tags 1 paper. This cannot be undone.`
          : `Updates the ${noun} vocabulary and re-tags ${n} papers. This cannot be undone.`,
    collides: (value: string) =>
      `“${value}” already exists — delete one instead of renaming onto it.`,
    renaming: 'Renaming…',
    confirm: 'Rename',
  },

  deleteValue: {
    title: (value: string) => `Delete “${value}”?`,
    // `n` = papers carrying the value.
    body: (value: string, noun: string, n: number) =>
      n === 0
        ? `This removes “${value}” from the ${noun} vocabulary. It is not attached to any paper.`
        : n === 1
          ? `This removes “${value}” from the ${noun} vocabulary and untags it from 1 paper. This cannot be undone.`
          : `This removes “${value}” from the ${noun} vocabulary and untags it from ${n} papers. This cannot be undone.`,
  },

  /** The Supplementary row's chips (cockpit/Supplementary.tsx). */
  supplementary: {
    // Toast: the SI file was opened with the default app of the server's
    // machine; `host` is that machine's name.
    opened: (file: string, host: string) => `Opened ${file} on ${host}`,
    removeAria: (file: string) => `Remove ${file}`,
    removeTitle: 'Remove',
    add: 'Add SI',
  },

  /** Confirm before an SI file is deleted. */
  siRemove: {
    // Heading, and the dialog's accessible name.
    title: 'Delete this SI file?',
    checking: 'Checking the notes for citations…',
    checkFailed: "Couldn't check the notes for citations.",
    // Where the notes cite the file: "Cited 1× in notes.md and 2× in discussion.md."
    cited: (where: ReadonlyArray<{ count: number; name: string }>) =>
      `Cited ${where.map((c) => `${c.count}× in ${c.name}`).join(' and ')}.`,
    // Note files the server could not read; names like notes.md.
    unchecked: (names: readonly string[]) =>
      `Couldn't check ${names.join(' and ')} for citations.`,
    noUndo: "This can't be undone — add it again with Add SI.",
  },

  /** The Relations row and its remove confirm (cockpit/Relations.tsx). The
   * relation kinds themselves are common.relation. */
  relations: {
    // `rel` is the kind's label from common.relation.
    removeAria: (rel: string, id: string) => `Remove ${rel} link to ${id}`,
    removeTitle: 'Remove link',
    // Unfolds a long group; `n` = links still hidden.
    more: (n: number) => `+ ${n} more`,
    showLess: 'Show less',
    confirmTitle: 'Remove this link?',
    confirm: 'Remove',
  },

  /** The ✎ metadata edit dialog (cockpit/MetadataEditDialog.tsx). */
  metadataEdit: {
    title: 'Edit metadata',
    // Hover title of the id line under the heading.
    idTitle:
      'The id is derived from year + first author + title keyword and is referenced by wikilinks. Changing it is `lit rename`, which updates every reference — not a field edit.',
    // After the paper id on that line; the · separates the two.
    idFixed: '· id is fixed — rename via CLI',
    // Field labels (shown in small caps). DOI stays a literal.
    labels: {
      title: 'Title',
      year: 'Year',
      journal: 'Journal',
      volume: 'Volume',
      issue: 'Issue',
      pages: 'Pages',
      publisher: 'Publisher',
      venueType: 'Venue type',
      booktitle: 'Book title',
    },
    // A typed year of the wrong shape. `hint` is reader.year.hint ("4
    // digits"), the same words shown beside the Year label.
    yearShape: (hint: string, got: string) => `Year must be ${hint} — got '${got}'.`,
    saving: 'Saving…',
    save: 'Save',
  },

  /** The ordered author list editor (ui/AuthorRows.tsx), also in Add paper. */
  authorRows: {
    label: 'Authors',
    // Beside the label.
    orderHint: '— order matters: the first author names the paper',
    dragTitle: 'Drag to reorder',
    placeholder: 'Family, Given',
    moveUpTitle: 'Move up',
    // `n` = the row's position, from 1.
    moveUpAria: (n: number) => `Move author ${n} up`,
    moveDownTitle: 'Move down',
    moveDownAria: (n: number) => `Move author ${n} down`,
    removeTitle: 'Remove this author',
    removeAria: (n: number) => `Remove author ${n}`,
    add: '+ Add author',
  },

  /** The Paper ID line of Add paper (ui/PaperIdField.tsx). */
  paperIdField: {
    // {id} is the derived id, a button that opens it for editing.
    savedAs: 'Saved as {id}',
    chooseTitle: 'Click to choose the id yourself',
    // The label over the input, and the input's accessible name.
    label: 'Paper ID',
    // Beside the label: the characters an id may hold.
    charset: 'A-Z, 0-9, - and _',
    // Placeholder of the keyword box (an id is ASCII only).
    keywordPlaceholder: 'Keyword',
    // Accessible names of the three part boxes.
    partAria: {
      year: 'Paper ID year',
      family: 'Paper ID family',
      keyword: 'Paper ID keyword',
    },
    // Placeholder of the whole-id input: an example id (ASCII only).
    example: '2018_Zhang_Keyword',
    permanent: 'Permanent — `lit rename` changes it later.',
  },

  /** The SI window (ui/AddSi.tsx). */
  addSi: {
    // Eyebrow over the paper title, and the dialog's accessible name.
    heading: 'Supplementary files',
    // Row error for a dropped folder.
    folder: "Folders can't be added — drop the files inside.",
    // Link to the article's page; opens a new tab.
    openWebsite: 'Open website ↗',
    downloadThere: 'Download the SI there, then drop the files here.',
    noLink: 'No link on record — drop the files you downloaded.',
    dropHere: 'Drop SI files here',
    dropHint: 'Checked on drop, added when you press Done',
    // The Done button while it runs.
    adding: 'Adding…',
    // A row's progress, right of its name.
    removing: 'Removing…',
    waiting: 'Waiting…',
    checking: 'Checking…',
    // A file over the size limit waits for a click; `size` like "120 MB".
    large: (size: string) => `Large file (${size}) — upload?`,
    upload: 'Upload',
    // Checkbox on a row the server could not verify.
    addAnyway: 'add anyway',
    removeAria: (name: string) => `Remove ${name}`,
    removeTitle: 'Remove',
  },
}
