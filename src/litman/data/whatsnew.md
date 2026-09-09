# What's new — user-facing release highlights

<!--
One `## X.Y.Z` section per release, newest first, 3-5 bullets each.

Authoring rules (this file is the SINGLE SOURCE for the GUI's post-update
popup AND release announcements on social media — write once, reuse verbatim):

- Only outcomes a user can feel. No tech stack, no internals, no file names.
- SHORT. One bullet = one plain sentence, ~10 words, copy-pasteable on its
  own. Details belong in CHANGELOG.md — the card links to it.
- A bullet may wrap onto continuation lines indented by two spaces; the
  parser (litman/core/whatsnew.py) joins them back into one sentence.
- Everything else in this file (this comment, blank lines, the title above)
  is ignored by the parser.

Release discipline: bump `litman.__version__` and the test suite goes red
until this file has a matching `## X.Y.Z` section (tests/core/test_whatsnew.py),
and release.sh refuses to publish without one.
-->

## 1.3.6

- Priority is now per project; the health check migrates your library.
- Hover a highlight in any tool to read its note.
- The toolbar shield now flags problems without being clicked.
- A project folder copied from another machine repairs itself.
- Remove a link between two papers from the Relations row.

## 1.3.5

- The window refreshes itself automatically.
- Follow a citation, then Alt+← back to where you were.
- Preview a note before you save it.
- litman is now AGPL-3.0.

## 1.3.4

- On a Mac, litman opens in its own window.
- Drag tabs to reorder, right-click to close in bulk.
- Your taskbar and Dock now show litman.

## 1.3.3

- Drag a PDF in to add it.
- Edit paper metadata, and choose the id when adding.
- Pin papers to keep them at the top.
- Faster deleting, restoring and tagging in large libraries.
