"""Tests for ``litman.core.taxonomy_similarity.similar_values``.

The rules have to catch the ways a vocabulary drifts (a plural, a hyphen, a
prefix, a typo) and stay quiet on vocabularies that are fine as they are. Both
sides are pinned with real word lists: a 36-topic sample library with eight
values drifted into it on purpose, and the ten values of a real library.
"""

from __future__ import annotations

from itertools import combinations

import pytest

from litman.core.taxonomy_similarity import similar_values

SAME = "same after normalisation"

# The sample library's topics: none of them is a drift of another.
SAMPLE_TOPICS = [
    "active-learning",
    "adme-tox",
    "antimicrobial-peptides",
    "benchmarking",
    "binding-affinity",
    "cell-penetrating-peptides",
    "cheminformatics",
    "chemistry-model",
    "conformer-generation",
    "cyclic-peptides",
    "diffusion-models",
    "docking",
    "generative-models",
    "graph-learning",
    "immunogenicity",
    "interpretability",
    "language-models",
    "macrocycles",
    "molecular-representation",
    "molecule-description",
    "native-chemical-ligation",
    "natural-products",
    "pep-descrip-conversion",
    "peptide-descriptors",
    "peptide-design",
    "peptide-permeability",
    "peptide-property-prediction",
    "peptide-synthesis",
    "protein-design",
    "protein-structure",
    "reaction-prediction",
    "retrosynthesis",
    "spps",
    "stapled-peptides",
    "transfer-learning",
    "uncertainty",
]

# Eight values drifted into the sample on purpose, each with the closest
# value it must be matched to once all 44 are in one dict.
DRIFTED = [
    ("peptidedrug", ("peptide", "prefix")),
    ("peptide", ("peptidedrug", "prefix")),
    ("Peptide Design", ("peptide-design", SAME)),
    ("peptides-design", ("Peptide Design", SAME)),
    ("cheminformatic", ("cheminformatics", SAME)),
    ("retro-synthesis", ("retrosynthesis", SAME)),
    ("language-model", ("language-models", SAME)),
    ("molecular-representations", ("molecular-representation", SAME)),
]

# A real library's whole vocabulary: its topics and methods.
REAL_VALUES = [
    "cheminformatics",
    "chemistry model",
    "molecule description",
    "natural product",
    "pep-descrip-conversion",
    "pep-property-prediction",
    "retrosynthesis",
    "spps",
    "BERT",
    "MPNN",
]


def _pairs(values: list[str]) -> list[tuple[str, str, str]]:
    return [
        (a, other, reason)
        for i, a in enumerate(values)
        for other, reason in similar_values(a, values[i + 1 :])
    ]


@pytest.mark.parametrize(("candidate", "closest"), DRIFTED, ids=[d for d, _ in DRIFTED])
def test_every_drifted_value_is_caught_with_its_reason(
    candidate: str, closest: tuple[str, str]
) -> None:
    pool = SAMPLE_TOPICS + [d for d, _ in DRIFTED]
    assert similar_values(candidate, pool)[0] == closest


def test_the_drifted_sample_holds_fifteen_pairs() -> None:
    pairs = _pairs(SAMPLE_TOPICS + [d for d, _ in DRIFTED])
    assert len(pairs) == 15
    drifted = {d for d, _ in DRIFTED}
    assert all(a in drifted or b in drifted for a, b, _ in pairs)


@pytest.mark.parametrize(
    "values", [SAMPLE_TOPICS, REAL_VALUES], ids=["sample-36", "real-10"]
)
def test_a_clean_vocabulary_has_no_pairs(values: list[str]) -> None:
    for a, b in combinations(values, 2):
        assert similar_values(a, [b]) == [], (a, b)
        assert similar_values(b, [a]) == [], (b, a)


def test_a_short_word_claims_no_prefix() -> None:
    assert similar_values("abc", ["abcd"]) == []
    assert similar_values("spps", ["sppss-review"]) == []


def test_a_plural_is_the_same_value() -> None:
    assert similar_values("transformer", ["transformers"]) == [("transformers", SAME)]


def test_word_boundaries_do_not_count() -> None:
    """Spaces, hyphens and underscores are all dropped, not just unified."""
    assert similar_values("retro-synthesis", ["retrosynthesis"]) == [
        ("retrosynthesis", SAME)
    ]
    assert similar_values("Peptide_Design", ["peptide design"]) == [
        ("peptide design", SAME)
    ]


def test_near_spellings_report_their_distance() -> None:
    assert similar_values("tokenization", ["tokenisation"]) == [
        ("tokenisation", "edit distance 1")
    ]
    # An adjacent swap is one edit, not two.
    assert similar_values("retrosynthesis", ["retorsynthesis"]) == [
        ("retorsynthesis", "edit distance 1")
    ]
    assert similar_values("tokenization", ["tokanisation"]) == [
        ("tokanisation", "edit distance 2")
    ]
    assert similar_values("tokenization", ["tokanisatiom"]) == []


def test_an_edit_needs_eight_characters() -> None:
    assert similar_values("docking", ["ducking"]) == []
    assert similar_values("dockingx", ["duckingx"]) == [("duckingx", "edit distance 1")]


def test_different_chinese_values_do_not_match() -> None:
    assert similar_values("多肽设计", ["分子对接", "蛋白质结构"]) == []


def test_chinese_values_are_compared_as_written() -> None:
    assert similar_values("蛋白质结构预测", ["蛋白质结构预测方法"]) == [
        ("蛋白质结构预测方法", "prefix")
    ]


def test_a_value_of_only_punctuation_matches_nothing() -> None:
    assert similar_values("???", ["!!!", "—", "peptide"]) == []
    assert similar_values("peptide", ["???"]) == []


def test_the_candidate_itself_is_skipped() -> None:
    assert similar_values("peptide", ["peptide", "peptides"]) == [("peptides", SAME)]


def test_matches_come_closest_first() -> None:
    """Rule first, then closeness, then the value alphabetically."""
    existing = [
        "tokanisation",
        "tokenizationxyz",
        "tokenisation",
        "Tokenizations",
        "tokenizationx",
        "tokenizationy",
    ]
    assert similar_values("tokenization", existing) == [
        ("Tokenizations", SAME),
        ("tokenizationx", "prefix"),
        ("tokenizationy", "prefix"),
        ("tokenizationxyz", "prefix"),
        ("tokenisation", "edit distance 1"),
        ("tokanisation", "edit distance 2"),
    ]
