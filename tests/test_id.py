"""Tests for `litman.core.id` — keyword + canonical id derivation."""

from __future__ import annotations

import pytest

from litman.core.id import derive_id, derive_keyword
from litman.exceptions import IDError

# ---------------------------------------------------------------------------
# derive_keyword
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "title,expected",
    [
        # Colon split: take part before colon.
        ("HELM-GPT: De novo macrocyclic peptide design", "HELM-GPT"),
        ("BERT: Pre-training of Deep Bidirectional Transformers", "BERT"),
        # No colon: take first significant word, capitalized.
        ("Attention Is All You Need", "Attention"),
        ("On the dangers of stochastic parrots", "Dangers"),
        ("Towards a unified theory of representation", "Unified"),
        # Single token title.
        ("BERT", "BERT"),
        # All stop words: fall back to first raw token.
        ("Of the for", "Of"),
        # Empty / unusable.
        ("", "untitled"),
        ("   ", "untitled"),
        # Punctuation around the first word gets stripped.
        ("(Foo): bar", "Foo"),
        # Hyphenated keyword preserved.
        ("HELM-spec examples", "HELM-spec"),
        # Non-ASCII gets stripped; remaining empty → untitled.
        ("中文标题", "untitled"),
    ],
)
def test_derive_keyword(title: str, expected: str) -> None:
    assert derive_keyword(title) == expected


def test_derive_keyword_truncates_long_token() -> None:
    long_word = "Supercalifragilisticexpialidociousnessitudeextra"
    keyword = derive_keyword(long_word)
    assert len(keyword) == 30
    assert keyword == long_word[:30].capitalize()


# ---------------------------------------------------------------------------
# derive_id
# ---------------------------------------------------------------------------


def test_derive_id_simple() -> None:
    assert (
        derive_id(2024, "Chen", "HELM-GPT: De novo macrocyclic peptide design")
        == "2024_Chen_HELM-GPT"
    )


def test_derive_id_classic() -> None:
    assert (
        derive_id(2017, "Vaswani", "Attention Is All You Need")
        == "2017_Vaswani_Attention"
    )


def test_derive_id_normalizes_family() -> None:
    # Hyphenated last name gets de-hyphenated by the slug rule (consistent
    # with single-token expectation in the canonical id format).
    assert (
        derive_id(2023, "van der Berg", "A novel method")
        == "2023_VanderBerg_Novel"
    )


def test_derive_id_missing_year_raises() -> None:
    with pytest.raises(IDError, match="year"):
        derive_id(None, "Smith", "Some title")


def test_derive_id_non_int_year_raises() -> None:
    with pytest.raises(IDError, match="integer"):
        derive_id("2024", "Smith", "Some title")  # type: ignore[arg-type]


def test_derive_id_empty_family_raises() -> None:
    with pytest.raises(IDError, match="family"):
        derive_id(2024, "", "Some title")


def test_derive_id_non_ascii_family_raises() -> None:
    with pytest.raises(IDError, match="family"):
        derive_id(2024, "张", "Some title")


def test_derive_id_untitled_raises() -> None:
    with pytest.raises(IDError, match="title"):
        derive_id(2024, "Smith", "")
