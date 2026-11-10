# lit-library — cloud sync: one library on several computers

Read this when the user wants their library on more than one computer or kept in a cloud drive, at the start of a session when `lit sync status --local` says the library syncs two-way (daily use below), or when sync reports trouble: a `Cloud sync stopped …` line after a `lit` command, a `lit sync` exit other than `0`, or the user saying the cloud button in the GUI is red, amber or stopped. SKILL.md's Rules hold here too; "[H]", "[I]" and "Rule N" refer to SKILL.md.

🔴 **Never add `--force` or `--first-from` on your own**, and never run `rclone bisync` (or its `--resync`) yourself: `lit sync` is the only way the library syncs. Say what the flag would do, wait until the user chooses it, then run it. 🔴 **A password, or the token `rclone authorize` prints, never passes through you** — not in the chat, not in a command you run: the user types it into `rclone config` in a terminal of their own.

## Set up — the first computer

1. **Ask which cloud the user has.** Any cloud rclone supports works: OneDrive, Google Drive, Dropbox, or a WebDAV service such as 坚果云. Baidu Netdisk (百度网盘) is not one of them.
2. **rclone 1.74.2 or newer** (`rclone version`). Missing or older → install or update it on the user's OK: Windows `winget install Rclone.Rclone`, macOS `brew install rclone`, Linux the official script `sudo -v ; curl https://rclone.org/install.sh | sudo bash`, which asks for the user's password, so they run it (a distro package is usually too old; without sudo, the binary from https://rclone.org/downloads/ in a folder on `PATH`). An older one updates the way it came: `winget upgrade Rclone.Rclone`, `brew upgrade rclone`, or `rclone selfupdate`.
3. **Create the remote**, under a name the user picks (`onedrive`, say):
   - OneDrive, Google Drive, Dropbox: `rclone config create '<name>' onedrive` (or `drive`, `dropbox`). A browser opens; the user signs in and clicks Allow, then the command returns. For Google Drive a newer rclone first asks "Continue using the shared client_id anyway?": the user answers `y`, as `n` leads to asking for a client id of their own. If your shell tool stops it first, the user runs the same line in a terminal of their own.
   - WebDAV, e.g. 坚果云: the user runs `rclone config` in a terminal of their own and adds a `webdav` remote: URL `https://dav.jianguoyun.com/dav/`, vendor `other`, their account e-mail, and the app password 坚果云 issues for third-party apps (not the login password), typed at rclone's prompt.
   - A computer with no browser (a server over ssh): the user runs `rclone config` there and answers `n` to "Use web browser…"; it prints an `rclone authorize …` line to run on a computer that has a browser and rclone, and the user pastes the result back at its prompt; tell them beforehand that it goes only there, never into the chat.
4. **`lit sync setup --remote '<name>' --path '<folder>'`**: a folder inside the remote (`litman`, say), never its top level. Backups go next to it, into `<folder>-litman-backup`.
5. **The first `lit sync`.** An empty cloud folder takes this library; a new, empty library takes the cloud's. Relay the summary. Exit `20` (`both hold a library already`) → explain, then wait for the user's choice: `lit sync --first-from local` keeps this computer's copy of every file that differs, `lit sync --first-from cloud` the cloud's; the other copies go to the backup, and files only one side has are kept on both.

**Each further computer:** litman installed there (by the user, Rule 8), an empty library (`lit init`), rclone and a remote on the same cloud account (steps 2–3), `lit sync setup` with the same `--path` (the user doesn't know it → `rclone lsf -R --max-depth 2 --files-only --include '.litman-sync-stamp.json' '<name>:'` lists the cloud folders that hold a synced library; they pick), then `lit sync`: the library comes down, and code repos follow by commit (below). A library already there → exit `20`, step 5.

## Daily use

**Is it on?** Once a session, `lit sync status --local` (this computer's records, no network) gives the `Mode`: `two-way` → the rules below; `one-way (push / pull)` → a one-way backup, never `lit sync` there unasked; `not synced from this machine yet` → its first `lit sync` is step 5: offer it, don't run it unasked.

- **While the GUI runs** (`lit gui` or the litman app window), it syncs by itself: on start, about a minute after changes, when another computer has synced, and as it closes. Its cloud button shows the state.
- **You run `lit sync`** at the start of the session and once you have finished changing the library (notes, discussion and stamps written while reading count too) — once per task, not after every command. Don't ask each time: the user's setting up sync is their instruction for it, the exception Rule 11 names. Never with `--force` or `--first-from` this way. In a sandbox (DeepSeek Harness, say) the user may have to allow your first `lit sync` once, as it writes outside the library, to rclone's config and litman's data folder.
- **A GUI syncing at the same time does no harm**: `lit sync` waits for its run, and exit `23` after 90 s only means busy — try again shortly.
- **`lit sync push` / `lit sync pull` refuse a library that syncs two-way**: never suggest them for one.
- **`No folder on this machine for project …`** in the summary → ask the user for the folder, then the `lit project set-path` it names ([H]).

## Stop syncing on a computer

The user wants to stop syncing here, leave this computer, or clean up a test sync → `lit sync disconnect`, then relay what it lists. It changes nothing in the library and needs no network; the cloud folders, the rclone remote and the backups stay. Run an `rclone purge` or `rclone config delete` it names only on the user's yes, and first say that a computer still syncing with that folder would see every file deleted from the cloud.

## Trouble

Exit codes: `0` synced · `21` synced, with conflicts · `22` synced, a code repo here differs · `20` nothing synced, a side must be picked · `23` busy · `1` failed.

A `Cloud sync stopped …` or `Cloud sync failed …` line after any `lit` command, a failed `lit sync`, or the user saying the cloud button is red, amber or stopped → **run `lit sync status` first**: it holds the whole reason, the backups and a `What to do:` line. Then explain it to the user in plain words, and fix it:

- **Sign-in refused** (an expired OAuth token, or a password the cloud no longer accepts) → `What to do:` names the way back for this remote. OneDrive, Google Drive, Dropbox and the like: on the user's OK, `rclone config reconnect '<remote>:'`; the browser opens and the user clicks Allow (no browser → they run it themselves, as in step 3). WebDAV and other password remotes: the user types the password into `rclone config` (edit the remote) in a terminal of their own. Then `lit sync`.
- **Safety stop** (more than half of the files deleted on one side, or every file changed) → say which side and what would go; `lit sync --force` only on the user's yes.
- **A side must be picked** (exit `20`; the message says why and names the flag) → explain what each choice keeps and what goes to the backup, as in step 5, and wait for the user's choice.
- **rclone too old or missing** → step 2, on the user's OK.
- **File locks refused** (litman's data folder is on a network disk) → the user sets `XDG_DATA_HOME` to a folder on a local disk.
- **Anything else** (offline, say) → relay it: the next `lit sync` tries again, and the GUI retries by itself.

A `lit sync` that goes through, from a terminal too, ends the GUI's stopped state: no click needed. Stray `*.partial` files in the cloud folder are cut-off uploads that two-way sync ignores; on the user's OK, `rclone delete '<remote>:<folder>' --include '*.partial'` clears them (`--dry-run` first).

## Conflicts (exit `21`)

The same file changed on both computers: the newer copy was kept, the older one went to the backup. A folder deleted on one computer and changed on the other stays deleted, with its changed files in the backup (a trashed paper comes back through [I]). Say which file and whose copy was kept. `lit sync status` also lists the conflict backups of the last 7 days: `<file>.sync-conflict1` is this computer's copy, `.sync-conflict2` the cloud's, and a folder ending in `-save` holds the copy a GUI save replaced. On request, diff a backup against the current file and help merge: `notes.md` rewritten on the user's go (Rule 4), `discussion.md` by appending what is missing, `metadata.yaml` changes through `lit modify`; a PDF cannot be merged — say where the other copy is. Never copy a backup over a library file yourself (Rules 4 and 7).

## Code repos (exit `22`)

Papers sync first. Each code repo then follows by commit, fetched from its upstream; the summary names every repo that did not follow, why, and the next step, and the next `lit sync` tries again. A long first fetch can outlast your shell tool's time limit: let it finish, or ask the user to run `lit code restore-all` in a terminal of their own. A slow or blocked network (GitHub from mainland China, say): a proxy (`https_proxy`) or a GitHub mirror only on the user's yes, for that one command; fetching by commit lets git check what a mirror sends. A checkout left behind by a repo removed on another computer, or one holding changes of the user's, is theirs to move or delete, never yours.

Asked whether their code synced, or the cloud button says code repos have changes only on this machine → `lit sync status` names each repo with uncommitted changes or commits no remote has: tell the user which. Commit or push only when they ask; no git write unasked.
