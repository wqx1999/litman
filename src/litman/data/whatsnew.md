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

## 1.3.3

- Drag a PDF in to add it, even without a DOI.
- Edit paper metadata, and choose the id when adding.
- Pin papers to keep them at the top.
- Faster deleting, restoring and tagging in large libraries.
