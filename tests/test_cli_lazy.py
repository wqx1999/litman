"""Part B guard: ``LitGroup`` lazy-loads its subcommands.

Importing ``litman.cli`` must not import any command module or the heavy libs
they drag in (``lit gui`` never needs pypdf / httpx / fastapi). ``get_command``
resolves a command — and its import chain — only on first use, and
``list_commands`` enumerates everything without importing anything.
"""

from __future__ import annotations

import importlib
import subprocess
import sys


def test_importing_cli_defers_command_modules() -> None:
    # B4, run hermetically in a fresh interpreter so the assertion is immune to
    # whatever this pytest session already imported: `import litman.cli` must
    # pull in NO command module (nor the pypdf/httpx/fastapi they drag in), and
    # resolving `add` must then pull in exactly that command's chain (pypdf and
    # all). In-process module surgery cannot prove the second half — the
    # intermediate litman.core modules stay cached, so a re-import of `add`
    # would not re-trigger pypdf.
    code = (
        "import sys, click;"
        "import litman.cli as c;"
        "heavy=('litman.commands.add','pypdf','httpx','fastapi',"
        "'litman.commands.gui','litman.server');"
        "pre=[m for m in heavy if m in sys.modules];"
        "assert not pre, 'imported just by `import litman.cli`: '+repr(pre);"
        "ctx=click.Context(c.cli);"
        "cmd=c.cli.get_command(ctx,'add');"
        "assert cmd is not None and cmd.name=='add';"
        "assert 'litman.commands.add' in sys.modules, 'add not resolved';"
        "assert 'pypdf' in sys.modules, 'pypdf should ride in with add';"
        "print('ok')"
    )
    result = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True
    )
    assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"


def test_list_commands_enumerates_without_importing() -> None:
    # Enumerating names must import nothing: list_commands is the union of the
    # static _LAZY table and Click's own registry (help/hello). Run in a fresh
    # interpreter so "still nothing imported" is a real check, not a leftover of
    # earlier tests.
    code = (
        "import sys, click;"
        "import litman.cli as c;"
        "ctx=click.Context(c.cli);"
        "names=set(c.cli.list_commands(ctx));"
        "want={'add','gui','help','hello','self-update','pdf-text'};"
        "assert want <= names, 'missing: '+repr(want-names);"
        "leaked=[m for m in ('litman.commands.add','litman.commands.gui') "
        "if m in sys.modules];"
        "assert not leaked, 'list_commands imported: '+repr(leaked);"
        "print('ok')"
    )
    result = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True
    )
    assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"


def test_lazy_table_covers_every_workflow_section_command() -> None:
    # Guard against a command being added to _COMMAND_SECTIONS but forgotten in
    # _LAZY (it would then 404 at dispatch). help/hello are decorator-only.
    cli_mod = importlib.import_module("litman.cli")
    sectioned = {
        name
        for _title, names in cli_mod._COMMAND_SECTIONS
        for name in names
    }
    decorator_only = {"help", "hello"}
    assert sectioned - decorator_only <= set(cli_mod.LitGroup._LAZY)
