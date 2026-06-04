"""Integration tests for ``lit graph`` (M35 Phase 3).

Covers the deterministic A-group acceptance criteria that the CliRunner can
reach (the rendering / interaction quality is B-group, manual-only):

* A6 — ``--check`` refuses to open the GUI on an error-severity finding (exits
  non-zero, ``webbrowser.open`` never called).
* A7 — (1) no new runtime Python dependency (graph-lib blocklist disjoint from
  deps, seven core deps still present); (2) the vendored asset ships and is
  readable via ``importlib.resources``; (3) ``_inject`` round-trips the real
  JSON with no leftover token.
* Smoke — default ``lit graph`` writes an HTML file outside the vault that
  contains the injected data (browser stubbed), and ``lit graph --help`` works.
"""

from __future__ import annotations

import tomllib
from importlib.resources import files
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner

import litman.commands.graph as graph_mod
from litman.cli import cli
from litman.commands.graph import _inject, _load_template_html
from litman.core.library import create_vault
from litman.core.taxonomy import update_user_dict_section
from litman.exceptions import LitmanError

# ---------------------------------------------------------------------------
# Vault construction helpers (mirror tests/core/test_graph_model.py style)
# ---------------------------------------------------------------------------


def _write_paper(vault: Path, paper_id: str, *, pdf: bool = True, **fields: Any) -> Path:
    paper_dir = vault / "papers" / paper_id
    paper_dir.mkdir(parents=True)
    base: dict[str, Any] = {
        "id": paper_id,
        "title": fields.pop("title", f"Title of {paper_id}"),
        "created-at": "2024-01-01T00:00:00+00:00",
        "updated-at": "2024-01-01T00:00:00+00:00",
        "status": "inbox",
    }
    base.update(fields)
    lines: list[str] = []
    for key, value in base.items():
        if isinstance(value, list):
            lines.append(f"{key}:")
            for item in value:
                lines.append(f"  - {item}")
        elif value is None:
            lines.append(f"{key}:")
        else:
            lines.append(f"{key}: {value}")
    (paper_dir / "metadata.yaml").write_text("\n".join(lines) + "\n", encoding="utf-8")
    if pdf:
        (paper_dir / "paper.pdf").write_bytes(b"%PDF-1.4\n%%EOF")
    return paper_dir


def _set_config_projects(vault: Path, projects: dict[str, str]) -> None:
    cfg = vault / "lit-config.yaml"
    lines = ["library_name: literature_vault"]
    if projects:
        lines.append("projects:")
        for name, path in projects.items():
            lines.append(f"  {name}: {path}")
    else:
        lines.append("projects: {}")
    cfg.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _register_taxonomy(vault: Path, dict_name: str, values: list[str]) -> None:
    taxonomy_file = vault / "TAXONOMY.md"
    taxonomy_file.chmod(0o644)
    text = taxonomy_file.read_text(encoding="utf-8")
    taxonomy_file.write_text(
        update_user_dict_section(text, dict_name, values), encoding="utf-8"
    )


@pytest.fixture
def vault(tmp_path: Path) -> Path:
    return create_vault(tmp_path)


@pytest.fixture
def healthy_vault(tmp_path: Path) -> Path:
    """A small clean vault: two projects, three papers, no findings."""
    v = create_vault(tmp_path)
    _set_config_projects(v, {"pepforge": "/tmp/pepforge", "pepcodec": "/tmp/pepcodec"})
    _register_taxonomy(v, "topics", ["AMP"])
    _write_paper(v, "2023_smith_amp", projects=["pepforge"], topics=["AMP"])
    _write_paper(v, "2022_lee_helm", projects=["pepforge"])
    _write_paper(v, "2021_kim_encoder", projects=["pepcodec"])
    return v


# ---------------------------------------------------------------------------
# A6 — --check refuses to open on an error-severity finding
# ---------------------------------------------------------------------------


def test_a6_check_refuses_and_does_not_open(vault: Path, monkeypatch: Any) -> None:
    # A broken relation pairing (extends -> nonexistent id) yields an
    # error-severity finding (see test_graph_model A6). The LitmanError raised
    # before the browser opens is captured by CliRunner as result.exception
    # (the top-level main() friendly-print wrapper is outside runner.invoke).
    _write_paper(vault, "a", extends=["nonexistent-paper"])

    opened: list[str] = []
    monkeypatch.setattr(
        "litman.commands.graph.webbrowser.open", lambda url: opened.append(url)
    )

    runner = CliRunner()
    result = runner.invoke(cli, ["graph", "--check", "--library", str(vault)])

    assert result.exit_code != 0
    assert opened == []  # browser never launched — gate fired before render
    assert isinstance(result.exception, LitmanError)
    assert "error-severity" in str(result.exception)


def test_check_clean_vault_opens(vault: Path, monkeypatch: Any) -> None:
    # The complement of A6: a vault with zero error-severity findings passes
    # --check and opens. A freshly created (empty) vault is health-clean.
    opened: list[str] = []
    monkeypatch.setattr(
        "litman.commands.graph.webbrowser.open", lambda url: opened.append(url)
    )

    runner = CliRunner()
    result = runner.invoke(cli, ["graph", "--check", "--library", str(vault)])

    assert result.exit_code == 0, result.output
    assert len(opened) == 1


# ---------------------------------------------------------------------------
# A7 — no new runtime dep / asset ships / injection round-trip
# ---------------------------------------------------------------------------


def _pyproject_path() -> Path:
    return Path(graph_mod.__file__).resolve().parents[3] / "pyproject.toml"


def test_a7_no_new_runtime_dependency() -> None:
    data = tomllib.loads(_pyproject_path().read_text(encoding="utf-8"))
    deps = data["project"]["dependencies"]
    dep_names = {
        dep.split(">")[0].split("<")[0].split("=")[0].split("[")[0].strip().lower()
        for dep in deps
    }
    graph_libs = {
        "networkx",
        "igraph",
        "python-igraph",
        "graph-tool",
        "pygraphviz",
        "pyvis",
    }
    assert graph_libs.isdisjoint(dep_names)
    assert dep_names >= {
        "click",
        "ruamel.yaml",
        "httpx",
        "pypdf",
        "pydantic",
        "rich",
        "platformdirs",
    }


def test_a7_vendored_asset_ships_and_is_readable() -> None:
    asset = files("litman").joinpath("assets/graph/index.html")
    assert asset.is_file()
    text = asset.read_text(encoding="utf-8")
    assert text  # non-empty
    # The injection token must survive the build verbatim.
    assert '"__LIT_GRAPH_DATA__"' in text


def test_a7_inject_round_trip() -> None:
    template = _load_template_html()
    # Precondition: the built asset carries the quoted token EXACTLY ONCE, so a
    # plain str.replace injects the JSON exactly once. A stray occurrence (doc
    # comment / compiled bundle) would multi-inject and is what fix-1 guards.
    assert template.count('"__LIT_GRAPH_DATA__"') == 1

    # Sentinel placed in a SINGLE field so it occurs once in the JSON payload;
    # any extra occurrences in the output mean the payload was injected >1 time.
    sentinel = "ZZ_SENTINEL_UNIQUE_TOKEN_ZZ"
    data = {
        "summary": {
            "papers": 1,
            "corrupt": 0,
            "invalid_edges": 0,
            "dimensions": {"projects": 1, "topics": 0, "methods": 0, "data": 0, "codes": 0},
        },
        "nodes": [
            {
                "id": "p",
                "label": sentinel,
                "type": "paper",
                "status": "ok",
                "degree": 0,
                "dims": {"projects": ["pf"], "topics": [], "methods": [], "data": [], "codes": []},
                "meta": {
                    "year": 2021,
                    "authors": ["X"],
                    "n_authors": 1,
                    "journal": "",
                    "doi": "",
                    "type": "",
                    "priority": "",
                    "read_status": "inbox",
                },
            }
        ],
        "edges": [],
        "dimensions": {
            "projects": {"values": ["pf"], "invalid": []},
            "topics": {"values": [], "invalid": []},
            "methods": {"values": [], "invalid": []},
            "data": {"values": [], "invalid": []},
            "codes": {"values": [], "invalid": []},
        },
    }
    html = _inject(template, data)
    # The real JSON is present...
    assert '"papers": 1' in html or '"papers":1' in html
    assert '"dimensions"' in html
    # ...and the quoted placeholder is gone (no double-injection / stale token).
    assert '"__LIT_GRAPH_DATA__"' not in html
    # The sentinel from the injected data appears EXACTLY ONCE: triple-injection
    # (the fix-1 defect) would embed it three times and fail here.
    assert html.count(sentinel) == 1


def test_inject_raises_when_token_absent() -> None:
    with pytest.raises(LitmanError):
        _inject("<html>no token here</html>", {"summary": {}})


def test_inject_raises_when_token_appears_more_than_once() -> None:
    # Two quoted occurrences (the fix-1 defect: stray token in comment/bundle).
    # _inject must fail loud rather than embed the JSON twice.
    template = (
        '<script>window.__LIT_GRAPH_DATA__ = "__LIT_GRAPH_DATA__";</script>'
        '<script>const x = "__LIT_GRAPH_DATA__";</script>'
    )
    assert template.count('"__LIT_GRAPH_DATA__"') == 2
    with pytest.raises(LitmanError):
        _inject(template, {"summary": {}})


# ---------------------------------------------------------------------------
# Smoke — default `lit graph` writes HTML outside the vault with injected data
# ---------------------------------------------------------------------------


def test_smoke_default_writes_outside_vault(
    healthy_vault: Path, monkeypatch: Any, tmp_path: Path
) -> None:
    opened: list[str] = []
    monkeypatch.setattr(
        "litman.commands.graph.webbrowser.open", lambda url: opened.append(url)
    )

    runner = CliRunner()
    result = runner.invoke(cli, ["graph", "--library", str(healthy_vault)])

    assert result.exit_code == 0, result.output
    assert len(opened) == 1
    file_url = opened[0]
    assert file_url.startswith("file://")
    out_path = Path(file_url[len("file://") :])
    assert out_path.is_file()
    # Written OUTSIDE the vault (invariant #9).
    assert healthy_vault not in out_path.parents
    # Contains the injected data (the project names landed in the JSON literal).
    html = out_path.read_text(encoding="utf-8")
    assert "pepforge" in html
    assert "pepcodec" in html
    assert '"__LIT_GRAPH_DATA__"' not in html


def test_smoke_output_flag_keeps_file(
    healthy_vault: Path, monkeypatch: Any, tmp_path: Path
) -> None:
    monkeypatch.setattr("litman.commands.graph.webbrowser.open", lambda url: None)
    target = tmp_path / "kept" / "graph.html"

    runner = CliRunner()
    result = runner.invoke(
        cli,
        ["graph", "--output", str(target), "--library", str(healthy_vault)],
    )

    assert result.exit_code == 0, result.output
    assert target.is_file()
    html = target.read_text(encoding="utf-8")
    assert "pepforge" in html


def test_output_inside_vault_is_rejected(
    healthy_vault: Path, monkeypatch: Any
) -> None:
    # Invariant #9: rendered products must live OUTSIDE the vault. An explicit
    # --output pointing inside the vault is refused before anything is written.
    opened: list[str] = []
    monkeypatch.setattr(
        "litman.commands.graph.webbrowser.open", lambda url: opened.append(url)
    )
    target = healthy_vault / "graph.html"

    runner = CliRunner()
    result = runner.invoke(
        cli, ["graph", "--output", str(target), "--library", str(healthy_vault)]
    )

    assert result.exit_code != 0
    assert isinstance(result.exception, LitmanError)
    assert "inside the vault" in str(result.exception)
    assert not target.exists()
    assert opened == []  # browser never launched


def test_help_lists_graph_command() -> None:
    runner = CliRunner()
    result = runner.invoke(cli, ["graph", "--help"])
    assert result.exit_code == 0
    assert "--check" in result.output
    assert "--output" in result.output
