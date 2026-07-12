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
