"""An argument as it can be pasted into a ``lit`` command.

Whoever copies a command from our output may be an agent, whose permission system
reads the command before running it: one token it cannot make sense of can send
the whole command to a sandbox. An id starting with a digit is such a token, and
it is every id litman makes, which is why ``shlex.quote`` (it sees nothing wrong
with ``2009_Smith_x``) is not enough here.
"""

from __future__ import annotations

import re
import shlex
import sys

# Needs quoting. Whitespace and the characters an agent's check refuses bare,
# then those a POSIX shell would act on itself.
_NOT_BARE = re.compile(r"""[\s$@'"(){}\[\];|&><,`*?~#!^]""")

# Means something inside double quotes as well.
_NOT_IN_DOUBLE_QUOTES = re.compile(r"""[$`"!]""")


def _platform() -> str:
    # Read at call time, so a test fakes the platform here instead of
    # patching ``sys.platform`` (the whole interpreter's).
    return sys.platform


def shell_arg(value: object) -> str:
    """``value`` as one token of a command, quoted only if it has to be.

    Bare when it can go bare, so a name reads as itself. Otherwise in double
    quotes, the one form cmd.exe, PowerShell and POSIX shells all read. In single
    quotes when double ones would change it: a ``$`` or a backtick, a ``"`` of its
    own, or a trailing backslash, which on Windows would escape the closing quote.
    """
    text = str(value)
    windows = _platform() == "win32"
    if (
        text
        and not text[0].isdigit()
        and not _NOT_BARE.search(text)
        and (windows or "\\" not in text)
    ):
        return text
    if (
        _NOT_IN_DOUBLE_QUOTES.search(text)
        or text.endswith("\\")
        or (not windows and "\\" in text)
    ):
        if windows:
            return "'" + text.replace("'", "''") + "'"
        return shlex.quote(text)
    return f'"{text}"'
