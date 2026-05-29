"""``lit pdf-text <pdf>`` — dump a PDF's embedded text layer to stdout.

The weak-LLM-tolerant bottom rung of the skill "read this PDF" ladder
(invariant #5). When a model cannot read a PDF natively (no multimodal
support) and no PDF MCP is available, the agent falls here instead of
reaching for a system tool (poppler / pdftoppm) that may be absent: ``lit``
ships pypdf as a hard dependency, so this command works wherever ``lit``
itself runs — no model, no network, no system binary.

Operates on a raw filesystem path (the PDF the user is about to ``lit add``,
before it is in any vault), so it needs no vault discovery.

Exit codes:
    0 — text extracted and printed.
    1 — file cannot be parsed as a PDF (corrupt / encrypted / not a PDF).
    2 — usage error (missing file / bad --pages), via Click.
    3 — opened, but no extractable text layer (scanned / image-only PDF);
        the caller should route back up the ladder to a multimodal reader
        or OCR. Pages are still printed (possibly empty) for inspection.

Multi-page output separates pages with a form feed (``\\f``), the de-facto
pdftotext convention.
"""

from __future__ import annotations

import sys
from pathlib import Path

import click

from litman.core.pdf_text import PdfTextError, extract_pdf_text


def _parse_pages(spec: str) -> list[int]:
    """Parse a 1-based page spec like ``"1-3,5"`` into ``[1, 2, 3, 5]``."""
    result: list[int] = []
    for tok in spec.split(","):
        tok = tok.strip()
        if not tok:
            continue
        if "-" in tok:
            lo_s, _, hi_s = tok.partition("-")
            try:
                lo, hi = int(lo_s), int(hi_s)
            except ValueError:
                raise click.BadParameter(f"invalid page range {tok!r}")
            if lo < 1 or hi < lo:
                raise click.BadParameter(f"invalid page range {tok!r}")
            result.extend(range(lo, hi + 1))
        else:
            try:
                p = int(tok)
            except ValueError:
                raise click.BadParameter(f"invalid page number {tok!r}")
            if p < 1:
                raise click.BadParameter(f"invalid page number {tok!r}")
            result.append(p)
    if not result:
        raise click.BadParameter("no pages specified")
    return result


@click.command("pdf-text")
@click.argument(
    "pdf_path",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
)
@click.option(
    "--pages",
    "pages_spec",
    default=None,
    help=(
        "1-based pages to extract, e.g. '1-3', '1', '1-3,5'. "
        "Omit to extract the whole document."
    ),
)
def pdf_text_cmd(pdf_path: Path, pages_spec: str | None) -> None:
    """Print a PDF's embedded text layer to stdout (pages joined by \\f).

    Deterministic pypdf extraction — no model, no network, no system tool.
    Reads only the text layer: a scanned / image-only PDF yields empty
    output and exit code 3.
    """
    pages = _parse_pages(pages_spec) if pages_spec else None
    try:
        per_page = extract_pdf_text(pdf_path, pages)
    except PdfTextError as exc:
        click.echo(
            f"Error: cannot read {pdf_path} as a PDF ({exc}).", err=True
        )
        sys.exit(1)

    text = "\f".join(per_page)
    click.echo(text)

    if not text.strip():
        click.echo(
            "Warning: no text extracted (scanned / image-only PDF, or the "
            "requested pages have no text layer). Use a multimodal reader "
            "or OCR, or check the page range.",
            err=True,
        )
        sys.exit(3)
