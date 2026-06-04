"""``lit graph`` — read-only knowledge-graph web GUI (M35 Phase 3).

Reconstructs the emergent literature<->project<->code graph from vault metadata
(:func:`core.graph_model.build_graph`), injects it into a vendored single-file
HTML page (built from ``frontend/`` by ``frontend/build.sh``), writes the result
to a temp file OUTSIDE the vault, and opens it in the browser. No server, offline.

Red lines (M35 §4):

* Read-only: never writes the vault (invariant #1). The only writes are the
  rendered HTML, which lands in the system temp dir or an explicit ``--output``
  path, both required to be OUTSIDE ``<vault>/`` (invariant #9).
* ``--check`` runs the full ``health-check`` first and refuses to launch the GUI
  on any error-severity finding (D10), so a broken vault can't be silently
  rendered.
* Corrupt papers and invalid edges are not this command's concern to drop — the
  data layer surfaces them as red nodes/edges (invariant #14); this command just
  ships the JSON through verbatim.
"""

from __future__ import annotations

import json
import tempfile
import webbrowser
from importlib.resources import files
from pathlib import Path
from typing import Any

import click
from rich.console import Console

from litman.core.checks import run_all_checks
from litman.core.document import list_papers
from litman.core.graph_model import build_graph
from litman.core.library import find_vault, resolve_library_or_vault
from litman.exceptions import LitmanError

console = Console()

# The placeholder the build product carries (a unique string in a plain inline
# script the singlefile plugin copies verbatim). We replace the QUOTED token so
# the injected value becomes a raw JS object literal, not a double-escaped
# string. Keep this in lock-step with frontend/index.html.
_INJECT_TOKEN = '"__LIT_GRAPH_DATA__"'


def _load_template_html() -> str:
    """Read the vendored single-file GUI shell from the installed package.

    Resolves via ``importlib.resources`` so it works from a wheel install too
    (the asset is shipped through ``[tool.setuptools.package-data]``). Raises
    ``LitmanError`` if the asset is missing — i.e. the frontend was never built
    or the wheel dropped it.
    """
    asset = files("litman").joinpath("assets/graph/index.html")
    if not asset.is_file():
        raise LitmanError(
            "Knowledge-graph GUI asset is missing "
            "(src/litman/assets/graph/index.html). The frontend has not been "
            "built — run `bash frontend/build.sh` (dev), or reinstall litman."
        )
    return asset.read_text(encoding="utf-8")


def _inject(template: str, data: dict[str, Any]) -> str:
    """Replace the single quoted injection token with the graph JSON literal.

    ``json.dumps`` output is itself valid JavaScript, so substituting it for the
    quoted token turns ``window.__LIT_GRAPH_DATA__ = "__LIT_GRAPH_DATA__";`` into
    ``window.__LIT_GRAPH_DATA__ = {...};``. ``ensure_ascii=False`` keeps non-ASCII
    titles readable.

    The quoted token must occur EXACTLY ONCE in a correctly-built asset. We fail
    loud on both failure modes rather than silently mis-rendering:

    * 0 occurrences — stale / wrong asset (a silently-empty graph would ship).
    * >1 occurrences — a stray quoted occurrence sneaked back into the build
      (e.g. the doc comment or the compiled bundle), which an all-occurrences
      ``str.replace`` would embed the full graph JSON into multiple times
      (bloat that scales with vault size, plus a corrupted constant).
    """
    count = template.count(_INJECT_TOKEN)
    if count == 0:
        raise LitmanError(
            "Knowledge-graph GUI asset does not contain the injection token "
            f"{_INJECT_TOKEN} — it is stale or was built from an incompatible "
            "frontend. Rebuild with `bash frontend/build.sh`."
        )
    if count > 1:
        raise LitmanError(
            f"Knowledge-graph GUI asset contains the injection token "
            f"{_INJECT_TOKEN} {count} times, expected exactly 1. A stray "
            "occurrence sneaked into the build (doc comment or compiled bundle) "
            "— injecting would embed the graph JSON multiple times. Rebuild "
            "with `bash frontend/build.sh` after removing the extra occurrence."
        )
    payload = json.dumps(data, ensure_ascii=False)
    return template.replace(_INJECT_TOKEN, payload)


def _render_and_open(html: str, output: Path | None, vault: Path) -> Path:
    """Write the rendered HTML outside the vault and open it in a browser.

    With ``--output`` the file is written there (parents created); otherwise a
    ``NamedTemporaryFile`` in the system temp dir is used (delete=False so it
    survives for the browser to read). Either way the path must be OUTSIDE the
    vault (invariant #9): the default temp path always is, and an explicit
    ``--output`` inside the vault is rejected with ``LitmanError``. Returns the
    written path.
    """
    if output is not None:
        path = output.resolve()
        if path.is_relative_to(vault.resolve()):
            raise LitmanError(
                f"--output path {path} is inside the vault ({vault}). The "
                "knowledge-graph render is a derived product and must live "
                "OUTSIDE the vault (invariant #9: the vault is not git-tracked "
                "and only holds authored truth). Point --output at a path "
                "outside the vault, or drop it to use a system temp file."
            )
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(html, encoding="utf-8")
    else:
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".html", delete=False, encoding="utf-8"
        ) as fh:
            fh.write(html)
            path = Path(fh.name)

    webbrowser.open(f"file://{path}")
    return path


@click.command("graph")
@click.option(
    "--output",
    "output",
    type=click.Path(file_okay=True, dir_okay=False, path_type=Path),
    default=None,
    help=(
        "Write the rendered HTML to this path (kept, not a throwaway temp "
        "file). Must be outside the vault."
    ),
)
@click.option(
    "--check",
    "check",
    is_flag=True,
    default=False,
    help=(
        "Run health-check first and refuse to open the GUI if any "
        "error-severity issue is found (so a broken vault isn't rendered)."
    ),
)
@click.option(
    "--library",
    type=click.Path(file_okay=False, dir_okay=True, path_type=Path),
    default=None,
    envvar="LIT_LIBRARY",
    help="Override the active vault. Discovery order: this flag / $LIT_LIBRARY, then the active registered vault, then cwd-walk.",
)
@click.option(
    "--vault",
    "vault_name",
    default=None,
    help=(
        "Vault name from ~/.config/litman/vaults.yaml. "
        "Mutually exclusive with --library."
    ),
)
def graph_cmd(
    library: Path | None,
    vault_name: str | None,
    output: Path | None,
    check: bool,
) -> None:
    """Open a read-only knowledge-graph view of the library in a browser.

    Scans all metadata and reconstructs the emergent network of papers, which the
    GUI colours / clusters / focuses by project, topic, method, data, or
    code-clone. Renders into a self-contained HTML page opened via file:// (no
    server, works offline). The GUI never writes the vault. Corrupt papers and
    broken references show up as red nodes/edges rather than disappearing.
    """
    vault = find_vault(resolve_library_or_vault(library, vault_name))
    papers = list_papers(vault)

    if check:
        issues = run_all_checks(vault, papers)
        n_err = sum(1 for i in issues if i.severity == "error")
        if n_err > 0:
            raise LitmanError(
                f"Refusing to open the graph: health-check found {n_err} "
                f"error-severity issue{'s' if n_err != 1 else ''}. "
                "Run `lit health-check` to inspect, fix them, then retry "
                "(or drop --check to render anyway)."
            )

    data = build_graph(vault)
    template = _load_template_html()
    html = _inject(template, data)
    path = _render_and_open(html, output, vault)

    s = data["summary"]
    dims = s["dimensions"]
    console.print(
        f"[green]✓[/] Opened knowledge graph "
        f"[dim]({s['papers']} papers, {dims['projects']} projects, "
        f"{dims['topics']} topics, {dims['codes']} code repos"
        + (f", {s['corrupt']} corrupt" if s["corrupt"] else "")
        + (f", {s['invalid_edges']} invalid edges" if s["invalid_edges"] else "")
        + f")[/] → [bold]{path}[/]"
    )
