"""Tests for ``litman.server.si_requests`` — the SI window's request table.

Pure state machine: no app, no HTTP, no files written. Two folders stand in
for two libraries (only their identity matters), and the module's ``_clock``
seam is stepped past the two lifetimes.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from litman.core.fs_identity import Anchor
from litman.server import si_requests as sr
from litman.server.si_requests import (
    ADDED,
    CANCELLED,
    CONFIRMING,
    PENDING,
    BadSelection,
    ConfirmPlan,
    RequestConflict,
    SiRequestTable,
    SiRow,
    UnknownRequest,
)

PAPER = "2019_Yang_Concepts"
OTHER_PAPER = "2023_Zhong_Peptides"


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> FakeClock:
    fake = FakeClock()
    monkeypatch.setattr(sr, "_clock", fake)
    return fake


@pytest.fixture
def libs(tmp_path: Path) -> tuple[Path, Path]:
    one = tmp_path / "lib-one"
    two = tmp_path / "lib-two"
    one.mkdir()
    two.mkdir()
    return one, two


@pytest.fixture
def table() -> SiRequestTable:
    return SiRequestTable()


def _open(
    table: SiRequestTable,
    vault: Path,
    source: str = "cli",
    paper: str = PAPER,
    *,
    force: bool = False,
    recorded: tuple[str, ...] = ("si-01.pdf",),
    read_mark: int | None = None,
) -> dict[str, Any]:
    """``recorded``: the SI files the paper records, by default the one
    :func:`_finish` adds, read from ``read_mark`` (by default: now) on."""
    return table.open(
        vault=vault,
        anchor=Anchor.of(vault),
        paper_id=paper,
        title="A title",
        link="https://doi.org/10.1/x",
        source=source,
        recorded=recorded,
        read_mark=table.mark() if read_mark is None else read_mark,
        force=force,
    )


def _row(fid: str, verdict: str = "ok", sha: str | None = None) -> SiRow:
    return SiRow(
        id=fid,
        name=f"{fid}.pdf",
        stash=Path(f"/stash/si-{fid}.pdf"),
        size=3,
        verdict=verdict,
        check="filename" if verdict == "ok" else None,
        reason="why",
        sha256=sha or fid * 4,
    )


def _finish(
    table: SiRequestTable, rid: str, vault: Path, files: tuple[str, ...] = ("si-01.pdf",)
) -> dict[str, Any]:
    served = Anchor.of(vault)
    table.add_row(rid, served, _row("a" * 32))
    plan = table.begin_confirm(rid, served, [])
    assert isinstance(plan, ConfirmPlan)
    return table.finish_confirm(rid, served, [{"file": name} for name in files], [])


# ---------------------------------------------------------------------------
# Snapshot
# ---------------------------------------------------------------------------


def test_snapshot_shape(table: SiRequestTable, libs: tuple[Path, Path], clock: FakeClock) -> None:
    one, two = libs
    snap = _open(table, one)
    assert set(snap) == {
        "id", "paperId", "title", "link", "status", "vaultServed",
        "rows", "added", "warnings",
    }
    assert len(snap["id"]) == 32
    assert (snap["paperId"], snap["status"], snap["vaultServed"]) == (PAPER, PENDING, True)
    row, snap = table.add_row(snap["id"], Anchor.of(one), _row("b" * 32, "unverified"))
    assert row == {
        "id": "b" * 32, "name": f"{'b' * 32}.pdf", "bytes": 3,
        "verdict": "unverified", "check": None, "reason": "why", "otherId": None,
    }
    assert snap["rows"] == [row]
    assert table.snapshot(snap["id"], Anchor.of(two))["vaultServed"] is False
    assert table.snapshot(snap["id"], None)["vaultServed"] is False


# ---------------------------------------------------------------------------
# Reuse rules ① ② ③
# ---------------------------------------------------------------------------


def test_rule_1_the_open_request_of_the_paper_is_reused(
    table: SiRequestTable, libs: tuple[Path, Path], clock: FakeClock
) -> None:
    one, two = libs
    first = _open(table, one)
    assert _open(table, one)["id"] == first["id"]
    assert _open(table, one, "page")["id"] == first["id"]
    assert _open(table, one, paper=OTHER_PAPER)["id"] != first["id"]
    assert _open(table, two)["id"] != first["id"]


def test_rule_1_a_cli_joining_a_page_request_awaits_it(
    table: SiRequestTable, libs: tuple[Path, Path], clock: FakeClock
) -> None:
    one, _two = libs
    page = _open(table, one, "page")
    assert table._requests[page["id"]].awaited is False
    assert _open(table, one, "cli")["id"] == page["id"]
    assert table._requests[page["id"]].awaited is True


def test_rule_2_a_result_the_cli_never_saw_is_handed_over_once(
    table: SiRequestTable, libs: tuple[Path, Path], clock: FakeClock
) -> None:
    """The CLI timed out (exit 11) before Done; its rerun collects the result,
    and the rerun after that opens a new window."""
    one, _two = libs
    rid = _open(table, one)["id"]
    _finish(table, rid, one)

    again = _open(table, one)
    assert again["id"] == rid and again["status"] == ADDED
    assert table._requests[rid].delivered is True

    fresh = _open(table, one)
    assert fresh["id"] != rid and fresh["status"] == PENDING


def test_rule_2_a_collected_result_is_not_handed_over_again(
    table: SiRequestTable, libs: tuple[Path, Path], clock: FakeClock
) -> None:
    """Cancel reached the waiting CLI (exit 10); when the user asks for the
    window again, it opens — not the old cancel for 30 minutes."""
    one, _two = libs
    rid = _open(table, one)["id"]
    table.cancel(rid, Anchor.of(one))
    assert table.snapshot(rid, Anchor.of(one), collect=True)["status"] == CANCELLED

    assert _open(table, one)["id"] != rid


def test_collect_on_an_open_request_delivers_nothing(
    table: SiRequestTable, libs: tuple[Path, Path], clock: FakeClock
) -> None:
    one, _two = libs
    rid = _open(table, one)["id"]
    table.snapshot(rid, Anchor.of(one), collect=True)
    assert table._requests[rid].delivered is False
    table.snapshot(rid, Anchor.of(one))  # without collect, never
    _finish(table, rid, one)
    table.snapshot(rid, Anchor.of(one))
    assert table._requests[rid].delivered is False
    assert _open(table, one)["id"] == rid  # still handed over by rule ②


def test_rule_2_never_hands_over_a_page_request_no_cli_waited_on(
    table: SiRequestTable, libs: tuple[Path, Path], clock: FakeClock
) -> None:
    """The user's own Add SI, ended before any agent asked: a later
    `lit add --si` opens a fresh window, not the page's old result."""
    one, _two = libs
    rid = _open(table, one, "page")["id"]
    table.cancel(rid, Anchor.of(one))
    fresh = _open(table, one, "cli")
    assert fresh["id"] != rid and fresh["status"] == PENDING
    assert table._requests[rid].delivered is False


def test_rule_2_hands_over_a_page_request_a_cli_joined_once(
    table: SiRequestTable, libs: tuple[Path, Path], clock: FakeClock
) -> None:
    """The page opened the window, `lit add --si` joined it (rule ①) and
    timed out before Done: its rerun gets that result once, the next rerun a
    fresh window."""
    one, _two = libs
    rid = _open(table, one, "page")["id"]
    assert _open(table, one, "cli")["id"] == rid  # joined while open
    _finish(table, rid, one)

    handed = _open(table, one, "cli")
    assert handed["id"] == rid and handed["status"] == ADDED
    assert table._requests[rid].delivered is True
    assert _open(table, one, "cli")["id"] != rid


def test_rule_2_is_for_the_cli_only(
    table: SiRequestTable, libs: tuple[Path, Path], clock: FakeClock
) -> None:
    one, _two = libs
    rid = _open(table, one, "cli")["id"]
    table.cancel(rid, Anchor.of(one))
    assert _open(table, one, "page")["id"] != rid
    assert table._requests[rid].delivered is False


def test_rule_2_ends_with_the_30_minutes(
    table: SiRequestTable, libs: tuple[Path, Path], clock: FakeClock
) -> None:
    one, _two = libs
    rid = _open(table, one)["id"]
    table.cancel(rid, Anchor.of(one))
    clock.now += sr.FINISHED_TTL_S + 1
    with pytest.raises(UnknownRequest):
        table.snapshot(rid, Anchor.of(one))
    assert _open(table, one)["id"] != rid


def test_rule_2_hands_a_result_only_to_a_run_asking_the_same_way(
    table: SiRequestTable, libs: tuple[Path, Path], clock: FakeClock
) -> None:
    """A plain run found the files in the library and never collected the
    result; a later --force run means "show the window again"."""
    one, _two = libs
    rid = _open(table, one)["id"]
    _finish(table, rid, one)

    fresh = _open(table, one, force=True)
    assert fresh["id"] != rid and fresh["status"] == PENDING
    assert table._requests[rid].delivered is True  # replaced by the window

    forced = _open(table, one, paper=OTHER_PAPER, force=True)["id"]
    _finish(table, forced, one)
    handed = _open(table, one, paper=OTHER_PAPER, force=True)
    assert handed["id"] == forced and handed["status"] == ADDED


def test_rule_1_a_joining_cli_brings_its_force(
    table: SiRequestTable, libs: tuple[Path, Path], clock: FakeClock
) -> None:
    """The page's own Add SI, joined by `lit add --si --force`: the result
    goes to a --force rerun, not to a plain one."""
    one, _two = libs
    rid = _open(table, one, "page", force=True)["id"]
    assert table._requests[rid].force is False  # a page never sets it
    assert _open(table, one, force=True)["id"] == rid
    assert table._requests[rid].force is True
    _finish(table, rid, one)

    assert _open(table, one)["id"] != rid  # plain: a fresh window
    assert table._requests[rid].delivered is True  # replaced by it


def test_rule_1_the_last_joining_cli_decides(
    table: SiRequestTable, libs: tuple[Path, Path], clock: FakeClock
) -> None:
    one, _two = libs
    rid = _open(table, one, force=True)["id"]
    assert _open(table, one)["id"] == rid  # a plain rerun joins it
    _finish(table, rid, one)
    assert _open(table, one)["id"] == rid


def test_rule_2_skips_an_added_result_whose_files_are_all_gone(
    table: SiRequestTable, libs: tuple[Path, Path], clock: FakeClock
) -> None:
    """The user removed what Done added: handing the result over would point
    the agent at files that are not there. It is never handed over again."""
    one, _two = libs
    rid = _open(table, one)["id"]
    _finish(table, rid, one, ("si-01.pdf", "si-02.csv"))

    fresh = _open(table, one, recorded=("si-03.pdf",))
    assert fresh["id"] != rid and fresh["status"] == PENDING
    assert table._requests[rid].delivered is True


def test_rule_2_hands_over_an_added_result_with_a_file_still_recorded(
    table: SiRequestTable, libs: tuple[Path, Path], clock: FakeClock
) -> None:
    one, _two = libs
    rid = _open(table, one)["id"]
    _finish(table, rid, one, ("si-01.pdf", "si-02.csv"))

    handed = _open(table, one, recorded=("si-02.csv",))
    assert handed["id"] == rid and handed["status"] == ADDED


def test_rule_2_a_result_done_while_the_files_were_read_is_not_moot(
    table: SiRequestTable, libs: tuple[Path, Path], clock: FakeClock
) -> None:
    """Done committed after the route began reading the paper's metadata:
    the read cannot show the new files, so it is no proof they are gone."""
    one, _two = libs
    rid = _open(table, one)["id"]
    read_mark = table.mark()
    _finish(table, rid, one)

    handed = _open(table, one, recorded=(), read_mark=read_mark)

    assert handed["id"] == rid and handed["status"] == ADDED


def test_rule_3_a_new_window_for_the_cli_replaces_every_unread_result(
    table: SiRequestTable, libs: tuple[Path, Path], clock: FakeClock
) -> None:
    """A plain run timed out and the user cancelled A; a --force run opened
    B (A asked differently), timed out, the user cancelled B, the --force
    rerun read that. A later plain run must open a window, not hand over A's
    old cancel: at most one unread result per paper."""
    one, _two = libs
    a = _open(table, one)["id"]
    table.cancel(a, Anchor.of(one))
    b = _open(table, one, force=True)["id"]
    assert b != a
    assert table._requests[a].delivered is True
    table.cancel(b, Anchor.of(one))
    assert _open(table, one, force=True)["id"] == b  # read: delivered

    later = _open(table, one)

    assert later["id"] not in (a, b) and later["status"] == PENDING


def test_rule_3_a_window_the_page_opens_replaces_nothing(
    table: SiRequestTable, libs: tuple[Path, Path], clock: FakeClock
) -> None:
    """The user's own Add SI says nothing about what the agent has seen."""
    one, _two = libs
    a = _open(table, one)["id"]
    table.cancel(a, Anchor.of(one))
    page = _open(table, one, "page")["id"]
    assert page != a
    assert table._requests[a].delivered is False
    table.cancel(page, Anchor.of(one))

    assert _open(table, one)["id"] == a


def test_rule_1_the_first_cli_to_join_a_page_window_replaces_unread_results(
    table: SiRequestTable, libs: tuple[Path, Path], clock: FakeClock
) -> None:
    """A plain run timed out and the user cancelled R1 (unread); then opened
    the cockpit's own Add SI (P), which a --force run joined; Done, and the
    --force rerun read P. With all SI removed later, a plain run must open a
    window — not replay R1's cancel."""
    one, _two = libs
    r1 = _open(table, one)["id"]
    table.cancel(r1, Anchor.of(one))
    page = _open(table, one, "page")["id"]
    assert table._requests[r1].delivered is False  # the page replaces nothing
    assert _open(table, one, force=True)["id"] == page
    assert table._requests[r1].delivered is True
    _finish(table, page, one)
    handed = _open(table, one, force=True)
    assert handed["id"] == page and handed["status"] == ADDED

    later = _open(table, one, recorded=())

    assert later["id"] not in (r1, page) and later["status"] == PENDING


def test_rule_1_a_cli_rejoining_its_window_changes_nothing_else(
    table: SiRequestTable, libs: tuple[Path, Path], clock: FakeClock
) -> None:
    """Only the first CLI to join a window replaces older results; a rerun
    joining the window it already awaits is just that."""
    one, _two = libs
    own = _open(table, one)["id"]
    assert _open(table, one)["id"] == own
    assert list(table._requests) == [own]

    old = _open(table, one, paper=OTHER_PAPER)["id"]
    table.cancel(old, Anchor.of(one))
    page = _open(table, one, "page", paper=OTHER_PAPER)["id"]
    assert _open(table, one, paper=OTHER_PAPER)["id"] == page  # first join
    assert table._requests[old].delivered is True
    # Not reachable through open() while the window is open — set here to
    # show that a rejoin leaves every other request alone.
    table._requests[old].delivered = False
    assert _open(table, one, paper=OTHER_PAPER)["id"] == page
    assert table._requests[old].delivered is False


def test_rule_2_a_cancel_needs_no_files(
    table: SiRequestTable, libs: tuple[Path, Path], clock: FakeClock
) -> None:
    one, _two = libs
    rid = _open(table, one)["id"]
    table.cancel(rid, Anchor.of(one))
    handed = _open(table, one, recorded=())
    assert handed["id"] == rid and handed["status"] == CANCELLED


def test_rule_2_takes_the_paper_and_library_into_account(
    table: SiRequestTable, libs: tuple[Path, Path], clock: FakeClock
) -> None:
    one, two = libs
    rid = _open(table, one)["id"]
    table.cancel(rid, Anchor.of(one))
    assert _open(table, two)["id"] != rid
    assert _open(table, one, paper=OTHER_PAPER)["id"] != rid
    assert _open(table, one)["id"] == rid


# ---------------------------------------------------------------------------
# What the page sees
# ---------------------------------------------------------------------------


def test_first_open_is_first_in_first_out_per_library(
    table: SiRequestTable, libs: tuple[Path, Path], clock: FakeClock
) -> None:
    one, two = libs
    first = _open(table, one)
    _open(table, two, paper=OTHER_PAPER)
    second = _open(table, one, paper=OTHER_PAPER)

    head = table.first_open(Anchor.of(one))
    assert head == {
        "id": first["id"], "paperId": PAPER, "title": "A title",
        "link": "https://doi.org/10.1/x",
    }
    assert table.has_open()
    table.cancel(first["id"], Anchor.of(one))
    assert (table.first_open(Anchor.of(one)) or {}).get("id") == second["id"]
    table.cancel(second["id"], Anchor.of(one))
    assert table.first_open(Anchor.of(one)) is None
    assert (table.first_open(Anchor.of(two)) or {}).get("paperId") == OTHER_PAPER


def test_a_confirming_request_still_shows(
    table: SiRequestTable, libs: tuple[Path, Path], clock: FakeClock
) -> None:
    one, _two = libs
    rid = _open(table, one)["id"]
    table.add_row(rid, Anchor.of(one), _row("a" * 32))
    table.begin_confirm(rid, Anchor.of(one), [])
    assert (table.first_open(Anchor.of(one)) or {}).get("id") == rid


def test_an_idle_open_request_expires_after_a_day(
    table: SiRequestTable, libs: tuple[Path, Path], clock: FakeClock
) -> None:
    one, _two = libs
    rid = _open(table, one)["id"]
    clock.now += sr.OPEN_TTL_S - 10
    table.add_row(rid, Anchor.of(one), _row("a" * 32))  # activity
    clock.now += sr.OPEN_TTL_S - 10
    assert table.snapshot(rid, Anchor.of(one))["status"] == PENDING
    clock.now += 20
    with pytest.raises(UnknownRequest):
        table.snapshot(rid, Anchor.of(one))
    assert not table.has_open()
    assert table.first_open(Anchor.of(one)) is None


# ---------------------------------------------------------------------------
# Rows
# ---------------------------------------------------------------------------


def test_the_same_file_twice_is_refused_under_the_lock(
    table: SiRequestTable, libs: tuple[Path, Path], clock: FakeClock
) -> None:
    one, _two = libs
    rid = _open(table, one)["id"]
    table.add_row(rid, Anchor.of(one), _row("a" * 32, sha="1" * 64))
    row, _snap = table.add_row(rid, Anchor.of(one), _row("b" * 32, "unverified", sha="1" * 64))
    assert row["verdict"] == "rejected"
    assert row["reason"] == f"same file as {'a' * 32}.pdf"
    # A rejected first copy does not shadow a later one.
    third, _snap = table.add_row(rid, Anchor.of(one), _row("c" * 32, sha="2" * 64))
    assert third["verdict"] == "ok"


def test_remove_row_is_idempotent(
    table: SiRequestTable, libs: tuple[Path, Path], clock: FakeClock
) -> None:
    one, _two = libs
    rid = _open(table, one)["id"]
    table.add_row(rid, Anchor.of(one), _row("a" * 32))
    removed, snap = table.remove_row(rid, "a" * 32, Anchor.of(one))
    assert removed is not None and removed.stash == Path(f"/stash/si-{'a' * 32}.pdf")
    assert snap["rows"] == []
    removed, _snap = table.remove_row(rid, "a" * 32, Anchor.of(one))
    assert removed is None


def test_rows_change_only_while_pending_and_in_its_library(
    table: SiRequestTable, libs: tuple[Path, Path], clock: FakeClock
) -> None:
    one, two = libs
    rid = _open(table, one)["id"]
    table.add_row(rid, Anchor.of(one), _row("a" * 32))
    with pytest.raises(RequestConflict, match="another library"):
        table.upload_target(rid, Anchor.of(two))
    with pytest.raises(RequestConflict, match="another library"):
        table.add_row(rid, Anchor.of(two), _row("b" * 32))
    with pytest.raises(RequestConflict, match="another library"):
        table.remove_row(rid, "a" * 32, Anchor.of(two))
    with pytest.raises(RequestConflict, match="another library"):
        table.upload_target(rid, None)

    table.begin_confirm(rid, Anchor.of(one), [])
    with pytest.raises(RequestConflict) as caught:
        table.add_row(rid, Anchor.of(one), _row("b" * 32))
    assert caught.value.request["status"] == CONFIRMING
    with pytest.raises(RequestConflict):
        table.remove_row(rid, "a" * 32, Anchor.of(one))


def test_unknown_requests(table: SiRequestTable, libs: tuple[Path, Path], clock: FakeClock) -> None:
    one, _two = libs
    served = Anchor.of(one)
    for call in (
        lambda: table.snapshot("f" * 32, served),
        lambda: table.upload_target("f" * 32, served),
        lambda: table.add_row("f" * 32, served, _row("a" * 32)),
        lambda: table.remove_row("f" * 32, "a" * 32, served),
        lambda: table.begin_confirm("f" * 32, served, []),
        lambda: table.cancel("f" * 32, served),
    ):
        with pytest.raises(UnknownRequest):
            call()


# ---------------------------------------------------------------------------
# Done
# ---------------------------------------------------------------------------


def test_done_takes_every_ok_row_and_only_the_ticked_unverified(
    table: SiRequestTable, libs: tuple[Path, Path], clock: FakeClock
) -> None:
    one, _two = libs
    served = Anchor.of(one)
    rid = _open(table, one)["id"]
    ok, ticked, unticked, refused = "a" * 32, "b" * 32, "c" * 32, "d" * 32
    table.add_row(rid, served, _row(ok))
    table.add_row(rid, served, _row(ticked, "unverified"))
    table.add_row(rid, served, _row(unticked, "unverified"))
    table.add_row(rid, served, _row(refused, "rejected"))

    plan = table.begin_confirm(rid, served, [ticked, ok])

    assert isinstance(plan, ConfirmPlan)
    assert [(row.id, approved) for row, approved in plan.chosen] == [
        (ok, False),  # listed, yet a ✓ never carries the user's "add anyway"
        (ticked, True),
    ]
    assert [row.id for row in plan.dropped] == [unticked]
    assert (plan.vault, plan.paper_id) == (one, PAPER)
    assert table.snapshot(rid, served)["status"] == CONFIRMING


@pytest.mark.parametrize(
    ("rows", "ticked", "message"),
    [
        ([("a", "ok")], ["e"], "No file"),
        ([("a", "ok"), ("d", "rejected")], ["d"], "refused"),
        ([("b", "unverified")], [], "Nothing to add"),
        ([("d", "rejected")], [], "Nothing to add"),
        ([], [], "Nothing to add"),
    ],
)
def test_done_refuses_a_bad_selection(
    table: SiRequestTable,
    libs: tuple[Path, Path],
    clock: FakeClock,
    rows: list[tuple[str, str]],
    ticked: list[str],
    message: str,
) -> None:
    one, _two = libs
    served = Anchor.of(one)
    rid = _open(table, one)["id"]
    for fid, verdict in rows:
        table.add_row(rid, served, _row(fid * 32, verdict))
    with pytest.raises(BadSelection, match=message):
        table.begin_confirm(rid, served, [fid * 32 for fid in ticked])
    assert table.snapshot(rid, served)["status"] == PENDING


def test_done_twice_and_done_after_cancel(
    table: SiRequestTable, libs: tuple[Path, Path], clock: FakeClock
) -> None:
    one, two = libs
    served = Anchor.of(one)
    rid = _open(table, one)["id"]
    table.add_row(rid, served, _row("a" * 32))
    table.begin_confirm(rid, served, [])
    with pytest.raises(RequestConflict, match="being added"):
        table.begin_confirm(rid, served, [])
    with pytest.raises(RequestConflict, match="being added"):
        table.cancel(rid, served)
    done = table.finish_confirm(rid, served, [{"file": "si-01.pdf"}], ["late INDEX"])
    assert (done["status"], done["added"], done["warnings"]) == (
        ADDED, [{"file": "si-01.pdf"}], ["late INDEX"],
    )
    # A repeated Done answers with the result, whichever library is shown.
    assert table.begin_confirm(rid, Anchor.of(two), []) == table.snapshot(rid, Anchor.of(two))
    with pytest.raises(RequestConflict, match="added already"):
        table.cancel(rid, served)

    other = _open(table, one, paper=OTHER_PAPER)["id"]
    table.cancel(other, served)
    with pytest.raises(RequestConflict, match="cancelled"):
        table.begin_confirm(other, served, [])


def test_done_in_another_library_is_refused(
    table: SiRequestTable, libs: tuple[Path, Path], clock: FakeClock
) -> None:
    one, two = libs
    rid = _open(table, one)["id"]
    table.add_row(rid, Anchor.of(one), _row("a" * 32))
    with pytest.raises(RequestConflict, match="another library"):
        table.begin_confirm(rid, Anchor.of(two), [])
    assert table.snapshot(rid, Anchor.of(one))["status"] == PENDING


def test_a_failed_done_goes_back_to_pending(
    table: SiRequestTable, libs: tuple[Path, Path], clock: FakeClock
) -> None:
    one, _two = libs
    served = Anchor.of(one)
    rid = _open(table, one)["id"]
    table.add_row(rid, served, _row("a" * 32))
    table.add_row(rid, served, _row("b" * 32))
    table.begin_confirm(rid, served, [])

    downgraded = _row("b" * 32, "unverified")
    snap = table.abort_confirm(rid, served, drop={"a" * 32}, refreshed={"b" * 32: downgraded})

    assert snap["status"] == PENDING
    assert [(r["id"], r["verdict"]) for r in snap["rows"]] == [("b" * 32, "unverified")]
    table.add_row(rid, served, _row("c" * 32))  # usable again


# ---------------------------------------------------------------------------
# Cancel
# ---------------------------------------------------------------------------


def test_cancel_hands_back_every_stash_once(
    table: SiRequestTable, libs: tuple[Path, Path], clock: FakeClock
) -> None:
    one, two = libs
    rid = _open(table, one)["id"]
    table.add_row(rid, Anchor.of(one), _row("a" * 32))
    table.add_row(rid, Anchor.of(one), _row("b" * 32, "unverified"))

    # Whichever library is shown: the window the user closes still ends.
    stashes, snap = table.cancel(rid, Anchor.of(two))

    assert stashes == [
        Path(f"/stash/si-{'a' * 32}.pdf"), Path(f"/stash/si-{'b' * 32}.pdf"),
    ]
    assert snap["status"] == CANCELLED
    again, snap = table.cancel(rid, Anchor.of(one))
    assert again == [] and snap["status"] == CANCELLED
    with pytest.raises(RequestConflict, match="cancelled"):
        table.add_row(rid, Anchor.of(one), _row("c" * 32))
    assert table.first_open(Anchor.of(one)) is None
