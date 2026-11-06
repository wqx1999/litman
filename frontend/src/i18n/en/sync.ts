/** The GUI's cloud sync: the cloud button by the library selector and its
 * popover, the toasts and activity-log lines a sync leaves, and a save that
 * replaced a file a sync (or an agent) had changed.
 *
 * Every line says only what happened: no commands, no remote paths, no
 * rclone output. The details are on disk, where `lit sync status` shows them
 * to the agent the user asks. */

const reposNotSynced = (n: number) => (n === 1 ? '1 code repo not synced' : `${n} code repos not synced`)

export const sync = {
  button: {
    // The button's hover title; its popover says the rest.
    title: 'Cloud sync',
    notConfigured: 'Cloud sync is not set up.',
    syncNow: 'Sync now',
    viewLog: 'View activity log',
  },
  // The popover's one line, by state.
  line: {
    checking: 'Checking…',
    ok: (ago: string) => `Last synced ${ago}.`,
    okUnknown: 'Synced.',
    // The last sync left `n` code repos behind (the activity log names them).
    okRepos: (ago: string, n: number) => `Last synced ${ago}. ${reposNotSynced(n)}.`,
    running: 'Syncing…',
    // `since`: a clock time, "10:07"; null when not known.
    busy: (since: string | null) =>
      since ? `Another sync is in progress (started ${since}).` : 'Another sync is in progress.',
    // `n`: conflicts plus folders whose deletion won; 0 when not known.
    conflicts: (ago: string, n: number) =>
      n > 0
        ? `Last synced ${ago}, with ${n === 1 ? '1 conflict' : `${n} conflicts`}.`
        : `Last synced ${ago}, with conflicts.`,
    // `at`: when the auto sync tries again by itself, "11:35"; null when not
    // known. Not only the network lands here: every failure without a cause
    // of its own is retried the same way, so the line names no cause.
    offline: (at: string | null) =>
      at ? `Sync unavailable. Retrying at ${at}.` : 'Sync unavailable. Retrying soon.',
    notInitialised: 'This machine has not completed its first sync.',
    idle: 'Not synced since launch.',
    // Stopped until the user acts. `name`: the rclone remote, no colon; null
    // when not known.
    auth: (name: string | null) =>
      name ? `${name} credentials expired. Sign in again.` : 'Cloud credentials expired. Sign in again.',
    safetyStop: 'Too many changes on one side. Confirmation required.',
    choice: 'Choose which side wins: this machine or the cloud.',
    // Any other stop (rclone missing or too old, the config, the locks); the
    // stopped toast says this line too.
    other: 'Sync failed. Auto sync stopped.',
  },
  ago: {
    now: 'just now',
    minutes: (n: number) => `${n} min ago`,
    hours: (n: number) => `${n} hr ago`,
    days: (n: number) => (n === 1 ? 'yesterday' : `${n} days ago`),
  },
  toast: {
    // One toast per sync that had something to settle; the rows go to the log.
    synced: (parts: string) => `Sync complete: ${parts}. See the activity log.`,
    // Between the parts of `synced`.
    sep: ', ',
    conflicts: (n: number) => (n === 1 ? '1 conflict' : `${n} conflicts`),
    deletedFolders: (n: number) =>
      n === 1 ? '1 deleted folder overrode edits' : `${n} deleted folders overrode edits`,
    repos: (n: number) => reposNotSynced(n),
    // `cause`: the popover's line for the stop (auth, too many changes, a
    // choice). A stop without a line of its own toasts `line.other`.
    stopped: (cause: string) => `Auto sync stopped. ${cause}`,
    syncNowFailed: 'Could not start sync.',
  },
  // Activity-log rows, one per thing a sync settled.
  log: {
    conflict: (path: string, keptHere: boolean, backup: string) =>
      `Sync conflict: ${path} changed here and in the cloud. Kept the ${keptHere ? 'local' : 'cloud'} version; the other is backed up to ${backup}`,
    deletedFolder: (folder: string, here: boolean, backup: string) =>
      `Sync: ${folder} was deleted ${here ? 'on this machine' : 'on another machine'}. Edits from the other side are backed up to ${backup}`,
    // `kind`: `not-restored` (not at the commit the library records),
    // `left-behind` (removed from the library, its folder kept on this
    // machine) or `failed` (the repo step itself failed; `name` is empty).
    repo: (name: string, kind: string) =>
      kind === 'failed' || !name
        ? 'Sync: code repos could not be synced'
        : kind === 'left-behind'
          ? `Sync: code repo ${name} was removed from the library; its folder remains on this machine`
          : `Sync: code repo ${name} not synced`,
  },
  // A save over a file changed since it was opened: a short toast, and the
  // backup's full path in the activity log.
  replaced: (file: string) =>
    `${file} changed after you opened it. Your version was saved and the replaced version backed up. See the activity log.`,
  replacedLog: (file: string, backup: string) =>
    `Saved over ${file}; the replaced version is backed up to ${backup}`,
}
