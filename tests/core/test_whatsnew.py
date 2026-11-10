"""Tests for the what's-new digest (task-whatsnew).

Two jobs. The parser edges run against literal text through ``_parse``; the
release forcing-function runs end-to-end through ``bullets_for`` — the REAL
packaged data file via importlib.resources, no injection — so bumping
``litman.__version__`` without writing a matching ``## X.Y.Z`` section in
``litman/data/whatsnew.md`` turns the suite red before release.sh even runs.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

import pytest

import litman
from litman.core import whatsnew
from litman.core.whatsnew import CHANGELOG_URL, _parse, bullets_for

SAMPLE = """# Title line (ignored)

<!-- comment (ignored) -->

## 2.0.0

- Newest bullet.

## 1.0.0

- First bullet.
- A sentence hard-wrapped by the author
  onto a continuation line.
Not a bullet, ignored.

## 0.9.0

- Old bullet.
"""


def test_parse_scopes_to_the_requested_section() -> None:
    assert _parse(SAMPLE, "2.0.0") == ["Newest bullet."]
    assert _parse(SAMPLE, "0.9.0") == ["Old bullet."]


def test_parse_joins_continuation_lines() -> None:
    assert _parse(SAMPLE, "1.0.0") == [
        "First bullet.",
        "A sentence hard-wrapped by the author onto a continuation line.",
    ]


def test_parse_unknown_version_is_empty() -> None:
    assert _parse(SAMPLE, "9.9.9") == []


def test_bullets_for_current_version_nonempty() -> None:
    """RELEASE GATE: the running version must have a digest in the packaged
    file. Red here means `__version__` was bumped without adding the matching
    `## X.Y.Z` section to src/litman/data/whatsnew.md."""
    bullets = bullets_for(litman.__version__)
    assert bullets, (
        f"litman/data/whatsnew.md has no `## {litman.__version__}` section — "
        "write the user-facing highlights for this release."
    )
    assert all(isinstance(b, str) and b for b in bullets)


def test_bullets_for_current_version_nonempty_zh() -> None:
    """RELEASE GATE, Chinese half: the GUI shows these when it runs in Chinese.
    Red here means the release has English highlights but no matching
    `## X.Y.Z` section in src/litman/data/whatsnew.zh.md."""
    bullets = bullets_for(litman.__version__, "zh")
    assert bullets, (
        f"litman/data/whatsnew.zh.md has no `## {litman.__version__}` section — "
        "write the Chinese highlights for this release."
    )
    assert all(isinstance(b, str) and b for b in bullets)


@pytest.mark.parametrize("name", ["whatsnew.md", "whatsnew.zh.md"])
def test_packaged_file_sections_are_versions(name: str) -> None:
    """Every `## ` heading in a shipped file is a plain X.Y.Z — a typo'd
    heading would silently orphan its bullets."""
    from importlib.resources import files

    text = files("litman.data").joinpath(name).read_text(encoding="utf-8")
    headings = [ln[3:].strip() for ln in text.splitlines() if ln.startswith("## ")]
    assert headings, f"packaged {name} has no version sections"
    for h in headings:
        assert re.fullmatch(r"\d+\.\d+\.\d+", h), f"malformed version heading: {h!r}"


def test_parse_joins_chinese_continuation_without_space() -> None:
    """A wrapped Chinese bullet joins back with no space between characters
    (one there reads as a typo); English wraps keep theirs (tested above)."""
    text = "## 1.0.0\n\n- 第一行\n  接着写。\n"
    assert _parse(text, "1.0.0") == ["第一行接着写。"]


def test_parse_empty_bullet_then_continuation_does_not_raise() -> None:
    """A bare `- ` followed by an indented line must not crash the popup's
    "never raises" read: the continuation simply becomes the bullet."""
    assert _parse("## 1.0.0\n\n- \n  late text\n", "1.0.0") == ["late text"]


def test_bullets_for_unreadable_file_is_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    """Best-effort contract: a broken install resolves to [], never raises."""

    def _boom(_pkg: str) -> None:
        raise OSError("no such resource")

    monkeypatch.setattr(whatsnew, "files", _boom)
    assert bullets_for("1.0.0") == []


def test_changelog_url_is_https() -> None:
    assert CHANGELOG_URL.startswith("https://")


def test_changelog_url_matches_pyproject() -> None:
    """The popup's "Read more" and the packaging metadata name one page.

    They drifted once already: the docs site went live, [project.urls] moved to
    it, and this constant stayed on the GitHub blob view — so a user clicking
    inside the app and a user clicking through PyPI landed in different places.
    A comment saying "keep in sync" did not catch it; this does.
    """
    pyproject = Path(__file__).resolve().parents[2] / "pyproject.toml"
    with pyproject.open("rb") as fh:
        urls = tomllib.load(fh)["project"]["urls"]
    assert CHANGELOG_URL == urls["Changelog"]
