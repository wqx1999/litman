"""Shared pytest fixtures for litman tests.

Provides ``make_text_pdf``: a zero-dependency builder for small, multi-page,
text-bearing PDFs. pypdf (the only declared PDF dep) cannot synthesize text
content streams, and reportlab/fpdf are intentionally not dependencies, so
the fixture hand-assembles a minimal valid PDF whose pages carry the given
lines as extractable text. Used by the M20 code-URL scanner tests and the
``lit add`` full-text-scan integration tests.
"""

from __future__ import annotations

import io
import os
from collections.abc import Callable, Sequence
from pathlib import Path

import pytest

from litman.core.document import list_papers
from litman.core.library import create_vault
from litman.core.vault_registry import REGISTRY_ENV_VAR
from litman.core.views import write_index


def _build_text_pdf(pages: Sequence[Sequence[str]]) -> bytes:
    """Hand-assemble a minimal multi-page PDF carrying the given text lines.

    ``pages`` is a sequence of pages; each page is a sequence of text lines.
    The produced bytes parse with ``pypdf.PdfReader`` and each page's
    ``extract_text()`` returns the lines in order.
    """
    n_pages = len(pages)
    page_obj_nums = [4 + i * 2 for i in range(n_pages)]
    content_obj_nums = [4 + i * 2 + 1 for i in range(n_pages)]

    pieces: dict[int, bytes] = {}
    pieces[1] = b"<< /Type /Catalog /Pages 2 0 R >>"
    kids = " ".join(f"{p} 0 R" for p in page_obj_nums)
    pieces[2] = (
        f"<< /Type /Pages /Kids [{kids}] /Count {n_pages} >>".encode()
    )
    pieces[3] = b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"

    for i, lines in enumerate(pages):
        pn = page_obj_nums[i]
        cn = content_obj_nums[i]
        pieces[pn] = (
            f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
            f"/Resources << /Font << /F1 3 0 R >> >> "
            f"/Contents {cn} 0 R >>"
        ).encode()
        ops = "BT /F1 12 Tf 50 700 Td 14 TL\n"
        for ln in lines:
            esc = ln.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
            ops += f"({esc}) Tj T*\n"
        ops += "ET"
        stream = ops.encode()
        pieces[cn] = (
            b"<< /Length %d >>\nstream\n%s\nendstream" % (len(stream), stream)
        )

    out = io.BytesIO()
    out.write(b"%PDF-1.4\n")
    offsets: dict[int, int] = {}
    max_obj = max(pieces)
    for num in range(1, max_obj + 1):
        offsets[num] = out.tell()
        out.write(f"{num} 0 obj\n".encode())
        out.write(pieces[num])
        out.write(b"\nendobj\n")
    xref_pos = out.tell()
    out.write(f"xref\n0 {max_obj + 1}\n".encode())
    out.write(b"0000000000 65535 f \n")
    for num in range(1, max_obj + 1):
        out.write(f"{offsets[num]:010d} 00000 n \n".encode())
    out.write(
        f"trailer\n<< /Size {max_obj + 1} /Root 1 0 R >>\n"
        f"startxref\n{xref_pos}\n%%EOF".encode()
    )
    return out.getvalue()


@pytest.fixture
def make_text_pdf(tmp_path: Path) -> Callable[..., Path]:
    """Factory: write a multi-page text PDF to tmp_path, return its path.

    Usage::

        pdf = make_text_pdf([["page 1 line"], ["page 2 line"]])
        pdf = make_text_pdf([["one page"]], name="custom.pdf")
    """

    def _make(pages: Sequence[Sequence[str]], name: str = "doc.pdf") -> Path:
        path = tmp_path / name
        path.write_bytes(_build_text_pdf(pages))
        return path

    return _make


@pytest.fixture
def vault_with_paper(tmp_path: Path) -> tuple[Path, str]:
    """Vault containing one paper with the canonical M2.0 metadata schema.

    INDEX.json is rebuilt from the on-disk metadata so the derived projection
    matches the paper (the GUI read endpoints read INDEX.json directly, and
    the modify command tests assert post-write INDEX state).
    """
    vault = create_vault(tmp_path)
    paper_id = "2024_Foo_Bar"
    paper_dir = vault / "papers" / paper_id
    paper_dir.mkdir(parents=True)

    # Hand-crafted minimal metadata matching what `lit add` writes today.
    meta = (paper_dir / "metadata.yaml")
    meta.write_text(
        "id: 2024_Foo_Bar\n"
        "title: Foo Bar\n"
        "authors:\n"
        "  - Foo, Alice\n"
        "year: 2024\n"
        "journal: Test J.\n"
        "doi: 10.1/x\n"
        "arxiv-id:\n"
        "github:\n"
        "created-at: '2026-04-28T10:00:00+02:00'\n"
        "updated-at: '2026-04-28T10:00:00+02:00'\n"
        "projects: []\n"
        "topics: []\n"
        "methods: []\n"
        "data: []\n"
        "type: research\n"
        "status: inbox\n"
        "priority: B\n"
        "read-date:\n"
        "last-revisited:\n"
        "related: []\n"
        "contradicts: []\n"
        "extends: []\n"
        "code-clones: []\n",
        encoding="utf-8",
    )
    write_index(vault, list_papers(vault))
    return vault, paper_id


@pytest.fixture
def fake_junction(
    monkeypatch: pytest.MonkeyPatch,
) -> Callable[[Path], Path]:
    """Factory: plant a Windows-junction stand-in at the given path.

    On NTFS every litman link is a directory junction — a mount-point
    reparse point, which Python does NOT consider a symlink: only the
    ``is_junction()`` API sees it. POSIX hosts cannot create one, so the
    stand-in is a real empty directory plus an ``is_junction`` patch scoped
    to the planted paths. Deletion behaves like Windows too: ``unlink()``
    raises and the ``rmdir()`` fallback inside ``remove_link_if_present``
    removes the entry without recursing anywhere. An existing symlink at the
    path (a POSIX-built fixture link) is replaced in place.
    """
    planted: set[Path] = set()
    real_is_junction = Path.is_junction

    def _plant(path: Path) -> Path:
        if path.is_symlink():
            path.unlink()
        path.mkdir(parents=True, exist_ok=True)
        if not planted:
            monkeypatch.setattr(
                Path,
                "is_junction",
                lambda self: self in planted or real_is_junction(self),
            )
        planted.add(path)
        return path

    return _plant


@pytest.fixture(autouse=True)
def _isolate_machine_config_roots(
    tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Pin every OS config-dir ROOT at tmp, so no test can reach the real box.

    ``_isolate_registry`` below pins ``$LITMAN_REGISTRY_DIR``, but 13 test
    modules deliberately ``delenv`` it — they exercise the *default* resolution
    and redirect ``$HOME`` instead. That holds on POSIX and is inert on
    Windows: ``Path.home()`` reads ``%USERPROFILE%`` and platformdirs reads
    ``%LOCALAPPDATA%``, so all 13 fell through to the machine-level
    ``C:\\Users\\<user>\\AppData\\Local\\litman`` — ONE registry shared by the
    whole session. Tests then saw each other's vaults ("already registered"),
    and a plain ``pytest`` run rewrote the real user's registry.

    Pinning the roots makes the fallback safe whichever override a test drops.

    ``XDG_*`` / ``CURSOR_CONFIG_DIR`` are cleared rather than pinned so the
    POSIX defaults (``$HOME/.config``, ``~/.cursor``) resolve under whatever
    tmp home is in force. Left set, ``lit install-skill --agent cursor`` writes
    to the developer's real ``~/.config/cursor/cli-config.json`` — which it did
    on every box where ``$XDG_CONFIG_HOME`` happens to be exported.
    """
    # Deliberately NOT under the test's own ``tmp_path``: several tests assert
    # a directory they were handed is empty ("the probe left nothing behind",
    # "the profile wrote nothing else"), and a home planted inside it would
    # read as litter the test is hunting for.
    home = tmp_path_factory.mktemp("machine-home")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    for var in (
        "XDG_CONFIG_HOME",
        "XDG_DATA_HOME",
        "XDG_CACHE_HOME",
        "CURSOR_CONFIG_DIR",
    ):
        monkeypatch.delenv(var, raising=False)

    # Pinning env vars is not enough on Windows, and this is the whole reason
    # 35 registry failures survived the first attempt at this fixture:
    #
    #   * ``Path.home()`` reads %USERPROFILE%, never $HOME — so the 29 modules
    #     that redirect $HOME (the suite's established convention) redirect
    #     nothing there.
    #   * platformdirs asks the OS through ctypes (``SHGetFolderPathW``) and
    #     ignores %LOCALAPPDATA% entirely, so no env var can move it.
    #
    # ``registry_path``'s docstring promises "tests that monkeypatch HOME see
    # the redirected location". That promise held on POSIX only. Redirecting
    # the two functions themselves makes it true everywhere — and both read
    # the environment at CALL time, so a module fixture that re-points $HOME
    # after this one still wins.
    monkeypatch.setattr(
        Path, "home", classmethod(lambda cls: Path(os.environ["HOME"]))
    )
    # config and cache must stay DISTINCT: the browser profile lives under the
    # cache dir precisely so it is not inside the config dir, and a test pins
    # that separation.
    for mod, attr, sub in (
        ("litman.core.vault_registry", "user_config_dir", ".config"),
        ("litman.core.agent_prefs", "user_config_dir", ".config"),
        ("litman.core.ui_state", "user_config_dir", ".config"),
        ("litman.commands.gui", "user_cache_dir", ".cache"),
    ):
        monkeypatch.setattr(
            f"{mod}.{attr}",
            lambda app, *a, _sub=sub, **k: str(
                Path(os.environ["HOME"]) / _sub / app
            ),
        )


@pytest.fixture(autouse=True)
def _isolate_registry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Redirect the vault registry to an empty per-test temp dir.

    The registry (``vaults.yaml``, by default under
    ``platformdirs.user_config_dir("litman")``) is machine-level global state.
    A test that exercises vault discovery without setting ``$LIT_LIBRARY`` —
    e.g. the negative ``LibraryNotFoundError`` path — would otherwise pick up
    whatever vault is registered and active on the developer's box: it passes
    in CI (empty registry) yet fails locally. Pointing ``$LITMAN_REGISTRY_DIR``
    at a fresh empty dir gives every test an empty registry unless it opts in
    by registering one. ``load_registry()`` treats an absent ``vaults.yaml`` as
    empty, so nothing needs to be created on disk.
    """
    monkeypatch.setenv(REGISTRY_ENV_VAR, str(tmp_path / "litman-registry"))


@pytest.fixture(autouse=True)
def _isolate_skills_dir(
    request: pytest.FixtureRequest,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Point call-time skill probes at empty per-test dirs, not the real home.

    ``skill_status`` (behind the health-check ``skill_drift`` arm, the GUI
    agent-status probe and ``lit install-skill``'s freshness check) compares
    the machine's installed skills against the bundled ones. Left on the real
    home it would make results depend on the developer's box — a stale
    ``~/.claude/skills`` copy flips every clean-vault health-check test to
    exit 1 (passes in CI, fails locally). An absent dir reads as "no skills
    installed", which is the neutral state every test starts from. All three
    resolvers are patched: the Claude Code dir, the open-standard
    ``~/.agents/skills`` dir the cursor adapter probes, AND the Antigravity
    CLI app-data dir the agy adapter probes — the per-agent status chain
    would otherwise read the developer's real install. Tests that need a
    populated skills dir pass ``parent_dir=`` explicitly or re-patch a
    resolver themselves (a test-body patch wins — it is applied after this
    fixture).

    The ``no_skills_isolation`` marker opts a test out entirely — that is for
    the HOME-only end-to-end tests that must drive the REAL resolver chain
    (inject-seam lesson); such a test must redirect ``$HOME`` itself.
    """
    if request.node.get_closest_marker("no_skills_isolation"):
        return
    monkeypatch.setattr(
        "litman.core.skill.default_skills_parent_dir",
        lambda: tmp_path / "skills-parent",
    )
    monkeypatch.setattr(
        "litman.core.skill.standard_skills_parent_dir",
        lambda: tmp_path / "agents-skills-parent",
    )
    monkeypatch.setattr(
        "litman.core.skill.antigravity_skills_parent_dir",
        lambda: tmp_path / "antigravity-skills-parent",
    )
    permission_result = {
        "mode": "unchanged",
        "rule": "test-isolated",
        "warning": None,
    }
    for function_name in (
        "install_claude_lit_permission",
        "install_antigravity_lit_permission",
        "install_codex_lit_permission",
        "install_cursor_lit_permission",
        "install_opencode_lit_permission",
    ):
        monkeypatch.setattr(
            f"litman.core.agent_permissions.{function_name}",
            lambda: permission_result.copy(),
        )
