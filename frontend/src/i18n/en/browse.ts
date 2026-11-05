/** UI text: browse area — the app shell (toasts, banners, the activity log's
 * change lines), the left paper list, the trash view, the projects manager, the
 * welcome page and the drag-in add dialog. See ../index.ts for the rules. */

/** A per-paper tag field, as the resync change log names it. */
type TagField = 'topics' | 'methods' | 'data' | 'projects'
/** A controlled-vocabulary list in TAXONOMY.md. */
type TaxonomyField = 'topics' | 'methods' | 'data'

// The singular field word inside a per-paper tag change, e.g. `X: +topic Y`.
const TAG_FIELD: Record<TagField, string> = {
  topics: 'topic',
  methods: 'method',
  data: 'data',
  projects: 'project',
}

export const browse = {
  // Shared by the paper list and the trash list while their list loads.
  loading: 'Loading…',
  // Toast when a paper shortcut (P, Shift+N, Shift+D, Alt+…) has no selected
  // paper to act on.
  noPaperSelected: 'No paper selected',
  // Row buttons that open a paper's notes.md / discussion.md (paper list, trash).
  openNotes: '📝 notes',
  openDiscussion: '💬 discussion',
  // Tab title of an open notes / discussion document: the paper id, then which
  // document. (A PDF tab shows the bare id.)
  docTab: {
    notes: (id: string) => `${id} · notes`,
    discussion: (id: string) => `${id} · discussion`,
  },

  /** App.tsx: toasts, the three top banners, the md-tab save prompt. */
  app: {
    // Sticky toast after a failed one-click update. `reason` is the server's.
    selfUpdateFailed: (reason: string) =>
      `Automatic update failed (${reason}). You are still on the previous version — run \`lit self-update\` in a terminal.`,
    // Toast after the SI window's Done.
    siAdded: (n: number) => (n === 1 ? 'Added 1 SI file' : `Added ${n} SI files`),
    // The same, when some files came with warnings (the warnings themselves go
    // to the activity log).
    siAddedWithWarnings: (n: number, warnings: number) =>
      `${n === 1 ? 'Added 1 SI file' : `Added ${n} SI files`} · ${
        warnings === 1 ? '1 warning' : `${warnings} warnings`
      } in the activity log`,
    refreshed: 'Refreshed from disk.',
    // A [[vault:id]] wikilink was clicked; the GUI shows one vault at a time.
    crossVaultLink: 'Cross-vault link — open that vault to follow it.',
    // A [[id]] wikilink to a paper this vault does not have.
    noSuchPaper: (id: string) => `No paper "${id}" in this vault.`,
    removedToTrash: (id: string) =>
      `Removed “${id}” to trash · restore from the 🗑 Trash`,
    vaultRegistered: (name: string) => `Registered vault “${name}”.`,
    vaultCreated: (name: string, path: string) =>
      `Created vault “${name}” at ${path}.`,
    vaultUnregistered: (name: string) => `Unregistered vault “${name}”.`,
    vaultRelocated: (name: string, path: string) =>
      `Re-pointed vault “${name}” to ${path}.`,
    // Toast after restoring a paper from the trash. Counts are the links the
    // restore rebuilt (any may be 0 and is then left out); `missingRepos` are
    // code repos that need `lit code restore-all` (the toast then stays up).
    restored: (r: {
      id: string
      reverseEdges: number
      repos: number
      projects: number
      missingRepos: string[]
    }) => {
      const parts: string[] = []
      if (r.reverseEdges) parts.push(`${r.reverseEdges} reverse edge(s)`)
      if (r.repos) parts.push(`${r.repos} repo(s)`)
      if (r.projects) parts.push(`${r.projects} project(s)`)
      let msg = `Restored “${r.id}”`
      if (parts.length) msg += ` · rebuilt ${parts.join(', ')}`
      if (r.missingRepos.length)
        msg += ` · ⚠ code not cloned again: ${r.missingRepos.join(', ')} — run lit code restore-all`
      return msg
    },
    // Toast after the drag-in add dialog added a paper.
    paperAdded: (id: string) => `Added ${id}`,
    // Toast after the Alt+R shortcut marked the selected paper read.
    markedRead: 'Marked read · Alt+Shift+R to undo',
    // Red banner: the vault folder vanished while the app was open. {path} is
    // the old folder path (monospace).
    vaultGone: 'This library is no longer at {path} — it was moved, renamed or deleted.',
    // Button on that banner; opens the vault manager to re-point the library.
    findIt: 'Find it',
    // Amber banner while the server is unreachable.
    disconnected: "Can't reach the litman server — retrying…",
    // Advisory on a drive that cannot store folder links (FAT32 / exFAT /
    // network share). {views} is the folder name `views/` (monospace);
    // {reassurance} is `linkNoticeFine`, set in a softer grey.
    linkNotice:
      "This drive can't hold folder links, so {views} and the project shortcuts aren't being made. {reassurance}",
    linkNoticeFine:
      'Your library is fine — every command, this app and the agent workflow all work normally.',
    linkNoticeDetail:
      'Usual on USB sticks (FAT32 / exFAT) and network drives; a library on an internal drive gets the shortcuts automatically.',
    // The advisory's ✕ button.
    dismiss: 'Dismiss',
  },

  /** App.tsx: one activity-log line per change made outside the GUI (a `lit`
   * command, an agent), found on a refresh from disk. */
  resyncLog: {
    paperAdded: (id: string) => `+ ${id} added`,
    paperTrashed: (id: string) => `🗑 ${id} trashed`,
    paperRemoved: (id: string) => `− ${id} removed`,
    // `from` / `to` are status labels, or — for none.
    statusChanged: (id: string, from: string, to: string) =>
      `${id}: status ${from} → ${to}`,
    typeChanged: (id: string, from: string, to: string) =>
      `${id}: type ${from} → ${to}`,
    markedRead: (id: string) => `${id}: marked read`,
    readDateCleared: (id: string) => `${id}: read-date cleared`,
    // A tag attached to / removed from a paper: `X: +topic protein-design`.
    // The field word comes from TAG_FIELD above.
    tagAdded: (id: string, field: TagField, value: string) =>
      `${id}: +${TAG_FIELD[field]} ${value}`,
    tagRemoved: (id: string, field: TagField, value: string) =>
      `${id}: −${TAG_FIELD[field]} ${value}`,
    // A value added to / removed from a TAXONOMY.md list.
    taxonomyAdded: (field: TaxonomyField, value: string) =>
      `taxonomy: +${field} ${value}`,
    taxonomyRemoved: (field: TaxonomyField, value: string) =>
      `taxonomy: −${field} ${value}`,
    projectAdded: (name: string) => `project: + ${name}`,
    projectRemoved: (name: string) => `project: − ${name}`,
    notesUpdated: (id: string) => `${id}: notes updated`,
    discussionAppended: (id: string) => `${id}: discussion appended`,
  },

  /** nav/BrowsePanel.tsx: the left column. */
  browsePanel: {
    // The three-way "Show" switch above the list.
    listMode: {
      reading: '📖 Reading',
      'recent-read': '✅ Read',
      all: 'All',
    },
    // Filter group headers (shown uppercase).
    dimension: {
      status: 'Status',
      type: 'Type',
      topics: 'Topics',
      methods: 'Methods',
      data: 'Data',
    },
    // The same fields in running text inside a filter popover ("Search topics…").
    dimensionNoun: {
      status: 'status',
      type: 'type',
      topics: 'topics',
      methods: 'methods',
      data: 'data',
    },
    // Hover text / screen-reader name of the status dot, which toggles a pin.
    pinTitle: 'Pin to the top of the list',
    unpinTitle: 'Unpin (send back to its place in the list)',
    pinAria: (id: string) => `Pin ${id}`,
    unpinAria: (id: string) => `Unpin ${id}`,
    // Hover text of the "dropped" badge on a muted row.
    droppedTitle: 'Dropped — evaluated and set aside (kept as a record)',
    removeTitle: 'Remove paper from library (move to trash)',
    removeAria: 'Remove paper from library',
    expandTitle: 'Expand browse panel',
    collapseTitle: 'Collapse browse panel',
    // First option of the project scope dropdown.
    allProjects: 'All projects',
    // Label above the Show switch (shown uppercase).
    show: 'Show',
    // Rows in the list right now, beside the "Show" label.
    paperCount: (n: number) => (n === 1 ? '1 paper' : `${n} papers`),
    // The collapsible filter section header (shown uppercase).
    filter: 'Filter',
    clearAll: 'Clear all',
    removeFilterTitle: 'Remove filter',
    noTags: 'No tags to filter yet.',
    loadFailed: "Couldn't reach the server — papers can't load.",
    // Header of the pinned group; `n` is how many pinned rows show (0 = none
    // match the current view, the header still shows).
    pinned: (n: number) => (n > 0 ? `📌 Pinned · ${n}` : '📌 Pinned'),
    unpinAllTitle: 'Unpin all pinned papers',
    pinsHidden: (n: number) =>
      `${n === 1 ? '1 pin is' : `${n} pins are`} hidden by the current view`,
    noMatch: 'No papers match.',
    // The getting-started card of an empty vault. The command between
    // `emptyFromTerminal` and `emptyOrAgent` is shown as is.
    emptyTitle: 'No papers in your vault yet.',
    emptyFromTerminal: 'Add your first paper from the terminal:',
    emptyOrAgent: '…or ask your coding agent to add it for you.',
    // "add" is the `lit add` command.
    emptyMovesSource: 'add moves the source PDF into the vault.',
    trashTitle: 'Open the trash (recover deleted papers) · capped at 100',
    trash: 'Trash',
  },

  /** trash/TrashView.tsx: the full-screen trash (recovery) view. */
  trash: {
    back: '← Back to library',
    // The amber banner. `vault` is the vault name ('' = unknown, left out);
    // `n` the entries in the trash, which holds at most 100.
    banner: (vault: string, n: number) =>
      `🗑 Trash${vault ? ` · ${vault}` : ''} · ${n} / 100 · restores back here (oldest auto-removed past 100)`,
    filterPlaceholder: 'Filter by id or title…',
    filterAria: 'Filter trash',
    empty: 'Trash is empty.',
    noMatch: 'No match.',
    restoring: 'Restoring…',
    restore: '↩ Restore',
    noTabs: 'Open a trashed paper from the list (read-only).',
    // Badge on each read-only tab.
    tabBadge: 'Trash',
    closeTab: 'Close tab',
    noPreview: 'Select a trashed paper to preview it (read-only).',
  },

  /** projects.ts: the marker on a project whose registration is broken (the
   * projects manager row, the paper's project picker). `badge` shows
   * uppercase; `title` is its hover text. */
  projectHealth: {
    missing: {
      badge: 'missing',
      title:
        'No folder at this path — it was moved, renamed or deleted. Re-point the project at its new location.',
    },
    configOnly: {
      badge: 'incomplete',
      title:
        'Has a folder in lit-config.yaml but is not listed in TAXONOMY.md. Tagging still works, but the tag lands outside the taxonomy — list it in TAXONOMY.md to repair.',
    },
    taxonomyOnly: {
      badge: 'incomplete',
      title:
        'Listed in TAXONOMY.md but has no folder in lit-config.yaml. It points nowhere until one is set.',
    },
  },

  /** topbar/ProjectManager.tsx: the Projects manager and its dialogs. */
  projectManager: {
    title: 'Projects',
    help: 'Register, rename or re-point your projects. Removing one never deletes its folder.',
    empty: 'No projects registered.',
    // Shown in place of the path of a project with no folder.
    noFolder: 'no folder set',
    // Papers tagged with the project, on its row.
    paperCount: (n: number) => (n === 1 ? '1 paper' : `${n} papers`),
    renameAria: (name: string) => `Rename project ${name}`,
    renameTitle: (name: string) => `Rename project “${name}”`,
    setPathAria: (name: string) => `Change path for project ${name}`,
    setPathTitle: (name: string) => `Re-point “${name}” at a new folder`,
    deleteAria: (name: string) => `Delete project ${name}`,
    deleteTitle: (name: string) => `Delete project “${name}”`,
    newProject: '+ New project',
    removed: (name: string) => `Removed project “${name}”.`,
    // Sticky toast: the project is gone, but some of its link folders could
    // not be removed. `reason` is the server's.
    removedWithLeftovers: (
      name: string,
      folders: ReadonlyArray<{ path: string; reason: string }>,
    ) =>
      `Removed project “${name}” · could not clean up ${folders
        .map((f) => `${f.path} (${f.reason})`)
        .join(' · ')} — delete ${folders.length === 1 ? 'its' : 'their'} links by hand`,
    renamed: (name: string) => `Renamed project to “${name}”.`,
    repointed: (name: string, path: string) =>
      `“${name}” now points at ${path}. If the folder wasn't moved with its links, rebuild views.`,
    alreadyPointed: (name: string) => `“${name}” already points there.`,
    linked: (name: string) => `Linked project “${name}”.`,
    deleteConfirmTitle: (name: string) => `Delete project “${name}”?`,
    // `count` = papers tagged with the project (0 leaves that clause out).
    deleteConfirmBody: (name: string, count: number) =>
      `This unregisters “${name}”${
        count > 0
          ? ` and unlinks it from ${count} paper${count === 1 ? '' : 's'}`
          : ''
      } and removes its reflib links + REFERENCES.md. The project folder on disk is kept. This cannot be undone.`,
    renameDialogTitle: (name: string) => `Rename “${name}”?`,
    // `count` = papers re-tagged (0 leaves that clause out).
    renameBody: (count: number) =>
      `Renames it everywhere${
        count > 0 ? ` — re-tags ${count} paper${count === 1 ? '' : 's'}` : ''
      }. The folder on disk and its path are unchanged.`,
    nameTaken: (name: string) => `“${name}” already exists — pick another name.`,
    renaming: 'Renaming…',
    rename: 'Rename',
    setPathDialogTitle: (name: string) => `Path for “${name}”`,
    setPathBody:
      'Points the registry at where the folder lives now. This does NOT move the folder — move it yourself first, then paste the new location here.',
    // Field label (shown uppercase), in both the path and the new-project dialog.
    absolutePath: 'Absolute path',
    setPathHint: 'the folder itself, must already exist',
    saving: 'Saving…',
    savePath: 'Save path',
    newDialogTitle: 'New project',
    newDialogBody: 'Links a name to an existing folder (nothing is created).',
    // Field label (shown uppercase).
    name: 'Name',
    newPathHint: 'the folder itself, must exist',
    creating: 'Creating…',
    create: 'Create',
  },

  /** welcome/WelcomePage.tsx: the first-run page (no library yet). */
  welcome: {
    title: 'Create your library',
    subtitle: 'litman keeps your papers in one folder on disk.',
    location: 'Location',
    name: 'Name',
    creating: 'Creating…',
    create: 'Create library',
    openExisting: 'Or open an existing library',
    open: 'Open',
    // Badge (shown uppercase) on a registered library whose folder is gone.
    moved: 'moved',
    locate: 'Locate',
    forget: 'Forget',
    forgetConfirm: 'Remove from the list? Nothing on disk is deleted.',
    remove: 'Remove',
  },

  /** ui/AddPaper.tsx: the confirm dialog after a PDF is dropped on the window. */
  addPaper: {
    // Dialog heading (shown uppercase) and its screen-reader name.
    title: 'Add paper',
    doiPlaceholder: 'DOI (e.g. 10.1093/nar/gkab813)',
    lookUp: 'Look up',
    lookingUp: 'Looking up…',
    enterManually: "No DOI, or CrossRef doesn't have it? Enter the details yourself →",
    backToDoi: '← Look up a DOI instead',
    // Field labels of the hand-entry form (shown uppercase).
    titleLabel: 'Title',
    yearLabel: 'Year',
    journalLabel: 'Journal / venue',
    typeLabel: 'Type',
    // Placeholder of the hand-entry DOI field.
    manualDoiPlaceholder: 'if it has one',
    // The venue-type dropdown. Keys are the values written to metadata.yaml.
    venueType: {
      'journal-article': 'Journal article',
      'proceedings-article': 'Conference paper',
      preprint: 'Preprint',
      patent: 'Patent',
      book: 'Book',
      'book-chapter': 'Book chapter',
      dissertation: 'Thesis',
      report: 'Report',
    },
    // Author line of the looked-up record.
    noAuthors: '(no authors)',
    manyAuthors: (first: string, n: number) => `${first} et al. (${n} authors)`,
    noTitle: '(no title)',
    // {id} is a button that opens the paper already in the vault.
    alreadyInLibrary: 'Already in your library: {id}',
    originalStays: 'Your original PDF stays where it is.',
    adding: 'Adding…',
    add: 'Add to library',
  },

  /** ui/DropZone.tsx: the overlay while a PDF is dragged over the window. */
  dropZone: {
    // Toast when several files are dropped at once.
    onePaperOnly: 'One paper at a time.',
    reading: 'Reading PDF…',
    lookingForDoi: 'Looking for a DOI',
    drop: 'Drop PDF to add',
    onePaperHint: 'One paper at a time',
  },
}
