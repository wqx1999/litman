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

### Changed

- A new logo, favicon and desktop-shortcut icon. The mark in the top bar follows
  your light / dark theme.
- Windows is now a declared supported platform.
- Your agent choice is stored once per machine rather than per vault.
- `lit list` prints at most 30 rows when sorted by default on an interactive
  terminal. Pass `--limit` for more.
- `lit uninstall` now also removes the desktop shortcut, the machine-level
  preferences, and the browser profile the app window uses.

### Fixed

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
- The `?` shortcut sheet no longer wraps its key captions.
- `lit setup` signposts the next step for a first-time user.

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
