"""``lit add --si PAPER`` without files: the open GUI page takes the SI.

The CLI's side of the SI window (task-supplementary-info, ADR-027). The
command first settles what it can on its own — the paper, a broken
``supplementary`` field or folder, SI already in the library — then finds the
``lit gui`` serving this library on this machine
(:func:`~litman.core.gui_registry.find_gui`), opens a request there
and asks for its state once a second. The page shows the request as the SI
window; the user downloads the files from the publisher, drops them in, and
presses Done or Cancel.

One run waits at most :data:`WAIT_BUDGET_S`, counted from the start of the
command, below the shortest time an agent lets a command run. Without an
answer by then it exits 11 and the agent runs the same command again: the GUI
hands the rerun the same open request (no second window), or the result the
timed-out run never saw (deliver-once, ``server/si_requests.py``).

stdout carries the agent contract through ``click.echo`` — rich would wrap a
long path at 80 columns when stdout is not a terminal — and everything else
goes to stderr. Exit codes:

    0   SI ready: N file(s), then one line per file: its absolute path, a
        tab and its original name (the path alone when none is recorded)
    10  SI request cancelled in the GUI
    11  still waiting in the GUI: run the same command again
    12  no GUI page open for this library: where to download, and the FILE form
    1   anything else (raised as a LitmanError)

The only network use is the GUI on this machine's 127.0.0.1 (red line 1).
httpx is imported inside the functions that use it and nothing from
``litman.server`` is imported at all, so the CLI's import path stays light
(invariant #5).
"""

from __future__ import annotations

import re
import time
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

import click
from rich.console import Console
from rich.markup import escape

from litman.core import supplementary
from litman.core.document import find_paper
from litman.core.gui_registry import GuiServer, find_gui, other_host_guis
from litman.core.library import find_vault, resolve_library_or_vault
from litman.core.paper_lookup import resolve_paper_id
from litman.exceptions import SupplementaryError

if TYPE_CHECKING:
    import httpx

WAIT_BUDGET_S = 90.0
POLL_S = 1.0
# Failed polls in a row that mean the GUI is gone rather than busy.
MAX_MISSES = 3

EXIT_CANCELLED = 10
EXIT_WAITING = 11
EXIT_NO_GUI = 12

# Read at call time, so tests replace them on this module.
_clock: Callable[[], float] = time.monotonic
_sleep: Callable[[float], None] = time.sleep

# Windows takes 1-2 s to refuse a closed local port.
_CONNECT_TIMEOUT_S = 3.0
_READ_TIMEOUT_S = 2.0
# Opening a request reads the paper's metadata, perhaps on a network mount.
_POST_TIMEOUT_S = 10.0

_REQUESTS_PATH = "/api/si/requests"
# The server's request ids (uuid4 hex); the id goes into the poll's URL path.
_RID_RE = re.compile(r"^[0-9a-f]{32}$")
# Copied from litman.server.si_requests, which this module must not import.
_ADDED = "added"
_CANCELLED = "cancelled"

NO_PAGE = "No litman GUI page is open for this library"
CANCELLED_LINE = "SI request cancelled in the GUI"
STILL_WAITING = "Still waiting in the GUI — run the same command again to keep waiting"
WAITING = "Waiting for the SI to be added in the litman GUI…"

Kind = Literal[
    "added",
    "cancelled",
    "timeout",
    "unanswered",
    "lost",
    "interrupted",
    "gone",
    "other_library",
]

# What stderr adds to an exit 11, by how the wait ended.
_WAIT_NOTES: dict[str, str] = {
    "unanswered": "The GUI did not answer in time; the rerun picks the request up.",
    "lost": "The GUI no longer knows this request (restarted?); the rerun asks again.",
    "interrupted": "Stopped waiting; the SI window stays open in the GUI.",
}


@dataclass(frozen=True)
class GuiOutcome:
    """How the GUI's request ended, as far as this run saw it.

    ``timeout``, ``unanswered`` (opening the request got no answer in time),
    ``lost`` (the GUI forgot it) and ``interrupted`` (Ctrl-C) all mean "run
    the same command again"; ``gone`` and ``other_library`` mean no GUI page
    serves this library now.
    """

    kind: Kind
    request_id: str | None = None
    files: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()


def _new_client() -> httpx.Client:
    """The client every call to the GUI goes through.

    ``trust_env=False``: a proxy from the environment would carry 127.0.0.1
    to the proxy host.
    """
    import httpx

    return httpx.Client(
        trust_env=False,
        timeout=httpx.Timeout(_READ_TIMEOUT_S, connect=_CONNECT_TIMEOUT_S),
    )


def _strings(value: Any) -> tuple[str, ...]:
    return tuple(v for v in value if isinstance(v, str)) if isinstance(value, list) else ()


def _request_of(response: httpx.Response | None) -> dict[str, Any] | None:
    """The request snapshot of a 200 answer; ``None`` for anything else."""
    if response is None or response.status_code != 200:
        return None
    try:
        body = response.json()
    except ValueError:
        return None
    request = body.get("request") if isinstance(body, dict) else None
    if (
        not isinstance(request, dict)
        or not isinstance(request.get("id"), str)
        or not _RID_RE.match(request["id"])
        or not isinstance(request.get("status"), str)
    ):
        return None
    return request


def _ended(request: dict[str, Any], paper_id: str) -> GuiOutcome | None:
    """The outcome of a finished request; ``None`` while it is open.

    Raises:
        SupplementaryError: ``added`` without one file named.
    """
    if request["status"] == _ADDED:
        added = request.get("added")
        files = tuple(
            entry["file"]
            for entry in (added if isinstance(added, list) else [])
            if isinstance(entry, dict) and isinstance(entry.get("file"), str)
        )
        if not files:
            raise SupplementaryError(
                "The litman GUI answered that SI was added but named no file. "
                f"`lit show {paper_id}` lists the paper's SI."
            )
        return GuiOutcome("added", request["id"], files, _strings(request.get("warnings")))
    if request["status"] == _CANCELLED:
        return GuiOutcome("cancelled", request["id"])
    return None


def _detail(response: httpx.Response) -> str:
    try:
        body = response.json()
    except ValueError:
        body = None
    detail = body.get("detail") if isinstance(body, dict) else None
    if isinstance(detail, str) and detail.strip():
        return detail.strip()
    return f"unexpected answer (HTTP {response.status_code})"


def ask_gui(
    client: httpx.Client,
    gui: GuiServer,
    vault: Path,
    paper_id: str,
    *,
    force: bool,
    deadline: float,
    clock: Callable[[], float],
    sleep: Callable[[float], None],
    on_wait: Callable[[], None],
) -> GuiOutcome:
    """Open (or rejoin) the paper's SI request in ``gui`` and wait for its end.

    Posts even past ``deadline``, so the window opens and a rerun finds it,
    then polls once a second until the request ends, the GUI fails to answer
    :data:`MAX_MISSES` polls in a row, or ``deadline`` passes. Ctrl-C, from
    the post on, stops the wait and leaves the request open. ``on_wait`` runs
    once, when there is something to wait for. ``force`` goes with the post:
    the GUI hands a result the agent missed only to a run asking the same way.

    Raises:
        SupplementaryError: the GUI refused the request for a reason a rerun
            does not fix (the paper's SI field or folder is broken, the paper
            is not there, an answer that makes no sense).
    """
    import httpx

    body = {"paperId": paper_id, "source": "cli", "vault": str(vault), "force": force}
    try:
        response = client.post(
            gui.url + _REQUESTS_PATH,
            json=body,
            timeout=httpx.Timeout(_POST_TIMEOUT_S, connect=_CONNECT_TIMEOUT_S),
        )
    except KeyboardInterrupt:
        # The GUI may have opened the request already: the rerun picks it up.
        return GuiOutcome("interrupted")
    except httpx.TimeoutException:
        return GuiOutcome("unanswered")
    except httpx.HTTPError:
        return GuiOutcome("gone")
    if response.status_code in (409, 410):
        # The GUI also refuses with 409 when the paper's SI cannot be written:
        # that is the user's to fix by hand, not a GUI to switch.
        if response.status_code == 409:
            problem = supplementary.si_write_problem(vault, paper_id)
            if problem:
                raise SupplementaryError(problem)
        return GuiOutcome("other_library")
    request = _request_of(response)
    if request is None:
        raise SupplementaryError(
            f"The litman GUI could not open the SI window: {_detail(response)}"
        )
    rid: str = request["id"]
    ended = _ended(request, paper_id)
    if ended is not None:
        return ended
    if clock() >= deadline:
        return GuiOutcome("timeout", rid)

    url = f"{gui.url}{_REQUESTS_PATH}/{rid}"
    misses = 0
    try:
        on_wait()
        while (remaining := deadline - clock()) > 0:
            sleep(min(POLL_S, remaining))
            polled: httpx.Response | None
            try:
                polled = client.get(url, params={"collect": "1"})
            except httpx.HTTPError:
                polled = None
            else:
                if polled.status_code == 404:
                    return GuiOutcome("lost", rid)
                if polled.status_code in (409, 410):
                    return GuiOutcome("other_library", rid)
            request = _request_of(polled)
            if request is None:
                misses += 1
                if misses >= MAX_MISSES:
                    return GuiOutcome("gone", rid)
                continue
            misses = 0
            ended = _ended(request, paper_id)
            if ended is not None:
                return ended
            if request.get("vaultServed") is False:
                return GuiOutcome("other_library", rid)
    except KeyboardInterrupt:
        return GuiOutcome("interrupted", rid)
    return GuiOutcome("timeout", rid)


def _warn(err: Console, message: str) -> None:
    err.print(f"[yellow]Warning:[/] {escape(message)}", soft_wrap=True)


def print_si_ready(
    rows: Iterable[tuple[Path, Mapping[str, Any]]], *, already: bool = False
) -> None:
    """The exit-0 lines both ``lit add --si`` forms print: ``SI ready: N
    file(s)``, then per file its absolute path, a tab and the name it was
    added under — the path alone when the record names none."""
    rows = list(rows)
    suffix = " (already in the library)" if already else ""
    click.echo(f"SI ready: {len(rows)} file(s){suffix}")
    for path, record in rows:
        label = supplementary.original_label(record)
        click.echo(f"{path}\t{label}" if label else str(path))


def _print_ready(
    err: Console, vault: Path, paper_id: str, names: list[str], *, already: bool
) -> None:
    """:func:`print_si_ready` for the recorded ``names``, the paths built
    from this library's own path (the GUI may spell it differently)."""
    rows: list[tuple[Path, dict[str, Any]]] = []
    for name in names:
        try:
            rows.append(supplementary.recorded_si_file(vault, paper_id, name))
        except SupplementaryError as exc:
            _warn(err, str(exc))
    if names and not rows:
        raise SupplementaryError(
            f"None of the SI files recorded for {paper_id} can be read. "
            "`lit health-check` says how to fix it."
        )
    print_si_ready(rows, already=already)


def _fallback(paper_id: str, link: str | None) -> list[str]:
    """How to get the SI in without the GUI: every exit 12 ends with these."""
    where = (
        f"Download the SI from the article page: {link}"
        if link
        else "No link on record — download the SI from the article's page yourself"
    )
    return [where, f"Then add the files: lit add --si {paper_id} <file>..."]


def _when(started: float) -> str:
    """``YYYY-MM-DD HH:MM`` local time; empty when the record's time is junk."""
    try:
        return datetime.fromtimestamp(started).strftime("%Y-%m-%d %H:%M")
    except (OverflowError, OSError, ValueError):
        return ""


def _no_gui(vault: Path) -> list[str]:
    lines = [NO_PAGE]
    others = other_host_guis(vault)
    if others:
        host, started = others[0]
        when = _when(started)
        # Never pinged, so maybe long dead: a crashed node leaves its record.
        lines.append(
            f"A litman GUI for this library may be running on host {host}"
            f"{f' (started {when})' if when else ''} — run the command there"
        )
    return lines


def _echo(lines: list[str]) -> None:
    for line in lines:
        click.echo(line)


def request_si(si_paper: str, *, force: bool, library: Path | None, vault_name: str | None) -> int:
    """``lit add --si PAPER``: ask the open GUI page for the paper's SI.

    Prints the contract lines (module docstring) and returns the exit code;
    exit 1 is raised as a LitmanError. ``force`` asks the GUI even when the
    paper has SI already. Whether the article's text mentions SI is never
    checked: no word list catches every way a paper points to it ("see
    Appendix D"), and a miss turned the user's own request into a refusal.
    """
    deadline = _clock() + WAIT_BUDGET_S
    err = Console(stderr=True)
    vault = find_vault(resolve_library_or_vault(library, vault_name))
    paper_id = resolve_paper_id(vault, si_paper)
    # Before any GUI is asked: what only a hand edit fixes is an error (1),
    # never "no GUI" (12) — the GUI would refuse it with a 409 too.
    problem = supplementary.si_write_problem(vault, paper_id)
    if problem:
        raise SupplementaryError(problem)
    meta = find_paper(vault, paper_id)
    if not force:
        recorded = [rec["file"] for rec in supplementary.list_supplementary(meta)]
        if recorded:
            _print_ready(err, vault, paper_id, recorded, already=True)
            return 0

    fallback = _fallback(paper_id, supplementary.official_link(meta))
    client = _new_client()
    try:
        gui = find_gui(vault, client=client)
        if gui is None:
            _echo([*_no_gui(vault), *fallback])
            return EXIT_NO_GUI
        if gui.pages == 0:
            _echo(
                [
                    NO_PAGE,
                    f"The litman GUI runs at {gui.url} but has no page open — "
                    "open it, then run the same command again",
                    *fallback,
                ]
            )
            return EXIT_NO_GUI
        outcome = ask_gui(
            client,
            gui,
            vault,
            paper_id,
            force=force,
            deadline=deadline,
            clock=_clock,
            sleep=_sleep,
            on_wait=lambda: err.print(WAITING, soft_wrap=True),
        )
    finally:
        client.close()

    if outcome.kind == "added":
        for message in outcome.warnings:
            _warn(err, message)
        _print_ready(err, vault, paper_id, list(outcome.files), already=False)
        return 0
    if outcome.kind == "cancelled":
        click.echo(CANCELLED_LINE)
        return EXIT_CANCELLED
    if outcome.kind == "gone":
        reason = (
            f"The litman GUI at {gui.url} stopped answering — start it again "
            "(lit gui), then run the same command again"
        )
        _echo([NO_PAGE, reason, *fallback])
        return EXIT_NO_GUI
    if outcome.kind == "other_library":
        if outcome.request_id is not None:
            err.print(
                "The request stays open in the GUI; once it shows this library "
                "again, the rerun picks it up.",
                soft_wrap=True,
            )
        reason = (
            f"The litman GUI at {gui.url} shows another library — switch it to "
            f"{vault}, then run the same command again"
        )
        _echo([NO_PAGE, reason, *fallback])
        return EXIT_NO_GUI
    note = _WAIT_NOTES.get(outcome.kind)
    if outcome.kind == "interrupted" and outcome.request_id is None:
        # Stopped during the post: the window may or may not have opened.
        note = "Stopped waiting; the SI window may be open in the GUI."
    if note:
        err.print(note, soft_wrap=True)
    click.echo(STILL_WAITING)
    return EXIT_WAITING


__all__ = [
    "EXIT_CANCELLED",
    "EXIT_NO_GUI",
    "EXIT_WAITING",
    "MAX_MISSES",
    "POLL_S",
    "WAIT_BUDGET_S",
    "GuiOutcome",
    "ask_gui",
    "print_si_ready",
    "request_si",
]
