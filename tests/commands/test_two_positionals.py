"""The two-positional shape a user reaches for first.

`lit link <paper> <project>` and `lit code add <paper> <repo>` are what these
commands look like they should take. They do not — the second value goes in a
flag — and Click's own answer was "Got unexpected extra argument (pepforge)",
which names what is wrong and nothing about how to be right.

We refuse rather than tolerantly accept the second positional: accepting it
would add a call shape that is in no documentation, that agents would start
depending on, and that could then never be taken back.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from click.testing import CliRunner

from litman.cli import cli
from litman.core.library import create_vault


@pytest.fixture
def vault(tmp_path: Path) -> Path:
    return create_vault(tmp_path)


# (argv, the command line the error must hand back)
_MISUSES = [
    (
        ["link", "2024_Foo", "pepforge"],
        "lit link 2024_Foo --project pepforge",
    ),
    (
        ["unlink", "2024_Foo", "pepforge"],
        "lit unlink 2024_Foo --project pepforge",
    ),
    (
        ["code", "add", "https://github.com/x/y", "2024_Foo"],
        "lit code add https://github.com/x/y --paper 2024_Foo",
    ),
    (
        ["code", "link", "myrepo", "2024_Foo"],
        "lit code link myrepo --paper 2024_Foo",
    ),
    (
        ["code", "unlink", "myrepo", "2024_Foo"],
        "lit code unlink myrepo --paper 2024_Foo",
    ),
]


@pytest.mark.parametrize(
    ("argv", "correct"), _MISUSES, ids=lambda v: " ".join(v) if isinstance(v, list) else ""
)
def test_a_second_positional_is_refused_with_the_right_command(
    vault: Path, argv: list[str], correct: str
) -> None:
    result = CliRunner().invoke(cli, [*argv, "--library", str(vault)])

    assert result.exit_code != 0
    assert correct in result.output, result.output


def test_code_add_names_the_source_even_when_the_two_are_swapped(
    vault: Path,
) -> None:
    """`lit code add <paper> <url>` reads as naturally as the other order.

    Assuming the given order would hand back a command with the arguments
    still the wrong way round — useless. The URL is recognisable, so use it.
    """
    result = CliRunner().invoke(
        cli,
        ["code", "add", "2024_Foo", "https://github.com/x/y",
         "--library", str(vault)],
    )
    assert result.exit_code != 0
    assert "lit code add https://github.com/x/y --paper 2024_Foo" in result.output


def test_code_add_keeps_the_given_order_when_neither_looks_like_a_source(
    vault: Path,
) -> None:
    """No guessing when there is nothing to go on."""
    result = CliRunner().invoke(
        cli, ["code", "add", "aaa", "bbb", "--library", str(vault)]
    )
    assert result.exit_code != 0
    assert "lit code add aaa --paper bbb" in result.output


# ---------------------------------------------------------------------------
# the correct shapes are untouched
# ---------------------------------------------------------------------------


def test_unlink_still_reports_a_genuinely_missing_project(vault: Path) -> None:
    """--project moved out of Click's `required=True` so the guard could run
    first; the message for actually forgetting it must not have changed."""
    result = CliRunner().invoke(
        cli, ["unlink", "2024_Foo", "--library", str(vault)]
    )
    assert result.exit_code != 0
    assert "Missing option '--project'." in result.output


@pytest.mark.parametrize(
    "argv",
    [
        ["link", "2024_Foo", "--project", "pepforge"],
        ["unlink", "2024_Foo", "--project", "pepforge"],
        ["code", "link", "myrepo", "--paper", "2024_Foo"],
        ["code", "unlink", "myrepo", "--paper", "2024_Foo"],
    ],
    ids=lambda v: " ".join(v),
)
def test_the_correct_shape_gets_past_the_guard(vault: Path, argv: list[str]) -> None:
    """It fails on the empty vault — but on the paper, not on usage."""
    result = CliRunner().invoke(cli, [*argv, "--library", str(vault)])

    assert "Got two arguments" not in result.output
    assert "No paper matching" in str(result.exception) or result.exit_code == 0


def test_link_rebuild_all_takes_no_positional_and_still_works(vault: Path) -> None:
    result = CliRunner().invoke(
        cli, ["link", "--rebuild-all", "--library", str(vault)]
    )
    assert result.exit_code == 0, result.output
