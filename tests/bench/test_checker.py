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


def _ckc(spec, vault, cwd, jsonl=None):
    """Like ``_ck`` but threads ``cwd`` for the file_* verbs."""
    return check_assertion(
        spec, vault=vault, jsonl=jsonl or [], golden_dir=GOLDEN_DIR, cwd=cwd
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
    # case-insensitive membership: metadata stores `PepCodec`, but the agent's
    # case is a coin-flip — a lowercase `has`/`empty-of` assertion must match the
    # stored CamelCase (mirrors litman's own case-folding for taxonomy keys).
    assert _ck(
        "yaml_list_has: papers/<peptidebert>/metadata.yaml :: projects has pepcodec",
        synth_vault,
    ).passed
    assert not _ck(
        "yaml_list_has: papers/<peptidebert>/metadata.yaml :: projects empty-of pepcodec",
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
    # case-insensitive: the dict stores `PepCodec` but the agent's case is a
    # coin-flip; a lowercase (or upper) assertion must still resolve to the same
    # project, mirroring litman's case-folding match for all 4 TAXONOMY keys.
    assert _ck("taxonomy_has: projects :: pepcodec", synth_vault).passed
    assert _ck("taxonomy_has: projects :: PEPCODEC", synth_vault).passed
    assert not _ck("taxonomy_absent: projects :: pepcodec", synth_vault).passed


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
# file_* verbs (resolve against cwd — the executor's neutral output dir)
# ---------------------------------------------------------------------------


def test_file_exists_and_nonempty(synth_vault: Path, tmp_path: Path) -> None:
    cwd = tmp_path / "out"
    cwd.mkdir()
    (cwd / "refs.bib").write_text("@article{x, title={T}}\n", encoding="utf-8")
    (cwd / "empty.bib").write_text("", encoding="utf-8")
    assert _ckc("file_exists: refs.bib", synth_vault, cwd).passed
    assert not _ckc("file_exists: missing.bib", synth_vault, cwd).passed
    assert _ckc("file_nonempty: refs.bib", synth_vault, cwd).passed
    assert not _ckc("file_nonempty: empty.bib", synth_vault, cwd).passed


def test_file_contains(synth_vault: Path, tmp_path: Path) -> None:
    cwd = tmp_path / "out"
    cwd.mkdir()
    (cwd / "refs.bib").write_text(
        "@article{2023_Guntuboina_PeptideBERT, title={PeptideBERT}}\n"
        "@article{2022_X_DiffDock, title={DiffDock}}\n",
        encoding="utf-8",
    )
    assert _ckc("file_contains: refs.bib :: ~PeptideBERT", synth_vault, cwd).passed
    assert _ckc("file_contains: refs.bib :: ~DiffDock", synth_vault, cwd).passed
    assert not _ckc("file_contains: refs.bib :: ~NotInBib", synth_vault, cwd).passed


def test_file_verb_no_cwd_is_hard_fail_not_silent_pass(synth_vault: Path) -> None:
    """cwd=None must FAIL, never silently pass (invariant #14 no-silent-skip)."""
    r = _ck("file_exists: refs.bib", synth_vault)  # no cwd threaded
    assert not r.passed
    assert "no cwd threaded" in r.detail


def test_vault_file_nonempty(synth_vault: Path) -> None:
    """``vault_file_nonempty`` anchors at the vault root (not cwd) and resolves
    ``<placeholder>`` ids — for asserting a paper's authored markdown got written
    (B4 discussion.md: catches "agent asked instead of writing")."""
    disc = synth_vault / "papers" / "2023_Guntuboina_PeptideBERT" / "discussion.md"
    # absent file -> fail (not a silent pass)
    assert not _ck("vault_file_nonempty: papers/<peptidebert>/discussion.md", synth_vault).passed
    disc.write_text("", encoding="utf-8")
    assert not _ck("vault_file_nonempty: papers/<peptidebert>/discussion.md", synth_vault).passed
    disc.write_text("讨论：DiffDock 用扩散式生成。\n", encoding="utf-8")
    assert _ck("vault_file_nonempty: papers/<peptidebert>/discussion.md", synth_vault).passed


def test_vault_file_contains(synth_vault: Path) -> None:
    disc = synth_vault / "papers" / "2023_Guntuboina_PeptideBERT" / "discussion.md"
    disc.write_text("讨论：DiffDock 用扩散式生成多个候选位姿。\n", encoding="utf-8")
    assert _ck("vault_file_contains: papers/<peptidebert>/discussion.md :: ~扩散", synth_vault).passed
    assert not _ck("vault_file_contains: papers/<peptidebert>/discussion.md :: ~不存在", synth_vault).passed


def test_vault_file_unresolvable_placeholder_fails(synth_vault: Path) -> None:
    r = _ck("vault_file_nonempty: papers/<unknownpaper>/discussion.md", synth_vault)
    assert not r.passed
    assert "placeholder" in r.detail


# ---------------------------------------------------------------------------
# count verb (over INDEX.json papers)
# ---------------------------------------------------------------------------


def test_count(synth_vault: Path) -> None:
    assert _ck("count: title~PeptideBERT == 1", synth_vault).passed
    assert not _ck("count: title~PeptideBERT == 2", synth_vault).passed
    assert _ck("count: title~NonexistentPaper == 0", synth_vault).passed
    # malformed specs fail with detail, never silently pass.
    assert not _ck("count: title~PeptideBERT", synth_vault).passed
    assert not _ck("count: PeptideBERT == 1", synth_vault).passed  # missing title~


def test_count_dedup_two_entries(synth_vault: Path) -> None:
    """A3's failure mode: a second PeptideBERT slipped in -> count == 1 fails."""
    _write_index(
        synth_vault,
        [
            {"id": "a", "title": "PeptideBERT: A Language Model for Peptides", "year": 2023},
            {"id": "b", "title": "PeptideBERT (duplicate)", "year": 2023},
        ],
    )
    assert not _ck("count: title~PeptideBERT == 1", synth_vault).passed
    assert _ck("count: title~PeptideBERT == 2", synth_vault).passed


# ---------------------------------------------------------------------------
# evidence verbs: stdout_contains (jsonl) / answer_contains (run)
# ---------------------------------------------------------------------------


def test_stdout_contains(synth_vault: Path) -> None:
    jsonl = [
        {"argv": ["list"], "stdout": "#4 PeptideBERT\n#5 Multi-Peptide"},
        {"argv": ["show", "id"], "stdout": "year: 2023"},
    ]
    assert _ck("stdout_contains: ~Multi-Peptide", synth_vault, jsonl).passed
    assert _ck("stdout_contains: ~2023", synth_vault, jsonl).passed
    assert not _ck("stdout_contains: ~DiffDock", synth_vault, jsonl).passed
    # records without a stdout key (legacy runlit records) -> empty, no crash.
    legacy = [{"argv": ["list"]}]
    assert not _ck("stdout_contains: ~PeptideBERT", synth_vault, legacy).passed


def test_stdout_contains_falls_back_to_blob(synth_vault: Path) -> None:
    """FIX 3: when the per-record stdout is empty (unmapped tool_result) but the
    ExecutorResult blob holds the output, the run fallback finds it."""
    from harness.executor import ExecutorResult, LitCall, ToolResult

    # The lit call's tool_use_id does NOT match the result block -> as_jsonl_records
    # leaves the record's stdout empty, but stdout_blob still holds the content.
    run = ExecutorResult(
        lit_calls=[LitCall(argv=["list"], raw="lit list", tool_use_id="b1")],
        tool_results=[ToolResult(tool="Bash", content="#5 Multi-Peptide", tool_use_id="OTHER")],
    )
    jsonl = run.as_jsonl_records()
    assert jsonl[0]["stdout"] == ""  # unmapped -> empty per-record stdout

    # Without run: today's behavior — the empty per-record stdout misses.
    no_run = check_assertion(
        "stdout_contains: ~Multi-Peptide", vault=synth_vault, jsonl=jsonl, golden_dir=GOLDEN_DIR
    )
    assert not no_run.passed

    # With run: the blob fallback recovers the substring (TRUE match made findable).
    with_run = check_assertion(
        "stdout_contains: ~Multi-Peptide",
        vault=synth_vault,
        jsonl=jsonl,
        golden_dir=GOLDEN_DIR,
        run=run,
    )
    assert with_run.passed, with_run.detail
    # A substring in neither the records nor the blob still misses (no false pass).
    miss = check_assertion(
        "stdout_contains: ~DiffDock",
        vault=synth_vault,
        jsonl=jsonl,
        golden_dir=GOLDEN_DIR,
        run=run,
    )
    assert not miss.passed


def test_stdout_not_contains(synth_vault: Path) -> None:
    """C1: a filtered list must NOT surface the out-of-range paper."""
    jsonl = [{"argv": ["list"], "stdout": "#4 PeptideBERT\n#5 Multi-Peptide"}]
    # Absent -> passes (the negative assertion holds).
    assert _ck("stdout_not_contains: ~DiffDock", synth_vault, jsonl).passed
    # Present -> fails (the paper leaked into the filtered output).
    assert not _ck("stdout_not_contains: ~PeptideBERT", synth_vault, jsonl).passed


def test_stdout_not_contains_searches_widest_evidence(synth_vault: Path) -> None:
    """Safety direction for the negative verb: a substring present ONLY in the
    unmapped blob (not the per-record stdout) must still FAIL not_contains when a
    run is threaded — never a false 'absent' pass."""
    from harness.executor import ExecutorResult, LitCall, ToolResult

    run = ExecutorResult(
        lit_calls=[LitCall(argv=["list"], raw="lit list", tool_use_id="b1")],
        tool_results=[ToolResult(tool="Bash", content="#1 DiffDock", tool_use_id="OTHER")],
    )
    jsonl = run.as_jsonl_records()
    assert jsonl[0]["stdout"] == ""  # unmapped -> empty per-record stdout

    # Without run: per-record stdout is empty, so DiffDock looks (wrongly) absent.
    no_run = check_assertion(
        "stdout_not_contains: ~DiffDock", vault=synth_vault, jsonl=jsonl, golden_dir=GOLDEN_DIR
    )
    assert no_run.passed  # only the jsonl is visible here
    # With run: the blob exposes DiffDock -> the negative assertion correctly fails.
    with_run = check_assertion(
        "stdout_not_contains: ~DiffDock",
        vault=synth_vault,
        jsonl=jsonl,
        golden_dir=GOLDEN_DIR,
        run=run,
    )
    assert not with_run.passed, with_run.detail


def test_answer_contains_no_run_is_hard_fail(synth_vault: Path) -> None:
    """run=None must FAIL, never silently pass (invariant #14 no-silent-skip)."""
    r = _ck("answer_contains: ~2023", synth_vault)  # no run threaded
    assert not r.passed
    assert "no run threaded" in r.detail


def test_answer_contains_with_run(synth_vault: Path) -> None:
    class _Run:
        final_text = "PeptideBERT was published in 2023 by Guntuboina."

    r = check_assertion(
        "answer_contains: ~2023",
        vault=synth_vault,
        jsonl=[],
        golden_dir=GOLDEN_DIR,
        run=_Run(),
    )
    assert r.passed, r.detail
    miss = check_assertion(
        "answer_contains: ~9999",
        vault=synth_vault,
        jsonl=[],
        golden_dir=GOLDEN_DIR,
        run=_Run(),
    )
    assert not miss.passed


# ---------------------------------------------------------------------------
# resolve threads cwd + run through to the file_* / answer_contains verbs
# ---------------------------------------------------------------------------


def test_resolve_threads_cwd_and_run(tmp_path: Path) -> None:
    from litman.core.library import create_vault

    parent = tmp_path / "vparent"
    parent.mkdir()
    vault = create_vault(parent)
    cwd = tmp_path / "out"
    cwd.mkdir()
    (cwd / "refs.bib").write_text("@article{x, title={DiffDock}}\n", encoding="utf-8")

    class _Run:
        final_text = "year is 2023"

    card = {
        "id": "F",
        "expected_end_state": [
            "file_exists: refs.bib",
            "file_contains: refs.bib :: ~DiffDock",
            "answer_contains: ~2023",
            "health: clean",
        ],
    }
    resolved, results = resolve(
        card, vault=vault, jsonl=[], golden_dir=GOLDEN_DIR, cwd=cwd, run=_Run()
    )
    assert resolved == 1, [(r.verb, r.passed, r.detail) for r in results]


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
