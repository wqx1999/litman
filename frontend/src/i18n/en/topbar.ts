/** UI text: topbar area. See ../index.ts for the rules. */
export const topbar = {
  /** Place names the server sends with a folder listing (the `anchors` of GET
   * /api/fs/list): the chips of the folder picker and the location card of the
   * new-vault dialog. The KEY is the server's label (it also picks the icon);
   * only the value is shown. A folder that is not one of these shows its own
   * name. */
  places: {
    Home: 'Home',
    Desktop: 'Desktop',
    Documents: 'Documents',
    Downloads: 'Downloads',
  },

  /** The toolbar itself: tooltips (title) and screen-reader names (…Label) of
   * its buttons, the vault selector, the update chip by the logo. */
  topBar: {
    whatsNewTitle: "litman — what's new in this version",
    whatsNewLabel: "What's new in this version",
    // Update chip: its tooltip, and the heading of the popover it opens.
    updateAvailable: (version: string) => `litman ${version} is available`,
    // Screen-reader name of the update chip.
    updateChipLabel: (version: string) => `Update available: litman ${version}`,
    // Under the popover heading; `version` is the one running now.
    youHave: (version: string) => `You have ${version}.`,
    updateStarting: 'Starting…',
    updateAndRestart: 'Update & restart',
    // Replaces the update button when the update could not start and the
    // server gave no reason of its own.
    updateStartFailed:
      'Could not start the update — run `lit self-update` in a terminal.',
    // Full-screen overlay while the app closes itself to update.
    updatingTitle: 'Updating litman…',
    updatingBody:
      'This window will close, and litman reopens by itself when the update finishes. If the window stays open, you can close it yourself.',
    // Tooltip of the vault selector: the vault shown was opened by a path that
    // is not in the registry.
    vaultSelectLoose: 'Opened by path, not in your vault list',
    vaultSelectSwitch: 'Switch the active vault (applies globally)',
    vaultSelectOnlyOne: 'Only one vault registered',
    vaultSelectNone: 'No vault registered',
    // One entry of the vault selector: the vault's name, then " (active)" when
    // it is the registry's active vault but not the one on screen, and
    // " — missing" when its folder is gone.
    vaultOption: (name: string, active: boolean, missing: boolean) =>
      `${name}${active ? ' (active)' : ''}${missing ? ' — missing' : ''}`,
    // The vault selector's only entry when nothing is registered.
    noVault: 'no vault',
    manageVaultsTitle: 'Register or unregister vaults',
    manageVaultsLabel: 'Manage vaults',
    projectsTitle: 'Register or remove projects',
    // Toolbar button, next to a folder icon.
    projects: 'Projects',
    // (R) is the key that does the same.
    refreshTitle: 'Refresh from disk (R) — pull in changes made via the CLI',
    refreshLabel: 'Refresh from disk',
    activityLogTitle: 'Activity log — recent actions this session',
    activityLogLabel: 'Activity log',
    healthTitle: 'Health check — audit library consistency',
    healthLabel: 'Health check',
    // Agent button. "~" and "Ctrl+~" are keys; keep them.
    agentSetupTitle: 'Set up your AI agent',
    agentLaunchTitle: 'Launch default agent — ~ · Right-click or Ctrl+~ to manage',
    agentSetupLabel: 'Set up agent',
    agentLaunchLabel: 'Launch agent',
    // Screen-reader name of the red dot on the agent button.
    agentSetupNeeded: 'Agent setup needed',
    focusExit: 'Exit focus mode',
    focusEnter: 'Focus mode — hide side panels',
    lightMode: 'Switch to light mode',
    darkMode: 'Switch to dark mode',
    // (?) is the key that does the same.
    shortcutsTitle: 'Keyboard shortcuts (?)',
    shortcutsLabel: 'Keyboard shortcuts',
    // Toasts of the agent button. `agent` is the agent's command name (claude,
    // codex…); `display` its display name (Claude Code…).
    launchedInTerminal: (agent: string) => `Launched ${agent} in a terminal window`,
    openedApp: (display: string) => `Opened ${display}`,
    skillInstalledDefault: (agent: string) =>
      `Installed the skill for ${agent}; it is now your default agent`,
    skillInstalled: 'Agent skill installed or updated',
    nowDefault: (agent: string) => `${agent} is now your default agent`,
  },

  /** The search box in the toolbar and its typeahead dropdown. */
  searchBox: {
    placeholder: 'Search id, title, notes, discussion…',
    clearLabel: 'Clear search',
    clearTitle: 'Clear search (Esc)',
    searching: 'Searching…',
    noMatches: 'No matches.',
    // Dropdown footer: how many more matches the middle list holds than the
    // dropdown shows (always at least 1).
    moreInList: (n: number) => `+${n} more in the list`,
    // Dropdown footer while the notes / discussion search is still running.
    searchingNotes: 'Searching notes…',
    // Small tag on each result naming the field that matched; shown uppercased.
    scope: {
      id: 'id',
      title: 'title',
      author: 'author',
      doi: 'doi',
      year: 'year',
      notes: 'notes',
      discussion: 'discussion',
    },
  },

  /** The vault manager panel: the list of registered vaults. */
  vaultManager: {
    title: 'Vaults',
    // The (?) hint next to the title.
    help: 'Create or register vaults, and switch between them from the toolbar. Unregistering never deletes the folder.',
    empty: 'No vaults registered.',
    // Badges on a vault row; shown uppercased.
    active: 'active',
    missing: 'missing',
    missingTitle: 'No library at this path — the folder was moved, renamed or deleted.',
    // Button on a missing vault's row: point the registry at the new folder.
    locate: 'Locate',
    locateLabel: (name: string) => `Locate vault ${name}`,
    locateTitle: (name: string) => `Point “${name}” at its new folder`,
    // The trash-can button of a row: its screen-reader name and tooltip.
    unregisterLabel: (name: string) => `Unregister vault ${name}`,
    unregisterTitle: (name: string) => `Unregister vault “${name}”`,
    // Tooltip of that button, disabled, on the vault being shown.
    unregisterServedTitle: 'Switch to another vault first',
    newVault: '+ New vault…',
    registerExisting: '+ Register existing…',
  },

  /** Confirm dialog: unregister a vault. */
  unregisterVault: {
    title: (name: string) => `Unregister vault “${name}”?`,
    // {name} = the vault's name; {keep} = `keepsFolder` below, in bold.
    body: 'This removes “{name}” from the vault registry only. {keep} — only the registry entry is removed. You can register it again later.',
    keepsFolder: 'The vault directory on disk is NOT deleted',
    unregistering: 'Unregistering…',
    unregister: 'Unregister',
  },

  /** Dialog: point a moved vault at its new folder. */
  locateVault: {
    title: (name: string) => `Locate “${name}”`,
    // {path} = the folder the vault used to be in, shown struck through.
    body: 'This library moved from {path}. Paste where it lives now — the vault folder itself, which must contain a lit-config.yaml.',
    // Label above the path field (shown uppercased).
    pathLabel: 'New vault path',
    locating: 'Locating…',
    locate: 'Locate',
  },

  /** Dialog: register a vault folder that already exists. */
  registerVault: {
    title: 'Register existing vault',
    intro:
      'Adds an existing litman vault to the registry (nothing is created). The path must already contain a lit-config.yaml.',
    // Field labels (shown uppercased) and the hint under the path field.
    nameLabel: 'Name',
    pathLabel: 'Vault path',
    pathHint: 'the vault folder itself, must exist + contain lit-config.yaml',
    switchAfter: 'Switch to it after registering',
    registering: 'Registering…',
    register: 'Register',
  },

  /** Dialog: create a new vault. */
  createVault: {
    title: 'New vault',
    intro:
      'Creates the vault folder and registers it. The location must already exist; the vault folder itself must not.',
    // Field labels (shown uppercased) and the hint under the location field.
    locationLabel: 'Location',
    locationHint: 'the folder to create the vault in',
    nameLabel: 'Name',
    switchAfter: 'Switch to it after creating',
    creating: 'Creating…',
    create: 'Create vault',
  },

  /** Confirm dialog: switch the active vault. */
  switchVault: {
    title: (name: string) => `Switch vault to “${name}”?`,
    // {globally} = `globally` below, in medium weight. {lit}, {library} and
    // {env} are the code `lit`, `--library` and `$LIT_LIBRARY`. `openTabs` =
    // how many tabs are open; a switch closes them all.
    body: (openTabs: number) =>
      `This changes the active vault {globally} — it affects {lit} in every terminal without an explicit {library} / {env}.${
        openTabs > 0
          ? openTabs === 1
            ? ' All 1 open tab will close.'
            : ` All ${openTabs} open tabs will close.`
          : ''
      }`,
    globally: 'globally',
    // Amber note; `n` = tabs with unsaved changes (at least 1).
    unsaved: (n: number) =>
      n === 1
        ? '1 tab has unsaved changes — they will be saved before switching.'
        : `${n} tabs have unsaved changes — they will be saved before switching.`,
    switching: 'Switching…',
    saveAndSwitch: 'Save & switch',
    switch: 'Switch',
  },

  /** The health-check report panel. */
  healthPanel: {
    title: 'Health check',
    running: 'Running…',
    rerun: 'Re-run',
    // {command} = " lit health-check --fix" in monospace; its leading space is
    // inside the code span, so none goes before the slot.
    intro: 'Audits library consistency (read-only). Fixes still run from the CLI:{command}.',
    runningChecks: 'Running checks…',
    noIssues: '✓ No issues',
    // Severity tag of one finding; shown uppercased. Keys are the server's values.
    severity: {
      error: 'error',
      warning: 'warning',
      info: 'info',
    },
  },

  /** The session activity-log panel. */
  activityLog: {
    title: 'Activity log',
    intro: 'Recent actions this session (newest first). Cleared when the page reloads.',
    empty: 'No activity yet.',
  },

  /** The box shown when the server could not open a terminal for the agent:
   * the command to run by hand, with a Copy button. */
  agentCopy: {
    title: 'Run it in a terminal',
    // `agent` = the agent's command name (claude, codex…); the command line to
    // copy follows under this text.
    body: (agent: string) =>
      `litman couldn't open a terminal window from here. Open one on the machine running litman — if you're connected to a remote server, that's the server, not this computer — then start ${agent} with:`,
    copy: 'Copy',
    copied: 'Command copied',
    copyFailed: 'Copy failed — select the text and copy it',
  },

  /** The agent panel: agents on the left, the selected one's settings on the
   * right. */
  agentSetup: {
    titleManage: 'Manage agents',
    titleSetup: 'Set up your agent',
    // The (?) hint next to the title. "~" is a key; keep it.
    help: 'The toolbar button and ~ open your default agent. A skill also lets it run lit without asking, where the agent allows it.',
    // Badge on the default agent's row in the list.
    defaultBadge: 'Default',
    recheck: 'Installed it just now? Recheck',
    // Install link for an agent that starts two ways (desktop app or terminal
    // CLI), when the chosen way is not installed.
    getApp: 'Get the app →',
    getCli: 'Get the CLI →',
    // Install link for an agent that starts one way; `display` = its name.
    getAgent: (display: string) => `Get ${display} →`,
    launch: 'Launch',
    // Row labels (left) of the settings list, and their values (right).
    comingSoon: 'Coming soon',
    launchWith: 'Launch with',
    // The two-way control in the "Launch with" row.
    modeApp: 'App',
    modeCli: 'CLI',
    notInstalled: 'Not installed',
    openOnce: 'Open it once after installing.',
    skill: 'Skill',
    upToDate: 'Up to date',
    updateAvailable: 'Update available',
    updating: 'Updating…',
    updateSkill: 'Update skill',
    installing: 'Installing…',
    installSkill: 'Install skill',
    // Installs the skill AND makes this agent the default.
    installSkillAndUse: 'Install skill & use',
    defaultAgent: 'Default agent',
    // "~" is a key; keep it.
    opensWith: 'Opens with the toolbar button or the ~ key',
    setDefault: 'Set default',
  },

  /** The in-app folder picker (opened by a path field's Browse…). */
  directoryPicker: {
    title: 'Choose a folder',
    showHidden: 'Show hidden',
    // Screen-reader name of the drive-letter buttons (Windows).
    drivesLabel: 'Drives',
    up: 'Up one level',
    // Tooltip of the breadcrumb path bar; a click turns it into a text field.
    editPathTitle: 'Click to edit the path',
    editPath: 'Edit path',
    newFolder: 'New folder',
    newFolderPlaceholder: 'New folder name',
    // Creates the folder typed in the new-folder field.
    create: 'Create',
    loading: 'Loading…',
    denied: 'Can’t open this folder.',
    empty: 'No subfolders here.',
    // Tooltip of a folder that cannot be opened because of its name.
    undecodableTitle: 'This folder’s name is not UTF-8 text. Rename it to open it here.',
    // Badge on a folder that is a litman vault; shown uppercased.
    vaultBadge: 'litman library',
    // Footer note when only a vault folder may be chosen.
    notVault: 'This folder isn’t a litman library.',
    select: 'Select this folder',
  },

  /** A path text field with its Browse… button. */
  pathField: {
    browse: 'Browse…',
  },
}
