"""``shell_arg``: what a printed command puts in place of a real value."""

from __future__ import annotations

from pathlib import PurePosixPath

import pytest

from litman.core import shell
from litman.core.shell import shell_arg
from litman.core.supplementary import shell_path


@pytest.fixture(params=["win32", "linux", "darwin"])
def platform(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> str:
    monkeypatch.setattr(shell, "_platform", lambda: request.param)
    return str(request.param)


@pytest.mark.parametrize(
    "value",
    [
        "pepforge",
        "Isidro_2009_Crystal-x",
        "si-02",
        "topics=x",
        "projects=2024-grant",
        "paper.pdf",
        "/home/me/a.pdf",
        "https://github.com/a/b.git",
        "测试",
    ],
)
def test_a_value_that_can_go_bare_is_left_as_it_is(platform: str, value: str) -> None:
    assert shell_arg(value) == value


@pytest.mark.parametrize(
    ("value", "quoted"),
    [
        ("2009_Isidro-Llobet_x", '"2009_Isidro-Llobet_x"'),
        ("10.1016/j.x.2020.01.001", '"10.1016/j.x.2020.01.001"'),
        ("2024-grant", '"2024-grant"'),
        ("my paper", '"my paper"'),
        ("a (1).pdf", '"a (1).pdf"'),
        ("a,b", '"a,b"'),
        ("a[1]", '"a[1]"'),
        ("x@y", '"x@y"'),
        ("x;y", '"x;y"'),
        ("x|y", '"x|y"'),
        ("x&y", '"x&y"'),
        ("<x>", '"<x>"'),
        ("{x}", '"{x}"'),
        ("relevance-x=Direct baseline", '"relevance-x=Direct baseline"'),
        ("it's", '"it\'s"'),
        ("", '""'),
    ],
)
def test_a_value_that_cannot_goes_in_double_quotes(
    platform: str, value: str, quoted: str
) -> None:
    assert shell_arg(value) == quoted


@pytest.mark.parametrize(
    ("value", "quoted"),
    [
        ("$HOME", "'$HOME'"),
        ("a`b", "'a`b'"),
        ('say "x"', "'say \"x\"'"),
        ("wow!", "'wow!'"),
    ],
)
def test_a_value_double_quotes_would_change_goes_in_single_ones(
    platform: str, value: str, quoted: str
) -> None:
    assert shell_arg(value) == quoted


def test_a_windows_path_keeps_its_backslashes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(shell, "_platform", lambda: "win32")
    assert shell_arg(r"C:\a\b.pdf") == r"C:\a\b.pdf"
    assert shell_arg("C:\\a b\\c.pdf") == '"C:\\a b\\c.pdf"'
    assert shell_arg("C:\\x\\") == "C:\\x\\"


def test_a_trailing_backslash_never_meets_a_closing_double_quote(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(shell, "_platform", lambda: "win32")
    assert shell_arg("C:\\a b\\") == "'C:\\a b\\'"


def test_a_single_quote_is_doubled_on_windows(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(shell, "_platform", lambda: "win32")
    assert shell_arg("$it's") == "'$it''s'"


@pytest.mark.parametrize("platform_name", ["linux", "darwin"])
def test_a_single_quote_is_shell_quoted_on_posix(
    monkeypatch: pytest.MonkeyPatch, platform_name: str
) -> None:
    monkeypatch.setattr(shell, "_platform", lambda: platform_name)
    assert shell_arg("$it's") == "'$it'\"'\"'s'"


@pytest.mark.parametrize("platform_name", ["linux", "darwin"])
def test_a_backslash_is_kept_on_posix(
    monkeypatch: pytest.MonkeyPatch, platform_name: str
) -> None:
    """Bare or in double quotes, the shell would take it as an escape."""
    monkeypatch.setattr(shell, "_platform", lambda: platform_name)
    assert shell_arg("a\\b") == "'a\\b'"


def test_it_takes_a_path(platform: str) -> None:
    assert shell_arg(PurePosixPath("/a b/c.pdf")) == '"/a b/c.pdf"'


@pytest.mark.parametrize(
    ("path", "shown"),
    [
        ("/a/b.xlsx", "/a/b.xlsx"),
        ("/a b/c.xlsx", '"/a b/c.xlsx"'),
        ("2009_table.xlsx", '"2009_table.xlsx"'),
        ("/a/b (1).xlsx", '"/a/b (1).xlsx"'),
        ("/a/$b.xlsx", "'/a/$b.xlsx'"),
    ],
)
def test_shell_path_quotes_an_ordinary_path_the_same_way(
    platform: str, path: str, shown: str
) -> None:
    assert shell_path(path) == shown


def test_shell_path_keeps_a_byte_that_is_not_text(platform: str) -> None:
    assert shell_path("/a/caf\udce9.xlsx") == "$'/a/caf\\xe9.xlsx'"
