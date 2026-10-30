# Changelog

Notable changes to litman. Dates are release dates on [PyPI](https://pypi.org/project/litman/).

Version numbers follow how much a release changes for you, not how much work
went into it: a minor release opens a way of working that litman did not have
before, and everything else is a patch — however large it was to build.

Anything that takes something out of your library is marked **Breaking** in its
own entry. Read those before you upgrade, whatever the number says.

## Unreleased

### Added

- **DeepSeek Harness is a supported agent.** Pick it in the agent panel or run
  `lit agent dsh`, and DeepSeek Harness opens — no terminal in between.
  Install the skills from the same panel: DeepSeek Harness loads both of them.
  It comes back to the session you used last, so the first time, add your
  library as a workspace in DeepSeek Harness.

### Fixed

- **Users in mainland China now see when a new version is out.** litman
  asked pypi.org for the newest version, which often does not answer there in
  time, and after one failed try it did not ask again for a day, so no update
  badge or tip appeared. It now asks the mirror litman was installed from (the
  China installers use the Tsinghua mirror), so a version it shows is one
  `lit self-update` can install. The web UI also waits for a slow answer, asks
  again an hour after a failed try and every hour while it stays open. If you
  are in mainland China and still on an older version, run `lit self-update`
  once.
- **`lit health-check` titles two more kinds of problem.** A `lit-config.yaml`
  it cannot read and a `.litman-staging/` leftover it cannot clean up were
  headed by internal names (`config_unreadable`, `stale_staging_unrecoverable`).
  They now get a title like every other finding.
- **`lit health-check --fix` lists what it fixed in a steady order.** The
  **Auto-fix** lines could come out in a different order on each run. They now
  follow the order of the report above them.
- **The web UI keeps up when `lit-config.yaml` or `TAXONOMY.md` cannot be
  read.** It stopped showing changes made from the terminal or by an agent
  until the page was reloaded, and adding a project, restoring a paper or
  removing one said only "Internal Server Error". It now stays up to date, and
  those actions say that `lit-config.yaml` needs fixing; the health panel says
  `lit-config.yaml` cannot be read.
- **Changing a paper while `lit-config.yaml` cannot be read now finishes.**
  `lit modify`, `lit code link`, `lit code unlink`, `lit code add --paper` and
  `lit code rm --cascade` saved the change and then stopped with an error —
  `lit code add` also removed the repo it had just added — and the web UI said
  the edit had failed. `lit rename` and changing a paper's projects left the
  project folders behind without a word. These commands now finish and say the
  project folders were not updated: fix the file, then run
  `lit health-check --fix`. The web UI now saves the edit without an error.
- **`lit code` says when it cannot update a project's `litman_code/` folder.**
  `lit code add --paper`, `lit code link` and `lit code unlink` left the
  folder out of date without a word, and `lit code rm --cascade` stopped
  before deleting the repo when the folder could not be read. They now finish
  and say so; once the folder is fixed, `lit health-check --fix` brings it up
  to date.
- **`lit trash restore` finishes when it cannot update a project folder.**
  When a folder of one of the paper's projects could not be read, the restore
  stopped with a Python error after the paper was already back, and the web UI
  said only "Internal Server Error". The address of a repo it still had to
  clone again was then lost for good, and a repo it did clone again was left
  out of the project without a word. It now finishes, clones the repo, and
  says the project folder was not updated; once the folder is fixed,
  `lit health-check --fix` brings it up to date.
- **Restoring a paper keeps the address of a repo it does not clone again.**
  When `lit rm` deleted a repo only that paper used, its address was kept only
  in the trash. Restoring the paper from the web UI, which never clones,
  lost it for good, and the web UI pointed to `lit trash restore -y`, which
  no longer had anything to restore. The repo now comes back with the paper,
  without its files: `lit code restore-all` clones it again and links it into
  the paper's projects. The same holds when `lit trash restore` skips or fails
  the clone. `lit code restore-all` also copies a repo added from a folder
  without git again, instead of failing on it.
- **A project folder that cannot be read no longer stops a command halfway.**
  When a project's `litman_reflib/` or `litman_code/` could not be read,
  `lit link`, `lit unlink`, `lit rm`, `lit rename`, `lit modify`,
  `lit project rename`, `lit project rm`, `lit refresh-views` and
  `lit health-check --fix` made their change and then stopped with a Python
  error, the project folder half updated; in the web UI the same actions said
  only "Internal Server Error". `lit unlink` also lost the relevance note it
  removes, which it shows so you can copy it back. They now finish and say the
  project folder was not updated; once it is fixed, `lit health-check --fix`
  brings it up to date. `lit project rm` names the folder to clear by hand,
  since nothing looks at it after. `lit health-check` and the web UI's health
  panel, which failed the same way, now name the folder they could not read.
- **A project folder litman can read but not change is called that.** In a
  `litman_reflib/` or `litman_code/` that takes no changes (`chmod 555`, a
  read-only mount, a folder another user shares), `lit unlink`, `lit rm`,
  `lit project rm`, `lit code unlink` and `lit code rm --cascade` left the old
  links behind without a word, and a link that could not be added (`lit link`,
  `lit code link`, `lit code add --paper` and the rest) was blamed on the
  drive ("this filesystem cannot hold folder links") or on an entry to remove
  by hand;
  `lit health-check` said the same of a project folder that took no changes
  at all. They now end with the one line a folder they cannot read gets, and
  `lit project rm` names the folder to clear by hand. Rebuilding a project
  folder also leaves the links and `REFERENCES.md` that are already right as
  they are, so a read-only folder in order raises nothing;
  `REFERENCES.md`'s "Last updated" is now when its list last changed. Nor
  does one such folder keep the project's other folder, or the paper's other
  projects, from being brought up to date.
- **A read-only folder in `views/` no longer stops a command.** When a folder
  under the library's `views/` took no changes (`chmod 555` on
  `views/by-status/` or one of its buckets), `lit drop`, `lit modify`,
  `lit rename`, `lit rm`, `lit refresh-views`, `lit health-check --fix` and
  the other commands that update `views/` either stopped with a Python error
  after the change was already saved, or left the old link there and still
  said "views/ refreshed"; changing a paper's status in the web UI answered
  with a server error. They now finish and name the view they could not
  update in one line, and `lit health-check` reports that view until the
  folder takes changes again. A rebuild leaves the links in `views/` that are
  already right as they are.
- **`lit health-check` names a folder in `views/` it cannot read.** When
  `views/`, one of its views or a bucket could not be read (`chmod 000`, or a
  copy owned by another account), `lit health-check` stopped with a Python
  error and the web UI's health panel answered with a server error. It now
  names that folder and checks the other views.
- **"could not clear" also says to make the folder writable.** When a real
  folder sat where a project link belongs and could not be removed, the hint
  said only to close whatever was using it, also when the folder itself did
  not allow changes.
- **Deleting a project in the web UI names the folder it could not clean up.**
  When a project's `litman_reflib/` or `litman_code/` could not be read or
  changed, the web UI said only "Removed project" and left the old links in
  it, and nothing looks at that folder again. It now names the folder and the
  reason, and the notice stays until you click it, as `lit project rm` already
  said.
- **`lit gui --make-shortcut` says in one line why it could not.** When the
  folder the shortcut goes in could not be written (a file named like the
  Desktop folder, an applications folder without write access), it printed a
  whole Python traceback, and `lit setup` stopped there. It now names the
  folder and the reason, and `lit setup` lists the shortcut as skipped and
  finishes.
- **On Windows, `lit gui --make-shortcut` works when `lit` is not on PATH.**
  It stopped with "Could not locate the `lit` executable", though that was the
  program running. It now uses the `lit.exe` it was started from.
- **`lit rm` no longer deletes the only copy of a repo.** A folder added with
  `lit code add --move` exists only in the library. When the paper you deleted
  was the only one using it, `lit rm` deleted the repo too, and restoring the
  paper could not bring it back. `lit rm` now keeps a repo it has nothing to
  clone again from, unbound, and says so; `lit code rm` deletes it if you want.
- **Restoring a paper brings back its repo's notes.** When `lit rm` deleted a
  repo only that paper used, the repo's `notes.md` and the fields you filled
  in its `repo-meta.yaml` (framework, runs-on, status) went with it, and a
  restore put back blank ones. They now wait in the trash with the paper and
  come back when you restore it. A paper deleted before this version cannot
  get them back.
- **`lit rm` no longer deletes a repo you changed.** When the paper you
  deleted was the only one using a repo, `lit rm` deleted the whole repo, on
  the grounds that it could be cloned again. A clone does not bring back what
  you changed in it: edited or new files, commits you had not pushed, a stash,
  files you put next to `repo/`. That repo is now kept, unbound, and
  `lit rm` says so; so is one it cannot check (no git, say). The web UI's
  delete dialog lists it too. `lit code rm` deletes it if you want.
- **`lit trash restore -y` no longer deletes a repo that is already back.**
  When a paper's repo had been deleted with it and its `repo/` folder was in
  place again (a sync from another computer, or a clone by hand), the restore
  tried to clone it again, failed because the folder was in the way, and
  deleted that folder with everything you had changed in it. The restore now
  binds the paper to the folder that is there.
- **`lit trash restore` no longer writes over a repo's newer notes.** When a
  `notes.md` was back in the folder of a repo deleted with the paper (written
  by hand, or brought back by a sync), the restore replaced it with the copy
  saved at the delete. It now keeps that one, puts the saved copy beside it as
  `notes.from-trash.md`, and says so; `lit health-check` lists that copy until
  you merge it in and delete it.

### Security

- **On Linux, `lit gui` refuses requests from other users of the same
  machine, wherever the kernel can say who sent them.** On a shared machine,
  such as an HPC login node, anyone logged in could send requests to the web
  UI's port and read or change your library. Your own web UI, command line and
  `ssh -L` tunnels work as before. The check does not run on Windows or macOS,
  nor on a Linux kernel that cannot tell users apart (no `tcp_diag`, or a user
  namespace that maps no uids); there `lit gui` behaves as before.

## 1.3.9 — 2026-10-28

### Added

- **ZCode is a supported agent.** Pick it in the agent panel or run
  `lit agent zcode`, and ZCode opens at your library — no terminal in between.
  Install the skills from the same panel: ZCode now loads both of them. In
  ZCode, turn on Autonomous mode so `lit` runs without asking each time.

- **Export for EndNote.** `lit export --format ris` writes a `.ris` file that
  EndNote, Zotero and Mendeley import as it is — in EndNote, choose the
  Reference Manager (RIS) import option. EndNote's own BibTeX import tends to
  lose fields or fail outright. Each record's label is the paper id, the same
  key you `\cite` in LaTeX.

- **The web UI shows how many papers the list holds.** The count sits at the
  right of the **Show** row above the list and follows everything that narrows
  it: the project, Reading / Read / All, the filters and the search box.

- **Lists in the web UI say how long they are.** The paper panel numbers its
  authors, relations, code clones and SI files, so an author list cut short
  with "…" now tells you how many authors there are. A tag menu shows how many
  values its vocabulary holds, or how many match while you search. The
  Projects, Vaults and Manage topics / methods / data windows show how many
  entries they list.

- **Click a code clone in the web UI to copy its folder path.** A name under
  **Code-clones** in the paper panel copies the clone's local folder
  (`codes/<name>/` in the vault), ready to paste into a terminal or an agent,
  the way **Copy path** does for the paper. The ↗ beside it opens the
  repository's web page, such as its GitHub page, in a new tab. A clone whose
  folder is missing can't be clicked, and a clone imported from a local folder
  has no ↗.

### Changed

- **Relations in the web UI's paper panel read as a tidy list.** Each kind
  (related, extends, …) has its own line with one paper per line below it, so
  a long id no longer wraps into a ragged block: it ends in "…" instead, and
  hovering shows it in full. A kind with more than five papers shows the first
  five and a **+ N more** to unfold the rest.

- **Your agent runs what you ask for, and writes only when you ask.** Ask it
  to add a paper, tag one or link it to a project and it runs the command
  without asking again; ask for something the skills have no step for —
  rebuilding the views, say — and it looks the command up and runs it, where it
  used to hand you the command to type. A remark is not a request: say "读完了"
  or "this one's done" and you get one proposal — the status and read date it
  would stamp, whether to rewrite `notes.md`, the empty fields it could fill —
  and nothing is written until you say yes; "mark it read, deep-read" runs at
  once. It no longer adds a line to `discussion.md` after cloning a paper's
  code.

- **Deleting and overwriting wait for your go.** Before it deletes a paper or
  an SI file, removes a tag value or a project, or overwrites a `.bib` you
  edited by hand, the agent shows what will go and waits for you to say so.
  Emptying the trash and `lit rm --purge` it never runs: it gives you the
  command to type.

- **`lit health-check --fix` updates the `notes.md` heading even if you
  changed it.** When it cleans a title, the first line of `notes.md` becomes
  `# ` and the clean title, as `lit modify` now does. 1.3.8 left a heading you
  had rewritten alone and printed `kept …: notes.md heading differs …`; that
  line is gone. A `notes.md` that starts without a heading gets one on top, and
  nothing below the first line changes.

- **`lit add --si <paper>` opens the SI window whatever the paper's text
  says.** It used to exit 13 when the text never said "supplementary" or
  "supporting information", so a paper whose appendix is a separate file
  ("see Appendix D", as NeurIPS papers write it) got no window unless you
  added `--force`. Exit code 13 is gone.

- **Dialogs in the web UI keep their explanation behind a ?.** The agent,
  Projects, Vaults and Manage topics / methods / data windows no longer open
  with a paragraph of explanation. Hover the circled ? beside the title to read
  what the window is for.

### Fixed

- **An author known only by family name exports cleanly.** A paper whose
  author is stored as `Yang, ` went into the `.bib` as `Yang, ` too, and BibTeX
  stopped on the trailing comma. The export now writes `Yang`.

- **Changing a paper's id or title now updates the heading that goes with
  it.** `lit rename` rewrites the first line of the paper's `discussion.md` to
  `# Discussion log for <new id>`, and `lit modify --set title=` — and the
  title in the web UI's **Edit metadata** — rewrites the first line of its
  `notes.md` to `# <new title>`. Both used to keep the old one. The line is
  replaced even if you edited it, and added on top if the file starts without
  one; the rest of the file stays as it was. The web UI's notes tab shows the
  new heading as soon as you save. If `notes.md` cannot be read, `lit modify`
  changes nothing and names the file.

- **`lit rename` lists where the old id is still written as plain text.** It
  rewrites every `[[old-id]]` link, but an id typed as plain text — in a
  quote, or a command such as `lit show <old-id>` — is left as it is, so it now
  prints each such file with the line numbers. A notes file it cannot read is
  named too, where it used to be skipped without a word.

- **`notes.md` and `discussion.md` keep Windows line endings.** When
  `lit rename`, `lit rm` or `lit trash restore` rewrote a `[[id]]` link in a
  file saved with Windows line endings, the whole file came back with Unix
  ones; on Windows, the `(deleted)` tag added after a paper folder was deleted
  by hand did the reverse. Each file now keeps its own.

- **`lit init --no-register` suggests commands you can paste.** The vault path
  in its three suggestions is now quoted, so a parent folder with a space no
  longer splits the command, and on Windows the environment-variable line is
  PowerShell's `$env:LIT_LIBRARY = "…"` instead of `export`.

- **`lit gui` on your own Windows or Mac no longer prints the SSH tunnel
  line.** It still shows inside an SSH session there, and always on Linux.

- **Copy path copies a path in the library you have open.** With `lit gui
  --library` or `--vault` open on a library other than the active one, Copy
  path and clicking a code clone's name copied the matching folder in the
  active library instead.

- **The web UI names the library it has open.** With `lit gui --library`,
  `--vault` or `$LIT_LIBRARY` on a library other than the active one, the vault
  list at the top showed the active one, picking that one did nothing, and the
  trash header named it too. The list now shows the open library — one opened
  by path that isn't in your list appears by its folder name, in amber — and
  picking any vault in the list switches to it. `(active)` now appears only
  when the active library isn't the one open.

## 1.3.8 — 2026-10-24

### Added

- **When your agent needs a paper's supplementary information, litman asks you
  for it.** Ask about a detail the article leaves to its SI and the agent runs
  `lit add --si <paper>`: the web UI opens an SI window, **Open website ↗**
  takes you to the article's page, and the files you download there and drop
  into the window are checked against the paper — the DOI in their names, or
  the title on their first pages — and added together when you press
  **Done**. A file that is empty, looks like another paper's SI, or is the
  article itself is refused; one litman cannot confirm goes in only if you tick
  **add anyway**. The files land in the paper's own `supplementary/` folder as
  `si-01.pdf`, `si-02.xlsx` and so on, read-only like its PDF and recorded in
  its `metadata.yaml` with the names they were downloaded under, and the agent
  answers with the file and the page it found the answer on. litman never
  downloads the SI itself. With no web UI open, the command prints the
  article's link and the way to add the files yourself; it has to run on the
  machine the web UI's server runs on.

- **SI you already have goes in from the command line.**
  `lit add --si <paper> <file>...` runs the same check, one line per file, and
  adds all the files or none: a refused file stops the whole add, even with
  `--force`. One litman cannot confirm, or one over 100 MiB, needs a yes at the
  terminal, or `--force`. Like `lit add`, it moves the files into the library.
  `lit show` lists a paper's SI files below its PDF and notes, with each file's
  original name and size.

- **A Supplementary row in the cockpit.** Each SI file is a chip —
  `si-01.pdf · 7.6 MB`, with the original name on hover — and **+ Add SI**
  opens the SI window without waiting to be asked. When the web UI's server
  runs on a machine with a desktop, clicking a chip opens the file there in the
  system's default app, and the toast names the machine;
  `lit open <paper> --si si-02` does the same from the command line. In an ssh
  session, or on a machine without a display, the chip stays a link that opens
  a PDF in the browser and downloads anything else, and `lit open --si` prints
  the file's path. A Ctrl- or Cmd-click still leaves it to the browser. A file
  that would run rather than open — a program, a script, an installer — is
  never handed to the system.

- **`lit rm <paper> --si si-02` deletes an SI file for good,** and so does the
  × on its chip. The confirmation lists how often your notes cite the file, and
  `--dry-run` shows the same without deleting. There is no trash for SI files:
  add the file again to get it back, and it takes the lowest free number —
  often its old one. Deleting the whole paper still takes its SI files into
  the trash with it, and `lit trash restore` brings them back.

- **`lit health-check` checks SI files and the notes that cite them.** It
  reports a recorded file that is missing or whose content changed, a file in
  `supplementary/` with no record, a malformed `supplementary` field, links or
  folders inside `supplementary/` that litman will not touch, and a file it
  cannot check. `lit health-check --fix` records a stray file that checks out
  as the paper's SI and, after asking at a terminal, drops the record of one
  that is gone; off a terminal it drops no record, records only the files that
  check out, and prints the command for the rest. It never deletes an SI file.
  A line in `notes.md` or `discussion.md` that cites `si-01.pdf` after that
  file was deleted, or whose name in brackets is not the file that number holds
  now, is listed with its file and line and what to change. That is a
  warning: `lit health-check` exits 1 until the line is changed, but
  `lit sync push` is not held up.

- **Filters and tags open in one panel that lays the values side by side.**
  The Status, Type, Topics, Methods and Data headings in the left panel, and
  the Tags pills in the cockpit, open the same panel: the values in up to
  three columns, scrolling only past that, each with the number of papers
  that use it — in the current view on the left, in the whole library in the
  cockpit, where a project shows the paper's grade instead. A search box
  narrows the list (on the left, once there are more than 18 values); Esc or
  a click outside closes it. On the left, a value that papers elsewhere in
  the library use, but none in the view, stays in the list, greyed, with a
  dash. The short scrolling lists and **Expand all / Collapse all** are gone.

- **litman asks before you create a value that looks like one you have.**
  With `peptide` registered, `lit taxonomy add topics peptidedrug` is refused
  and names `peptide`; `--force` adds it anyway. In the web UI, creating such
  a tag opens a confirmation that lists the similar values: click one to tag
  the paper with it, or **Create anyway**. Two values look alike when they
  differ only in case, `-` / `_`, spaces, punctuation or a plural `s`, when
  one starts with the other (6 characters or more), or when they are a letter
  or two apart (8 characters or more).

- **See how many papers use each value, and what to tidy.**
  `lit taxonomy list` shows the count next to each value, and
  `--format json` gives it as `used_by`. In topics, methods and data,
  `lit health-check` notes values that look alike, with the
  `lit taxonomy merge` that would fold them, values no paper uses, and a
  vocabulary where many values are each used by one paper. They are notes:
  they do not change the exit code, and `--fix` leaves them alone. Ask your
  agent to tidy your tags, and it proposes the merges and removals for you
  to approve.

### Changed

- **The small uppercase labels are one shade darker.** The titles over each
  field in the cockpit (METADATA, AUTHORS, STATUS…), the group titles in the
  left panel and the field names in dialogs were faint on a bright screen; they
  now read clearly, in light and dark mode alike.

- **`lit open` no longer says `Opened` when nothing opened.** When the viewer
  fails as it starts — a display that is set but no longer reachable, or no
  app for PDFs — `lit open` now says why, prints the PDF's path and exits 2,
  as it already did with no display at all. It gives the viewer up to 1.5
  seconds to report such a failure, so a viewer that stays in the foreground
  (zathura, evince) opens at once and the command returns 1.5 seconds later.

### Fixed

- **Titles from CrossRef no longer keep the publisher's line breaks and
  markup.** Some publishers deposit a title or journal name with line breaks,
  indentation and tags such as `<i>` inside it, and `lit add --doi` stored it
  that way: the tag ended up in the paper id (`2026_Zhang_INi--Hydroxyamide-…`),
  the heading of `notes.md` broke across lines, and the panel after the add
  showed only the title's first line. Titles, journal names and authors now
  come in as one plain line — from CrossRef, from `--from-llm-json` and from
  `lit modify`. For papers already in your library, `lit health-check` lists
  every title, journal or author name with line breaks, stray spaces or
  markup, and `lit health-check --fix` cleans them, along with the `notes.md`
  heading `lit add` wrote from the title if you have not changed it. It never
  changes a paper id; when an id was made from a broken title, the report and
  the `--fix` output give the `lit rename` that would make it match.

- **Code links that `lit add` finds in a PDF are no longer cut off at a line
  break.** When the PDF broke a repository link across lines, `lit add` missed
  it, or listed only the part before the break — a user page such as
  `https://github.com/dfwlab`, or a bare `https://github.com`. It now finds the
  whole link (`https://github.com/dfwlab/cyclicpeptide`) and no longer lists the
  cut-off part.

- **A file name that is not valid UTF-8 no longer stops `lit`.** On Linux, a
  name from an old archive or another system's encoding (GBK, Latin-1) made
  any `lit` command that printed it stop with a Python error. It now prints,
  with each byte that cannot be read shown as an escape such as `\udce9`.
  Such a name, or text with such a byte, is no longer written into a file
  litman cannot read back. `lit project add`, `lit project set-path` and
  `lit sync setup --path` recorded such a folder in `lit-config.yaml`, and
  `lit vault set-path` in the vault registry, after which every command that
  reads them failed until the line was edited by hand; `lit modify --set` and
  `lit add --from-llm-json` wrote such text into a paper's `metadata.yaml`,
  after which litman could no longer read that paper. They now refuse it before
  writing anything, and so do `lit vault add`, which stopped with a Python
  error, and `lit init`, which stopped with one after creating the library
  (with `--no-register` it created one that could never be registered). Rename
  the folder and run the command again; other text is named in a one-line
  error. `lit code add` imports such a folder (given `--name`) and a
  repository whose origin holds such a byte, noting where it came from with
  `�` for each byte it cannot read, as `lit vault add --import-from` now does
  for its note, and a vault registry whose note already holds one is fixed on
  its own. Any other file an earlier version wrote such a name into is still
  reported by name (`lit health-check` lists a `metadata.yaml` it cannot
  read); edit that line by hand.
  The web UI no longer fails on such a name either: the folder picker lists
  the folder greyed out (rename it to open it), and the health-check panel,
  search and the page's live refresh carry on, showing the name with `�`.
  `lit health-check` names such an SI file the same way, and the
  `lit add --si` command its `--fix` prints for one pastes back into bash or
  zsh.

- **Long error messages stay on one line.** Piped into another program or a
  log, a `lit` error longer than 80 characters was broken across lines, which
  split the paths inside it. It now prints on one line. A path holding a
  bracketed word, such as `draft[v2].pdf`, keeps it too — it was taken for
  formatting and dropped.

- **`lit health-check` no longer stops at a notes file that is not UTF-8.** A
  `notes.md` or `discussion.md` saved in another encoding by another editor
  made `lit health-check` stop with a Python error, and the health panel in
  the web UI fail to load. The file is now reported as one that could not be
  read, and the rest of the check runs.

- **A `TAXONOMY.md` saved in another encoding no longer stops litman.**
  Re-saved by another editor as Latin-1, GBK or UTF-16, it made
  `lit taxonomy`, `lit project`, `lit modify --add-tag`, `lit health-check` and
  `lit sync push` stop with a Python error, the web UI's health panel fail to
  load, and the web UI answer "Internal Server Error" when you tagged a paper or
  added a topic or project. They now name the line that is not UTF-8 and ask you
  to re-save the file as UTF-8; litman leaves the file as it is.

- **The path `lit open` prints is no longer split across lines.** When
  `lit open` cannot open a PDF, the `PDF path:` line it prints broke at the
  edge of a narrow terminal, or at 80 characters when piped, and so did its
  other messages. Each now prints on one line however narrow the terminal, so
  the path can be pasted as it stands.

- **`lit uninstall` no longer leaves litman's own files behind.** It removed
  the vault registry and the agent preferences but kept the update-check cache
  (`update-check.json`) and the self-update log (`self-update.log`,
  `self-update-failed`) beside them, so the folder that held the registry
  stayed on disk. Those files now go too, and so does the folder once nothing
  of yours is left in it.

- **Focus rings in the web UI are blue.** Text boxes and the tag search box had
  a near-black ring (near-white in dark mode); a few other outlines were off too.

- **Notices in the web UI go away on time.** A notice at the bottom stayed up
  while you kept working, moving through the list or typing a search.

- **Links in the desktop app open in your own browser.** In the window the
  desktop shortcut opens (`lit gui --window`) on Windows and Linux, a link to
  another site — Open website in the SI window, a paper's DOI, a link in your
  notes, in a PDF or in What's new, an agent's install link — opened in a
  browser window with none of your sign-ins or extensions, so SI behind a
  login could not be downloaded. These links now open in your default
  browser. `lit gui` in a browser tab is unchanged.

- **Accented letters in a paper id are no longer dropped.** The first author
  `Brüssow` gave an id `…_Brssow_…`, `Søren` gave `Sren`. Such letters are now
  written plainly — `Brussow`, `Soren`, `Gosswein`, `Lukasiewicz` — in the
  author's name and in the title words, from `lit add` and from the web UI.
  Ids already in your library stay as they are.

- **`lit rename` no longer leaves a paper half-renamed.** When the paper's
  folder could not be renamed — on Windows a PDF open in Acrobat is
  enough — its id had already changed everywhere else and the folder kept the
  old name. The folder is now renamed first; if that fails nothing changes and
  `lit rename` says to close the file. A library already left this way is put
  right by `lit health-check --fix`, and at a terminal litman no longer marks
  that paper's links `(deleted)` in the meantime.

- **On Windows, a command your agent runs no longer stops at a bare
  `Aborted!`.** When the command needed an answer — `lit install-skill` over
  an older skill, `lit add` onto an id already taken — it asked anyway, got
  no answer and printed only `Aborted!`. It now says which option to pass, as
  it does on macOS and Linux, and the first `lit init` uses the default
  registry location instead of stopping at its question. `lit add
  --from-llm-json -` with nothing piped in now says so, on every system.

- **Commands with no one to answer them no longer take stray input for a
  yes.** `lit self-update`, the first `lit sync push`, `lit vault remove`,
  `lit uninstall` and `lit code rm` without `--yes`, and `lit sync setup`
  without `--remote` or `--path`, asked their question even when no one was
  at a terminal: they stopped at a bare `Aborted!`, or took a `y` piped in as
  the answer. They now say which option to pass and change nothing. With no
  terminal, `lit trash restore` without `-y` restores the paper and skips
  cloning a deleted code repository again, printing the `lit code add` that
  clones it; it used to stop with an error there, and the repository's
  address was lost.

## 1.3.7 — 2026-10-05

### Added

- **`Shift+N` and `Shift+D` open the selected paper's notes and discussion**
  in the GUI, like the 📝 notes and 💬 discussion buttons on its row. `D` alone
  is still the PDF drawing tool. Press `?` for the full shortcut list.

### Changed

- **Clicking the PDF, notes or discussion you are reading selects that paper
  again** in the GUI. After moving to another paper in the list, a click in
  the open document brings the list and the cockpit back to it, as clicking
  its tab already did. If it is still selected but the list has been scrolled
  away from it, the click, or a click on its tab, scrolls the list back to it.

### Fixed

- **Paths in the moved-directory prompt are no longer split across lines.**
  When a project directory has moved, the warning naming its old location,
  the line confirming the new one, and the example in the list of broken
  project links each print their path on one line however narrow the
  terminal, so it can be pasted as it stands.

- **The paths `lit add`, `lit link`, `lit code add` and `lit init` report can
  be copied whole.** On a narrow terminal the box these commands print split
  every long path inside it, with a border character in the middle. The box
  now holds the summary, and the paths — with the code links `lit add` finds
  and the commands the others suggest next — print below it, one per line.
  `lit project add` and `set-path` print their path on one line too.

- **A conflicted copy no longer makes the count disagree with itself.** When
  cloud sync has left two folders holding one paper, `lit taxonomy merge` and
  `rm` and `lit project rm` asked to change 1 paper and then reported 2, and
  `lit taxonomy rm` listed the paper twice. The confirmation and the result
  now both count papers, list each one once, and add the folder count when
  it differs — `1 paper (2 folders — 1 is a conflicted copy)` — so you know
  the copy is there to clean up. `lit taxonomy rename` and `lit project
  rename` report the same way.

- **`lit code add` on a local repository no longer fails while git is busy in
  it.** git tidies a repository in the background on its own schedule, and a
  file it removed mid-copy stopped the import with a Python error. The copy
  now starts over when that happens, and says so plainly if the folder keeps
  changing. Git's own lock files are no longer copied either, so a git
  command running in the source can no longer leave the copy locked.

- **A link to a paper that is no longer in the library can be removed.** When
  a paper was deleted outside litman or lost in a sync, the papers it linked to
  kept their link to it. On the side litman fills in for you (`extended-by`,
  `contradicted-by`) nothing could remove it: `lit modify` refused, the web
  UI's × reported the missing paper, `lit health-check` suggested the very
  command `lit modify` refused — and the finding kept `lit sync push` from
  running. `lit health-check --fix` now removes every link to a paper no
  longer in the library, and the × removes a single one. A link to a paper in
  the trash is reported as such: restore that paper first to keep the link.

- **A list field edited by hand into a single value is reported, not misread.**
  `topics`, `authors`, the relation fields and the other list fields of
  `metadata.yaml` are lists. Written as one value instead (`topics: peptide`),
  litman read it letter by letter: a folder under `views/` for each letter, a
  blank web UI on that paper, and the next `lit modify --add-tag` saving the
  letters into the file. A number there (`topics: 42`) stopped `lit
  health-check` and every later write to any paper. Such a file is now treated
  like one whose YAML does not parse. `lit health-check` names the file, the
  field and the line to write, and `lit health-check --fix` takes the paper out
  of the index and `views/` until it is fixed. litman never writes to the file:
  a command that would change it refuses, and one that sweeps the whole
  library leaves it as it is. `lit sync push` waits until it is fixed. The
  other papers are unaffected. Opening such a paper in the web UI, or one whose
  YAML does not parse, now shows that message instead of an empty panel.

- **Search highlights in notes and discussion stay in place.** Opening a notes
  or discussion search hit in the GUI marked the matches, but the marks
  vanished as soon as anything else in the window changed, such as selecting
  another paper in the list.

- **The selected paper's card is no longer cut off at the bottom of the list.**
  Moving down with `J`, or clicking a row near the bottom of the GUI's list,
  left the lower part of the card that opens under it out of view. The list
  now scrolls just far enough to show the whole card. Clicking a row whose
  card already fits still leaves the list where it is.

- **Closing a long PDF right after opening it no longer floods the browser
  console.** Closing its tab, or opening the Trash, just as its first page
  appeared logged one error for every other page of the PDF.

- **The metadata panel always shows the paper selected in the list.** Moving
  through the GUI's list with `J` just after switching back to its window
  could leave the panel on the paper selected before, under the newly selected
  one, until another paper was clicked.

### Security

- **Other websites can no longer use `lit gui` through your browser.** While
  the web UI was running, any page open in your browser could send it
  requests, and litman acted on them as if you had clicked: launching an
  agent, creating a folder, registering a library. A site set up for it could
  also read your library and notes through the browser. litman now answers
  only its own page: it refuses requests from any other site, and no other
  site can show it inside its own page. The web UI, the command line and
  `ssh -L` tunnels work as before.

## 1.3.6 — 2026-09-09

### Added

- **Remove a relation from the GUI.** In the cockpit's Relations row, hover a
  linked paper id and click the `×` beside it, then confirm. The paired reverse
  link on the other paper is removed in the same step, exactly as
  `lit modify --rm-tag` does.

### Changed

- **Breaking: a paper's priority is now set per project.** The A/B/C grade used
  to belong to the paper, so one letter had to stand for how much it mattered
  everywhere at once. It now belongs to each project link, as
  `priority-<project>`, and each project's `REFERENCES.md` groups by its own
  letters. Grade a paper as you link it with
  `lit link <id> --project <name> --priority A`, or later with
  `lit modify <id> --set priority-<name>=A`. `lit list --priority A` finds
  papers graded A by any of their projects; `--project <name>` narrows it to
  one, and `lit export` filters the same way. In the web UI the grade moved
  onto each row of the Projects list.

  `lit health-check --fix` migrates an existing library in one step. Run
  `lit health-check --all` first to see every paper it will touch. **A paper
  that belongs to no project loses its grade** — link it to a project first if
  you want to keep it, and take a backup before migrating.

### Fixed

- **A cloud-sync conflict no longer hides half a rename.** Sync can leave two
  folders in the library holding the same paper — the original and a
  "conflicted copy" beside it. `lit taxonomy rename`, `merge` and `rm`, and
  `lit project rename` and `rm`, built each file's path out of the id written
  inside the file, so they rewrote one of those folders twice, left the other
  holding the old value, and still counted both as updated. They now go
  through the folders themselves, so both halves of a conflicted copy are
  rewritten and the number reported is the number of files.

- **A project folder copied from another machine now repairs itself.** Copying a
  project directory — over the network, onto a USB stick, out of a tar, through
  cloud sync — turns every `litman_reflib/<id>` and `litman_code/<repo>`
  shortcut into a real folder — holding a full copy of the vault entry, or,
  if the tool did not follow the shortcut, holding nothing at all.
  `lit health-check` used to call each of those shortcuts "missing" and `--fix`
  then refused the folder with a permissions error per entry, leaving a dozen
  directories to delete by hand. The check now says
  `... is a folder copy, not a litman link (copied from another machine?)`, and
  `--fix` clears them: a folder that matches the vault, or that is empty, is
  replaced with the shortcut, and one that does not match it — a note you wrote
  on the other machine, or a code checkout this machine never had — is kept
  under `<vault>/.trash/replaced-folders/` and named in the output, so nothing
  you wrote is thrown away. `lit trash empty` clears those folders; `lit trash
  list` and `restore` never show them, because they must not come back.
  `lit link` and `lit trash restore` handle the same folders the same way, and
  so does the prompt that asks for the new location after a project directory
  or the library has moved — the most likely place to meet any of this. The
  path is printed on one line however narrow the terminal, so it can be pasted
  as it stands. On a drive that cannot hold shortcuts at all (FAT32, exFAT,
  network shares) the copies are left exactly where they are.

- **The health badge no longer waits to be clicked.** The shield in the toolbar
  showed nothing until you opened its panel, so a library problem could sit
  there unseen for as long as you never thought to look. litman now runs the
  check by itself once the window has finished drawing, and the shield carries
  the count from then on. A library with nothing wrong still shows no badge at
  all. The check repeats when you open the panel and when the vault changes; it
  does not repeat every time you focus the window or refresh the list.

- **A note on a highlight now shows wherever you hover it.** Writing a note on a
  highlight or a drawing left it readable only while the Highlight, Text or Draw
  tool was selected. In Cursor — the tool you actually read in — hovering that
  same annotation showed nothing, whether you had just written the note or were
  coming back to it days later. Hovering now surfaces the note in every tool, and
  it steps out of the way while you drag to select text. Annotations without a
  note still show nothing, and one you have open for editing keeps its note in
  the editing panel rather than also floating it as a tooltip.

- **One folder can no longer be registered as two vaults.** `lit vault add`
  checked that the name was free and that the folder held a library, but not
  whether that folder was already registered — so one library could answer to
  `main` and `clone` at once and turn up twice in `lit vault list`. Adding a
  folder that is already registered now says so and names the vault it is:
  `That folder is already registered as 'main'. Switch to it with: lit vault use
  main`. `lit vault set-path` is refused the same way when the new path is
  another vault's folder, and so are both actions in the web UI. The comparison
  resolves `..` and symlinks, and where the filesystem itself treats `Vault` and
  `VAULT` as one folder it catches that too, so a detour or a different spelling
  of the same folder is refused like any other duplicate. Moving a vault and
  pointing it at its new folder works as before. A duplicate already in your
  registry is left alone — drop the spare with `lit vault remove <name>`.

- **An update that cannot work no longer pretends it can.** If you installed
  litman straight from its git repository instead of from a release, `lit
  self-update` and the app's update button used to announce a new version and
  then fail out of sight — the reason buried in a log file, or the window
  closing and coming back with nothing changed. Both now tell you up front that
  this copy did not come from a release, and hand you the two commands that
  reinstall it.

- **The warning about a filler author is short enough to be read.** `lit add`
  warns when the author list still holds an `Unknown` or `N/A` after the first
  name. The message ran long enough that the half naming the repair sat past
  the point where a person at a terminal stops reading. It now carries the
  position that went wrong and the one command that fixes it, and stops:
  `Warning: author 2 is 'Unknown', not a real name. Fix: lit modify <id>
  --set-author "Family, Given" ... (repeat, in order).` The add still goes
  through, as before — a filler after the first author never reaches the paper
  id.

## 1.3.5 — 2026-08-09

### Changed

- **litman is now under the GNU Affero General Public License, version 3 or
  later.** It was MIT through 1.3.4. For using litman nothing changes: install
  it, read with it, script it, use it at work or on paid research, no
  permission needed and no fee. What changes is what happens to a modified
  copy — if you alter litman and then pass it on, whether by shipping a build
  or by running it somewhere other people can reach over a network, the source
  of your version has to be available under the same licence. Every release up
  to and including 1.3.4 stays MIT; that grant is not withdrawn.

### Added

- **Following a citation no longer strands you.** Clicking a reference in a PDF
  used to be a one-way trip: the reader jumped to the bibliography and finding
  your way back meant scrolling until you recognised the page. `Alt+←` now
  returns you to the exact spot — same place on the page, same zoom — and
  `Alt+→` goes forward again, however many links deep you went. A small pill
  floats at the bottom of the page after a jump showing where back leads
  (`← Back to p.7`); it shrinks to a plain arrow after a moment, and it is there
  to click if you would rather not use the keyboard. The trail is remembered for
  as long as the window is open, including across switching to another tab and
  back, and nothing is written into the PDF.
- **Preview a note before you save it.** Notes and discussion now have a
  **Preview** button next to Save: it renders the draft you are writing —
  headings, lists, links to other papers and all — and **Edit** takes you back
  to the text with every character still there. Flipping between the two keeps
  your place in both: the cursor where you left it in the text, and the spot
  you were reading in the preview. It sits where **Cancel** used to, which is
  the point: cancelling only ever did one thing, throw the draft away.
  Abandoning an edit is still possible — close the tab and answer "Don't save",
  the same question an unsaved PDF asks — but nothing inside the editor can
  lose your work any more. `Ctrl+S` saves from either view.
- **The shortcut list mentions undo.** `Ctrl+Z` and `Ctrl+Y` have always worked
  on annotations without being written down anywhere. `?` now lists them,
  together with the part that is easy to trip over: they work while an
  annotation tool is on, not in Cursor.

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
- **Selecting text in a PDF lines up with the text.** The blue band used to sit
  low — cutting across the tops of the letters while leaving an empty strip
  under them — and drifted further off the line the further down the page you
  read, until near the bottom it was most of a line out. Both are fixed: a
  selection now brackets the line it is on, at the top of the page and at the
  bottom, at any zoom. Highlights you make from a selection follow the same
  rectangles, so new ones sit about a point higher on the page than ones saved
  by earlier versions.
- **"Unsaved annotations" now means there are some.** Draw a highlight and erase
  it, or undo it with `Ctrl+Z`, and the Save button used to stay lit and closing
  the tab still asked whether to save — over a document that was back exactly
  where it started. litman now compares the annotations themselves, so undoing
  or deleting your way back to the saved state is recognised as saved. Saving
  and immediately switching tabs also writes the PDF once now, not twice.
- **`Escape` works on the dialog underneath.** Opening a dialog from inside
  another one — Vaults → Register existing, Projects → New project, a rename in
  a field's Manage list — used to leave the outer dialog deaf to `Escape` once
  the inner one closed, whether you cancelled it or completed it; only the mouse
  could get you out. `Escape` now closes it as it should.
- **`Escape` leaves an annotation's note, and keeps what you typed.** The note box
  in the annotation popover had no keyboard exit at all: `Escape` did nothing.
  It now saves the note and closes the popover, and a second `Escape` puts the
  toolbar back to Cursor.
- **The annotation popover is dark in dark mode.** It stayed a white card around
  a dark text box.
- **`Escape` no longer throws away a note you are writing.** In the notes and
  discussion editor it discarded everything since your last save — no
  confirmation, nothing to undo, one keystroke. It does nothing there now.
  (`Escape` still clears the search box and puts a PDF back to Cursor.)
- **On macOS, closing a dialog no longer drops you out of full screen.** With
  the window in full screen, `Escape` on an open dialog did two things at once:
  it dismissed the dialog and left full screen with it, so getting back meant
  reaching for the green button. Only some dialogs behaved that way, which made
  it look random. `Escape` now closes the dialog — or the tab's right-click menu
  — and nothing else. With no dialog open it still leaves full screen, the way
  any other Mac window does.
- **A missing library or project folder can now be pointed at its new home —
  or removed — right from the prompt.** When litman notices that a registered
  library, or a project a paper is linked to, is no longer where it was
  recorded, it asks about it on your next command. That question used to offer
  half the answers: for a library, only "remove the registration", so a library
  you had merely moved had to be re-registered by hand afterwards; for a
  project, only "type the new path", so a project you had genuinely finished
  with kept asking forever unless you went and found the right command. Both
  now take the same three answers — the new path, `rm`, or Enter to skip — and
  Enter never destroys anything. `rm` hands you to `lit vault remove` /
  `lit project rm` with their usual warning and confirmation, and a library is
  asked about one at a time so each can get its own answer.

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
