"""Tests for `lit open <id>` and `core.viewer` helpers (M9.1).

Viewer launches are mocked at ``subprocess.Popen`` so tests never actually
spawn a GUI. The platform-detection branch is exercised by monkeypatching
``sys.platform`` plus ``shutil.which``.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner
from ruamel.yaml import YAML

from litman.cli import cli
from litman.core import viewer as viewer_mod
from litman.core.library import create_vault
from litman.core.viewer import (
    detect_platform_viewer,
    is_headless,
    launch_pdf,
    resolve_paper_id,
)
from litman.exceptions import AmbiguousPaperIdError, PaperNotFoundError


def _write_paper(
    vault: Path, paper_id: str, *, with_pdf: bool = True
) -> None:
    paper_dir = vault / "papers" / paper_id
    paper_dir.mkdir(parents=True)
    payload: dict[str, Any] = {
        "id": paper_id,
        "title": f"Title for {paper_id}",
        "year": 2024,
    }
    yaml = YAML()
    yaml.default_flow_style = False
    with (paper_dir / "metadata.yaml").open("w", encoding="utf-8") as f:
        yaml.dump(payload, f)
    if with_pdf:
        (paper_dir / "paper.pdf").write_bytes(b"%PDF-1.4 fake\n")


@pytest.fixture
def vault(tmp_path: Path) -> Path:
    """Vault seeded with three papers — two share a 'Smith' substring."""
    v = create_vault(tmp_path)
    _write_paper(v, "2024_Smith_Foo")
    _write_paper(v, "2024_Jones_Bar")
    _write_paper(v, "2023_Smith_Baz")
    return v


# ---------------------------------------------------------------------------
# resolve_paper_id — pure function
# ---------------------------------------------------------------------------


def test_resolve_exact_match(vault: Path) -> None:
    assert resolve_paper_id(vault, "2024_Smith_Foo") == "2024_Smith_Foo"


def test_resolve_unique_substring(vault: Path) -> None:
    # "Jones" appears only in 2024_Jones_Bar.
    assert resolve_paper_id(vault, "Jones") == "2024_Jones_Bar"


def test_resolve_substring_is_case_insensitive(vault: Path) -> None:
    assert resolve_paper_id(vault, "jones") == "2024_Jones_Bar"


def test_resolve_ambiguous_raises_with_candidates(vault: Path) -> None:
    # "Smith" appears in both 2024_Smith_Foo and 2023_Smith_Baz.
    with pytest.raises(AmbiguousPaperIdError) as excinfo:
        resolve_paper_id(vault, "Smith")
    assert set(excinfo.value.candidates) == {
        "2024_Smith_Foo",
        "2023_Smith_Baz",
    }


def test_resolve_zero_matches_raises(vault: Path) -> None:
    with pytest.raises(PaperNotFoundError):
        resolve_paper_id(vault, "ZZZ_NothingHere")


def test_resolve_skips_dirs_without_metadata(tmp_path: Path) -> None:
    """A folder under papers/ without metadata.yaml is not a candidate."""
    v = create_vault(tmp_path)
    _write_paper(v, "2024_Real_Paper")
    # Empty folder — looks like a paper dir but missing metadata.
    (v / "papers" / "2024_Empty_Dir").mkdir()
    with pytest.raises(PaperNotFoundError):
        resolve_paper_id(v, "Empty")


# ---------------------------------------------------------------------------
# detect_platform_viewer
# ---------------------------------------------------------------------------


def test_detect_macos_returns_open(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(viewer_mod.sys, "platform", "darwin")
    assert detect_platform_viewer() == "open"


def test_detect_linux_returns_xdg_open(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(viewer_mod.sys, "platform", "linux")
    monkeypatch.setattr(
        viewer_mod.shutil,
        "which",
        lambda cmd: f"/usr/bin/{cmd}" if cmd == "xdg-open" else None,
    )
    assert detect_platform_viewer() == "xdg-open"


def test_detect_linux_falls_back_to_wslview(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(viewer_mod.sys, "platform", "linux")
    monkeypatch.setattr(
        viewer_mod.shutil,
        "which",
        lambda cmd: "/usr/bin/wslview" if cmd == "wslview" else None,
    )
    assert detect_platform_viewer() == "wslview"


def test_detect_linux_no_viewer_returns_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(viewer_mod.sys, "platform", "linux")
    monkeypatch.setattr(viewer_mod.shutil, "which", lambda cmd: None)
    assert detect_platform_viewer() is None


# ---------------------------------------------------------------------------
# launch_pdf
# ---------------------------------------------------------------------------


class _PopenRecorder:
    """Records arg-lists handed to subprocess.Popen across the test."""

    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def __call__(self, args: list[str], **kwargs: Any) -> "_PopenRecorder":
        self.calls.append(list(args))
        return self


def test_launch_pdf_uses_configured_viewer(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    recorder = _PopenRecorder()
    monkeypatch.setattr(viewer_mod.subprocess, "Popen", recorder)
    pdf = tmp_path / "fake.pdf"
    pdf.write_bytes(b"%PDF")
    cmd, source = launch_pdf(pdf, "okular")
    assert cmd == "okular"
    assert source == "configured"
    assert recorder.calls == [["okular", str(pdf)]]


def test_launch_pdf_falls_back_to_platform(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    recorder = _PopenRecorder()
    monkeypatch.setattr(viewer_mod.subprocess, "Popen", recorder)
    monkeypatch.setattr(viewer_mod.sys, "platform", "darwin")
    pdf = tmp_path / "fake.pdf"
    pdf.write_bytes(b"%PDF")
    cmd, source = launch_pdf(pdf, None)
    assert cmd == "open"
    assert source == "platform"
    assert recorder.calls == [["open", str(pdf)]]


def test_launch_pdf_configured_missing_raises(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    def fake_popen(args: list[str], **kwargs: Any) -> None:
        raise FileNotFoundError(args[0])

    monkeypatch.setattr(viewer_mod.subprocess, "Popen", fake_popen)
    pdf = tmp_path / "fake.pdf"
    pdf.write_bytes(b"%PDF")
    with pytest.raises(FileNotFoundError, match="not found on PATH"):
        launch_pdf(pdf, "nonexistent-viewer")


def test_launch_pdf_no_viewer_raises(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(viewer_mod.sys, "platform", "linux")
    monkeypatch.setattr(viewer_mod.shutil, "which", lambda cmd: None)
    pdf = tmp_path / "fake.pdf"
    pdf.write_bytes(b"%PDF")
    with pytest.raises(FileNotFoundError, match="No platform PDF viewer"):
        launch_pdf(pdf, None)


def test_launch_pdf_treats_empty_string_as_no_config(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Empty-string default_pdf_viewer must fall through to platform default,
    same as None — defensive in case a hand-edited yaml has ``"".``"""
    recorder = _PopenRecorder()
    monkeypatch.setattr(viewer_mod.subprocess, "Popen", recorder)
    monkeypatch.setattr(viewer_mod.sys, "platform", "darwin")
    pdf = tmp_path / "fake.pdf"
    pdf.write_bytes(b"%PDF")
    cmd, source = launch_pdf(pdf, "")
    assert cmd == "open"
    assert source == "platform"


# ---------------------------------------------------------------------------
# launch_pdf — headless (xdg-open without a display)
# ---------------------------------------------------------------------------


def _patch_linux_xdg(monkeypatch: pytest.MonkeyPatch) -> None:
    """Linux with xdg-open present (and no wslview)."""
    monkeypatch.setattr(viewer_mod.sys, "platform", "linux")
    monkeypatch.setattr(
        viewer_mod.shutil,
        "which",
        lambda cmd: f"/usr/bin/{cmd}" if cmd == "xdg-open" else None,
    )


def test_launch_pdf_xdg_open_with_display_launches(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    recorder = _PopenRecorder()
    monkeypatch.setattr(viewer_mod.subprocess, "Popen", recorder)
    _patch_linux_xdg(monkeypatch)
    monkeypatch.setenv("DISPLAY", ":0")
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
    pdf = tmp_path / "fake.pdf"
    pdf.write_bytes(b"%PDF")
    cmd, source = launch_pdf(pdf, None)
    assert (cmd, source) == ("xdg-open", "platform")
    assert recorder.calls == [["xdg-open", str(pdf)]]


def test_launch_pdf_xdg_open_headless_raises_and_skips_popen(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    recorder = _PopenRecorder()
    monkeypatch.setattr(viewer_mod.subprocess, "Popen", recorder)
    _patch_linux_xdg(monkeypatch)
    monkeypatch.delenv("DISPLAY", raising=False)
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
    pdf = tmp_path / "fake.pdf"
    pdf.write_bytes(b"%PDF")
    with pytest.raises(FileNotFoundError, match="No graphical display"):
        launch_pdf(pdf, None)
    # Must NOT have forked a process that silently fails.
    assert recorder.calls == []


def test_launch_pdf_xdg_open_wayland_only_launches(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """WAYLAND_DISPLAY set (DISPLAY unset) still counts as a display."""
    recorder = _PopenRecorder()
    monkeypatch.setattr(viewer_mod.subprocess, "Popen", recorder)
    _patch_linux_xdg(monkeypatch)
    monkeypatch.delenv("DISPLAY", raising=False)
    monkeypatch.setenv("WAYLAND_DISPLAY", "wayland-0")
    pdf = tmp_path / "fake.pdf"
    pdf.write_bytes(b"%PDF")
    cmd, source = launch_pdf(pdf, None)
    assert (cmd, source) == ("xdg-open", "platform")
    assert recorder.calls == [["xdg-open", str(pdf)]]


def test_launch_pdf_wslview_not_gated_by_display(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """wslview reaches the Windows host regardless of any Linux display."""
    recorder = _PopenRecorder()
    monkeypatch.setattr(viewer_mod.subprocess, "Popen", recorder)
    monkeypatch.setattr(viewer_mod.sys, "platform", "linux")
    monkeypatch.setattr(
        viewer_mod.shutil,
        "which",
        lambda cmd: "/usr/bin/wslview" if cmd == "wslview" else None,
    )
    monkeypatch.delenv("DISPLAY", raising=False)
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
    pdf = tmp_path / "fake.pdf"
    pdf.write_bytes(b"%PDF")
    cmd, source = launch_pdf(pdf, None)
    assert (cmd, source) == ("wslview", "wsl-fallback")
    assert recorder.calls == [["wslview", str(pdf)]]


def test_is_headless_reads_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DISPLAY", raising=False)
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
    assert is_headless() is True
    monkeypatch.setenv("DISPLAY", ":0")
    assert is_headless() is False


# ---------------------------------------------------------------------------
# lit open CLI
# ---------------------------------------------------------------------------


@pytest.fixture
def darwin_popen(monkeypatch: pytest.MonkeyPatch) -> _PopenRecorder:
    """Default test environment: macOS, viewer always launches successfully."""
    recorder = _PopenRecorder()
    monkeypatch.setattr(viewer_mod.subprocess, "Popen", recorder)
    monkeypatch.setattr(viewer_mod.sys, "platform", "darwin")
    return recorder


def test_open_exact_id_launches_viewer(
    vault: Path, darwin_popen: _PopenRecorder
) -> None:
    runner = CliRunner()
    result = runner.invoke(
        cli, ["open", "2024_Smith_Foo", "--library", str(vault)]
    )
    assert result.exit_code == 0, result.output
    assert len(darwin_popen.calls) == 1
    assert darwin_popen.calls[0][0] == "open"
    assert darwin_popen.calls[0][1].endswith(
        "/papers/2024_Smith_Foo/paper.pdf"
    )
    assert "Opened" in result.output
    assert "2024_Smith_Foo" in result.output


def test_open_substring_match_works(
    vault: Path, darwin_popen: _PopenRecorder
) -> None:
    runner = CliRunner()
    result = runner.invoke(
        cli, ["open", "Jones", "--library", str(vault)]
    )
    assert result.exit_code == 0, result.output
    assert darwin_popen.calls[0][1].endswith(
        "/papers/2024_Jones_Bar/paper.pdf"
    )


def test_open_ambiguous_lists_candidates_and_exits_1(
    vault: Path, darwin_popen: _PopenRecorder
) -> None:
    runner = CliRunner()
    result = runner.invoke(
        cli, ["open", "Smith", "--library", str(vault)]
    )
    assert result.exit_code == 1
    assert "Ambiguous" in result.output
    assert "2024_Smith_Foo" in result.output
    assert "2023_Smith_Baz" in result.output
    # Did NOT launch a viewer.
    assert darwin_popen.calls == []


def test_open_unknown_id_exits_1(
    vault: Path, darwin_popen: _PopenRecorder
) -> None:
    runner = CliRunner()
    result = runner.invoke(
        cli, ["open", "ZZZ_NothingHere", "--library", str(vault)]
    )
    assert result.exit_code == 1
    assert isinstance(result.exception, PaperNotFoundError)
    assert darwin_popen.calls == []


def test_open_missing_pdf_exits_1(
    vault: Path, darwin_popen: _PopenRecorder
) -> None:
    _write_paper(vault, "2024_NoPDF_Paper", with_pdf=False)
    runner = CliRunner()
    result = runner.invoke(
        cli, ["open", "2024_NoPDF_Paper", "--library", str(vault)]
    )
    assert result.exit_code == 1
    assert isinstance(result.exception, PaperNotFoundError)
    assert "paper.pdf" in str(result.exception)
    assert darwin_popen.calls == []


def test_open_uses_configured_viewer_from_config(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Setting default_pdf_viewer in lit-config.yaml overrides platform default."""
    config_path = vault / "lit-config.yaml"
    config_path.write_text(
        config_path.read_text().replace(
            "default_pdf_viewer: null",
            "default_pdf_viewer: my-custom-viewer",
        ),
        encoding="utf-8",
    )
    recorder = _PopenRecorder()
    monkeypatch.setattr(viewer_mod.subprocess, "Popen", recorder)
    # platform doesn't matter since configured viewer takes priority.

    runner = CliRunner()
    result = runner.invoke(
        cli, ["open", "2024_Smith_Foo", "--library", str(vault)]
    )
    assert result.exit_code == 0, result.output
    assert recorder.calls[0][0] == "my-custom-viewer"
    assert "configured" in result.output


def test_open_no_viewer_exits_2(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Linux platform with neither xdg-open nor wslview → exit 2 + path."""
    monkeypatch.setattr(viewer_mod.sys, "platform", "linux")
    monkeypatch.setattr(viewer_mod.shutil, "which", lambda cmd: None)

    runner = CliRunner()
    result = runner.invoke(
        cli, ["open", "2024_Smith_Foo", "--library", str(vault)]
    )
    assert result.exit_code == 2
    assert "No platform PDF viewer" in result.output
    # Path must be printed so the user can copy / pipe it.
    assert "paper.pdf" in result.output


def test_open_xdg_open_headless_exits_2(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Linux + xdg-open present but no display → exit 2, path, no 'Opened'."""
    recorder = _PopenRecorder()
    monkeypatch.setattr(viewer_mod.subprocess, "Popen", recorder)
    monkeypatch.setattr(viewer_mod.sys, "platform", "linux")
    monkeypatch.setattr(
        viewer_mod.shutil,
        "which",
        lambda cmd: f"/usr/bin/{cmd}" if cmd == "xdg-open" else None,
    )
    monkeypatch.delenv("DISPLAY", raising=False)
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)

    runner = CliRunner()
    result = runner.invoke(
        cli, ["open", "2024_Smith_Foo", "--library", str(vault)]
    )
    assert result.exit_code == 2
    assert "No graphical display" in result.output
    assert "paper.pdf" in result.output
    assert "Opened" not in result.output
    # No process was forked.
    assert recorder.calls == []


def test_open_configured_viewer_missing_exits_2(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Configured viewer that isn't on PATH → exit 2 with install hint."""
    config_path = vault / "lit-config.yaml"
    config_path.write_text(
        config_path.read_text().replace(
            "default_pdf_viewer: null",
            "default_pdf_viewer: nonexistent-viewer-binary",
        ),
        encoding="utf-8",
    )

    def fake_popen(args: list[str], **kwargs: Any) -> None:
        raise FileNotFoundError(args[0])

    monkeypatch.setattr(viewer_mod.subprocess, "Popen", fake_popen)

    runner = CliRunner()
    result = runner.invoke(
        cli, ["open", "2024_Smith_Foo", "--library", str(vault)]
    )
    assert result.exit_code == 2
    assert "not found on PATH" in result.output


def test_open_via_env_var(
    vault: Path, darwin_popen: _PopenRecorder, monkeypatch: pytest.MonkeyPatch
) -> None:
    """LIT_LIBRARY env var works the same as --library."""
    monkeypatch.setenv("LIT_LIBRARY", str(vault))
    runner = CliRunner()
    result = runner.invoke(cli, ["open", "2024_Smith_Foo"])
    assert result.exit_code == 0, result.output
    assert darwin_popen.calls[0][1].endswith(
        "/papers/2024_Smith_Foo/paper.pdf"
    )


def test_open_paper_doi_resolves(
    vault: Path, darwin_popen: _PopenRecorder
) -> None:
    """M11 smoke: --paper-doi reverse-lookup works for `lit open`."""
    paper_dir = vault / "papers" / "2025_Doe_Test"
    paper_dir.mkdir(parents=True)
    yaml = YAML()
    yaml.default_flow_style = False
    with (paper_dir / "metadata.yaml").open("w", encoding="utf-8") as f:
        yaml.dump(
            {
                "id": "2025_Doe_Test",
                "title": "Title for 2025_Doe_Test",
                "year": 2025,
                "doi": "10.7777/test-doe",
            },
            f,
        )
    (paper_dir / "paper.pdf").write_bytes(b"%PDF-1.4 fake\n")
    runner = CliRunner()
    result = runner.invoke(
        cli,
        [
            "open",
            "--paper-doi",
            "10.7777/test-doe",
            "--library",
            str(vault),
        ],
    )
    assert result.exit_code == 0, result.output
    assert darwin_popen.calls[0][1].endswith(
        "/papers/2025_Doe_Test/paper.pdf"
    )


def test_open_id_and_doi_mutually_exclusive(
    vault: Path, darwin_popen: _PopenRecorder
) -> None:
    from litman.exceptions import LitmanError

    runner = CliRunner()
    result = runner.invoke(
        cli,
        [
            "open",
            "2024_Smith_Foo",
            "--paper-doi",
            "10.0/x",
            "--library",
            str(vault),
        ],
    )
    assert result.exit_code != 0
    assert isinstance(result.exception, LitmanError)
