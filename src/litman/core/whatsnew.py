"""User-facing release highlights ("What's new").

The bullets live in ``litman/data/whatsnew.md`` — a hand-curated, plain-language
digest of CHANGELOG.md, one ``## X.Y.Z`` section per release, newest first. The
same text feeds the GUI's post-update popup (``GET /api/whatsnew``) and release
announcements, so it is written once per release and reused verbatim.

PURE LOCAL READ: the file ships inside the installed package, so the popup can
never depend on the network. What the *next* release will bring is deliberately
out of scope — the update-check probe caches only a version number, and teaching
it to fetch and trust remote prose would trade its never-blocks/zero-telemetry
discipline for a preview the user sees again right after updating anyway.
"""

from __future__ import annotations

from importlib.resources import files

# The "Read more" target of the popup. Must equal [project.urls] Changelog in
# pyproject.toml — ``test_changelog_url_matches_pyproject`` pins the two
# together, because keeping it a comment alone did not work: when the docs site
# went live the pyproject entry moved and this constant was left behind on the
# GitHub blob view.
CHANGELOG_URL = "https://litman.dev/docs/changelog/"


def _parse(text: str, version: str) -> list[str]:
    """Extract the ``- `` bullets of the ``## <version>`` section of *text*.

    Continuation lines (indented, non-bullet) are joined back onto the previous
    bullet so authors may hard-wrap long sentences. Anything outside the
    section — headers, comments, blank lines — is ignored. Unknown version ⇒ [].
    """
    bullets: list[str] = []
    in_section = False
    for line in text.splitlines():
        if line.startswith("## "):
            in_section = line[3:].strip() == version
            continue
        if not in_section:
            continue
        if line.startswith("- "):
            bullets.append(line[2:].strip())
        elif bullets and line.startswith(" ") and line.strip():
            bullets[-1] += " " + line.strip()
    return bullets


def bullets_for(version: str) -> list[str]:
    """Highlight bullets for *version*, or ``[]`` when none are recorded.

    Best-effort by contract: a missing or unreadable data file resolves to an
    empty list, never an exception — the popup simply stays closed.
    """
    try:
        text = files("litman.data").joinpath("whatsnew.md").read_text(encoding="utf-8")
    except OSError:
        return []
    return _parse(text, version)
