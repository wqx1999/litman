"""Tests for M8.4 cross-vault wikilinks ``[[vault-name:paper-id]]``.

Three layers of coverage:

1. ``parse_wikilink_target`` parametrized unit tests — the pure parser
   for the inner text of a ``[[...]]`` wikilink.
2. ``check_dangling_wikilinks`` core integration: registered vault +
   correct id resolves, every dangling failure mode (unregistered
   vault, registered-but-unreadable vault, wrong paper id, malformed
   prefix forms) surfaces an explicit Issue.
3. ``lit health-check`` end-to-end with two vaults + cross-vault refs.

Every test redirects ``$HOME`` so the registry lands in tmp.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from click.testing import CliRunner
from ruamel.yaml import YAML

from litman.cli import cli
from litman.core.checks import check_dangling_wikilinks
from litman.core.document import list_papers
from litman.core.library import create_vault
from litman.core.notes import parse_wikilink_target
from litman.core.vault_registry import (
    add_vault,
    save_registry,
)
from litman.core.vault_registry import VaultRegistry

_yaml = YAML()


# ---------------------------------------------------------------------------
# parse_wikilink_target
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw,expected",
    [
        # Legacy same-vault form.
        ("2024_Wang_AMP", (None, "2024_Wang_AMP")),
        ("p1", (None, "p1")),
        # Cross-vault form.
        ("zhang:2024_Wang_AMP", ("zhang", "2024_Wang_AMP")),
        ("my-main:p1", ("my-main", "p1")),
        # Whitespace stripped on both halves.
        ("  zhang  :  id  ", ("zhang", "id")),
        # First colon only splits — paper ids never carry colons, so any
        # second colon goes into the paper-id half and will fail to resolve.
        ("vault:a:b", ("vault", "a:b")),
        # Empty halves preserved (caller decides whether to flag).
        (":id", ("", "id")),
        ("vault:", ("vault", "")),
        # Bare empty input → same-vault, empty id (caller filters).
        ("", (None, "")),
    ],
)
def test_parse_wikilink_target(raw: str, expected: tuple[str | None, str]) -> None:
    assert parse_wikilink_target(raw) == expected


# ---------------------------------------------------------------------------
# Fixtures shared by the integration tests
# ---------------------------------------------------------------------------


@pytest.fixture
def fake_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("LIT_LIBRARY", raising=False)
    return home


@pytest.fixture
def vault_a(tmp_path: Path) -> Path:
    parent = tmp_path / "parent_a"
    parent.mkdir()
    return create_vault(parent, name="vault_a")


@pytest.fixture
def vault_b(tmp_path: Path) -> Path:
    parent = tmp_path / "parent_b"
    parent.mkdir()
    return create_vault(parent, name="vault_b")


def _seed_paper(
    vault: Path, paper_id: str, title: str = "Test", notes_body: str = ""
) -> None:
    """Drop a minimal paper folder with metadata + notes.md."""
    paper_dir = vault / "papers" / paper_id
    paper_dir.mkdir(parents=True, exist_ok=True)
    meta = {
        "id": paper_id,
        "title": title,
        "year": 2024,
        "status": "inbox",
        "priority": "B",
        "type": "research",
        "doi": f"10.fake/{paper_id}",
        "projects": [],
        "topics": [],
        "methods": [],
        "data": [],
        "authors": ["Test, A."],
        "created-at": "2026-05-12T10:00:00+02:00",
        "updated-at": "2026-05-12T10:00:00+02:00",
    }
    with (paper_dir / "metadata.yaml").open("w", encoding="utf-8") as f:
        _yaml.dump(meta, f)
    (paper_dir / "notes.md").write_text(notes_body, encoding="utf-8")


def _write_index(vault: Path) -> None:
    """Regenerate INDEX.json from the on-disk paper set."""
    papers = list_papers(vault)
    payload = {
        "schema_version": 1,
        "updated_at": "2026-05-12T10:00:00+02:00",
        "papers": [
            {
                "id": p.get("id"),
                "title": p.get("title"),
                "year": p.get("year"),
                "status": p.get("status"),
                "priority": p.get("priority"),
                "type": p.get("type"),
            }
            for p in papers
        ],
        "by_doi": {},
    }
    (vault / "INDEX.json").write_text(
        json.dumps(payload, indent=2), encoding="utf-8"
    )


# ---------------------------------------------------------------------------
# Same-vault behavior unchanged
# ---------------------------------------------------------------------------


def test_same_vault_dangling_link_still_detected(
    fake_home: Path, vault_a: Path
) -> None:
    """A plain ``[[ghost]]`` (no vault prefix, no ``(deleted)`` marker) is
    still reported even when no registry is configured. M24 reclassified this
    same-vault case from an error to a missing-tag warning (the filesystem
    cannot tell "deleted" from "never existed"), but the link is still
    surfaced one-per-file."""
    _seed_paper(vault_a, "p1", notes_body="See [[ghost]]\n")
    issues = check_dangling_wikilinks(vault_a, list_papers(vault_a))
    assert len(issues) == 1
    assert "[[ghost]]" in issues[0].message
    assert issues[0].severity == "warning"
    assert "not tagged" in issues[0].message


def test_same_vault_resolved_link_not_flagged(
    fake_home: Path, vault_a: Path
) -> None:
    _seed_paper(vault_a, "p1")
    _seed_paper(vault_a, "p2", notes_body="cf. [[p1]]\n")
    assert check_dangling_wikilinks(vault_a, list_papers(vault_a)) == []


# ---------------------------------------------------------------------------
# Cross-vault: happy path
# ---------------------------------------------------------------------------


def test_cross_vault_link_resolved_when_target_exists(
    fake_home: Path, vault_a: Path, vault_b: Path
) -> None:
    """Register vault_b, drop a [[second:p1]] in vault_a notes → clean."""
    reg = add_vault(VaultRegistry(), "main", vault_a)
    reg = add_vault(reg, "second", vault_b)
    save_registry(reg)

    _seed_paper(vault_b, "p1")
    _write_index(vault_b)

    _seed_paper(vault_a, "n1", notes_body="see [[second:p1]]\n")

    issues = check_dangling_wikilinks(vault_a, list_papers(vault_a))
    assert issues == []


def test_cross_vault_link_caches_target_lookup(
    fake_home: Path, vault_a: Path, vault_b: Path
) -> None:
    """Many refs to the same target vault don't re-read its INDEX.json
    once per occurrence. We verify this indirectly: with 50 references
    the run completes (no perf catastrophe) and yields zero issues."""
    reg = add_vault(VaultRegistry(), "main", vault_a)
    reg = add_vault(reg, "second", vault_b)
    save_registry(reg)

    _seed_paper(vault_b, "p1")
    _write_index(vault_b)

    body = "\n".join(f"line {i}: see [[second:p1]]" for i in range(50))
    _seed_paper(vault_a, "n1", notes_body=body)

    issues = check_dangling_wikilinks(vault_a, list_papers(vault_a))
    assert issues == []


# ---------------------------------------------------------------------------
# Cross-vault: failure modes
# ---------------------------------------------------------------------------


def test_cross_vault_unregistered_prefix_is_dangling(
    fake_home: Path, vault_a: Path
) -> None:
    """[[ghost:p1]] when no vault named 'ghost' is in the registry."""
    _seed_paper(vault_a, "n1", notes_body="see [[ghost:p1]]\n")
    # No registry exists yet — the check should still fire and produce a
    # clear "unregistered vault" message rather than silent acceptance.
    issues = check_dangling_wikilinks(vault_a, list_papers(vault_a))
    assert len(issues) == 1
    assert "unregistered" in issues[0].message
    assert "ghost" in issues[0].message
    assert "lit vault add" in (issues[0].hint or "")


def test_cross_vault_registered_but_directory_gone(
    fake_home: Path, vault_a: Path, vault_b: Path
) -> None:
    """Vault registered, but the on-disk directory has since vanished."""
    reg = add_vault(VaultRegistry(), "main", vault_a)
    reg = add_vault(reg, "second", vault_b)
    save_registry(reg)

    _seed_paper(vault_b, "p1")
    _write_index(vault_b)
    _seed_paper(vault_a, "n1", notes_body="see [[second:p1]]\n")

    # Now drop vault_b from disk (simulate unmount / accidental rm).
    import shutil
    shutil.rmtree(vault_b)

    issues = check_dangling_wikilinks(vault_a, list_papers(vault_a))
    assert len(issues) == 1
    assert "unreadable" in issues[0].message
    assert "second" in issues[0].message


def test_cross_vault_registered_but_no_index_json(
    fake_home: Path, vault_a: Path, vault_b: Path
) -> None:
    """Vault registered, directory there, but INDEX.json missing."""
    reg = add_vault(VaultRegistry(), "main", vault_a)
    reg = add_vault(reg, "second", vault_b)
    save_registry(reg)

    _seed_paper(vault_a, "n1", notes_body="see [[second:p1]]\n")
    # vault_b lacks INDEX.json (we never wrote one and create_vault doesn't
    # emit one without explicit refresh).
    # Actually create_vault DOES seed INDEX.json — remove it manually.
    (vault_b / "INDEX.json").unlink()

    issues = check_dangling_wikilinks(vault_a, list_papers(vault_a))
    assert len(issues) == 1
    assert "unreadable" in issues[0].message


def test_cross_vault_registered_but_id_not_in_target(
    fake_home: Path, vault_a: Path, vault_b: Path
) -> None:
    """Vault registered, INDEX.json there, but target id doesn't exist."""
    reg = add_vault(VaultRegistry(), "main", vault_a)
    reg = add_vault(reg, "second", vault_b)
    save_registry(reg)

    _seed_paper(vault_b, "real_paper")
    _write_index(vault_b)
    _seed_paper(vault_a, "n1", notes_body="see [[second:ghost_paper]]\n")

    issues = check_dangling_wikilinks(vault_a, list_papers(vault_a))
    assert len(issues) == 1
    assert "no paper id" in issues[0].message
    assert "second" in issues[0].message
    assert "ghost_paper" in issues[0].message
    assert "lit list --vault second" in (issues[0].hint or "")


def test_cross_vault_malformed_empty_vault_prefix(
    fake_home: Path, vault_a: Path
) -> None:
    """[[:p1]] — empty vault name is malformed."""
    _seed_paper(vault_a, "n1", notes_body="see [[:p1]]\n")
    issues = check_dangling_wikilinks(vault_a, list_papers(vault_a))
    assert len(issues) == 1
    assert "malformed" in issues[0].message


def test_cross_vault_malformed_empty_paper_id(
    fake_home: Path, vault_a: Path
) -> None:
    """[[vault:]] — empty paper id is malformed."""
    _seed_paper(vault_a, "n1", notes_body="see [[main:]]\n")
    issues = check_dangling_wikilinks(vault_a, list_papers(vault_a))
    assert len(issues) == 1
    assert "malformed" in issues[0].message


# ---------------------------------------------------------------------------
# Multiple cross-vault references in one note
# ---------------------------------------------------------------------------


def test_cross_vault_mixed_resolved_and_dangling(
    fake_home: Path, vault_a: Path, vault_b: Path
) -> None:
    """One note with a mix of good + bad cross-vault refs reports only
    the bad ones, in order."""
    reg = add_vault(VaultRegistry(), "main", vault_a)
    reg = add_vault(reg, "second", vault_b)
    save_registry(reg)

    _seed_paper(vault_b, "good_paper")
    _write_index(vault_b)

    _seed_paper(
        vault_a,
        "n1",
        notes_body=(
            "good: [[second:good_paper]]\n"
            "bad id: [[second:nonexistent]]\n"
            "bad vault: [[ghost:any]]\n"
        ),
    )
    issues = check_dangling_wikilinks(vault_a, list_papers(vault_a))
    assert len(issues) == 2
    msgs = " | ".join(i.message for i in issues)
    assert "nonexistent" in msgs
    assert "ghost" in msgs
    assert "good_paper" not in msgs  # the resolved one not in any issue


# ---------------------------------------------------------------------------
# Corrupt registry doesn't brick the check
# ---------------------------------------------------------------------------


def test_corrupt_registry_does_not_crash_check(
    fake_home: Path, vault_a: Path
) -> None:
    """A malformed vaults.yaml shouldn't bring down health-check; the
    cross-vault links just become dangling with the unregistered-vault
    message."""
    config_dir = fake_home / ".config" / "litman"
    config_dir.mkdir(parents=True)
    (config_dir / "vaults.yaml").write_text(
        "not: valid: yaml: : : :", encoding="utf-8"
    )

    _seed_paper(vault_a, "n1", notes_body="see [[ghost:p1]]\n")
    issues = check_dangling_wikilinks(vault_a, list_papers(vault_a))
    assert len(issues) == 1
    assert "unregistered" in issues[0].message


# ---------------------------------------------------------------------------
# Full lit health-check integration
# ---------------------------------------------------------------------------


def test_cli_health_check_flags_cross_vault_dangling(
    fake_home: Path, vault_a: Path, vault_b: Path
) -> None:
    """End-to-end through ``lit health-check`` — the dangling cross-vault
    link should turn up in the report."""
    reg = add_vault(VaultRegistry(), "main", vault_a)
    reg = add_vault(reg, "second", vault_b)
    save_registry(reg)

    _seed_paper(vault_b, "real_paper")
    _write_index(vault_b)
    _seed_paper(vault_a, "n1", notes_body="see [[second:ghost_paper]]\n")

    runner = CliRunner()
    result = runner.invoke(cli, ["health-check", "--vault", "main"])
    assert result.exit_code == 1, result.output  # non-zero on issues
    # The CLI renders the category as the friendly header "Dangling [[id]]
    # wikilinks in notes" (see _CATEGORY_HEADERS in commands/health.py).
    assert "wikilinks" in result.output.lower()
    assert "ghost_paper" in result.output


def test_cli_health_check_clean_with_resolved_cross_vault_link(
    fake_home: Path,
    vault_a: Path,
    vault_b: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cross-vault link that resolves → health-check exits 0."""
    # Pin DISPLAY so the pdf_viewer probe is deterministically clean on a
    # headless host (this asserts cross-vault link resolution, not viewer
    # availability). setenv (not sys.platform=darwin) keeps the --vault
    # registry lookup untouched.
    monkeypatch.setenv("DISPLAY", ":0")
    reg = add_vault(VaultRegistry(), "main", vault_a)
    reg = add_vault(reg, "second", vault_b)
    save_registry(reg)

    _seed_paper(vault_b, "p1")
    _write_index(vault_b)
    _seed_paper(vault_a, "n1", notes_body="see [[second:p1]]\n")

    runner = CliRunner()
    result = runner.invoke(cli, ["health-check", "--vault", "main"])
    assert result.exit_code == 0, result.output
