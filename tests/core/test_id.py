"""Tests for `litman.core.id` — keyword + canonical id derivation."""

from __future__ import annotations

import pytest

from litman.core.id import (
    _KEYWORD_MAX_LEN,
    derive_id,
    derive_keyword,
    derive_keyword_alternatives,
    find_case_fold_collision,
    is_valid_id,
    is_weak_keyword,
    suggest_id,
)
from litman.exceptions import IDError

# ---------------------------------------------------------------------------
# derive_keyword — M2.9 upgraded heuristic
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "title,expected",
    [
        # --- Colon special-case (prefix slug < 12 chars) ---
        # "Prefix: Description" with model-name prefix < 12 chars and a
        # post-colon word whose hyphen-stripped slug >= 5 chars.
        (
            "HELM-GPT: De novo macrocyclic peptide design",
            "HELM-GPT-Macrocyclic",
        ),
        # Hyphen stripped from post-colon word for clean read.
        (
            "BERT: Pre-training of Deep Bidirectional Transformers",
            "BERT-Pretraining",
        ),
        # Short Latin connective ("De") skipped by the >=5-char rule.
        ("ESM: De novo protein design", "ESM-Protein"),
        # Prefix length boundary: 11 chars (slug "AlphaFold-3" = 11) still
        # inside the colon path.
        ("AlphaFold-3: Predicting protein structure", "AlphaFold-3-Predicting"),
        # Prefix length 12+ chars → bypass colon path → top-N from full title.
        (
            "Towards efficient: peptide design via diffusion",
            "Efficient-peptide-design",
        ),
        # Colon path with no qualifying post-colon word (all < 5 chars after
        # stop-word + length filter) → fall through to top-N.
        ("X: tiny bit", "X-tiny-bit"),
        # --- Top-N path (no colon) ---
        (
            "Cell-free biosynthesis of antimicrobial peptides",
            "Cell-free-biosynthesis-antimicrobial",
        ),
        ("A Survey on Peptide Foundation Models", "Survey-Peptide-Foundation"),
        # Stop list misses "all" / "you", so they survive (documented gap).
        ("Attention Is All You Need", "Attention-All-You"),
        ("On the dangers of stochastic parrots", "Dangers-stochastic-parrots"),
        ("Towards a unified theory of representation", "Unified-theory-representation"),
        # Single significant token.
        ("BERT", "BERT"),
        # All stop words: fall back to first raw token, capitalized.
        ("Of the for", "Of"),
        # Empty / whitespace / non-ASCII.
        ("", "untitled"),
        ("   ", "untitled"),
        ("中文标题", "untitled"),
        # Punctuation stripped, top-N still produces hyphenated keyword.
        ("(Foo): bar", "Foo-bar"),
        # Hyphenated keyword preserved through top-N path.
        ("HELM-spec examples", "HELM-spec-examples"),
    ],
)
def test_derive_keyword(title: str, expected: str) -> None:
    assert derive_keyword(title) == expected


def test_derive_keyword_truncates_at_hyphen_boundary() -> None:
    title = (
        "Comprehensive systematic exhaustive thorough rigorous evaluation "
        "of methods"
    )
    keyword = derive_keyword(title)
    # Top-3 of significant words joined would be
    # "Comprehensive-systematic-exhaustive" (35 chars) — fits under 40.
    assert keyword == "Comprehensive-systematic-exhaustive"
    assert len(keyword) <= _KEYWORD_MAX_LEN


def test_derive_keyword_truncates_long_token() -> None:
    # Single very long token with no hyphens → hard cut at MAX_LEN.
    long_word = "Supercalifragilisticexpialidociousnessitudeextraordinarius"
    keyword = derive_keyword(long_word)
    assert len(keyword) == _KEYWORD_MAX_LEN
    assert keyword == long_word[:_KEYWORD_MAX_LEN]


def test_derive_keyword_truncate_prefers_hyphen_boundary() -> None:
    # Three significant words; joined length 45 > MAX_LEN(40). Truncation
    # should cut at the last hyphen ≤ 40, not mid-word.
    title = "Multimodal pretraining of representation networks here"
    keyword = derive_keyword(title)
    # "Multimodal-pretraining-representation" = 37 chars (top-3 after drop "of").
    assert keyword == "Multimodal-pretraining-representation"
    assert "-" in keyword


# ---------------------------------------------------------------------------
# derive_keyword_alternatives — M2.9 collision fallback support
# ---------------------------------------------------------------------------


def test_alternatives_slides_window_and_skips_primary() -> None:
    title = "Cell-free biosynthesis of antimicrobial peptides at scale"
    primary = derive_keyword(title)
    assert primary == "Cell-free-biosynthesis-antimicrobial"

    alts = derive_keyword_alternatives(title, n=3)
    assert primary not in alts
    # Significant tokens: ["Cell-free","biosynthesis","antimicrobial",
    # "peptides","at","scale"] (no stop drops since "at" isn't in list).
    # offset 1 → "Biosynthesis-antimicrobial-peptides"
    # offset 2 → "Antimicrobial-peptides-at"
    # offset 3 → "Peptides-at-scale"
    assert alts == [
        "Biosynthesis-antimicrobial-peptides",
        "Antimicrobial-peptides-at",
        "Peptides-at-scale",
    ]


def test_alternatives_empty_for_short_titles() -> None:
    assert derive_keyword_alternatives("BERT", n=3) == []
    assert derive_keyword_alternatives("", n=3) == []
    assert derive_keyword_alternatives("   ", n=3) == []


def test_alternatives_dedupes_against_primary() -> None:
    # The colon path picks "HELM-GPT-Macrocyclic" as primary; alternatives
    # use top-N starting from offset 1.
    title = "HELM-GPT: De novo macrocyclic peptide design"
    primary = derive_keyword(title)
    alts = derive_keyword_alternatives(title, n=5)
    assert primary not in alts
    assert all(a != primary for a in alts)


def test_alternatives_n_zero_returns_empty() -> None:
    assert derive_keyword_alternatives("Attention is all you need", n=0) == []


# ---------------------------------------------------------------------------
# derive_id
# ---------------------------------------------------------------------------


def test_derive_id_simple() -> None:
    assert (
        derive_id(2024, "Chen", "HELM-GPT: De novo macrocyclic peptide design")
        == "2024_Chen_HELM-GPT-Macrocyclic"
    )


def test_derive_id_classic() -> None:
    assert (
        derive_id(2017, "Vaswani", "Attention Is All You Need")
        == "2017_Vaswani_Attention-All-You"
    )


def test_derive_id_normalizes_family() -> None:
    # Hyphenated last name gets de-hyphenated by the slug rule (consistent
    # with single-token expectation in the canonical id format).
    assert (
        derive_id(2023, "van der Berg", "A novel method")
        == "2023_VanderBerg_Novel-method"
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


# ---------------------------------------------------------------------------
# The ASCII gate — a title in a space-less script must not name a paper by
# whatever Latin fragment it happens to contain.
# ---------------------------------------------------------------------------


def test_a_chinese_title_with_a_compound_label_does_not_become_that_label() -> None:
    """The regression this gate exists for: `2018_Zhang_A` was born silently.

    One token (no spaces in the script), slugged down to the one Latin
    character in it. The id is a folder name and a wiki-link target, so it
    outlives the mistake in a way a wrong field does not.
    """
    assert derive_keyword("关于化合物A的合成方法") == "untitled"
    with pytest.raises(IDError, match="mostly not"):
        derive_id(2018, "Zhang", "关于化合物A的合成方法")


def test_a_wholly_chinese_title_is_refused_too() -> None:
    assert derive_keyword("气泡呼吸行为在气液反应器中的研究") == "untitled"


def test_a_mixed_title_below_the_ratio_is_refused_however_good_the_fragment() -> None:
    """35% Latin: `PROTAC` alone would be a legal id and still a poor one.

    wangq's call, deliberately the strict reading — "a Chinese title always
    needs an id from you" is easier to live with than "sometimes it works".
    """
    assert derive_keyword("一种新型 PROTAC 分子的设计与合成") == "untitled"


@pytest.mark.parametrize(
    "title,expected",
    [
        ("Bubble breathing behaviour in a gas-liquid reactor",
         "Bubble-breathing-behaviour"),
        ("BERT: Pre-training of Deep Bidirectional Transformers",
         "BERT-Pretraining"),
        ("Attention Is All You Need", "Attention-All-You"),
        # Latin script with non-ASCII letters in it stays comfortably above the
        # ratio — the gate is about scripts, not about stray diacritics. (The
        # keyword itself is poor because the stop-word list is English-only;
        # that predates the gate and is not what this asserts.)
        ("Rôle de la protéine dans la réaction", "Rle-de-la"),
        ("α-Synuclein aggregation in vivo", "-Synuclein-aggregation-vivo"),
    ],
)
def test_the_gate_leaves_ordinary_titles_alone(title: str, expected: str) -> None:
    """The gate's whole risk is false positives; these are the guard against it."""
    assert derive_keyword(title) == expected


def test_a_title_that_is_half_latin_still_passes() -> None:
    """The boundary is inclusive, and `CRISPR-Cas9` is worth keeping."""
    assert derive_id(2018, "Zhang", "CRISPR-Cas9 基因编辑技术的研究进展") == (
        "2018_Zhang_CRISPR-Cas9"
    )


# ---------------------------------------------------------------------------
# suggest_id — what the CLI error and the GUI's Paper ID field start from
# ---------------------------------------------------------------------------


def test_suggest_id_keeps_the_latin_part_of_a_refused_title() -> None:
    """The gate refuses the title as a whole; the fragment is still usable."""
    assert derive_keyword("一种新型 PROTAC 分子的设计与合成") == "untitled"
    assert suggest_id(2018, "Zhang", "一种新型 PROTAC 分子的设计与合成") == (
        "2018_Zhang_PROTAC"
    )


def test_suggest_id_refuses_to_offer_the_very_id_the_gate_rejected() -> None:
    """Offering `2018_Zhang_A` would hand back exactly what was just refused."""
    assert suggest_id(2018, "Zhang", "关于化合物A的合成方法") is None


def test_suggest_id_has_nothing_to_offer_for_a_title_with_no_latin_at_all() -> None:
    assert suggest_id(2018, "Zhang", "气泡呼吸行为在气液反应器中的研究") is None


@pytest.mark.parametrize(
    "year,family,title",
    [
        (None, "Zhang", "一种新型 PROTAC 分子的设计与合成"),
        (2018, "张", "一种新型 PROTAC 分子的设计与合成"),
        (2018, "Zhang", "   "),
    ],
)
def test_suggest_id_returns_none_when_a_segment_is_missing(
    year: int | None, family: str, title: str
) -> None:
    """Callers render None as an empty field rather than as a bad default."""
    assert suggest_id(year, family, title) is None


def test_the_error_message_carries_a_command_the_reader_can_run() -> None:
    """As often an agent mid-batch as a person; an unactionable error stalls it."""
    with pytest.raises(IDError) as exc:
        derive_id(2018, "Zhang", "一种新型 PROTAC 分子的设计与合成")
    assert "--id 2018_Zhang_PROTAC" in str(exc.value)

    with pytest.raises(IDError) as exc:
        derive_id(2018, "Zhang", "气泡呼吸行为在气液反应器中的研究")
    assert "--id 2018_Zhang_<Keyword>" in str(exc.value)


@pytest.mark.parametrize(
    "keyword,weak",
    [("A", True), ("AI", True), ("ML", True), ("PROTAC", False),
     ("Qipao-huxi", False), ("Cell-free", False)],
)
def test_is_weak_keyword(keyword: str, weak: bool) -> None:
    assert is_weak_keyword(keyword) is weak


# ---------------------------------------------------------------------------
# find_case_fold_collision — cross-platform safety (ADR-005)
# ---------------------------------------------------------------------------


def test_case_fold_collision_detects_only_case_difference() -> None:
    """Same letters, different case → clash."""
    existing = ["2023_Pandi_Cell-free", "2024_Smith_Other"]
    assert (
        find_case_fold_collision(existing, "2023_pandi_cell-free")
        == "2023_Pandi_Cell-free"
    )


def test_case_fold_collision_ignores_exact_match() -> None:
    """Byte-identical id is not a *case-only* clash — caller's exact
    collision path handles it."""
    existing = ["2023_Pandi_Cell-free"]
    assert find_case_fold_collision(existing, "2023_Pandi_Cell-free") is None


def test_case_fold_collision_no_clash() -> None:
    existing = ["2023_Pandi_Cell-free", "2024_Smith_Other"]
    assert find_case_fold_collision(existing, "2025_Jones_New") is None


def test_case_fold_collision_empty_pool() -> None:
    assert find_case_fold_collision([], "anything") is None


def test_case_fold_collision_returns_first_match() -> None:
    """Should return *some* existing id that case-folds equal — the
    contract guarantees there is at least one match; we don't pin order
    too tightly but the first matching entry is a sensible default."""
    existing = ["FOO", "Foo"]
    clash = find_case_fold_collision(existing, "foo")
    assert clash in {"FOO", "Foo"}


# ---------------------------------------------------------------------------
# is_valid_id — filesystem-safety gate
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "paper_id",
    ["2024_Foo_Bar", "a", "2023_Pandi_Cell-free", "v1.2.3-x", "A_b.c-d"],
)
def test_valid_ids_accepted(paper_id: str) -> None:
    assert is_valid_id(paper_id)


@pytest.mark.parametrize(
    "paper_id",
    [
        "",
        ".hidden",
        "a..b",
        "a/b",
        "a\\b",
        "has space",
        # Trailing dot: Windows strips it when creating the directory, so the
        # folder would exist under a different name than the id claims.
        "2024_Foo.",
        "2024_Foo_Bar.",
    ],
)
def test_invalid_ids_rejected(paper_id: str) -> None:
    assert not is_valid_id(paper_id)
