"""Intercept the two-positional shape a user reaches for first.

``lit link <paper> <project>`` and ``lit code add <paper> <repo>`` are what
the commands *look* like they should take. They do not: the second value goes
in a flag (``--project`` / ``--paper``). Click's own answer to the extra word
is "Got unexpected extra argument (pepforge)", which names the thing that is
wrong and nothing about how to be right.

Each affected command declares a trailing optional argument purely to catch
this, and hands it here. We refuse — we do NOT quietly accept the second
positional, which would add an undocumented call shape that agents would
start depending on and that could never be taken back.
"""

from __future__ import annotations

import click


def reject_second_positional(*, taken: str, extra: str, correct: str) -> click.UsageError:
    """Build the error for a command handed two positionals instead of one.

    Args:
        taken: the positional the command actually accepts.
        extra: the surplus word.
        correct: the whole command line the user meant, ready to copy.
    """
    return click.UsageError(
        f"Got two arguments ({taken!r} and {extra!r}), but this command "
        f"takes one — the other value goes in a flag.\n\n"
        f"Did you mean:\n\n"
        f"    {correct}"
    )
