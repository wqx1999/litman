"""Shell-command evidence helpers, shared by the adapters that see raw commands.

An agent whose event stream reports a Bash tool's command as ONE raw shell string
— claude's ``Bash`` ``input.command`` and opencode's ``bash``
``state.input.command`` — hands the evidence layer a compound line like
``lit hello && lit vault list`` that has to be split back into its individual
``lit`` invocations. (cursor is the exception: it pre-tokenizes the command into
``executableCommands`` for us, so it needs none of this and does not import it.)

Two adapters now need the identical splitter, so it lives here rather than under
either one — importing a private ``_``-prefixed function across adapter modules is
the smell this move removes. The behavior is byte-for-byte the claude original
(claude re-imports the name, so its existing tests and ``test_executor``'s direct
import both keep resolving it); nothing about how evidence is gathered changed.
"""

from __future__ import annotations

import re
import shlex

# Shell statement separators we split a compound command on before looking for
# ``lit`` segments (so ``lit add ... && rm -f tmp`` does not swallow ``rm`` into
# the lit argv, and ``echo x && lit list`` is found correctly).
_CMD_SEP = re.compile(r"&&|\|\||[;|\n]")
# A leading ``VAR=value`` env assignment to skip before the command word.
_ENV_ASSIGN = re.compile(r"^[A-Za-z_]\w*=")


def _lit_calls_from_bash(command: str) -> list[list[str]]:
    """Extract every ``lit`` invocation's argv from a Bash command string.

    Splits the command on shell separators (``&&`` / ``||`` / ``;`` / ``|`` /
    newline), then for each segment skips any leading ``VAR=val`` assignments and,
    if the command word is ``lit`` (or a path ending ``/lit``), captures the rest
    as argv. A single Bash command may issue several ``lit`` calls, so this
    returns a list. Best-effort — used for substring ``ran:`` evidence, not exact
    replay (``$VAR`` stays literal, redirects survive as tokens).
    """
    calls: list[list[str]] = []
    for segment in _CMD_SEP.split(command):
        segment = segment.strip()
        if not segment:
            continue
        try:
            tokens = shlex.split(segment)
        except ValueError:
            tokens = segment.split()
        i = 0
        while i < len(tokens) and _ENV_ASSIGN.match(tokens[i]):
            i += 1
        if i < len(tokens) and (tokens[i] == "lit" or tokens[i].endswith("/lit")):
            calls.append(tokens[i + 1 :])
    return calls
