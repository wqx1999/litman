# Changelog

Notable changes to litman. Dates are release dates on [PyPI](https://pypi.org/project/litman/).

Versions follow [semantic versioning](https://semver.org/): a patch release fixes
behaviour, a minor release adds it, a major release breaks it.

## 1.2.0 — unreleased

### Added

- **`lit agent`** — open your AI agent in the vault with one command. It changes
  into the active vault and hands the terminal over to the agent, so the agent
  starts where your papers are.
- **Agent setup, in the GUI.** The agent button carries a red dot until an agent
  is configured. Click it to pick your agent, install the litman skill, and clear
  the dot. Claude Code is supported today; Codex, Cursor, Gemini CLI and OpenCode
  are listed as coming soon.
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

### Changed

- A new logo, favicon and desktop-shortcut icon. The mark in the top bar follows
  your light / dark theme.
- Windows is now a declared supported platform.
- Your agent choice is stored once per machine rather than per vault.
- `lit list` prints at most 30 rows when sorted by default on an interactive
  terminal. Pass `--limit` for more.
- `lit uninstall` now also removes the desktop shortcut, the machine-level
  preferences, and the browser profile the app window uses.
- A shorter README. The agent model benchmark moved to
  [docs/6-agent-benchmark.md](docs/6-agent-benchmark.md) and the usage caveats to
  [docs/0-readme.md](docs/0-readme.md); the install instructions lead with the
  one-line installer, and the pipx, source-install, update and uninstall routes
  are folded away.
- The Chinese README is gone. The documentation is English only.
- The one-line description on the PyPI page, the documentation site and
  `lit --help` now says what litman does in plain English.

### Fixed

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
