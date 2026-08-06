# Changelog

Notable changes to litman. Dates are release dates on [PyPI](https://pypi.org/project/litman/).

Versions follow [semantic versioning](https://semver.org/): a major release
breaks something you relied on, a minor release opens a new way of working with
litman, and a patch release is everything else — fixes, new controls, and
conveniences.

## Unreleased

### Fixed

- **The window keeps up with the work happening outside it.** A paper added,
  retagged or promoted from the terminal — or a paper's notes rewritten by an
  agent — now appears in the open window within a few seconds, without you
  touching anything. Until now litman only re-read your library when its window
  regained focus, so an agent filing papers while you watched looked exactly
  like nothing happening. The refresh button and `R` still force a re-read on
  demand, a note you are in the middle of writing is never overwritten by one of
  these refreshes, and a window you have switched away from goes quiet until you
  come back to it.
- **The paper list no longer stays filtered after a search.** Picking a result
  from the search box now clears the box, so the list goes straight back to your
  whole library instead of holding the last query until you deleted its text by
  hand. `Esc` clears the box too, and a ✕ sits in it whenever there is something
  to clear.

## 1.3.4 — 2026-08-06

### Added

- **On macOS, litman's window is now litman's own.** Double-clicking litman
  opens a WKWebView window that belongs to litman itself: the Dock shows
  litman's book mark instead of a browser's, Cmd-Tab and Force Quit list
  litman, a second double-click brings the window forward instead of bouncing
  the icon, and closing the window stops the server as before. WebKit ships
  with macOS, so the standalone window no longer needs Chrome — a fresh Mac
  gets it out of the box. The window opens on a small loading page and swaps
  to litman the moment the server is ready; if the server ever stops first
  (an update, say), the window closes with it rather than lingering as a dead
  page. Windows and Linux keep the browser-held window they already had.
- **Reorder your tabs, and close them in bulk.** Drag a tab along the strip to
  put it where you want it; the strip scrolls itself when you drag past its
  edge, so a crowded row is no obstacle. Right-click the tab strip for "Close
  other tabs" (everything but the one you are reading) and "Close all tabs" —
  a tab with unsaved notes or annotations still asks before it goes.

### Fixed

- **The taskbar shows litman, not the browser.** On Windows the litman
  window's taskbar button wore Edge's icon and name, and pinning it pinned a
  bare browser; the button now wears litman's mark, reads "litman", and a
  pinned button launches litman. On Linux the window filed itself under
  Chromium in the dock; window and launcher entry now identify as litman —
  on Wayland sessions too.
- **macOS installs where you look.** `lit gui --make-shortcut` now places
  litman in /Applications, where the Finder sidebar actually shows it,
  falling back to ~/Applications only when /Applications is not writable —
  and it migrates the bundle it may have left in the old spot.
- **The macOS app looks and behaves like one.** The bundle now carries
  litman's icon (it used to show the generic one), each launch is logged to
  `~/Library/Logs/litman/litman.log` so a failed start finally leaves a
  trace, Chromium and Brave are recognized alongside Chrome and Edge, and
  the stray Tk splash window that flashed at launch is gone.
- **The Windows folder picker knows your drives.** Browse… dialogs list each
  real drive as its own cell in a segmented control beside the Home/Desktop
  shortcuts, with the drive you are on highlighted — and announced as such to
  a screen reader, which now says the group is a drive list and which drive
  you are in.
- **The "no year" import error fits on a line.** It now meets the same
  one-verdict-one-way-out budget as the other import errors, instead of
  running past it when a DOI or file path was attached.
- **On Windows, a tag ending in a period gets its browsing folder.** A topic
  or method named "Fig." built its folder under `views/by-topic/` but never
  the shortcut inside it — and litman blamed the drive, reporting that the
  filesystem could not hold folder links and suggesting you move the library
  off a USB stick. The tag name was the problem, not the disk, and such names
  are now stored in a form Windows keeps.

## 1.3.3 — 2026-08-02

### Added

- **Add a paper by dragging its PDF into the window.** litman reads the DOI
  off the first pages and shows you what it found — title, authors, year,
  journal — before anything is saved; correct or type the DOI yourself when
  the PDF has none, as scanned papers often do. A DOI already in your library
  is refused with a link to the paper you already have. When CrossRef has no
  record for it — a patent, or a journal that registers its DOIs elsewhere —
  fill in the title, year and authors yourself and it goes in the same way;
  a DOI you typed is kept even though CrossRef could not resolve it. The paper
  id is shown before anything is written, and when litman cannot name it on
  its own the field opens into the three parts an id is made of — year, first
  author, keyword — with the parts it worked out already filled in and only
  the missing one waiting for you. A Chinese-titled paper therefore costs you
  one box, not the whole id. The drop is a copy, and the dialog says so: your
  original file stays where it is, unlike `lit add`, which moves the PDF it
  imports. This is otherwise the same import the CLI runs, so a dragged-in
  paper is indistinguishable from one added there.
- **litman now tells you what changed after an update.** The first time the
  app opens on a new version, a short "What's new" card lists the handful of
  changes you will actually notice, with a link to this changelog for the
  rest; click the litman mark in the top-left corner to read it again later.
  The card's text ships inside the package, so it needs no network and a
  fresh install never sees it — there is no previous version to tell it
  about.
- **Pin papers to the top of the list.** Papers you are actively working with
  can be pinned: they gather in a "Pinned" group at the top of the browse
  panel and stay there, instead of drifting as the reading list re-ranks
  itself. Pin from the row (the pin icon, or `P` on the selected paper), and
  unpin to send a paper back to its usual place. Pins survive closing and
  reopening litman, and each library keeps its own.
- **`lit health-check` reports papers whose author or title is a placeholder.**
  Papers imported with a filler value — `Unknown`, `N/A`, `untitled` — used to
  pass every check, because the field was technically filled in. They are now
  listed by name, each with the `lit modify` command that writes the real value
  — and, when the filler reached the paper's id as well, the `lit rename` that
  clears it from there.
- **`lit health-check` reports papers whose id says nothing about them.** An id
  like `2018_Zhang_A` — the keyword reduced to a single stray letter — is
  listed with the `lit rename` that replaces it. Only papers whose title
  cannot produce a better keyword are reported, so an id you chose yourself is
  left alone.
- **`lit health-check` reports placeholders left inside a paper id.** A paper
  imported as `2024_Unknown_Untitled` keeps that name after you correct its
  author and title — the id is the folder name, the target of every `[[link]]`
  in your notes, and the cite key in exported BibTeX, and only `lit rename`
  changes it. It is now listed on its own, so correcting the fields no longer
  makes the last mention of the bad id disappear along with them. Once the
  fields are right the report hands you the complete rename, both ids filled
  in; when the title is in a script that cannot produce a keyword, it fills in
  everything it can and leaves that one blank for you.
- **`lit health-check --all`.** Each category now lists its first few findings
  and folds the rest into a count — a library imported before a guard existed
  can hold hundreds of one kind, and printing every one buries everything
  else. The counts stay exact; `--all` prints the full list, which is what to
  use when working through a category paper by paper.
- **Patents export as patents.** A paper with `venue-type: patent` now becomes
  a `@patent` entry instead of a bare `@misc`, and a `patent-number` field is
  rendered as the entry's number.
- **Edit metadata in the GUI.** The selected paper's panel has an Edit button:
  title, year, journal, DOI, volume/issue/pages, publisher, venue type, book
  title — and the author list, where authors can be renamed, added, removed
  and reordered (drag the handle, or the ↑/↓ buttons). One Save writes
  everything in a single transaction through the same validated path the CLI
  uses; if something is rejected — a DOI another paper already carries, a
  non-numeric year — the dialog shows the reason and keeps your input. The
  paper id is not editable here: changing it is a rename that updates every
  reference, which remains `lit rename`'s job.
- **`lit modify --set-author` rewrites the author list in order.** Repeat the
  flag once per author; the order the flags appear in is the order stored.
  This is the way to reorder authors or correct a name in place —
  `--add-tag` appends to the end of the list, so it cannot express either.

### Changed

- **Deleting, restoring, re-tagging and project-linking papers is now fast in
  large libraries.** These operations used to re-read every paper's metadata
  from disk — several times each — so in a 4,000-paper library one click in
  the GUI could stall for seconds. They now reuse the library's index and
  update only what actually changed, making their cost independent of library
  size; the same atomic write path and the same on-disk results as before,
  just without the re-reading. Editing a paper's title or authors in the GUI
  got the same treatment when the paper belongs to a project.
- **YAML parsing is C-accelerated.** litman now installs `ruamel.yaml.clib`,
  which recent versions of the YAML library stopped bundling — full-library
  operations such as `lit health-check` read metadata roughly 3–4× faster.
- **A paper can no longer be imported with a placeholder title or first
  author.** `lit add --from-llm-json` refuses values like `Unknown`, `N/A` or
  `untitled` in the two places that become part of the paper id, and explains
  what to write instead. **This will interrupt an agent that used to fill those
  in** — which is the point: an id is permanent short of renaming the paper and
  every reference to it. When a work has no personal author, name the issuing
  body — the patent assignee, the journal, the organisation. When it is
  genuinely unattributed, write `Anonymous`. A filler further down the author
  list still comes in, since it never reaches the id; the import warns and says
  how to correct it. Metadata fetched by DOI is unaffected, and `lit modify`
  still lets you write anything you like into your own library.

### Fixed

- **A title written in a script the id cannot carry no longer produces a
  nonsense id.** Paper ids are ASCII, because they are folder names on
  Windows, macOS and Linux alike — and litman picked the keyword by splitting
  the title on spaces. A Chinese, Japanese or Korean title has none, so the
  whole title arrived as one word and was reduced to whatever Latin characters
  happened to sit inside it: `关于化合物A的合成方法` became `2018_Zhang_A`,
  silently, and short of renaming the paper that id was permanent. Such a
  title is now refused instead, and the message names the `--id` that gets
  past it, offering a candidate whenever the title held a usable fragment.
  Nothing about the stored metadata changed — titles, authors and journals in
  any script are kept exactly as written, and only the id is ASCII. Write the
  first author's family name in romanised form and the rest can stay in its
  own script.
- **The "no year" error no longer reads as an invitation to guess one.** When
  imported metadata carried no publication year, the message asked for an id
  and left the rest open, and the quickest way past it was to put the download
  year in — which then passed every later check and surfaced only as a wrong
  date in an exported citation. It now says outright that the year must not be
  guessed, and where to find the real one.
- **The two places you can type a year now ask for the same thing.** The add
  dialog took three or four digits; the metadata editor checked nothing at all
  and left it to the backend, which only requires a whole number. A library
  could therefore end up holding both `145` and `12311`, each arrived at
  through a different door. Both now want four digits, and both print that
  rule beside the field before you type rather than only greying out the
  button afterwards — a control that goes grey without saying why sends you
  looking for the mistake in the wrong place. Four digits rather than a
  plausible range: a range has to decide where the future ends, and preprints
  routinely carry next year's date. The editor judges only a year you actually
  changed, so a paper that already holds a wrong one can still have its title
  corrected, and `lit modify` still writes whatever you tell it to.
- **Starting litman from its icon now finds the AI agents you have installed.**
  On Linux and macOS an app started from a desktop shortcut or the Dock is
  handed a shorter search path than a terminal window gets, and every agent
  CLI lives outside it — so litman reported Claude Code, Codex, Cursor,
  OpenCode and Antigravity as all missing at once, Recheck kept saying the
  same, and the skills could not be installed. It now looks in the places
  those tools actually install to, so double-clicking the icon and running
  `lit gui` in a terminal see the same agents. Installing an agent while
  litman is open still needs only Recheck, not a restart.
- **Microsoft Edge on Linux is recognised as a window browser.** litman opens
  its own window when it finds a Chrome-family browser, and Edge's Linux
  package was only matched by chance. Installing Edge — or Chrome, or
  Chromium — now reliably gets you a standalone window instead of a browser
  tab. A browser installed under your home directory is found too.
- **A snap-packaged Chromium now opens litman's own window.** On Ubuntu,
  installing Chromium gets you a snap, and a snap is not allowed to reach
  hidden directories under your home — including the one litman kept its
  browser profile in. Chromium will not run a profile it cannot lock, so it
  quit the moment it started: the desktop shortcut showed nothing at all, and
  installing a browser left you worse off than having none. A snap-packaged
  browser now keeps its profile in the area snapd grants it, and the shortcut
  opens a standalone window as it does everywhere else. Every other browser —
  Windows, macOS, and a Chromium installed from a `.deb` — keeps the location
  it has always used, and `lit uninstall` clears both.
- **A browser that quits on startup no longer takes litman with it.** The
  window launcher read "the browser is gone and no page ever connected" as
  proof that the launch had failed, and shut the server down — correct when
  the browser never came up, but it also caught a browser that started and
  then gave up, leaving nothing on screen at all. litman now opens the page in
  your usual browser instead, so a window that cannot be had degrades to a tab
  rather than to nothing. A browser that exits cleanly is left alone: it has
  handed the page to a window you already had open, and a tab appearing on top
  of that would be a surprise rather than a rescue. A launch that failed also
  no longer waits out the patience meant for a slow one.

## 1.3.2 — 2026-07-29

### Added

- **Update from inside litman.** The update chip now has an “Update &
  restart” button: litman closes itself, upgrades, and reopens — no terminal
  needed. If the update cannot run (for example litman was not installed via
  uv or pipx), the chip explains why instead. If the upgrade itself fails,
  litman comes back on the version you were already on and tells you what
  went wrong, rather than leaving you with a window that never returns.
- **litman installs from mainland China.** The install command is the same one
  everywhere, and it now works from inside China without a VPN: uv, the Python
  runtime and litman itself all arrive from sources that are reachable there,
  and a first install takes seconds.

### Changed

- **The update reminder is now a labelled chip.** When a new release is out, a
  small chip with the new version number appears next to the logo, instead of
  a bare blue dot. Click it to see the version you are on and update from
  there.

### Fixed

- **Links now open in a new window.** Clicking a hyperlink inside a paper's
  PDF — or an external link in notes / discussion — used to navigate the
  litman window itself away from the app. External links now open a separate
  window; wikilinks and in-PDF outline jumps behave exactly as before.
- **The update reminder no longer skips the first start.** After a release,
  the GUI used to need one extra restart before the reminder could appear; it
  now shows up on the first start, within a few seconds.
- **`lit self-update` works on Windows.** It used to fail every time with
  "the process cannot access the file" — Windows will not let litman replace
  its own `lit.exe` while it is running — and a failed run could delete
  `litw.exe`, breaking the desktop shortcut. The upgrade now starts the moment
  the command exits, the way the in-app update already worked; the command says
  so and prints where to check. A launcher lost to an earlier failed upgrade is
  restored automatically at the next `lit gui` or `lit self-update`, and
  `lit gui --make-shortcut` says so out loud if it ever has to fall back to the
  console launcher. The install scripts got the same guard for the
  re-run-to-upgrade path.
- **The documented Windows config directory was wrong.** The docs and
  `lit vault --help` pointed at `%APPDATA%\litman\`; the registry, and
  everything beside it, actually lives in `%LOCALAPPDATA%\litman\litman\`.
  Nothing moved — only the description was wrong.
- **`lit hello` now mentions available updates**, and says so every time you
  ask — other commands mention a new release once a day, so that the tip does
  not trail every command in a working session, but `lit hello` is how you
  check on purpose. It is also the one command that reports this to a coding
  agent, which runs `lit hello` to check on litman: if you only ever reach
  litman through an agent, the agent can now pass a new release on to you.
  `lit hello` keeps skipping the interactive registry prompts.

## 1.3.1 — 2026-07-26

### Added

- **Every path field in the GUI now has a “Browse…” button.** Click familiar
  places (Desktop, Documents, Home), step through folders, and pick one —
  instead of typing an absolute path — when creating or relocating a library,
  linking or relocating a project, or registering an existing library. Folders
  that are already litman libraries show a ✓ badge. Pasting a path and pressing
  Enter still works exactly as before.
- **Make a new folder while browsing.** The Browse window has a “＋ New folder”
  button, so you can create a folder for a new library or project without
  leaving litman for your file manager.
- **Click through the path in the Browse window.** The address bar shows the
  current path as clickable segments (Home › Desktop › research) — click any one
  to jump straight to that folder. Click the bar to type or paste a path as
  before.
- **New libraries default to your Desktop.** The create-library screens show
  where the library will appear — for example “Desktop / literature_vault” —
  and fall back to Documents, then your home folder, on machines without a
  Desktop.
- **Forget a library that has moved or been deleted.** The first-run screen’s
  “open an existing library” list now offers **Forget** next to a moved entry,
  beside Locate — clearing a stale entry whose folder is gone. The folder on
  disk is never touched.

### Fixed

- **Scanned PDFs no longer open as a blank white page.** Black-and-white scanned
  papers — the common fax/CCITT-encoded kind — were rendering as an empty page in
  the reader while text PDFs opened fine. They now display correctly.
- **Paths and commands use a proper monospace font on Windows and Linux.** They
  previously fell back to a serif face (Courier New) on machines without the
  macOS system fonts; the stack now includes Cascadia, Consolas and Liberation
  Mono.
- **The agent “run it in a terminal” prompt now names the right machine.** When
  litman is served from a remote or headless server, the GUI agent button can't
  open a terminal window, so it shows the `lit agent` command to run by hand —
  now worded to run it on the server, not on the computer showing the browser.
- **Locate can be backed out of.** On the first-run screen, opening **Locate**
  on a moved library left no way to close the path input short of submitting a
  path or clicking another entry. A **Cancel** button now sits beside it.

## 1.3.0 — 2026-07-22

### Added

- **Press `R` in the GUI to refresh the current library from disk.** It uses
  the same resync as the toolbar refresh button; `Alt+R` still marks a paper as
  read, and `Ctrl+R`/`Cmd+R` remains the browser's page reload.
- **Five AI agents are supported:** Claude Code, Antigravity CLI, Codex,
  Cursor, and OpenCode. All are selectable in the GUI agent manager and
  launchable with `lit agent <name>`. Codex, Cursor, and OpenCode share the
  Agent Skills open-standard directory `~/.agents/skills`; Claude Code keeps
  `~/.claude/skills`, and Antigravity CLI uses its own
  `~/.gemini/antigravity-cli/skills` directory.
- **The GUI agent manager now shows familiar brands next to agent names.**
  Labels such as “ChatGPT · OpenAI”, “Google”, and “Claude · Anthropic” help
  newcomers recognize Codex, Antigravity CLI, and Claude Code.
- **`lit install-skill --agent <name>`** installs the skills into the named
  agent's directory. With no flags the command now follows your default agent;
  users who never changed the default get exactly the previous behaviour.
- **Installed skills can run `lit` without repeated permission prompts.**
  Skill onboarding and launches through Litman merge or heal a narrowly scoped
  `lit` command allow rule in the selected agent's native permission store.
  This remains per-agent even when agents share one skill directory; Litman
  never enables a global bypass. Malformed policy and conflicts that cannot be
  normalized without changing unrelated permissions are left untouched and
  reported; Antigravity's redundant catch-all ask rule is removed only in its
  default request-review mode, which still asks for every other unlisted
  command. Strict mode is preserved and reported because it intentionally
  ignores terminal allow rules. Claude's `CLAUDE_CONFIG_DIR`, Cursor's
  `CURSOR_CONFIG_DIR`/`XDG_CONFIG_HOME`, and OpenCode's `OPENCODE_CONFIG` are
  honored so both the skill and permission rule reach the active profile.
- **Skill copies for your other agents stay fresh.** A bare
  `lit install-skill`, the setup wizard's skill step and `lit health-check
  --fix` also refresh out-of-date litman skills found in the other agents'
  directories — one `[Y/n]` per copy in interactive runs, automatic under
  `--fix`; files you added next to a skill are always kept. Runs with an
  explicit `--agent`/`--parent-dir` touch only what they name.
- **`lit setup` asks which agent you use** (step 2) and records the answer as
  the machine-level default before installing its skill — pressing Enter keeps
  the previous Claude Code flow.

### Changed

- The agent toolbar button and `~` now launch the configured default directly.
  Right-click the same button or press `Ctrl+~` to manage agents and change the
  default; agents that are not installed can no longer be launched or selected
  as the default.
- The GUI agent panel shows each supported agent's own install/update/ready
  state instead of repeating the default agent's state on every card.
- `lit health-check` probes skill drift in the default agent's skills
  directory (and `--fix` refreshes that directory, plus stale copies in the
  other known directories). `lit uninstall` still sweeps every known skills
  directory.

## 1.2.1 — 2026-07-21

### Added

- **The desktop shortcut now shows a splash while it launches.** Started from
  the Windows shortcut there is no console, and litman's startup messages only
  go to `litw.log`, so a cold launch looked like nothing had happened until the
  window finally appeared. A small floating app mark now shows the instant you
  launch and vanishes the moment the page connects. It appears only on the
  console-less window launch with a local display; remote and headless sessions
  are unchanged — the server still prints its URL and SSH-tunnel line.

### Changed

- **The app starts noticeably faster.** `lit` used to import every command — and
  the heavy libraries behind them (PDF parsing, HTTP, the server stack) — on
  every invocation, including ones that never touch them; each command is now
  imported only when it actually runs, taking hundreds of milliseconds off the
  start of every `lit` command (most visibly on Windows). And `lit gui` no
  longer waits a fixed one second before opening the browser — it opens the
  moment the server is actually listening.

### Fixed

- **Closing the app window reliably stops the server — even when the browser
  lingers.** `lit gui --window` stops the server when its last page closes,
  which it now tracks through litman's own window-open connection rather than the
  spawned browser process. Two things broke the old process-based approach. On
  Windows, Edge keeps its process running in the background after the window is
  gone (Startup boost, one process per profile), so litman waited forever on a
  process that never exited — the server, and its "this library moved" banner,
  could outlive the window for the whole session and pile up across launches. And
  a brand-new profile made Edge restart itself partway through its first run,
  handing the real window to a process litman never saw. Shutdown now follows the
  last live page: close the window and the server stops a few seconds later,
  whatever the browser process does. A second tab you opened on the same server
  still keeps it alive after the app window closes.

- **A window from a past session can no longer haunt the next launch.**
  Force-closing litman (Task Manager, `taskkill`) made the browser record a
  crash; the next launch of the app window then resurrected the dead session's
  page next to the live one — a days-old view, loaded from the browser's cache
  against a server that no longer existed, still wearing whatever banner was
  true back then (typically the red "library is no longer at…" one). Two fixes:
  the launcher now clears the app profile's session-restore state before every
  window (there is never a session worth restoring — each launch brings its own
  address), and the page itself is now served with `Cache-Control: no-cache`,
  so the browser must check with the server instead of booting a stale copy on
  its own authority.

- **The red "library is no longer at…" banner can no longer outlive the
  problem it reported.** Browsers are allowed to cache a `410 Gone` answer, and
  litman's API responses never said otherwise — so once a launch had caught the
  library mid-move, the browser could keep replaying that stale "it's gone"
  answer from its own cache: the banner survived the relocate that had already
  healed the server, and even reappeared on later launches whose server was
  perfectly healthy, until the cache happened to expire. Every `/api/` response
  now carries `Cache-Control: no-store` — answers about the library's live
  state are never reused from cache. The page is also more honest while
  something else is wrong: a request failing for an unrelated reason used to
  leave a stale banner frozen on screen, and a hiccup in the change-log diffing
  could silently stop a refresh from landing; both now degrade gracefully
  instead of pinning the old picture. Upgrades also heal app profiles that
  cached a `410` before this fix: API requests explicitly bypass the browser
  cache, and the shortcut launcher removes that profile's legacy HTTP cache
  once while preserving its preferences and local state.

- **A moved library can be pointed at its new home — no forced rename.** When you
  move or rename your library folder, litman's registry still records the old
  path and there was no clean way to update it: the app's "Find it" opened a
  panel that could only register a *new* name, re-registering under the old name
  was refused, and relaunching landed on a dead-end first-run page. Now `lit vault
  set-path <name> <new-path>` repoints a registered library in place, and the app
  grows a **Locate** action — on the library-moved banner and the first-run page —
  that reconnects the open session with no restart and no rename. Relocating the
  active library also rebuilds its projects' shortcuts, which the move had left
  pointing at the old path. Locate now clears the banner even when the moved
  library is the one the open window is showing but no longer the *active* one
  (for instance after switching the active library from another terminal) —
  that case previously left the banner stuck for the life of the window.

## 1.2.0 — 2026-07-15

### Added

- **`lit agent`** — open your AI agent in the vault with one command. It changes
  into the active vault and hands the terminal over to the agent, so the agent
  starts where your papers are.
- **Agent setup, in the GUI.** The agent button carries a red dot until an agent
  is configured. Click it to pick your agent, install the litman skill, and clear
  the dot. Claude Code is supported today; Codex, Cursor, Gemini CLI and OpenCode
  are listed as coming soon.
- **Skill updates are detected.** Upgrading litman ships new skill content, but
  the installed copy used to stay silently stale. Now the GUI agent button
  raises its red dot and offers **Update skill**, `lit health-check` flags the
  drift (`--fix` refreshes it), re-running `lit install-skill` reports
  "up to date" or offers the refresh instead of erroring, and `lit setup`
  refreshes on Enter. Files you added next to a skill are always kept.
- **`~` launches the agent** from anywhere in the GUI — the key left of `1`, with
  or without Shift. Press `?` for the full shortcut list.
- **A welcome page.** `lit gui` now starts without a vault and offers to create
  your first library from the browser.
- **New libraries can be created from the GUI**, not just your first one. The
  vault manager now has **New vault** alongside **Register existing** — pick a
  location and a name, and optionally switch to it once it is made.
- **`lit self-update`** upgrades litman through whichever tool installed it (uv or
  pipx). litman also checks PyPI once a day and mentions when a newer version is
  out; set `LITMAN_NO_UPDATE_CHECK=1` to silence it. No telemetry is sent.
- **One-line install scripts** for uv, on macOS, Linux and Windows. They create a
  desktop shortcut as part of the install. See the README for the command.
- **`lit gui` opens your browser** automatically. `--no-browser` suppresses it,
  `--window` opens a chromeless app window, and `--make-shortcut` writes a desktop
  entry. Closing the app window stops the server, and the desktop shortcut starts
  it without a console window.
- **Search matches authors, DOI and year**, in the CLI and the GUI. In the GUI
  list, `J` / `K` move the selection, `Enter` opens the paper, and `/` focuses
  search.
- **Every paper now starts with a `discussion.md`.** `lit add` creates the log
  empty, headed by a line stating how it is written: one dated section per
  discussion, `[[paper-id]]` for cross-references. Your agent reads that line
  before it appends, so discussions come out in one shape across the library.
  `lit health-check` reports papers added before this (their log is missing) and
  `--fix` creates it — existing logs keep every section they already hold.
- **`lit open` and `lit show` with no argument** open the paper you engaged
  with most recently — the one `lit list --sort recent` puts at the top. Coming
  back to the paper you just closed should not cost you its id. They name the
  paper they picked on stderr, so `lit show --format json` still emits only
  JSON. There is still no stored "current paper": the ranking is computed from
  `updated-at` and the PDF's mtime, the same way it always was.
- **`--format json` on the five commands that enumerate a library**:
  `lit taxonomy list`, `lit vault list`, `lit project list`, `lit code list`
  and `lit trash list`. They printed a table and nothing else, so an agent had
  to parse a table that folds its own long cells — or go read `TAXONOMY.md`
  and `vaults.yaml` behind the CLI's back. Each now emits one object per row,
  keyed the way the underlying file is, and an empty library is `[]`.

### Changed

- `INDEX.json` and `lit list --format json` carry one more field per paper:
  `updated-at`. Ranking a library by recency — `lit list --sort recent`, and the
  web UI's reading list — needs it, and reading it used to mean opening every
  paper. A consumer that ignores the field sees exactly what it saw before, and
  an index written by an older litman is regenerated on the next write.
- A new logo, favicon and desktop-shortcut icon. The mark in the top bar follows
  your light / dark theme.
- `lit project rm` now asks before removing a project that no paper
  references. It used to remove it on the spot: no papers meant nothing to
  warn about. But an unreferenced project still owns a path binding in
  `lit-config.yaml`, and `litman_reflib/` and `REFERENCES.md` inside your
  own project folder — outside the vault, where the trash does not reach.
  Undoing it took three commands and remembering the path. The prompt says
  when no paper is affected, so the Enter is a cheap one. `-y` skips it.
- Windows is now a declared supported platform.
- Your agent choice is stored once per machine rather than per vault.
- `lit list` prints at most 30 rows when sorted by default on an interactive
  terminal. Pass `--limit` for more.
- litman's messages say **link** where they used to say symlink. On Windows the
  browsing folders and project shortcuts are directory junctions, so "12
  symlinks" was the wrong word on the one platform where the word choice
  matters; the web UI already said "folder links". Output wording only —
  nothing on disk changes.
- New libraries' `lit-config.yaml` no longer includes `view_definitions` and
  `unique_keys` — two keys litman parses but has never read (the view set and
  the DOI duplicate check are fixed in code). Older libraries that carry them
  keep loading exactly as before.
- `lit uninstall` now also removes the desktop shortcut, the machine-level
  preferences, and the browser profile the app window uses.
- A shorter README. The agent model benchmark moved to
  [the agent model benchmark](https://litman.dev/docs/6-agent-benchmark/) and the
  usage caveats to [the docs home](https://litman.dev/docs/0-readme/); the
  install instructions lead with the
  one-line installer, and the pipx, source-install, update and uninstall routes
  are folded away.
- The Chinese README is gone. The documentation is English only.
- The one-line description on the PyPI page, the documentation site and
  `lit --help` now says what litman does in plain English.

### Performance

Everyday commands no longer re-read the whole library to change one paper.
Measured on a 300-paper library (a real two-year collection):

- **`lit modify` — 3.0s → 0.7s.** Tagging a paper or setting its status used to
  read every `metadata.yaml` in the library twice, then delete and recreate every
  link under `views/`. It now reads the index, writes the one paper, and moves
  only the links that actually changed. `lit read`, `lit skim`, `lit revisit`,
  `lit drop`, `lit promote` and the web UI's metadata edits all take the same
  path.
- **`lit add` — 4.5s → 0.05s** of library work (the rest is the CrossRef fetch).
  Ingesting the 301st paper now costs what the 2nd did; before, a batch import
  got slower with every paper.
- **`lit list` — 1.9s → 0.6s**, which is litman's start-up floor: the query
  itself is now a single index read, as the documentation always said it was.
  `--sort recent` included.
- **A DOI lookup — 2.1s → 0.01s.** `lit add`'s duplicate check and every
  `--paper-doi` lookup (`show`, `cite`, `rm`, `modify`) used to parse the whole
  library to find one paper.
- **The web UI's paper list — 2.3s → 0.02s.** Opening the library, and every
  window focus after it, re-read every paper to rank the reading list by
  recency. The change-detection sweep that runs beside it went 2.3s → 0.06s,
  and the recently-read list 2.0s → 0.01s.

`INDEX.json` stays a derived file, never a second source of truth: whenever it
is missing, stale, or written by another version, litman silently falls back to
reading the library and regenerates it. `lit health-check --fix` and
`lit refresh-views` remain the full rebuild.

### Fixed

- **Two papers can no longer end up sharing a DOI.** `lit add` always refused
  duplicates, but `lit modify --set doi=` did not check — and once two papers
  shared a DOI, every `--paper-doi` lookup (`show`, `cite`, and destructively
  `rm`) resolved to an arbitrary one of them. `modify` now refuses the
  collision and names the paper that owns the DOI, and `lit health-check`
  reports any collision already present (a new `duplicate_doi` check).
- **`--set year=` only accepts numbers now.** A mistyped year was written
  as-is and surfaced much later as an invalid `year = {...}` entry in exported
  BibTeX.
- **`lit modify --set topic=X` now says you probably meant `--add-tag
  topics=X`.** The singular is a one-letter miss that wrote a junk scalar
  field and said nothing: `--set` accepts any field (metadata is schemaless
  by design), so the register-first check that guards `--add-tag` never ran,
  the taxonomy never heard about the value, and no view indexed it. The write
  still goes through — your metadata is yours — but litman now points at the
  command you wanted. Fields unrelated to a tag list stay silent.
- **The web UI no longer goes quietly blank when a vault switch fails.** The
  five reads that repopulate the panels after a switch had no failure path, so
  a server that blinked at exactly that moment — the moment it is rebinding —
  left every panel empty with nothing said. They now raise the same banner
  every other failed read raises.
- **A paper with a field it never set is served the same by both paper
  endpoints.** `GET /api/paper/{id}` returned `metadata.yaml` as it is on disk,
  and metadata is schemaless — so a paper that had never been given a topic
  came back with no `topics` key at all, while the list endpoint gave `[]` and
  the web UI's types said the field was always there. It happened to be
  defended against everywhere it mattered. Both endpoints now agree on the
  fields they share, and everything else in the file still passes through
  untouched.
- **The web UI explains unreadable files instead of blanking.** A notes or
  discussion file that is not UTF-8 (an external editor's doing), or a
  missing/garbled TAXONOMY.md, used to crash the request behind a silent empty
  panel. The affected tab now says what is wrong and leaves the file untouched;
  taxonomy and projects report the damage and point at `lit health-check`.
- **`lit health-check` no longer fails forever on headless machines.** "This
  SSH/cron session has no display for `lit open`" is now an info note, not a
  warning, so a structurally clean library exits 0 on servers — the documented
  cron/CI-gate behaviour.
- **Windows: write commands no longer print a power-loss warning every time.**
  The reduced crash-window guarantee on Windows is a property of the platform,
  documented once, and litman still reports the moment an interrupted write is
  actually found.
- **Windows: the desktop shortcut follows OneDrive's Desktop.** With OneDrive
  folder backup on, the shortcut used to be written into the old, no longer
  displayed Desktop folder — installing looked like it had produced no icon.
- **Windows: removing a trashed paper's half-cloned repository no longer
  strands read-only git files** (which then blocked every later re-clone of
  that repository).
- **`lit init <path>` offers to create the parent folder** (one Enter) instead
  of erroring when it does not exist. Scripts still get the explicit error.
- **`lit project set-path` offers to rebuild the project's links right there**
  (one Enter) instead of telling you to run `lit link --rebuild-all` yourself
  later.
- **A path passed to `--vault` now points at `--library`.** `--vault` takes a
  registered name; handing it a filesystem path used to dead-end at
  "no vault named …, run `lit vault add`" — the wrong fix.
- **`lit search` line numbers stay right when notes contain form feeds**
  (pdftotext page separators pasted inside an HTML comment shifted every later
  hit by a line and hid the file's last line).
- **Paper ids can no longer end in a dot** — Windows strips a trailing dot
  when creating the folder, which would leave the id and the folder name
  permanently disagreeing.
- **Windows: the browsing folders and project shortcuts now work out of the
  box.** `views/` and the shortcuts `lit link` places in your project folders
  are created as directory junctions on Windows — a native folder link that
  needs no special mode and no administrator rights. Before, they silently
  required a symbolic-link privilege nobody has by default, and litman
  reported every missing one as an error — about six per paper, several
  hundred for a real library — none of which `lit health-check --fix` could
  repair. On a drive that cannot hold links at all (FAT32 / exFAT USB sticks,
  network shares) litman now says so once, calmly — one info line in
  `lit health-check`, which exits clean, and a dismissible note in the web
  UI — and skips them: those shortcuts are conveniences, and papers, notes,
  search, the web UI and the agent workflow all work without them.
- **`lit search` no longer matches the comment lines litman seeds into your notes.**
  Searching a word that only appears in one of them (`wikilink`, say) returned a
  hit on every paper in the library. Comments are litman's, not yours, so they are
  no longer part of the search corpus.
- **Moving or deleting your library while the GUI is open is no longer silent.**
  litman kept serving the old location: the paper list came back empty, and saving
  a note rebuilt a stub library at the dead path — so the note landed there and
  never reached the real library. The GUI now says the library is gone, names the
  path, and offers to find it; every write is refused until it is found. Putting
  the folder back restores the session on its own.
- **A library whose folder has moved is marked `missing`** in the vault selector
  and the vault manager. Switching to one is refused with a message naming the
  path it lost; before, such a library was offered like any other, and picking it
  left the selector where it was and said nothing.
- **Moving your library no longer silently breaks its project links.** The
  `litman_reflib/` and `litman_code/` shortcuts that `lit link` places in your
  project folders kept pointing at the library's old location, and
  `lit health-check` reported all clear. Now the next `lit` command notices and
  offers to rebuild them with one Enter, `lit health-check` reports them and
  `--fix` repairs them, and switching to the found-again library in the GUI
  rebuilds them on its own.
- **A project whose folder has moved is marked `missing`** in the project manager
  and in a paper's project picker, and one registered in only one of litman's two
  records is marked `incomplete`. The CLI has always reported both; the GUI listed
  them like any other project.
- `lit add` rejects a file that is not a PDF instead of ingesting it, and its help
  text now says plainly that the source file is **moved** into the vault, not
  copied.
- **`lit link <paper> <project>` now shows you the command you meant.** That
  shape, and `lit code add <paper> <repo>`, are the ones a person reaches for
  first — but the second value belongs in a flag, and all these commands used
  to say was "Got unexpected extra argument (pepforge)". They now print the
  whole corrected line, ready to copy. (`lit unlink`, `lit code link` and
  `lit code unlink` too; `lit code add` works out which word is the repo, so
  the command it hands back is right even if you wrote them the other way
  round.)
- A mistyped command or paper id suggests the closest match instead of failing
  blankly.
- `lit rm` and `lit trash` route every delete through the same confirmation.
- `lit vault add` records the health-check clock, so a newly added vault is not
  reported as overdue.
- An empty vault, and a lost connection to the server, now explain themselves in
  the GUI instead of showing an empty list or silently doing nothing.
- The browser no longer offers to translate the GUI, which blanked the page.
- A few GUI messages that appeared in Chinese are now in English, and the
  mark-read toast names its undo key the way the `?` sheet does.
- The `?` shortcut sheet no longer wraps its key captions.
- `lit setup` signposts the next step for a first-time user.
- The Windows note in the docs claimed the linked folders need administrator
  rights and pointed users at WSL. They need neither — nor anything else: see
  the junction change above.
- **The bundled skills describe the CLI as it is.** The instructions an AI
  agent reads had drifted: they promised a deletion report only an interactive
  terminal ever prints (the agent path is now spelled out — preview with
  `lit rm --dry-run`, relay it, run with `--yes`), claimed `lit list` rows
  don't carry the author (they do), described a `related` output shape the
  JSON never had, said health-check cannot fix anything (`--fix` exists), and
  still routed vocabulary reads through the taxonomy file instead of
  `lit taxonomy list --format json`. Also new there: a drive that cannot hold
  folder links is a fact about the drive — the skills now say so and forbid
  proposing system-setting remedies for it.
- **The docs caught up with the CLI too.** The command reference claimed
  `lit health-check` exits 1 on *any* finding (info notes never gate — the
  cron/CI contract is errors and warnings only), and still described the old
  `lit project set-path` that only printed a hint. Now documented: the
  singular-tag warning, the interactive 30-row table cap, `code link/unlink`
  flags, that `taxonomy add` treats a known value as a no-op and `merge` may
  create its destination — and `lit agent` finally appears in the README, the
  tutorial, and the agent map. The two `lit-config.yaml` keys nothing reads
  (`view_definitions`, `unique_keys`) are labeled inert instead of documented
  as switches.

## 1.1.0 — 2026-07-06

The web GUI: `lit gui` serves a browser reader for the active vault, with PDF
annotation, notes, tags, and vault and project management.
[Release](https://github.com/wqx1999/litman/releases/tag/v1.1.0)

## 1.0.1 — 2026-06-17

Bug fixes and packaging corrections. No change to existing workflows.
[Release](https://github.com/wqx1999/litman/releases/tag/v1.0.1)

## 1.0.0 — 2026-06-09

First stable release.
[Release](https://github.com/wqx1999/litman/releases/tag/v1.0.0)
