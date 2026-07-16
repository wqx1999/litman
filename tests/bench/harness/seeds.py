"""Phase B-seed — deterministic seed-snapshot builder (no agent).

A *seed* is a named, fully-populated vault state that one or more scenario
cards start from (M34 §3.0 layer 1). Seeds are built with deterministic ``lit``
commands only — never an agent — so they are perfectly reproducible and always
match the current litman schema (they are rebuilt, never committed; invariant
#9). The scoring unit (a once-off run vault) is a ``cp`` of a seed, never a
fresh ``lit init`` (M34 §3.0 layer 2 lives in :mod:`harness.runlit`).

Design:

* :class:`SeedStep` is one build operation (``init`` / ``add`` / ``taxonomy_add``
  / ``modify`` / ``project_add`` / ``link`` / ``read``). A :class:`SeedSpec` is
  an ordered list of steps. The model is **scale-agnostic** — a spec could
  enumerate 100 ``add`` steps; we simply do not define a 100-paper seed yet.
* Each ``add`` step names a fixture id (1..10). The fixture's PDF lives in
  ``fixtures/pdfs/<n>.pdf`` and its golden metadata in ``fixtures/golden/<n>.json``
  (invariant #1: the CLI writes metadata.yaml from the LLM-shaped JSON; the
  harness never hand-writes a metadata file).
* :func:`build_seed` is **idempotent**: a built seed caches a ``.seed-key`` stamp
  derived from the litman source fingerprint; a second build with the same key
  is a cache hit (no-op). A litman change (schema drift) flips the key and forces
  a rebuild, so a seed can never go stale against the code under test.

All paths default to ``/tmp`` (never ``/work`` — EDQUOT). Seeds are cached under
``/tmp/litman-bench-seeds/<name>/``.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

# ---------------------------------------------------------------------------
# Paths + the lit binary
# ---------------------------------------------------------------------------

BENCH_DIR = Path(__file__).resolve().parent.parent
FIXTURES_DIR = BENCH_DIR / "fixtures"
PDFS_DIR = FIXTURES_DIR / "pdfs"
GOLDEN_DIR = FIXTURES_DIR / "golden"

DEFAULT_CACHE_ROOT = Path("/tmp/litman-bench-seeds")
SEED_KEY_FILE = ".seed-key"

# The `lit` CLI entry point, resolved in priority order: an explicit env override
# (LITMAN_BENCH_LIT_BIN — pin a specific install for reproducibility) -> `lit`
# discovered on PATH (the portable default — a venv `pip install ./litman` or
# `pipx install` puts it there, so a fresh clone needs no config) -> bare "lit"
# (subprocess resolves it, or fails with a clear error if litman is not installed).
LIT_BIN = Path(
    os.environ.get("LITMAN_BENCH_LIT_BIN")
    or shutil.which("lit")
    or "lit"
)


# ---------------------------------------------------------------------------
# litman source fingerprint (cache-invalidation key)
# ---------------------------------------------------------------------------


def litman_fingerprint() -> str:
    """A short hash that changes when the installed litman code changes.

    Combines ``litman.__version__`` with a content digest of every ``.py`` file
    in the installed package (path + size + mtime-ns). An editable install
    points at the repo src, so any schema edit flips the digest and forces a
    seed rebuild — the seed can never silently go stale against the code under
    test. Falls back to the version string alone if the package cannot be
    located (it always can in this env, but be defensive at the boundary).
    """
    import litman

    parts: list[str] = [getattr(litman, "__version__", "?")]
    pkg_root = Path(litman.__file__).resolve().parent
    for py in sorted(pkg_root.rglob("*.py")):
        try:
            st = py.stat()
        except OSError:
            continue
        rel = py.relative_to(pkg_root)
        parts.append(f"{rel}:{st.st_size}:{st.st_mtime_ns}")
    # Fold in the seed-builder source itself (this file). The litman digest above
    # only catches schema/behavior drift in the *package*; it does NOT see edits to
    # the bench harness's own SEED_SPECS / step logic. Without this, adding a step
    # (e.g. the `relate` edge for C4) would NOT invalidate a `/tmp` seed cached
    # before the edit, so a run would silently reuse a stale seed and the card that
    # needed the new precondition would fail. Same size+mtime scheme as above.
    try:
        st = Path(__file__).resolve().stat()
        parts.append(f"__seedsrc__:{st.st_size}:{st.st_mtime_ns}")
    except OSError:
        pass
    digest = hashlib.sha256("\n".join(parts).encode("utf-8")).hexdigest()
    return digest[:16]


# ---------------------------------------------------------------------------
# Seed spec model
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SeedStep:
    """One deterministic build operation.

    ``op`` is the operation name; the remaining fields carry its arguments.
    Only the fields relevant to ``op`` are read (see :func:`_apply_step`):

    * ``init``         — no extra fields.
    * ``add``          — ``fixture`` (manifest id 1..10).
    * ``taxonomy_add`` — ``dict_name`` + ``values``.
    * ``modify``       — ``fixture`` + ``set`` (dict of key=value) and/or
                         ``add_tags`` (list of ``"key=value"``).
    * ``project_add``  — ``project`` (name); the dir is created under the vault.
    * ``link``         — ``fixture`` + ``project``.
    * ``read``         — ``fixture``.
    * ``notes``        — ``fixture`` + ``notes_text`` (appended to that paper's
                         ``notes.md``; emulates a user-authored note so cards like
                         C3 ``lit search`` have content to find — there is no CLI
                         notes-writer, notes.md is a plain authored TRUTH file).
    * ``relate``       — ``fixture`` + ``fixture_b``: assert a symmetric
                         ``related`` edge between the two papers (``lit modify
                         <a> --add-tag related=<b>``; the CLI auto double-writes
                         the reverse field). Satisfies cards whose precondition
                         is "#a related to #b" (C4 ``lit related``, G2 rename
                         ripple) — author overlap alone does NOT drive ``lit
                         related``; it needs an explicit edge or a shared topic.
    """

    op: str
    fixture: int | None = None
    fixture_b: int | None = None
    dict_name: str | None = None
    values: tuple[str, ...] = ()
    set: tuple[tuple[str, str], ...] = ()
    add_tags: tuple[str, ...] = ()
    project: str | None = None
    notes_text: str | None = None


@dataclass(frozen=True)
class SeedSpec:
    """An ordered list of build steps that produce one named seed state."""

    name: str
    steps: tuple[SeedStep, ...]
    description: str = ""


def _add(fixture: int) -> SeedStep:
    return SeedStep("add", fixture=fixture)


def _tax(dict_name: str, *values: str) -> SeedStep:
    return SeedStep("taxonomy_add", dict_name=dict_name, values=tuple(values))


def _modify(
    fixture: int,
    *,
    set_: tuple[tuple[str, str], ...] = (),
    add_tags: tuple[str, ...] = (),
) -> SeedStep:
    """``lit modify`` step. ``set_`` drives ``--set k=v`` (scalar fields, incl.
    semantic ones like ``read-date`` / ``status`` — used to seed a paper into an
    already-read state with a FIXED past date so cross-state checks stay
    deterministic, e.g. B2-revisit's "read-date unchanged"). ``add_tags`` drives
    ``--add-tag``."""
    return SeedStep("modify", fixture=fixture, set=set_, add_tags=add_tags)


def _project(name: str) -> SeedStep:
    return SeedStep("project_add", project=name)


def _link(fixture: int, project: str) -> SeedStep:
    return SeedStep("link", fixture=fixture, project=project)


def _notes(fixture: int, text: str) -> SeedStep:
    return SeedStep("notes", fixture=fixture, notes_text=text)


def _relate(fixture_a: int, fixture_b: int) -> SeedStep:
    return SeedStep("relate", fixture=fixture_a, fixture_b=fixture_b)


# ---------------------------------------------------------------------------
# The seed set (4 seeds; seed-100papers DEFERRED but the model scales to it)
# ---------------------------------------------------------------------------
#
# Build steps were derived by scanning every card's ``precondition`` in
# scenarios/*.yaml and merging same-state needs (M34 §3.0 / ruling 2). Fixture
# id -> paper, confirmed against manifest.yaml + fixtures/golden/<n>.json:
#   #1 DiffDock (2022) | #2 EDM no-repo (2022) | #4 PeptideBERT (2023)
#   #5 Multi-Peptide same-group as #4 (2024) | #9/#10 AMP papers (2025).

_INIT = SeedStep("init")

SEED_SPECS: dict[str, SeedSpec] = {
    # --- empty: just an initialized vault -----------------------------------
    # Covers every card whose precondition is "隔离库已 init" with nothing in
    # it (A1, E3, D2-pty parent, F1-build-from-empty, G5, H1-build, J1, J2).
    "seed-empty": SeedSpec(
        name="seed-empty",
        steps=(_INIT,),
        description="Initialized but empty vault.",
    ),
    # --- 1 paper: DiffDock (#1) ---------------------------------------------
    # B1/B2/B3 (modify/read/revisit on #1), C3 (search notes), G3 (trash+restore).
    "seed-1paper-diffdock": SeedSpec(
        name="seed-1paper-diffdock",
        steps=(
            _INIT,
            _add(1),
            # C3 precondition: the user has written "focal loss" in #1's notes, so
            # `lit search "focal loss"` has something to locate (id contains
            # "DiffDock" → satisfies the stdout assertion deterministically).
            _notes(
                1,
                "Reading notes — DiffDock.\n\n"
                "Tried reframing the confidence head with a focal loss to fight "
                "the easy-negative imbalance in the pose ranking.",
            ),
        ),
        description="Vault with #1 DiffDock added (status=inbox); notes mention focal loss (C3).",
    ),
    # --- 1 paper, already read (FIXED past read-date) -----------------------
    # B2-revisit precondition: #1 is a FINISHED paper (read-date set, status
    # deep-read) so "re-opening it" → `lit revisit` is the right stamp. The date
    # is a FIXED past value (not `lit read`, which stamps today) so B2's
    # "read-date unchanged after revisit" is a deterministic `yaml_eq`. Distinct
    # from seed-1paper-diffdock (status=inbox/unread) which B1 needs.
    "seed-1paper-diffdock-read": SeedSpec(
        name="seed-1paper-diffdock-read",
        steps=(
            _INIT,
            _add(1),
            _modify(1, set_=(("read-date", "2026-05-01"), ("status", "deep-read"))),
        ),
        description=(
            "Vault with #1 DiffDock already read (read-date=2026-05-01, "
            "status=deep-read) — B2 revisit precondition."
        ),
    ),
    # --- 2 papers: PeptideBERT (#4) + Multi-Peptide (#5), same group --------
    # A2 (precondition has #1; add #4 — but the *checked* state is #4 present;
    # we seed #4+#5 which also serves C2/C4/D1/D4/A3/G2 same-group retrieval).
    "seed-2papers-peptide": SeedSpec(
        name="seed-2papers-peptide",
        # _relate(4, 5) asserts the #4↔#5 `related` edge (CLI double-writes the
        # reverse side). Without it `lit related #4` returns empty — author
        # overlap is NOT a `lit related` neighbour kind — so C4 was a guaranteed-0
        # false negative; it also satisfies G2's "#4 related #5" rename-ripple
        # precondition. Health stays clean (both papers exist; symmetric edge).
        steps=(_INIT, _add(4), _add(5), _relate(4, 5)),
        description=(
            "#4 PeptideBERT + #5 Multi-Peptide (same author group), joined by a "
            "symmetric `related` edge (C4 retrieval / G2 ripple precondition)."
        ),
    ),
    # --- 2 papers, #4 read AND revisited (both dates FIXED) -----------------
    # C2's precondition: #4 is finished (read-date + status) and was re-opened
    # once, on a known past date. `last-revisited` is the ONLY field that is
    # show-only across EVERY exit — not just the lookup commands, but `lit cite`
    # and `lit export` too (measured; the card's notes carry the table). That is
    # what lets C2 assert `ran: show` honestly. A superset of seed-2papers-peptide
    # (same three steps, one `modify` appended) rather than an edit of it:
    # A3/D1/F1/G1/C4/G2 all start from that seed and must not inherit a
    # read/revisited #4.
    "seed-2papers-peptide-revisited": SeedSpec(
        name="seed-2papers-peptide-revisited",
        steps=(
            _INIT,
            _add(4),
            _add(5),
            _relate(4, 5),
            # All three values FIXED, never "today": the card asserts the exact
            # date back, so a re-run tomorrow must score identically (same
            # reasoning as seed-1paper-diffdock-read's read-date).
            # `read-date` is not optional dressing: `last-revisited` without it
            # is a semantically impossible state (you cannot re-read what you
            # never read — `lit revisit`, the sugar for this `--set`, requires a
            # finished paper), and a seed must not build a vault the product
            # itself would never produce.
            _modify(
                4,
                set_=(
                    ("read-date", "2026-05-01"),
                    ("status", "deep-read"),
                    ("last-revisited", "2026-06-15"),
                ),
            ),
        ),
        description=(
            "#4 PeptideBERT + #5 Multi-Peptide (related edge, as "
            "seed-2papers-peptide), plus #4 read (read-date=2026-05-01, "
            "status=deep-read) and revisited on 2026-06-15 — C2's show-only "
            "precondition."
        ),
    ),
    # --- 5 papers + tagged + a project --------------------------------------
    # Governance / export / list-filter cards (C1, D2, F1-with-content). Two
    # papers carry topic "diffusion" so D2's `taxonomy rm diffusion` has a real
    # cascade to clean; the peptide papers carry topic "peptide" for C1's filter.
    "seed-5papers-tagged": SeedSpec(
        name="seed-5papers-tagged",
        steps=(
            _INIT,
            _add(1),  # DiffDock 2022
            _add(2),  # EDM 2022
            _add(4),  # PeptideBERT 2023
            _add(5),  # Multi-Peptide 2024
            _add(9),  # therapeutic-peptide predictor 2025
            _tax("topics", "diffusion", "peptide", "amp"),
            _modify(1, add_tags=("topics=diffusion",)),
            _modify(2, add_tags=("topics=diffusion",)),
            _modify(4, add_tags=("topics=peptide",)),
            _modify(5, add_tags=("topics=peptide",)),
            _modify(9, add_tags=("topics=amp",)),
            _project("PepCodec"),
            _link(4, "PepCodec"),
        ),
        description=(
            "5 papers; #1/#2 tagged diffusion, #4/#5 tagged peptide, #9 tagged "
            "amp; project PepCodec registered with #4 linked."
        ),
    ),
}


# ---------------------------------------------------------------------------
# Build execution
# ---------------------------------------------------------------------------


@dataclass
class _BuildEnv:
    """Per-build mutable context threaded through the steps."""

    vault: Path
    env: dict[str, str]
    scratch: Path
    project_ids: dict[int, str] = field(default_factory=dict)


def _isolated_seed_env(registry_dir: Path) -> dict[str, str]:
    """Child env for seed-build subprocesses: redirect registry, drop LIT_LIBRARY.

    Same red line as the run-vault isolation (M34 §4): the real registry must
    not be touched and the real vault (discoverable ONLY via ``$LIT_LIBRARY``)
    must be unreachable. We start from ``os.environ`` so PATH / conda bits
    survive, then mutate.
    """
    env = os.environ.copy()
    env["LITMAN_REGISTRY_DIR"] = str(registry_dir)
    env.pop("LIT_LIBRARY", None)
    return env


def _run_lit(args: list[str], *, env: dict[str, str], cwd: Path | None = None) -> None:
    """Run a ``lit`` subcommand, raising on non-zero exit (build must be exact).

    stdin is ``/dev/null`` (never a pipe) so a non-TTY confirm prompt aborts
    rather than hanging (OQ1). Output is captured and surfaced in the exception
    so a build break is debuggable.
    """
    proc = subprocess.run(
        [str(LIT_BIN), *args],
        env=env,
        cwd=str(cwd) if cwd else None,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        raise RuntimeError(
            "seed build step failed: lit "
            + " ".join(args)
            + f"\nexit={proc.returncode}\nstdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
        )


def _paper_id_for_fixture(vault: Path, fixture: int) -> str:
    """Resolve the id `lit add` derived for a fixture by title-substring match.

    We never hardcode ``derive_id()`` output (scenarios §2): read INDEX.json and
    match on the golden title. Returns the single matching paper id.
    """
    import json

    golden = _load_golden(fixture)
    want = _norm(golden["title"])
    index = vault / "INDEX.json"
    payload = json.loads(index.read_text(encoding="utf-8"))
    matches = [
        p["id"]
        for p in payload.get("papers", [])
        if _norm(str(p.get("title", ""))).startswith(want[:30])
    ]
    if len(matches) != 1:
        raise RuntimeError(
            f"could not resolve unique paper id for fixture {fixture} "
            f"(title {golden['title']!r}); matches={matches}"
        )
    return matches[0]


def _norm(s: str) -> str:
    return " ".join(s.split()).casefold()


def _load_golden(fixture: int) -> dict:
    import json

    path = GOLDEN_DIR / f"{fixture}.json"
    return json.loads(path.read_text(encoding="utf-8"))


def _apply_step(step: SeedStep, ctx: _BuildEnv) -> None:
    vault = ctx.vault
    lib = ["--library", str(vault)]

    if step.op == "init":
        parent = vault.parent
        parent.mkdir(parents=True, exist_ok=True)
        _run_lit(
            ["init", str(parent), "--name", vault.name, "--no-register"],
            env=ctx.env,
        )
        return

    if step.op == "add":
        assert step.fixture is not None
        src_pdf = PDFS_DIR / f"{step.fixture}.pdf"
        golden = GOLDEN_DIR / f"{step.fixture}.json"
        if not src_pdf.is_file():
            raise RuntimeError(
                f"fixture PDF missing: {src_pdf} — run fetch_fixtures.py first"
            )
        # `lit add` MOVES the source PDF; copy into scratch so the fixture cache
        # is never consumed.
        staged_pdf = ctx.scratch / f"{step.fixture}.pdf"
        shutil.copy2(src_pdf, staged_pdf)
        _run_lit(
            ["add", str(staged_pdf), "--from-llm-json", str(golden), *lib],
            env=ctx.env,
        )
        return

    if step.op == "taxonomy_add":
        assert step.dict_name is not None
        _run_lit(
            ["taxonomy", "add", step.dict_name, *step.values, *lib],
            env=ctx.env,
        )
        return

    if step.op == "modify":
        assert step.fixture is not None
        pid = _paper_id_for_fixture(vault, step.fixture)
        args = ["modify", pid]
        for key, val in step.set:
            args += ["--set", f"{key}={val}"]
        for tag in step.add_tags:
            args += ["--add-tag", tag]
        _run_lit([*args, *lib], env=ctx.env)
        return

    if step.op == "project_add":
        assert step.project is not None
        # `lit project add --path` requires the dir to already exist.
        proj_dir = vault.parent / "projects" / step.project
        proj_dir.mkdir(parents=True, exist_ok=True)
        _run_lit(
            ["project", "add", step.project, "--path", str(proj_dir), *lib],
            env=ctx.env,
        )
        return

    if step.op == "link":
        assert step.fixture is not None and step.project is not None
        pid = _paper_id_for_fixture(vault, step.fixture)
        _run_lit(
            ["link", pid, "--project", step.project, *lib],
            env=ctx.env,
        )
        return

    if step.op == "read":
        assert step.fixture is not None
        pid = _paper_id_for_fixture(vault, step.fixture)
        _run_lit(["read", pid, *lib], env=ctx.env)
        return

    if step.op == "notes":
        assert step.fixture is not None and step.notes_text is not None
        pid = _paper_id_for_fixture(vault, step.fixture)
        # No CLI notes-writer exists; notes.md is a plain user-authored file
        # (`lit add` seeds a template). Append the fixture note directly — this
        # emulates the user having written it, the only way to satisfy a card
        # whose precondition is "the user wrote X in notes" (e.g. C3). notes
        # content is NOT in INDEX.json / views, so nothing derived desyncs.
        notes_path = vault / "papers" / pid / "notes.md"
        existing = notes_path.read_text(encoding="utf-8") if notes_path.is_file() else ""
        sep = "" if existing.endswith("\n") or not existing else "\n"
        notes_path.write_text(
            existing + sep + step.notes_text.rstrip("\n") + "\n", encoding="utf-8"
        )
        return

    if step.op == "relate":
        assert step.fixture is not None and step.fixture_b is not None
        pid_a = _paper_id_for_fixture(vault, step.fixture)
        pid_b = _paper_id_for_fixture(vault, step.fixture_b)
        # `related` is symmetric/self-paired: this one call also writes the
        # reverse edge on pid_b (core/relations.py double-write), so the seed
        # need not relate both directions.
        _run_lit(
            ["modify", pid_a, "--add-tag", f"related={pid_b}", *lib],
            env=ctx.env,
        )
        return

    raise ValueError(f"unknown seed step op: {step.op!r}")


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def build_seed(
    name: str,
    *,
    golden_dir: Path = GOLDEN_DIR,
    cache_root: Path = DEFAULT_CACHE_ROOT,
    force: bool = False,
) -> Path:
    """Build (or reuse a cached) seed vault, returning the vault path.

    Idempotent: if ``<cache_root>/<name>/`` already exists and its ``.seed-key``
    matches the current litman fingerprint, the build is skipped and the cached
    vault path is returned. A fingerprint mismatch (litman code changed) or
    ``force=True`` rebuilds from scratch.

    The returned path is the *vault* directory (``<cache_root>/<name>/vault``),
    not the parent. ``golden_dir`` is accepted so a caller can point at an
    alternate golden set; it defaults to the committed ``fixtures/golden/``.
    """
    if name not in SEED_SPECS:
        raise KeyError(f"unknown seed {name!r}; known: {sorted(SEED_SPECS)}")
    spec = SEED_SPECS[name]

    global GOLDEN_DIR  # noqa: PLW0603 — allow per-call override of the golden src
    prev_golden = GOLDEN_DIR
    GOLDEN_DIR = golden_dir

    try:
        cache_root.mkdir(parents=True, exist_ok=True)
        seed_root = cache_root / name
        vault = seed_root / "vault"
        key_file = seed_root / SEED_KEY_FILE
        want_key = litman_fingerprint()

        if (
            not force
            and vault.is_dir()
            and (vault / "lit-config.yaml").is_file()
            and key_file.is_file()
            and key_file.read_text(encoding="utf-8").strip() == want_key
        ):
            return vault  # cache hit

        # Stale / missing / forced: rebuild from clean slate.
        if seed_root.exists():
            shutil.rmtree(seed_root)
        seed_root.mkdir(parents=True)
        scratch = seed_root / "_scratch"
        scratch.mkdir()
        registry = seed_root / "registry"
        env = _isolated_seed_env(registry)

        ctx = _BuildEnv(vault=vault, env=env, scratch=scratch)
        for step in spec.steps:
            _apply_step(step, ctx)

        # Drop the build scratch (staged PDFs already moved into the vault).
        shutil.rmtree(scratch, ignore_errors=True)
        key_file.write_text(want_key + "\n", encoding="utf-8")
        return vault
    finally:
        GOLDEN_DIR = prev_golden


def ensure_seeds(
    names: list[str],
    *,
    golden_dir: Path = GOLDEN_DIR,
    cache_root: Path = DEFAULT_CACHE_ROOT,
    force: bool = False,
) -> dict[str, Path]:
    """Build each named seed (idempotent) and return ``{name: vault_path}``."""
    return {
        name: build_seed(
            name, golden_dir=golden_dir, cache_root=cache_root, force=force
        )
        for name in names
    }
