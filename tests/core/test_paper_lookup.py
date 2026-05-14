"""Tests for ``core.paper_lookup`` — fuzzy id resolution + DOI reverse-lookup
+ shell-completion callback (M11).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from ruamel.yaml import YAML

from litman.core.library import create_vault
from litman.core.paper_lookup import (
    complete_paper_id,
    find_paper_id_by_doi,
    resolve_paper_id,
    resolve_paper_input,
)
from litman.exceptions import LitmanError, PaperNotFoundError


def _write_paper(
    vault: Path,
    paper_id: str,
    *,
    doi: str | None = None,
    extra: dict[str, Any] | None = None,
) -> None:
    paper_dir = vault / "papers" / paper_id
    paper_dir.mkdir(parents=True)
    payload: dict[str, Any] = {
        "id": paper_id,
        "title": f"Title for {paper_id}",
        "year": 2024,
    }
    if doi is not None:
        payload["doi"] = doi
    if extra:
        payload.update(extra)
    yaml = YAML()
    yaml.default_flow_style = False
    with (paper_dir / "metadata.yaml").open("w", encoding="utf-8") as f:
        yaml.dump(payload, f)


@pytest.fixture
def vault(tmp_path: Path) -> Path:
    """Three papers — two share a 'Pandi' family, one disjoint."""
    v = create_vault(tmp_path)
    _write_paper(v, "2023_Pandi_Cell-free", doi="10.1038/x")
    _write_paper(v, "2024_Pandi_Synthesis", doi="10.1038/y")
    _write_paper(v, "2024_Jones_Bar", doi="10.1093/foo")
    return v


# ---------------------------------------------------------------------------
# resolve_paper_id
# ---------------------------------------------------------------------------


def test_resolve_exact_id(vault: Path) -> None:
    assert resolve_paper_id(vault, "2023_Pandi_Cell-free") == "2023_Pandi_Cell-free"


def test_resolve_substring_unique(vault: Path) -> None:
    """A substring matching exactly one paper resolves to it."""
    assert resolve_paper_id(vault, "Synthesis") == "2024_Pandi_Synthesis"


def test_resolve_substring_unique_keyword(vault: Path) -> None:
    """Even the year-author prefix returns the unique match."""
    assert resolve_paper_id(vault, "2023_Pandi") == "2023_Pandi_Cell-free"


def test_resolve_substring_ambiguous_lists_candidates(vault: Path) -> None:
    """'Pandi' matches two papers — the error must surface both ids."""
    with pytest.raises(PaperNotFoundError) as excinfo:
        resolve_paper_id(vault, "Pandi")
    msg = str(excinfo.value)
    assert "Ambiguous" in msg
    assert "2023_Pandi_Cell-free" in msg
    assert "2024_Pandi_Synthesis" in msg
    assert "more specific" in msg


def test_resolve_substring_none_raises(vault: Path) -> None:
    with pytest.raises(PaperNotFoundError) as excinfo:
        resolve_paper_id(vault, "Nonexistent")
    assert "No paper matching" in str(excinfo.value)
    assert "Nonexistent" in str(excinfo.value)


def test_resolve_case_fold(vault: Path) -> None:
    """Substring match is case-insensitive."""
    assert resolve_paper_id(vault, "pandi_synthesis") == "2024_Pandi_Synthesis"
    assert resolve_paper_id(vault, "JONES") == "2024_Jones_Bar"


def test_resolve_skips_dirs_without_metadata(tmp_path: Path) -> None:
    """A folder under papers/ without metadata.yaml is not a candidate."""
    v = create_vault(tmp_path)
    _write_paper(v, "2024_Real_Paper")
    (v / "papers" / "2024_Empty_Dir").mkdir()
    with pytest.raises(PaperNotFoundError):
        resolve_paper_id(v, "Empty")


def test_resolve_empty_vault_raises(tmp_path: Path) -> None:
    v = create_vault(tmp_path)
    with pytest.raises(PaperNotFoundError):
        resolve_paper_id(v, "anything")


# ---------------------------------------------------------------------------
# find_paper_id_by_doi
# ---------------------------------------------------------------------------


def test_find_paper_id_by_doi_hit(vault: Path) -> None:
    assert find_paper_id_by_doi(vault, "10.1038/x") == "2023_Pandi_Cell-free"


def test_find_paper_id_by_doi_case_insensitive(vault: Path) -> None:
    """DOIs are case-insensitive per the DOI Handbook."""
    assert (
        find_paper_id_by_doi(vault, "10.1038/X") == "2023_Pandi_Cell-free"
    )


def test_find_paper_id_by_doi_miss_raises(vault: Path) -> None:
    with pytest.raises(PaperNotFoundError) as excinfo:
        find_paper_id_by_doi(vault, "10.9999/ghost")
    assert "No paper with DOI" in str(excinfo.value)
    assert "10.9999/ghost" in str(excinfo.value)


def test_find_paper_id_by_doi_empty_raises(vault: Path) -> None:
    with pytest.raises(PaperNotFoundError):
        find_paper_id_by_doi(vault, "")


# ---------------------------------------------------------------------------
# resolve_paper_input — XOR dispatch
# ---------------------------------------------------------------------------


def test_resolve_input_paper_id_path(vault: Path) -> None:
    assert (
        resolve_paper_input(vault, "Synthesis", None) == "2024_Pandi_Synthesis"
    )


def test_resolve_input_doi_path(vault: Path) -> None:
    assert (
        resolve_paper_input(vault, None, "10.1038/x")
        == "2023_Pandi_Cell-free"
    )


def test_resolve_input_both_set_raises(vault: Path) -> None:
    with pytest.raises(LitmanError) as excinfo:
        resolve_paper_input(vault, "Synthesis", "10.1038/x")
    assert "mutually exclusive" in str(excinfo.value)


def test_resolve_input_neither_set_raises(vault: Path) -> None:
    with pytest.raises(LitmanError) as excinfo:
        resolve_paper_input(vault, None, None)
    assert "No paper specified" in str(excinfo.value)


def test_resolve_input_empty_string_treated_as_none(vault: Path) -> None:
    """Empty string from a default-None click option counts as 'not set'."""
    with pytest.raises(LitmanError):
        resolve_paper_input(vault, "", "")


# ---------------------------------------------------------------------------
# complete_paper_id — shell completion callback
# ---------------------------------------------------------------------------


def test_complete_paper_id_prefix_filter(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With a vault on $LIT_LIBRARY, completion returns prefix matches."""
    monkeypatch.setenv("LIT_LIBRARY", str(vault))
    monkeypatch.chdir(vault.parent)
    out = complete_paper_id(None, None, "2024_")
    assert "2024_Pandi_Synthesis" in out
    assert "2024_Jones_Bar" in out
    assert "2023_Pandi_Cell-free" not in out


def test_complete_paper_id_empty_incomplete_returns_all(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LIT_LIBRARY", str(vault))
    monkeypatch.chdir(vault.parent)
    out = complete_paper_id(None, None, "")
    assert set(out) == {
        "2023_Pandi_Cell-free",
        "2024_Pandi_Synthesis",
        "2024_Jones_Bar",
    }


def test_complete_paper_id_no_match_returns_empty(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LIT_LIBRARY", str(vault))
    monkeypatch.chdir(vault.parent)
    assert complete_paper_id(None, None, "ZZZ") == []


def test_complete_paper_id_failure_safe_no_vault(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If no vault is discoverable anywhere, completion returns []."""
    monkeypatch.delenv("LIT_LIBRARY", raising=False)
    empty = tmp_path / "empty"
    empty.mkdir()
    monkeypatch.chdir(empty)
    assert complete_paper_id(None, None, "anything") == []


def test_complete_paper_id_failure_safe_broken_vault(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A vault whose papers/ dir was deleted still returns [] (not raise)."""
    v = create_vault(tmp_path)
    (v / "papers").rmdir()
    monkeypatch.setenv("LIT_LIBRARY", str(v))
    monkeypatch.chdir(v.parent)
    assert complete_paper_id(None, None, "") == []


def test_complete_paper_id_failure_safe_swallows_arbitrary_exceptions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Catch-all: any unexpected exception bubbling up from find_vault
    must be swallowed so shell completion never breaks the user's shell.
    """
    from litman.core import paper_lookup as pl

    def boom() -> Path:
        raise RuntimeError("synthetic failure")

    monkeypatch.setattr(pl, "find_vault", boom)
    assert complete_paper_id(None, None, "x") == []
