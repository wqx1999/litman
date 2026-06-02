"""Deterministic tests for the assertion checker (Phase C).

These exercise the verb dispatch + ``resolve`` on synthetic vaults built with
litman's public ``create_vault`` + a hand-written INDEX.json / metadata.yaml,
so no agent and no network are needed. The structured health oracle is exercised
against a real (clean) vault.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from ruamel.yaml import YAML

from harness import checker
from harness.checker import AssertResult, check_assertion, resolve

GOLDEN_DIR = Path(__file__).resolve().parent / "fixtures" / "golden"

_yaml = YAML()
_yaml.default_flow_style = False


def _write_index(vault: Path, papers: list[dict]) -> None:
    payload = {"papers": papers}
    (vault / "INDEX.json").write_text(json.dumps(payload), encoding="utf-8")


def _write_meta(vault: Path, pid: str, data: dict) -> Path:
    pdir = vault / "papers" / pid
    pdir.mkdir(parents=True, exist_ok=True)
    with (pdir / "metadata.yaml").open("w", encoding="utf-8") as f:
        _yaml.dump(data, f)
    return pdir


@pytest.fixture
def synth_vault(tmp_path: Path) -> Path:
    """A minimal synthetic vault: one PeptideBERT paper, INDEX + metadata."""
    vault = tmp_path / "vault"
    (vault / "papers").mkdir(parents=True)
    pid = "2023_Guntuboina_PeptideBERT"
    _write_meta(
        vault,
        pid,
        {
            "id": pid,
            "title": "PeptideBERT: A Language Model for Peptides",
            "authors": ["Guntuboina, Chakradhar", "Das, Adrita"],
            "year": 2023,
            "status": "inbox",
            "priority": "A",
            "type": "research",
            "topics": ["peptide", "protein-language-model"],
            "projects": ["PepCodec"],
            "code-clones": [],
            "github": None,
            "read-date": None,
            "last-revisited": None,
        },
    )
    _write_index(
        vault,
        [{"id": pid, "title": "PeptideBERT: A Language Model for Peptides", "year": 2023}],
    )
    (vault / "TAXONOMY.md").write_text(
        "## topics\n- peptide\n- protein-language-model\n\n"
        "## projects\n- PepCodec\n\n## methods\n(empty)\n\n## data\n(empty)\n",
        encoding="utf-8",
    )
    return vault


def _ck(spec, vault, jsonl=None):
    return check_assertion(
        spec, vault=vault, jsonl=jsonl or [], golden_dir=GOLDEN_DIR
    )


# ---------------------------------------------------------------------------
# Path verbs
# ---------------------------------------------------------------------------


def test_path_exists_and_absent(synth_vault: Path) -> None:
    assert _ck("path_exists: papers", synth_vault).passed
    assert _ck("path_exists: TAXONOMY.md", synth_vault).passed
    assert _ck("path_absent: nonexistent", synth_vault).passed
    assert not _ck("path_absent: papers", synth_vault).passed
    assert not _ck("path_exists: nonexistent", synth_vault).passed


def test_dir_empty(synth_vault: Path) -> None:
    (synth_vault / "codes").mkdir()
    assert _ck("dir_empty: codes", synth_vault).passed
    # absent dir counts as empty (no repo cloned)
    assert _ck("dir_empty: codes-missing", synth_vault).passed
    (synth_vault / "codes" / "repo").mkdir()
    assert not _ck("dir_empty: codes", synth_vault).passed


def test_path_placeholder_resolution(synth_vault: Path) -> None:
    r = _ck("path_exists: papers/<peptidebert>/metadata.yaml", synth_vault)
    assert r.passed, r.detail


def test_path_unresolvable_placeholder_fails(synth_vault: Path) -> None:
    r = _ck("path_exists: papers/<unknownpaper>/metadata.yaml", synth_vault)
    assert not r.passed
    assert "placeholder" in r.detail


# ---------------------------------------------------------------------------
# yaml verbs
# ---------------------------------------------------------------------------


def test_yaml_eq(synth_vault: Path) -> None:
    assert _ck(
        "yaml_eq: papers/<peptidebert>/metadata.yaml :: year == 2023", synth_vault
    ).passed
    assert _ck(
        "yaml_eq: papers/<peptidebert>/metadata.yaml :: status == inbox", synth_vault
    ).passed
    assert _ck(
        "yaml_eq: papers/<peptidebert>/metadata.yaml :: github == null", synth_vault
    ).passed
    assert not _ck(
        "yaml_eq: papers/<peptidebert>/metadata.yaml :: year == 2099", synth_vault
    ).passed


def test_yaml_ne(synth_vault: Path) -> None:
    # read-date is null -> != null should FAIL (it IS null)
    assert not _ck(
        "yaml_ne: papers/<peptidebert>/metadata.yaml :: read-date == null", synth_vault
    ).passed
    # year is 2023 -> != null should PASS
    assert _ck(
        "yaml_ne: papers/<peptidebert>/metadata.yaml :: year == null", synth_vault
    ).passed


def test_yaml_contains_tolerant(synth_vault: Path) -> None:
    assert _ck(
        "yaml_contains: papers/<peptidebert>/metadata.yaml :: title ~ peptidebert",
        synth_vault,
    ).passed  # casefold tolerance
    assert _ck(
        "yaml_contains: papers/<peptidebert>/metadata.yaml :: authors[0] ~ Guntuboina",
        synth_vault,
    ).passed
    assert not _ck(
        "yaml_contains: papers/<peptidebert>/metadata.yaml :: title ~ DiffDock",
        synth_vault,
    ).passed


def test_yaml_list_has_and_empty(synth_vault: Path) -> None:
    assert _ck(
        "yaml_list_has: papers/<peptidebert>/metadata.yaml :: topics has peptide",
        synth_vault,
    ).passed
    assert _ck(
        "yaml_list_has: papers/<peptidebert>/metadata.yaml :: code-clones empty",
        synth_vault,
    ).passed
    assert _ck(
        "yaml_list_has: papers/<peptidebert>/metadata.yaml :: topics empty-of diffusion",
        synth_vault,
    ).passed
    assert not _ck(
        "yaml_list_has: papers/<peptidebert>/metadata.yaml :: topics empty-of peptide",
        synth_vault,
    ).passed
    assert not _ck(
        "yaml_list_has: papers/<peptidebert>/metadata.yaml :: topics has diffusion",
        synth_vault,
    ).passed


# ---------------------------------------------------------------------------
# index / taxonomy verbs
# ---------------------------------------------------------------------------


def test_index_has(synth_vault: Path) -> None:
    assert _ck("index_has: title~PeptideBERT year==2023", synth_vault).passed
    assert _ck("index_has: title~PeptideBERT", synth_vault).passed
    assert not _ck("index_has: title~PeptideBERT year==2099", synth_vault).passed
    assert not _ck("index_has: title~NonexistentPaper", synth_vault).passed


def test_taxonomy_has_absent(synth_vault: Path) -> None:
    assert _ck("taxonomy_has: topics :: peptide", synth_vault).passed
    assert _ck("taxonomy_has: projects :: PepCodec", synth_vault).passed
    assert _ck("taxonomy_absent: topics :: diffusion", synth_vault).passed
    assert not _ck("taxonomy_has: topics :: diffusion", synth_vault).passed
    assert not _ck("taxonomy_absent: topics :: peptide", synth_vault).passed


# ---------------------------------------------------------------------------
# pdf_eq (against the committed fixture PDFs)
# ---------------------------------------------------------------------------


def test_pdf_eq(synth_vault: Path) -> None:
    fixture4 = GOLDEN_DIR.parent / "pdfs" / "4.pdf"
    if not fixture4.is_file():
        pytest.skip("fixture PDFs not fetched")
    pdir = synth_vault / "papers" / "2023_Guntuboina_PeptideBERT"
    (pdir / "paper.pdf").write_bytes(fixture4.read_bytes())
    r = _ck("pdf_eq: papers/<peptidebert>/paper.pdf == fixture:4", synth_vault)
    assert r.passed, r.detail
    # tamper -> fail
    (pdir / "paper.pdf").write_bytes(b"%PDF-1.4 not the fixture")
    assert not _ck(
        "pdf_eq: papers/<peptidebert>/paper.pdf == fixture:4", synth_vault
    ).passed


# ---------------------------------------------------------------------------
# ran / not_ran (argv log)
# ---------------------------------------------------------------------------


def test_ran_not_ran(synth_vault: Path) -> None:
    jsonl = [
        {"argv": ["add", "/tmp/4.pdf", "--from-llm-json", "x.json"], "exit_code": 0},
        {"argv": ["list", "--library", "/tmp/v"], "exit_code": 0},
    ]
    assert _ck("ran: add", synth_vault, jsonl).passed
    assert _ck("ran: list", synth_vault, jsonl).passed
    assert _ck("not_ran: taxonomy rm", synth_vault, jsonl).passed
    assert not _ck("ran: taxonomy rm", synth_vault, jsonl).passed
    assert not _ck("not_ran: add", synth_vault, jsonl).passed


def test_ran_multiword_argv(synth_vault: Path) -> None:
    jsonl = [{"argv": ["taxonomy", "rm", "topics", "diffusion", "--yes"], "exit_code": 0}]
    assert _ck("ran: taxonomy rm", synth_vault, jsonl).passed
    assert _ck("ran: project add", synth_vault, jsonl).passed is False


# ---------------------------------------------------------------------------
# health oracle (structured API — ruling 1)
# ---------------------------------------------------------------------------


def test_health_clean_on_real_vault(tmp_path: Path) -> None:
    from litman.core.library import create_vault

    vault = create_vault(tmp_path)
    r = _ck("health: clean", vault)
    assert r.passed, r.detail


def test_health_reports_catches_injected_corruption(tmp_path: Path) -> None:
    """Inject a dangling related-ref and confirm health_reports surfaces it."""
    from litman.core.library import create_vault

    vault = create_vault(tmp_path)
    pid = "2023_Guntuboina_PeptideBERT"
    pdir = vault / "papers" / pid
    pdir.mkdir(parents=True)
    (pdir / "paper.pdf").write_bytes(b"%PDF-1.4\n%%EOF\n")
    _write_meta(
        vault,
        pid,
        {
            "id": pid,
            "title": "PeptideBERT",
            "authors": ["Guntuboina, Chakradhar"],
            "year": 2023,
            "created-at": "2026-01-01T00:00:00+00:00",
            "updated-at": "2026-01-01T00:00:00+00:00",
            "status": "inbox",
            "related": ["2099_Ghost_Paper"],  # dangling ref
        },
    )
    # health: clean should now FAIL (an error-severity dangling_refs Issue)
    assert not _ck("health: clean", vault).passed
    # health_reports: ~dangling should HIT
    assert _ck("health_reports: ~dangling", vault).passed


def test_health_reports_catches_code_clone_dangling(tmp_path: Path) -> None:
    """The ACTUAL H1 / J1-corrupt injection: a paper's ``code-clones`` points at
    a repo with no ``codes/<name>/`` dir (Critical 2 regression).

    ``core/checks.py:check_code_clone_integrity`` emits an ``error``-severity
    ``code_clone_integrity`` Issue whose message reads
    ``'<pid>'.code-clones references '<repo>' but no codes/<repo>/repo-meta.yaml
    exists`` — it contains NEITHER the substring "dangling" (the original card
    text, a latent proposal bug) NOR any other token besides "code-clones" /
    "code_clone_integrity". Confirms the substring the H1 / J1-corrupt cards now
    use actually matches, and that the old one does not.
    """
    from litman.core.library import create_vault

    vault = create_vault(tmp_path)
    pid = "2023_Guntuboina_PeptideBERT"
    pdir = vault / "papers" / pid
    pdir.mkdir(parents=True)
    (pdir / "paper.pdf").write_bytes(b"%PDF-1.4\n%%EOF\n")
    _write_meta(
        vault,
        pid,
        {
            "id": pid,
            "title": "PeptideBERT",
            "authors": ["Guntuboina, Chakradhar"],
            "year": 2023,
            "created-at": "2026-01-01T00:00:00+00:00",
            "updated-at": "2026-01-01T00:00:00+00:00",
            "status": "inbox",
            # references a repo with no codes/<name>/repo-meta.yaml on disk:
            # exactly the dangling code-clone H1 / J1-corrupt inject.
            "code-clones": ["PeptideBERT-repo"],
        },
    )
    # (a) the substring the cards now carry HITS the real Issue.
    assert _ck("health_reports: ~code-clones", vault).passed
    # the original card text ~dangling MISSES this corruption (the bug Critical 2
    # fixes — it would have false-failed the two highest-value GR cards).
    assert not _ck("health_reports: ~dangling", vault).passed
    # (b) health: clean FAILS (an error-severity code_clone_integrity Issue).
    assert not _ck("health: clean", vault).passed


# ---------------------------------------------------------------------------
# prose / unknown -> manual (no silent pass)
# ---------------------------------------------------------------------------


def test_prose_line_is_manual_not_passing(synth_vault: Path) -> None:
    r = _ck("输出包含 Multi-Peptide(#5)", synth_vault)
    assert r.verb == "manual"
    assert not r.passed
    assert "not auto-checkable" in r.detail


def test_unknown_verb_is_manual(synth_vault: Path) -> None:
    r = _ck("frobnicate: papers", synth_vault)
    assert r.verb == "manual"
    assert not r.passed


# ---------------------------------------------------------------------------
# resolve() fold
# ---------------------------------------------------------------------------


def test_resolve_all_pass() -> None:
    """An all-DSL (no prose) card that passes resolves to 1 (regression).

    Built on a REAL seeded vault so the implicit health gate is genuinely clean
    (the old synth_vault has no ``created-at`` and would trip the gate). Asserts
    the returned ``resolved`` value, not just the result shape — the prior
    version only checked ``isinstance``, so a regression in the fold went unseen.
    """
    from harness.seeds import build_seed

    vault = build_seed("seed-2papers-peptide")
    card = {
        "id": "T",
        "expected_end_state": [
            "index_has: title~PeptideBERT year==2023",
            "yaml_eq: papers/<peptidebert>/metadata.yaml :: status == inbox",
            "ran: add",
            "health: clean",
        ],
    }
    jsonl = [{"argv": ["add", "x"], "exit_code": 0}]
    resolved, results = resolve(card, vault=vault, jsonl=jsonl, golden_dir=GOLDEN_DIR)
    assert resolved == 1
    assert all(isinstance(r, AssertResult) for r in results)
    assert all(r.passed for r in results)


def test_resolve_blocks_on_manual_prose_line() -> None:
    """A card mixing one passing DSL line with one ``manual`` prose line must
    NOT resolve (Critical 1 regression — manual lines were silently dropped).

    The deterministic core cannot auto-score a prose ``expected_end_state`` line;
    such a card (one of the 17 mixed DSL+prose cards) is honestly unresolved here
    and is scored by the deferred Phase E executor instead. The detail trail must
    still distinguish the unevaluable manual line from the passing DSL line.
    """
    from harness.seeds import build_seed

    vault = build_seed("seed-2papers-peptide")
    card = {
        "id": "M",
        "expected_end_state": [
            "index_has: title~PeptideBERT year==2023",  # passing DSL
            "输出包含 PeptideBERT(#4)",  # un-machine-checkable prose -> manual
        ],
    }
    resolved, results = resolve(card, vault=vault, jsonl=[], golden_dir=GOLDEN_DIR)
    assert resolved != 1
    # The DSL line still passed; the manual line is the blocker, and the trail
    # keeps both so the report shows WHICH line was unevaluable vs failed.
    manual = [r for r in results if r.verb == "manual"]
    assert len(manual) == 1
    assert not manual[0].passed
    assert any(r.verb == "index_has" and r.passed for r in results)


def test_resolve_on_real_clean_vault(tmp_path: Path) -> None:
    from litman.core.library import create_vault

    vault = create_vault(tmp_path)
    card = {"id": "T", "expected_end_state": ["path_exists: papers", "health: clean"]}
    resolved, results = resolve(vault=vault, card=card, jsonl=[], golden_dir=GOLDEN_DIR)
    assert resolved == 1
    assert all(r.passed for r in results)


def test_resolve_fails_when_one_assertion_fails(tmp_path: Path) -> None:
    from litman.core.library import create_vault

    vault = create_vault(tmp_path)
    card = {
        "id": "T",
        "expected_end_state": [
            "path_exists: papers",
            "path_exists: this-does-not-exist",
        ],
    }
    resolved, results = resolve(card, vault=vault, jsonl=[], golden_dir=GOLDEN_DIR)
    assert resolved == 0
    assert any(not r.passed for r in results)


def test_resolve_implicit_health_gate(tmp_path: Path) -> None:
    """A card with no explicit health assertion still fails on a dirty vault."""
    from litman.core.library import create_vault

    vault = create_vault(tmp_path)
    pid = "2023_X_Y"
    pdir = vault / "papers" / pid
    pdir.mkdir(parents=True)
    (pdir / "paper.pdf").write_bytes(b"%PDF-1.4\n%%EOF\n")
    _write_meta(
        vault,
        pid,
        {
            "id": pid,
            "title": "X",
            "authors": ["X, Y"],
            "year": 2023,
            "created-at": "2026-01-01T00:00:00+00:00",
            "updated-at": "2026-01-01T00:00:00+00:00",
            "status": "inbox",
            "related": ["2099_Ghost"],  # dangling -> error
        },
    )
    card = {"id": "T", "expected_end_state": ["path_exists: papers"]}
    resolved, results = resolve(card, vault=vault, jsonl=[], golden_dir=GOLDEN_DIR)
    assert resolved == 0
    assert any(r.verb == "health" and not r.passed for r in results)


def test_resolve_skips_health_gate_for_adversarial(tmp_path: Path) -> None:
    """A card asserting health_reports must NOT get the implicit clean gate."""
    from litman.core.library import create_vault

    vault = create_vault(tmp_path)
    pid = "2023_X_Y"
    pdir = vault / "papers" / pid
    pdir.mkdir(parents=True)
    (pdir / "paper.pdf").write_bytes(b"%PDF-1.4\n%%EOF\n")
    _write_meta(
        vault,
        pid,
        {
            "id": pid,
            "title": "X",
            "authors": ["X, Y"],
            "year": 2023,
            "created-at": "2026-01-01T00:00:00+00:00",
            "updated-at": "2026-01-01T00:00:00+00:00",
            "status": "inbox",
            "related": ["2099_Ghost"],
        },
    )
    card = {"id": "H", "expected_end_state": ["health_reports: ~dangling"]}
    resolved, results = resolve(card, vault=vault, jsonl=[], golden_dir=GOLDEN_DIR)
    # The adversarial card resolves because the finding it expects is present,
    # and no implicit clean gate was added.
    assert resolved == 1
    assert not any(r.verb == "health" for r in results)
