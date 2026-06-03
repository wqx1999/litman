"""Phase C — deterministic checker (the assertion oracle).

Evaluates the ~13 assertion verbs of the scenario DSL (scenarios proposal §2)
against a finished run vault + its ``littest-run.jsonl``. Each assertion is one
``AssertResult`` (pass/fail + a per-assertion failure detail). :func:`resolve`
folds a card's ``expected_end_state`` + ``auto_fail`` + the health oracle into
``resolved ∈ {0,1}`` (M34 §3: all assertions pass ∧ no auto_fail ∧ health clean).

Two oracle concerns are kept deliberately separate (ruling 1):

* **Health = structured API.** ``health: clean`` / ``health_reports: ~substr``
  call :func:`litman.core.checks.run_all_checks` (the same engine the CLI runs),
  NOT the Rich stdout of ``lit health-check``. ``health: clean`` = zero
  ``error``-severity ``Issue``; ``health_reports: ~substr`` = some ``Issue``
  whose category / message contains the substring.
* **ran / not_ran = argv log.** ``ran``/``not_ran`` are proved from the
  ``littest-run.jsonl`` argv (proving the agent actually invoked the CLI),
  independent of the health engine.

Four further verbs read state the vault YAML does not own:

* ``file_exists`` / ``file_contains`` / ``file_nonempty`` resolve their arg
  against ``cwd`` — the executor's neutral output dir where ``lit export`` drops
  ``refs.bib`` (M34 §3.5: F1). These need ``cwd`` threaded; when it is ``None``
  the result is a *failed* assertion (``detail="no cwd threaded"``), NEVER a
  silent pass (invariant #14 no-silent-skip).
* ``count`` matches over INDEX.json papers (e.g. ``count: title~PeptideBERT == 1``
  proves the dedup did not create a duplicate, M34 §3.5: A3).

Two evidence verbs score the agent's observable output:

* ``stdout_contains: ~substr`` greps the joined ``record["stdout"]`` of the
  jsonl (works from the jsonl alone — the executor widens its records to carry
  each lit call's captured stdout). When that misses and a ``run`` is threaded
  it falls back to the executor's ``stdout_blob`` (every captured result block),
  recovering output whose result block could not be paired to a call by id.
* ``answer_contains: ~substr`` greps ``run.final_text`` (the agent's natural
  language answer, e.g. C2 "year=2023"). Needs ``run`` threaded; when it is
  ``None`` the result is a *failed* assertion (``detail="no run threaded"``),
  same no-silent-skip rule.

Placeholders: card paths use ``<peptidebert>``-style id placeholders. They are
resolved by scanning the vault / INDEX.json for a paper whose title matches the
placeholder's semantics (title-substring), never by hardcoding ``derive_id()``.

Extraction tolerance (A-class cards): titles match by normalize+substring;
authors by family-name; never exact-equality on messy metadata.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------


@dataclass
class AssertResult:
    """Outcome of one assertion.

    ``verb`` is the assertion verb (``path_exists`` / ``yaml_eq`` / ...) or the
    sentinel ``"manual"`` for a free-form prose line the DSL cannot evaluate.
    ``passed`` is ``False`` for a sentinel (it is *not auto-checkable*, never a
    silent pass — invariant #14 no-silent-skip spirit); ``detail`` explains.
    """

    verb: str
    spec: str
    passed: bool
    detail: str


# Placeholder -> the title-substring that identifies the paper. Derived from
# the fixture golden titles; the checker resolves these to a real on-disk id by
# scanning INDEX.json, so the concrete derive_id() string never has to be known.
PLACEHOLDER_TITLES: dict[str, str] = {
    "diffdock": "DiffDock",
    "edm": "Equivariant Diffusion",
    "chemberta": "ChemBERTa",
    "peptidebert": "PeptideBERT",
    "multipeptide": "Multi-Peptide",
    "multi-peptide": "Multi-Peptide",
    "uni-mol-plus": "Uni-Mol+",
    "unimolplus": "Uni-Mol+",
    "p1": "DiffDock",
    "p2": "Equivariant Diffusion",
    "old-peptidebert-id": "PeptideBERT",
}


def _norm(s: str) -> str:
    """Normalize for tolerant comparison: collapse whitespace + casefold."""
    return " ".join(str(s).split()).casefold()


# ---------------------------------------------------------------------------
# Vault read helpers (read-only; never mutate)
# ---------------------------------------------------------------------------


def _load_yaml(path: Path) -> Any:
    from ruamel.yaml import YAML

    yaml = YAML(typ="safe")
    return yaml.load(path.read_text(encoding="utf-8"))


def _load_index(vault: Path) -> dict[str, Any]:
    index = vault / "INDEX.json"
    if not index.is_file():
        return {"papers": []}
    return json.loads(index.read_text(encoding="utf-8"))


def _resolve_placeholder(vault: Path, placeholder: str) -> str | None:
    """Map a ``<placeholder>`` to a real paper id by title-substring on INDEX.json.

    Returns the single matching id, or ``None`` when zero or many match (the
    caller turns that into a failed assertion with a clear detail).
    """
    title_sub = PLACEHOLDER_TITLES.get(placeholder.strip().lower())
    if title_sub is None:
        return None
    want = _norm(title_sub)
    matches = [
        str(p.get("id"))
        for p in _load_index(vault).get("papers", [])
        if want in _norm(p.get("title", ""))
    ]
    return matches[0] if len(matches) == 1 else None


def _expand_path(vault: Path, rel: str) -> tuple[Path | None, str]:
    """Resolve a card-relative path, substituting any ``<placeholder>`` id.

    Returns ``(absolute_path | None, detail)``. ``None`` means a placeholder in
    the path could not be resolved (reported as a failed assertion).
    """
    rel = rel.strip()
    out_parts: list[str] = []
    for part in rel.split("/"):
        if part.startswith("<") and part.endswith(">"):
            pid = _resolve_placeholder(vault, part[1:-1])
            if pid is None:
                return None, f"could not resolve placeholder {part!r}"
            out_parts.append(pid)
        else:
            out_parts.append(part)
    return vault / "/".join(out_parts), ""


def _dig(data: Any, dotted_key: str) -> tuple[bool, Any]:
    """Follow a dotted key (with optional ``[i]`` indices) into a mapping.

    Returns ``(found, value)``. Supports ``authors[0]`` style indexing used by
    the A-class extraction cards.
    """
    cur = data
    for raw in dotted_key.split("."):
        key, _, idx = raw.partition("[")
        if key:
            if not isinstance(cur, dict) or key not in cur:
                return False, None
            cur = cur[key]
        if idx:
            i = int(idx.rstrip("]"))
            if not isinstance(cur, (list, tuple)) or i >= len(cur):
                return False, None
            cur = cur[i]
    return True, cur


def _coerce_scalar(text: str) -> Any:
    """Parse a DSL literal (``null`` / ``2023`` / ``A`` / ``research``)."""
    t = text.strip()
    if t in ("null", "None", "~"):
        return None
    if t.lstrip("-").isdigit():
        return int(t)
    return t


# ---------------------------------------------------------------------------
# Health oracle (structured API — ruling 1)
# ---------------------------------------------------------------------------


def _health_issues(vault: Path) -> list:
    """Return the structured ``Issue`` list from the same engine the CLI runs."""
    from litman.core.checks import run_all_checks
    from litman.core.document import list_papers

    papers = list_papers(vault)
    return run_all_checks(vault, papers)


# ---------------------------------------------------------------------------
# Individual verb implementations
# ---------------------------------------------------------------------------


def _check_path_exists(vault: Path, arg: str, *, want: bool) -> AssertResult:
    verb = "path_exists" if want else "path_absent"
    path, detail = _expand_path(vault, arg)
    if path is None:
        return AssertResult(verb, arg, False, detail)
    exists = path.exists() or path.is_symlink()
    ok = exists is want
    return AssertResult(
        verb, arg, ok,
        "" if ok else f"{path} exists={exists}, wanted {want}",
    )


def _check_dir_empty(vault: Path, arg: str) -> AssertResult:
    """``dir_empty: <rel>`` — directory exists and contains no entries.

    Expresses "no repo was cloned" for cards whose prose says ``path_absent:
    codes`` even though ``lit init`` always creates an empty ``codes/`` (the
    faithful machine-checkable intent is "the dir is empty", not "absent").
    """
    path, detail = _expand_path(vault, arg)
    if path is None:
        return AssertResult("dir_empty", arg, False, detail)
    if not path.is_dir():
        # Absent or a file is "no entries" for our purposes — still empty.
        return AssertResult("dir_empty", arg, True, "")
    entries = list(path.iterdir())
    ok = len(entries) == 0
    return AssertResult(
        "dir_empty", arg, ok,
        "" if ok else f"{path} not empty: {[e.name for e in entries[:5]]}",
    )


def _check_symlink_ok(vault: Path, arg: str) -> AssertResult:
    path, detail = _expand_path(vault, arg)
    if path is None:
        return AssertResult("symlink_ok", arg, False, detail)
    if not path.is_symlink():
        return AssertResult("symlink_ok", arg, False, f"{path} is not a symlink")
    target_ok = path.exists()  # follows the link; False on a dangling target
    return AssertResult(
        "symlink_ok", arg, target_ok,
        "" if target_ok else f"{path} -> {os_readlink(path)} is dangling",
    )


def os_readlink(path: Path) -> str:
    import os

    try:
        return os.readlink(path)
    except OSError:
        return "?"


def _check_yaml(vault: Path, arg: str, verb: str) -> AssertResult:
    """Handle ``yaml_eq`` / ``yaml_contains`` / ``yaml_list_has`` (incl. empty)."""
    file_part, _, rest = arg.partition("::")
    path, detail = _expand_path(vault, file_part)
    if path is None:
        return AssertResult(verb, arg, False, detail)
    if not path.is_file():
        return AssertResult(verb, arg, False, f"{path} does not exist")
    data = _load_yaml(path)

    if verb == "yaml_eq":
        key, _, val = rest.partition("==")
        found, actual = _dig(data, key.strip())
        if not found:
            return AssertResult(verb, arg, False, f"key {key.strip()!r} not found")
        want = _coerce_scalar(val)
        ok = actual == want
        return AssertResult(
            verb, arg, ok, "" if ok else f"{key.strip()}={actual!r}, wanted {want!r}"
        )

    if verb == "yaml_ne":
        # ``<key> != <val>`` — the original cards spell the "field is set"
        # check as ``yaml_ne ... == null`` (read-date / last-revisited got a
        # stamp). Accept both ``!=`` and the legacy ``== <val>`` separator the
        # proposal used for yaml_ne lines.
        if "!=" in rest:
            key, _, val = rest.partition("!=")
        else:
            key, _, val = rest.partition("==")
        found, actual = _dig(data, key.strip())
        if not found:
            return AssertResult(verb, arg, False, f"key {key.strip()!r} not found")
        avoid = _coerce_scalar(val)
        ok = actual != avoid
        return AssertResult(
            verb, arg, ok, "" if ok else f"{key.strip()}={actual!r} equals forbidden {avoid!r}"
        )

    if verb == "yaml_contains":
        key, _, sub = rest.partition("~")
        found, actual = _dig(data, key.strip())
        if not found:
            return AssertResult(verb, arg, False, f"key {key.strip()!r} not found")
        ok = _norm(sub) in _norm(str(actual))
        return AssertResult(
            verb, arg, ok, "" if ok else f"{actual!r} does not contain {sub.strip()!r}"
        )

    if verb == "yaml_list_has":
        return _check_yaml_list(data, rest, arg)

    return AssertResult(verb, arg, False, f"unknown yaml verb {verb!r}")


def _check_yaml_list(data: Any, rest: str, arg: str) -> AssertResult:
    """``<key> has <val>`` / ``<key> empty`` / ``<key> empty-of <val>``."""
    verb = "yaml_list_has"
    tokens = rest.split()
    if not tokens:
        return AssertResult(verb, arg, False, "empty yaml_list_has spec")
    key = tokens[0]
    found, actual = _dig(data, key)
    items = [str(x) for x in actual] if isinstance(actual, (list, tuple)) else []
    if not found:
        items = []  # absent list field = empty (schema-less, invariant #7)

    if len(tokens) >= 2 and tokens[1] == "empty":
        ok = len(items) == 0
        return AssertResult(verb, arg, ok, "" if ok else f"{key}={items!r} not empty")

    if len(tokens) >= 3 and tokens[1] == "empty-of":
        val = tokens[2]
        ok = val not in items
        return AssertResult(
            verb, arg, ok, "" if ok else f"{key}={items!r} still contains {val!r}"
        )

    if len(tokens) >= 3 and tokens[1] == "has":
        val = tokens[2]
        ok = val in items
        return AssertResult(
            verb, arg, ok, "" if ok else f"{key}={items!r} does not contain {val!r}"
        )

    return AssertResult(verb, arg, False, f"unparseable yaml_list_has: {rest!r}")


def _check_index_has(vault: Path, arg: str) -> AssertResult:
    """``title~<substr> [year==N]`` — match a paper by field, never by id."""
    verb = "index_has"
    spec = arg.strip()
    title_sub = ""
    year_eq: int | None = None
    # Parse "title~PeptideBERT year==2023" (year clause optional).
    if "year==" in spec:
        head, _, ytail = spec.partition("year==")
        try:
            year_eq = int(ytail.strip().split()[0])
        except (ValueError, IndexError):
            return AssertResult(verb, arg, False, f"bad year clause in {spec!r}")
        spec = head.strip()
    if spec.startswith("title~"):
        title_sub = spec[len("title~"):].strip()
    else:
        return AssertResult(verb, arg, False, f"index_has needs title~: {arg!r}")

    want = _norm(title_sub)
    matches = [
        p
        for p in _load_index(vault).get("papers", [])
        if want in _norm(p.get("title", ""))
    ]
    if year_eq is not None:
        matches = [p for p in matches if p.get("year") == year_eq]
    ok = len(matches) >= 1
    return AssertResult(
        verb, arg, ok,
        "" if ok else f"no INDEX paper with title~{title_sub!r}"
        + (f" year=={year_eq}" if year_eq is not None else ""),
    )


def _check_taxonomy(vault: Path, arg: str, *, want: bool) -> AssertResult:
    verb = "taxonomy_has" if want else "taxonomy_absent"
    dict_part, _, val = arg.partition("::")
    dict_name = dict_part.strip()
    value = val.strip()
    tax_file = vault / "TAXONOMY.md"
    if not tax_file.is_file():
        return AssertResult(verb, arg, False, "TAXONOMY.md missing")
    from litman.core.taxonomy import parse_taxonomy

    parsed = parse_taxonomy(tax_file.read_text(encoding="utf-8"))
    present = value in parsed.get(dict_name, [])
    ok = present is want
    return AssertResult(
        verb, arg, ok,
        "" if ok else f"{dict_name}:{value} present={present}, wanted {want}",
    )


def _check_pdf_eq(vault: Path, arg: str, *, golden_dir: Path) -> AssertResult:
    """``papers/<q>/paper.pdf == fixture:<n>`` — byte-compare against the fixture."""
    verb = "pdf_eq"
    left, _, right = arg.partition("==")
    path, detail = _expand_path(vault, left.strip())
    if path is None:
        return AssertResult(verb, arg, False, detail)
    right = right.strip()
    if not right.startswith("fixture:"):
        return AssertResult(verb, arg, False, f"bad pdf_eq RHS: {right!r}")
    fixture_id = right[len("fixture:"):].strip()
    pdfs_dir = golden_dir.parent / "pdfs"
    fixture_pdf = pdfs_dir / f"{fixture_id}.pdf"
    if not path.is_file():
        return AssertResult(verb, arg, False, f"{path} missing")
    if not fixture_pdf.is_file():
        return AssertResult(verb, arg, False, f"fixture {fixture_pdf} missing")
    ok = path.read_bytes() == fixture_pdf.read_bytes()
    return AssertResult(verb, arg, ok, "" if ok else "pdf bytes differ from fixture")


def _check_health_clean(vault: Path) -> AssertResult:
    issues = _health_issues(vault)
    errors = [i for i in issues if i.severity == "error"]
    ok = len(errors) == 0
    detail = "" if ok else "; ".join(f"{i.category}: {i.message}" for i in errors[:5])
    return AssertResult("health", "clean", ok, detail)


def _check_health_reports(vault: Path, arg: str) -> AssertResult:
    sub = arg.lstrip("~").strip()
    issues = _health_issues(vault)
    hit = any(
        _norm(sub) in _norm(i.category) or _norm(sub) in _norm(i.message)
        for i in issues
    )
    return AssertResult(
        "health_reports", arg, hit,
        "" if hit else f"no health Issue mentions {sub!r}",
    )


def _argv_str(record: dict) -> str:
    return " ".join(str(a) for a in record.get("argv", []))


def _check_ran(jsonl: list[dict], arg: str, *, want: bool) -> AssertResult:
    verb = "ran" if want else "not_ran"
    sub = arg.strip().strip('"')
    present = any(_norm(sub) in _norm(_argv_str(r)) for r in jsonl)
    ok = present is want
    return AssertResult(
        verb, arg, ok,
        "" if ok else f"argv-substr {sub!r} present={present}, wanted {want}",
    )


# ---------------------------------------------------------------------------
# File verbs (resolve against the executor's neutral output dir, ``cwd``)
# ---------------------------------------------------------------------------


def _check_file(cwd: Path | None, arg: str, verb: str) -> AssertResult:
    """``file_exists`` / ``file_contains`` / ``file_nonempty``.

    The path is resolved against ``cwd`` (the executor's neutral output dir,
    e.g. where ``lit export`` drops ``refs.bib``). A ``None`` ``cwd`` is a hard
    fail, never a silent pass (invariant #14 no-silent-skip): the harness simply
    forgot to thread it.
    """
    if cwd is None:
        return AssertResult(verb, arg, False, "no cwd threaded")

    if verb == "file_contains":
        rel, _, sub = arg.partition("::")
        path = Path(cwd) / rel.strip()
        sub = sub.strip().lstrip("~").strip()
        if not path.is_file():
            return AssertResult(verb, arg, False, f"{path} does not exist")
        body = path.read_text(encoding="utf-8", errors="replace")
        ok = _norm(sub) in _norm(body)
        return AssertResult(
            verb, arg, ok, "" if ok else f"{path} does not contain {sub!r}"
        )

    rel = arg.strip()
    path = Path(cwd) / rel
    if verb == "file_exists":
        ok = path.exists()
        return AssertResult(verb, arg, ok, "" if ok else f"{path} does not exist")
    if verb == "file_nonempty":
        if not path.is_file():
            return AssertResult(verb, arg, False, f"{path} does not exist")
        ok = path.stat().st_size > 0
        return AssertResult(verb, arg, ok, "" if ok else f"{path} is empty")

    return AssertResult(verb, arg, False, f"unknown file verb {verb!r}")


# ---------------------------------------------------------------------------
# count verb (over INDEX.json papers)
# ---------------------------------------------------------------------------


def _check_count(vault: Path, arg: str) -> AssertResult:
    """``count: title~PeptideBERT == 1`` — count INDEX papers matching a field.

    Proves a dedup did not create a duplicate (A3): exactly N papers whose title
    contains the substring. Comparison is ``==`` only (the corpus needs no other).
    """
    verb = "count"
    spec, sep, count_part = arg.partition("==")
    if not sep:
        return AssertResult(verb, arg, False, f"count needs '== N': {arg!r}")
    try:
        want = int(count_part.strip())
    except ValueError:
        return AssertResult(verb, arg, False, f"bad count target in {arg!r}")
    spec = spec.strip()
    if not spec.startswith("title~"):
        return AssertResult(verb, arg, False, f"count needs title~: {arg!r}")
    title_sub = _norm(spec[len("title~"):])
    n = sum(
        1
        for p in _load_index(vault).get("papers", [])
        if title_sub in _norm(p.get("title", ""))
    )
    ok = n == want
    return AssertResult(
        verb, arg, ok, "" if ok else f"count title~{spec[len('title~'):].strip()!r}={n}, wanted {want}"
    )


# ---------------------------------------------------------------------------
# Evidence verbs (agent stdout / final answer)
# ---------------------------------------------------------------------------


def _check_stdout_contains(jsonl: list[dict], arg: str, run: Any = None) -> AssertResult:
    """``stdout_contains: ~substr`` — grep the agent's captured lit-call stdout.

    Greps ``record["stdout"]`` of every jsonl record first (the executor widens
    its records to carry each lit call's captured stdout, paired by
    ``tool_use_id``). This path works from a runlit jsonl alone, with no ``run``.

    When that misses AND a ``run`` (:class:`harness.executor.ExecutorResult`) was
    threaded, fall back to grepping :func:`harness.executor.stdout_blob` — the
    join of every captured ``tool_result`` block. ``as_jsonl_records`` can leave a
    record's ``stdout`` empty when its result block is unmappable by
    ``tool_use_id`` (best-effort pairing), so the blob recovers the real output
    that lives in an unpaired result block. This can only make a TRUE match more
    findable; the blob holds only real agent output, never a fabricated pass.
    """
    sub = arg.strip().lstrip("~").strip()
    blob = "\n".join(str(r.get("stdout", "")) for r in jsonl)
    ok = _norm(sub) in _norm(blob)
    if not ok and run is not None:
        from harness.executor import stdout_blob

        ok = _norm(sub) in _norm(stdout_blob(run))
    return AssertResult(
        "stdout_contains", arg, ok,
        "" if ok else f"no lit-call stdout contains {sub!r}",
    )


def _check_answer_contains(run: Any, arg: str) -> AssertResult:
    """``answer_contains: ~substr`` — grep the agent's final natural-language answer.

    Reads ``run.final_text`` (an :class:`harness.executor.ExecutorResult`). A
    ``None`` ``run`` is a hard fail, never a silent pass (invariant #14).
    """
    sub = arg.strip().lstrip("~").strip()
    if run is None:
        return AssertResult("answer_contains", arg, False, "no run threaded")
    final = getattr(run, "final_text", "")
    ok = _norm(sub) in _norm(str(final))
    return AssertResult(
        "answer_contains", arg, ok,
        "" if ok else f"final answer does not contain {sub!r}",
    )


# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------

_KNOWN_VERBS: frozenset[str] = frozenset(
    {
        "path_exists", "path_absent", "dir_empty",
        "yaml_eq", "yaml_ne", "yaml_contains", "yaml_list_has",
        "index_has",
        "taxonomy_has", "taxonomy_absent",
        "symlink_ok",
        "pdf_eq",
        "health", "health_reports",
        "ran", "not_ran",
        "file_exists", "file_contains", "file_nonempty",
        "count",
        "stdout_contains", "answer_contains",
    }
)


def check_assertion(
    spec: Any,
    *,
    vault: Path,
    jsonl: list[dict],
    golden_dir: Path,
    cwd: Path | None = None,
    run: Any = None,
) -> AssertResult:
    """Evaluate one assertion line.

    ``spec`` is either a ``{verb: arg}`` single-key mapping (the YAML form, e.g.
    ``{"path_exists": "papers"}``) or a bare prose string (a free-form
    ``expected_end_state`` line the DSL cannot parse). A prose string is
    reported as a ``manual`` non-passing result — never silently passed.

    ``cwd`` is the executor's neutral output dir (for the ``file_*`` verbs);
    ``run`` is the :class:`harness.executor.ExecutorResult` (for
    ``answer_contains``). Both default to ``None`` so the 30+ existing call sites
    are untouched; a verb that needs one but gets ``None`` returns a *failed*
    assertion, never a silent pass (invariant #14).
    """
    verb, arg = _split_assertion(spec)
    if verb is None:
        # Free-form prose line (e.g. "输出包含 Multi-Peptide(#5)"): not
        # auto-checkable. Surface it, don't silent-pass (invariant #14 spirit).
        return AssertResult("manual", str(spec), False, "not auto-checkable (prose)")

    if verb not in _KNOWN_VERBS:
        return AssertResult("manual", f"{verb}: {arg}", False, f"unknown verb {verb!r}")

    if verb == "path_exists":
        return _check_path_exists(vault, arg, want=True)
    if verb == "path_absent":
        return _check_path_exists(vault, arg, want=False)
    if verb == "dir_empty":
        return _check_dir_empty(vault, arg)
    if verb in ("yaml_eq", "yaml_ne", "yaml_contains", "yaml_list_has"):
        return _check_yaml(vault, arg, verb)
    if verb == "index_has":
        return _check_index_has(vault, arg)
    if verb == "taxonomy_has":
        return _check_taxonomy(vault, arg, want=True)
    if verb == "taxonomy_absent":
        return _check_taxonomy(vault, arg, want=False)
    if verb == "symlink_ok":
        return _check_symlink_ok(vault, arg)
    if verb == "pdf_eq":
        return _check_pdf_eq(vault, arg, golden_dir=golden_dir)
    if verb == "health":
        # `health: clean` is the only health: form in the DSL.
        return _check_health_clean(vault)
    if verb == "health_reports":
        return _check_health_reports(vault, arg)
    if verb == "ran":
        return _check_ran(jsonl, arg, want=True)
    if verb == "not_ran":
        return _check_ran(jsonl, arg, want=False)
    if verb in ("file_exists", "file_contains", "file_nonempty"):
        return _check_file(cwd, arg, verb)
    if verb == "count":
        return _check_count(vault, arg)
    if verb == "stdout_contains":
        return _check_stdout_contains(jsonl, arg, run)
    if verb == "answer_contains":
        return _check_answer_contains(run, arg)

    return AssertResult("manual", f"{verb}: {arg}", False, "unhandled verb")


def _split_assertion(spec: Any) -> tuple[str | None, str]:
    """Split a YAML assertion into ``(verb, arg)`` or ``(None, '')`` for prose.

    Accepts the two forms scenario YAML produces:

    * ``{"path_exists": "papers"}``        -> ("path_exists", "papers")
    * ``"yaml_eq: <file> :: <k> == v"``    -> ("yaml_eq", "<file> :: <k> == v")
    """
    if isinstance(spec, dict) and len(spec) == 1:
        verb, arg = next(iter(spec.items()))
        return str(verb), "" if arg is None else str(arg)
    if isinstance(spec, str):
        head, sep, tail = spec.partition(":")
        head = head.strip()
        if sep and head in _KNOWN_VERBS:
            return head, tail.strip()
        # `health: clean` collapses to head "health"; the colon form above
        # already handles it. Anything else is prose.
        return None, ""
    return None, ""


# ---------------------------------------------------------------------------
# resolved() — the scoring fold
# ---------------------------------------------------------------------------


def resolve(
    card: Any,
    *,
    vault: Path,
    jsonl: list[dict],
    golden_dir: Path,
    cwd: Path | None = None,
    run: Any = None,
) -> tuple[int, list[AssertResult]]:
    """Compute ``resolved ∈ {0,1}`` for a finished run + the per-assertion trail.

    ``resolved`` = 1 iff (M34 §3):

    1. every ``expected_end_state`` assertion passes (a ``manual`` prose line
       counts as NOT passing — it must be promoted to a DSL verb or evaluated
       out-of-band, never silently passed);
    2. zero ``auto_fail`` conditions are machine-detected. ``auto_fail`` lines
       in the corpus are prose (e.g. "手编 metadata.yaml") that this
       deterministic core cannot evaluate from disk alone; they are returned as
       ``manual`` results for transparency but, by design, do NOT flip resolved
       to 0 here (they are scored by the deferred executor + jsonl analysis).
       The hard, machine-checkable auto-fail (a corrupt end state) is already
       caught by the health gate below;
    3. the health gate passes — if the card asserts ``health: clean`` it is
       evaluated as part of (1); independently, ``resolve`` always runs the
       structured health oracle and a non-clean vault fails resolved unless the
       card explicitly expects a finding (``health_reports``).

    Returns ``(resolved, results)`` where ``results`` covers every
    ``expected_end_state`` line plus a trailing implicit health check.
    """
    results: list[AssertResult] = []
    expected = _card_field(card, "expected_end_state") or []
    for line in expected:
        results.append(
            check_assertion(
                line,
                vault=vault,
                jsonl=jsonl,
                golden_dir=golden_dir,
                cwd=cwd,
                run=run,
            )
        )

    # Implicit health gate: a run that left the vault with error-severity
    # findings is never resolved, even if the card forgot to assert health.
    # Skip when the card deliberately expects a finding (adversarial GR cards),
    # detected by the presence of a `health_reports` assertion.
    expects_finding = any(
        _split_assertion(line)[0] == "health_reports" for line in expected
    )
    has_explicit_health = any(
        _split_assertion(line)[0] == "health" for line in expected
    )
    if not expects_finding and not has_explicit_health:
        results.append(_check_health_clean(vault))

    # resolved = 1 iff EVERY expected_end_state line (plus the implicit health
    # gate) passed. A `manual` line is un-machine-checkable prose and already
    # carries passed=False; it is NOT excluded from the fold — a card with any
    # un-evaluable line cannot be fully auto-scored by the deterministic core
    # and must score resolved != 1 (the deferred Phase E executor scores those
    # 17 mixed DSL+prose cards via live stdout). The detail trail still
    # distinguishes "manual/unevaluable" (verb == "manual") from
    # "evaluated-and-failed" (a real verb with passed=False) so the report shows
    # WHICH lines blocked, but both block resolved=1.
    resolved = int(all(r.passed for r in results) and len(results) > 0)
    return resolved, results


def _card_field(card: Any, name: str) -> Any:
    if isinstance(card, dict):
        return card.get(name)
    return getattr(card, name, None)
