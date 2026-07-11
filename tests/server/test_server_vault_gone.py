"""The bound vault vanishes mid-session — ``_guard_vault``'s 410 branch.

``lit gui`` binds one vault for the life of the process (``app.state.vault``).
Nothing in POSIX stops the user from moving or deleting that directory while the
browser is open: a rename acts on the parent's directory entry, so an open file
descriptor, a cwd, even a running server cannot veto it. The process gets no
notification. Before this guard the server kept serving the dead path, and both
halves of it lied:

* **reads** — a missing ``papers/`` makes ``list_papers`` return ``[]``, so
  ``GET /api/papers`` answered ``200 []`` and the GUI rendered *your library is
  empty*, which is a far worse thing to say to someone who owns 19 papers than
  *I can't find it*.
* **writes** — ``staged_write`` mkdirs its staging root with ``parents=True``
  and the commit mkdirs the paper's parent the same way, so a note saved after
  the move would *rebuild* a one-paper ghost library at the dead path, report
  success, and silently fork the user's data.

The guard is one ``stat`` per ``/api/`` request, placed before the route so the
write dies before it touches the disk. It answers **410 Gone** — never the 409
the welcome page keys off, because telling this user to "create your first
library" invites them to create a second one on top of the wreckage.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient

from litman.core.library import create_vault
from litman.core.vault_registry import (
    add_vault,
    find_active,
    load_registry,
    save_registry,
)
from litman.server import create_app


def _client(vault: Path) -> TestClient:
    return TestClient(create_app(vault))


def _move_away(vault: Path, dest: Path) -> Path:
    """Move the whole vault, the way a user drags a folder in a file manager."""
    shutil.move(str(vault), str(dest))
    assert not vault.exists()
    return dest


# ===========================================================================
# reads: 410, not a 200 that claims the library is empty
# ===========================================================================


def test_moved_vault_read_is_410_not_empty_list(tmp_path: Path) -> None:
    """The regression that started this: ``200 []`` — *your library is empty*."""
    vault = create_vault(tmp_path, name="lib")
    client = _client(vault)
    assert client.get("/api/papers").status_code == 200  # alive

    _move_away(vault, tmp_path / "moved")

    resp = client.get("/api/papers")
    assert resp.status_code == 410
    assert resp.json() != []


def test_410_body_names_the_path_it_lost(tmp_path: Path) -> None:
    """The banner needs the dead path to tell the user *which* library went."""
    vault = create_vault(tmp_path, name="lib")
    client = _client(vault)
    _move_away(vault, tmp_path / "moved")

    body = client.get("/api/papers").json()
    assert Path(body["path"]) == vault
    assert str(vault) in body["detail"]


def test_deleted_vault_is_410(tmp_path: Path) -> None:
    """Deleted, not moved — same verdict, the server can no longer see it."""
    vault = create_vault(tmp_path, name="lib")
    client = _client(vault)
    shutil.rmtree(vault)

    assert client.get("/api/papers").status_code == 410


# ===========================================================================
# writes: refused BEFORE staged_write can rebuild the vault it lost
# ===========================================================================


def test_write_after_move_does_not_rebuild_a_ghost_vault(
    vault_with_paper: tuple[Path, str], tmp_path: Path
) -> None:
    """The data-forking one. ``staged_write`` mkdirs ``parents=True``; without the
    guard this write reconstructs the vault at the dead path, saves the note into
    it, and answers 200 — leaving the real library short one note."""
    vault, paper_id = vault_with_paper
    client = _client(vault)

    _move_away(vault, tmp_path / "moved")

    resp = client.put(f"/api/paper/{paper_id}/notes", json={"text": "# Notes\n\nbody\n"})
    assert resp.status_code == 410
    # Nothing was resurrected at the old path — not the vault root, not the
    # staging area, not the paper folder.
    assert not vault.exists()


def test_pdf_annotation_write_after_move_is_410(
    vault_with_paper: tuple[Path, str], tmp_path: Path
) -> None:
    """Every write goes through the same door, not just the markdown ones."""
    vault, paper_id = vault_with_paper
    client = _client(vault)
    _move_away(vault, tmp_path / "moved")

    resp = client.put(
        f"/api/paper/{paper_id}/pdf-annotations",
        content=b"%PDF-1.4\n",
        headers={"Content-Type": "application/pdf"},
    )
    assert resp.status_code == 410
    assert not vault.exists()


# ===========================================================================
# the sentinel is lit-config.yaml, not the directory
# ===========================================================================


def test_directory_without_lit_config_is_still_gone(tmp_path: Path) -> None:
    """A carcass at the old path does not count as the library coming back.

    This is exactly what a pre-guard write left behind (``staged_write`` never
    writes a ``lit-config.yaml``), and it is also what happens when the user moves
    the vault and then creates an unrelated folder with the same name.
    """
    vault = create_vault(tmp_path, name="lib")
    client = _client(vault)
    _move_away(vault, tmp_path / "moved")

    (vault / "papers").mkdir(parents=True)
    assert client.get("/api/papers").status_code == 410


def test_vault_moved_back_heals_itself(tmp_path: Path) -> None:
    """Undo the move and the next request just works — the guard holds no state,
    so the frontend's 5s retry recovers with no restart and no user action."""
    vault = create_vault(tmp_path, name="lib")
    client = _client(vault)
    moved = _move_away(vault, tmp_path / "moved")
    assert client.get("/api/papers").status_code == 410

    shutil.move(str(moved), str(vault))
    assert client.get("/api/papers").status_code == 200


# ===========================================================================
# 410 ≠ 409: the welcome page must never be offered to a user who HAS a library
# ===========================================================================


def test_no_vault_is_409_not_410(tmp_path: Path) -> None:
    """A server that never had a vault keeps its 409 — the SPA keys the welcome
    page ("create your first library") off that code, and a user whose library
    merely moved must not be sent down that path."""
    assert TestClient(create_app(None)).get("/api/papers").status_code == 409


def test_gone_vault_keeps_the_escape_doors_open(tmp_path: Path) -> None:
    """The recovery routes stay reachable in the gone state — otherwise the user
    is locked out of the very dialog that fixes it."""
    vault = create_vault(tmp_path, name="lib")
    client = _client(vault)
    _move_away(vault, tmp_path / "moved")

    resp = client.get("/api/vaults")
    assert resp.status_code == 200
    assert Path(resp.json()["served"]) == vault  # still bound to the dead path


def test_re_registering_the_new_path_recovers(tmp_path: Path) -> None:
    """End-to-end recovery, entirely through the doors the guard leaves open:
    register the vault where it now lives, switch to it, and the server is whole
    again in place — no restart."""
    vault = create_vault(tmp_path, name="lib")
    save_registry(add_vault(load_registry(), "lib", vault))
    client = _client(vault)

    moved = _move_away(vault, tmp_path / "moved")
    assert client.get("/api/papers").status_code == 410

    assert client.post("/api/vaults", json={"name": "lib2", "path": str(moved)}).status_code == 200
    assert client.put("/api/vaults/active", json={"name": "lib2"}).status_code == 200

    assert client.get("/api/papers").status_code == 200


def test_gone_state_can_create_a_fresh_vault(tmp_path: Path) -> None:
    """The fourth door: `lit init` through the GUI. A user whose library is
    gone for good (deleted on purpose, disk replaced) escapes by making a new
    one — the whitelist entry existed, this pins that the door actually opens."""
    vault = create_vault(tmp_path, name="lib")
    client = _client(vault)
    _move_away(vault, tmp_path / "moved")

    resp = client.post(
        "/api/vaults/create", json={"parent_dir": str(tmp_path), "name": "fresh"}
    )
    assert resp.status_code == 200
    assert (tmp_path / "fresh" / "lit-config.yaml").is_file()


def test_gone_state_can_unregister_an_unrelated_entry(tmp_path: Path) -> None:
    """Unregister is a pure registry write — the route never touches any vault
    directory — and the manager the banner opens shows its button on every row.
    It must not be answered with the middleware's complaint about the SERVED
    vault when the user clicked a different one."""
    beta, alpha = _register_pair(tmp_path)
    client = _client(beta)
    _move_away(beta, tmp_path / "beta-moved")  # the SERVED vault is gone

    resp = client.delete("/api/vaults/alpha")
    assert resp.status_code == 200
    names = [v["name"] for v in client.get("/api/vaults").json()["vaults"]]
    assert names == ["beta"]

    # The served vault itself still refuses — with the route's own guard and
    # its own reason (the server is bound to it), not the middleware's 410.
    resp = client.delete("/api/vaults/beta")
    assert resp.status_code == 409
    assert "serv" in resp.json()["detail"].lower()


# ===========================================================================
# a REGISTERED but NOT served vault vanishes — the switch-vault path
#
# The other half of the same accident, and the one the 410 guard cannot see: the
# user is working in vault B when vault A's folder moves. Nothing the server is
# bound to has changed, so no request is in the gone state — A is simply a
# registry entry whose path is now a lie. Two things must hold:
#
# * ``PUT /vaults/active`` must REFUSE (``apply_vault_use(require_path=True)``
#   validates before it persists), or the registry's global active — read by
#   every ``lit`` command in every terminal — would be left pointing at nothing.
# * ``GET /vaults`` must SAY SO up front (``exists``), so the selector can mark A
#   missing instead of offering it as a normal choice and failing on the click.
# ===========================================================================


def _register_pair(tmp_path: Path) -> tuple[Path, Path]:
    """Serve ``beta`` (registry-active); register ``alpha`` alongside it."""
    beta = create_vault(tmp_path, name="beta")
    alpha = create_vault(tmp_path, name="alpha")
    save_registry(add_vault(load_registry(), "beta", beta, set_active=True))
    save_registry(add_vault(load_registry(), "alpha", alpha))
    return beta, alpha


def test_get_vaults_marks_a_moved_vault_missing(tmp_path: Path) -> None:
    """`exists` is re-probed per call, so the selector can mark it before the click."""
    beta, alpha = _register_pair(tmp_path)
    client = _client(beta)

    before = {v["name"]: v["exists"] for v in client.get("/api/vaults").json()["vaults"]}
    assert before == {"beta": True, "alpha": True}

    _move_away(alpha, tmp_path / "alpha-moved")

    after = {v["name"]: v["exists"] for v in client.get("/api/vaults").json()["vaults"]}
    assert after == {"beta": True, "alpha": False}


def test_get_vaults_marks_the_served_vault_missing_too(tmp_path: Path) -> None:
    """The gone state is visible in the manager as well as in the 410 banner."""
    vault = create_vault(tmp_path, name="lib")
    save_registry(add_vault(load_registry(), "lib", vault, set_active=True))
    client = _client(vault)
    _move_away(vault, tmp_path / "moved")

    entry = client.get("/api/vaults").json()["vaults"][0]
    assert entry["name"] == "lib"
    assert entry["exists"] is False


def test_switch_to_a_moved_vault_is_refused(tmp_path: Path) -> None:
    """400 — and the refusal lands BEFORE the registry is written."""
    beta, alpha = _register_pair(tmp_path)
    app = create_app(beta)
    client = TestClient(app)
    _move_away(alpha, tmp_path / "alpha-moved")

    resp = client.put("/api/vaults/active", json={"name": "alpha"})
    assert resp.status_code == 400
    detail = resp.json()["detail"]
    assert "alpha" in detail and str(alpha) in detail  # names the path it lost

    # Nothing moved: the global active still points at a vault that is really
    # there, and this server keeps serving what it was serving.
    assert find_active(load_registry()).name == "beta"
    assert Path(app.state.vault).resolve() == beta.resolve()
    assert client.get("/api/papers").status_code == 200


def test_a_carcass_dir_is_marked_missing_and_refused(tmp_path: Path) -> None:
    """One aliveness sentinel everywhere: the ``lit-config.yaml``.

    A bare directory at the registered path — the ghost a pre-guard write left
    behind, or an unrelated same-name folder that landed where the vault used to
    be — is not the vault. The 410 guard already knew that
    (``test_directory_without_lit_config_is_still_gone``); this pins that the
    other two probes agree. Before they did, ``exists`` said true, the switch
    SUCCEEDED, and the global registry active — read by every ``lit`` command in
    every terminal — was persisted at a non-vault while every subsequent GUI
    request answered 410 with no error at switch time.
    """
    beta, alpha = _register_pair(tmp_path)
    app = create_app(beta)
    client = TestClient(app)

    _move_away(alpha, tmp_path / "alpha-moved")
    (alpha / "papers").mkdir(parents=True)  # a directory, but not a vault

    vaults = {v["name"]: v["exists"] for v in client.get("/api/vaults").json()["vaults"]}
    assert vaults == {"beta": True, "alpha": False}

    resp = client.put("/api/vaults/active", json={"name": "alpha"})
    assert resp.status_code == 400
    detail = resp.json()["detail"]
    assert "alpha" in detail and str(alpha) in detail

    # The worst half of the old behaviour: nothing was persisted. The global
    # active still points at a real vault, and this server keeps serving.
    assert find_active(load_registry()).name == "beta"
    assert Path(app.state.vault).resolve() == beta.resolve()
    assert client.get("/api/papers").status_code == 200


def test_switch_succeeds_once_the_folder_is_put_back(tmp_path: Path) -> None:
    """The recovery a user reaches for first: undo the move. No restart."""
    beta, alpha = _register_pair(tmp_path)
    app = create_app(beta)
    client = TestClient(app)

    moved = _move_away(alpha, tmp_path / "alpha-moved")
    assert client.put("/api/vaults/active", json={"name": "alpha"}).status_code == 400

    shutil.move(str(moved), str(alpha))  # the user drags it back

    assert client.put("/api/vaults/active", json={"name": "alpha"}).status_code == 200
    assert find_active(load_registry()).name == "alpha"
    assert Path(app.state.vault).resolve() == alpha.resolve()
    assert client.get("/api/papers").status_code == 200


# ===========================================================================
# the recovery's last loose end: project bridges heal on the completing switch
# ===========================================================================


def _link_paper_into_project(vault: Path, tmp_path: Path) -> Path:
    """One paper linked into one project — healthy bridge. Returns the link."""
    from ruamel.yaml import YAML

    from litman.core.project_link import rebuild_all_project_links

    project_dir = tmp_path / "pepforge"
    project_dir.mkdir()
    (vault / "lit-config.yaml").write_text(
        f"library_name: {vault.name}\nprojects:\n  pepforge: {project_dir}\n",
        encoding="utf-8",
    )
    paper_dir = vault / "papers" / "p1"
    paper_dir.mkdir(parents=True)
    with (paper_dir / "metadata.yaml").open("w", encoding="utf-8") as f:
        YAML().dump(
            {
                "id": "p1",
                "title": "Test paper",
                "authors": ["Doe, Jane"],
                "year": 2024,
                "doi": "10.test/p1",
                "status": "inbox",
                "priority": "B",
                "type": "research",
                "projects": ["pepforge"],
                "topics": [],
                "methods": [],
                "code-clones": [],
                "created-at": "2026-05-11T10:00:00+02:00",
                "updated-at": "2026-05-11T10:00:00+02:00",
            },
            f,
        )
    rebuild_all_project_links(vault, {"pepforge": str(project_dir)})
    link = project_dir / "litman_reflib" / "p1"
    assert link.is_symlink() and link.exists()
    return link


def test_switch_to_recovered_vault_heals_project_bridges(tmp_path: Path) -> None:
    """After `test_re_registering_the_new_path_recovers`'s flow the SERVER is
    whole again — but every project's litman_reflib/litman_code bridge still
    encodes the old location. A GUI-only user never runs the CLI command that
    would offer the rebuild, so the switch that completes the recovery is
    their only seam: it must re-point the bridges on its own."""
    vault = create_vault(tmp_path, name="lib")
    save_registry(add_vault(load_registry(), "lib", vault))
    link = _link_paper_into_project(vault, tmp_path)
    client = _client(vault)

    moved = _move_away(vault, tmp_path / "moved")
    assert link.is_symlink() and not link.exists()  # bridge dangles

    assert (
        client.post(
            "/api/vaults", json={"name": "lib2", "path": str(moved)}
        ).status_code
        == 200
    )
    assert (
        client.put("/api/vaults/active", json={"name": "lib2"}).status_code
        == 200
    )

    assert link.is_symlink() and link.exists()
    assert link.resolve() == (moved / "papers" / "p1").resolve()


def test_switch_heal_touches_only_dangling_projects(tmp_path: Path) -> None:
    """The consent-free heal is NARROWED to the projects that dangle.

    ``rebuild_all_project_links`` wipes both hubs of every project it is
    handed, so a full-map heal on this promptless path would clobber links a
    healthy project's hub got from elsewhere (a sibling vault sharing the
    project dir). Second project's hub holds one healthy link pointing
    OUTSIDE the served vault: it must survive the switch untouched — same
    inode, never unlinked-and-recreated."""
    vault = create_vault(tmp_path, name="lib")
    save_registry(add_vault(load_registry(), "lib", vault))
    link = _link_paper_into_project(vault, tmp_path)

    other = tmp_path / "otherproj"
    (other / "litman_reflib").mkdir(parents=True)
    foreign_target = tmp_path / "foreign_paper"
    foreign_target.mkdir()
    foreign_link = other / "litman_reflib" / "foreign"
    foreign_link.symlink_to("../../foreign_paper")
    assert foreign_link.exists()
    ino_before = foreign_link.lstat().st_ino

    (vault / "lit-config.yaml").write_text(
        f"library_name: {vault.name}\nprojects:\n"
        f"  pepforge: {tmp_path / 'pepforge'}\n"
        f"  other: {other}\n",
        encoding="utf-8",
    )

    client = _client(vault)
    moved = _move_away(vault, tmp_path / "moved")
    assert (
        client.post(
            "/api/vaults", json={"name": "lib2", "path": str(moved)}
        ).status_code
        == 200
    )
    assert (
        client.put("/api/vaults/active", json={"name": "lib2"}).status_code
        == 200
    )

    # The dangling project healed...
    assert link.exists()
    assert link.resolve() == (moved / "papers" / "p1").resolve()
    # ...and the healthy one was not even touched.
    assert foreign_link.exists()
    assert foreign_link.lstat().st_ino == ino_before


def test_switch_heal_failure_does_not_fail_the_switch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The heal is best-effort by contract: the switch is already persisted
    when it runs, so a rebuild blowing up must not turn the 200 into a 500 —
    `lit health-check --fix` stays the fallback."""
    import litman.core.project_link as project_link_mod

    vault = create_vault(tmp_path, name="lib")
    save_registry(add_vault(load_registry(), "lib", vault))
    _link_paper_into_project(vault, tmp_path)
    client = _client(vault)
    moved = _move_away(vault, tmp_path / "moved")
    assert (
        client.post(
            "/api/vaults", json={"name": "lib2", "path": str(moved)}
        ).status_code
        == 200
    )

    def _boom(*a: object, **kw: object) -> dict:
        raise RuntimeError("rebuild exploded")

    monkeypatch.setattr(project_link_mod, "rebuild_all_project_links", _boom)

    resp = client.put("/api/vaults/active", json={"name": "lib2"})
    assert resp.status_code == 200
    assert find_active(load_registry()).name == "lib2"


def test_switch_between_healthy_vaults_rebuilds_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Probe-gated: a plain switch between two healthy vaults must not touch
    anyone's project directory — the heal fires only on a definitely-dangling
    bridge."""
    import litman.core.project_link as project_link_mod

    lib1 = create_vault(tmp_path, name="lib1")
    save_registry(add_vault(load_registry(), "lib1", lib1))
    lib2 = create_vault(tmp_path, name="lib2")
    save_registry(add_vault(load_registry(), "lib2", lib2))
    _link_paper_into_project(lib2, tmp_path)  # healthy bridges on the target

    # A recorder, not a raiser: the route deliberately swallows heal
    # exceptions (a heal failure must not fail the switch), so a stub that
    # raises would be silently absorbed and the test would pass either way.
    calls: list[tuple[object, ...]] = []
    monkeypatch.setattr(
        project_link_mod,
        "rebuild_all_project_links",
        lambda *a, **kw: (calls.append(a), {})[1],
    )

    client = _client(lib1)
    assert (
        client.put("/api/vaults/active", json={"name": "lib2"}).status_code
        == 200
    )
    assert calls == []
