"""CrossRef DOI metadata importer.

Hits ``https://api.crossref.org/works/<doi>`` and converts the response into
the standard litman metadata dict. CrossRef's public REST API is keyless;
including a ``mailto:`` in the User-Agent grants polite-pool priority.

Split into two functions so unit tests can verify parsing without HTTP:

- ``fetch_crossref(doi, client=None)``: makes the request, returns the
  ``message`` subobject (raises ``ImporterError`` on any failure).
- ``parse_crossref(message)``: pure, no network.
"""

from __future__ import annotations

from typing import Any

import httpx

from litman import __version__
from litman.exceptions import ImporterError

_CROSSREF_URL = "https://api.crossref.org/works/{doi}"
_USER_AGENT = f"litman/{__version__} (mailto:qingxinwong2@gmail.com)"
_TIMEOUT_SECONDS = 10.0


def fetch_crossref(doi: str, client: httpx.Client | None = None) -> dict[str, Any]:
    """Fetch CrossRef metadata for a DOI; return the ``message`` subobject.

    Raises:
        ImporterError: network failure, non-200 status, malformed JSON, or
            a response missing the expected ``message`` key.
    """
    own_client = client is None
    if own_client:
        client = httpx.Client(timeout=_TIMEOUT_SECONDS)

    try:
        url = _CROSSREF_URL.format(doi=doi)
        try:
            resp = client.get(
                url,
                headers={"User-Agent": _USER_AGENT},
                follow_redirects=True,
            )
        except httpx.HTTPError as e:
            raise ImporterError(
                f"CrossRef request failed for DOI {doi!r}: {e}"
            ) from e

        if resp.status_code == 404:
            raise ImporterError(f"DOI not found in CrossRef: {doi!r}")
        if resp.status_code != 200:
            raise ImporterError(
                f"CrossRef returned HTTP {resp.status_code} for DOI {doi!r}"
            )

        try:
            data = resp.json()
        except ValueError as e:
            raise ImporterError(
                f"CrossRef response was not valid JSON for DOI {doi!r}: {e}"
            ) from e

        if "message" not in data:
            raise ImporterError(
                f"CrossRef response missing 'message' key for DOI {doi!r}"
            )

        return data["message"]
    finally:
        if own_client:
            client.close()


def _extract_year(message: dict[str, Any]) -> int | None:
    """Pull the year from whichever date field CrossRef populates first."""
    for key in ("published-print", "published-online", "issued", "created"):
        date_obj = message.get(key)
        if not date_obj:
            continue
        parts = date_obj.get("date-parts")
        if parts and parts[0] and parts[0][0]:
            try:
                return int(parts[0][0])
            except (TypeError, ValueError):
                continue
    return None


def parse_crossref(message: dict[str, Any]) -> dict[str, Any]:
    """Convert a CrossRef ``message`` object into the standard metadata dict."""
    title_list = message.get("title") or []
    title = title_list[0] if title_list else ""

    authors: list[str] = []
    for a in message.get("author") or []:
        family = (a.get("family") or "").strip()
        given = (a.get("given") or "").strip()
        if family and given:
            authors.append(f"{family}, {given}")
        elif family:
            authors.append(family)
        elif given:
            authors.append(given)

    container_list = message.get("container-title") or []
    journal = container_list[0] if container_list else ""

    return {
        "title": title,
        "authors": authors,
        "year": _extract_year(message),
        "journal": journal,
        "doi": message.get("DOI", ""),
    }
