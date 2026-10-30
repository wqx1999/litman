"""Tests for ``litman.core.yaml_pool.ThreadLocalYAML`` — thread-safe sharing of
ruamel YAML instances (a ruamel ``YAML`` object is stateful and not thread-safe,
and the ``lit gui`` server parses metadata from a threadpool)."""

from __future__ import annotations

import io
import threading

import pytest
from ruamel.yaml import YAML

from litman.commands import add, modify, project, rename, rm
from litman.commands import config as config_cmd
from litman.core import (
    agent_prefs,
    code,
    project_link,
    ripple,
    supplementary,
    sync,
    trash,
    vault_registry,
)
from litman.core.yaml_pool import ThreadLocalYAML


def test_same_thread_reuses_one_instance() -> None:
    # Within a thread the underlying YAML is built once and reused — the
    # single-threaded CLI pays no per-call construction cost.
    pool = ThreadLocalYAML(typ="safe")
    assert pool._yaml is pool._yaml


def test_distinct_instances_across_threads() -> None:
    # Each thread gets its own underlying instance, so concurrent parses never
    # share the mutable parser/scanner/reader state.
    pool = ThreadLocalYAML(typ="safe")
    main_instance = pool._yaml
    captured: dict[str, object] = {}

    def grab() -> None:
        captured["worker"] = pool._yaml
        captured["reused"] = pool._yaml is pool._yaml

    worker = threading.Thread(target=grab)
    worker.start()
    worker.join()

    assert captured["reused"] is True
    assert captured["worker"] is not main_instance


def test_safe_loader_parses() -> None:
    pool = ThreadLocalYAML(typ="safe")
    assert pool.load("a: 1\nb: [x, y]\n") == {"a": 1, "b": ["x", "y"]}


def test_round_trip_dump_matches_plain_yaml() -> None:
    # The proxy must apply indent / preserve_quotes / default_flow_style exactly
    # like the plain YAML it replaces, so written metadata stays byte-identical.
    pool = ThreadLocalYAML(
        indent={"mapping": 2, "sequence": 4, "offset": 2},
        preserve_quotes=True,
        default_flow_style=False,
    )
    plain = YAML()
    plain.indent(mapping=2, sequence=4, offset=2)
    plain.preserve_quotes = True
    plain.default_flow_style = False

    source = 'name: "quoted"\nitems:\n  - a\n  - b\n'

    pool_buf = io.StringIO()
    pool.dump(pool.load(source), pool_buf)
    plain_buf = io.StringIO()
    plain.dump(plain.load(source), plain_buf)

    assert pool_buf.getvalue() == plain_buf.getvalue()


def test_concurrent_load_does_not_corrupt() -> None:
    # The reason this class exists: many threads sharing one pool must never trip
    # the ruamel "AttributeError: 'NoneType' has no attribute 'anchor'" race that
    # a shared module-level YAML instance suffered.
    pool = ThreadLocalYAML(typ="safe")
    source = "root:\n" + "".join(f"  k{i}: v{i}\n" for i in range(40))
    errors: list[str] = []
    barrier = threading.Barrier(8)

    def worker() -> None:
        barrier.wait()
        for _ in range(100):
            try:
                pool.load(source)
            except Exception as exc:  # record any escape
                errors.append(f"{type(exc).__name__}: {exc}")

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert errors == []


# Long strings, by what decides how ruamel writes them: plain, or in double
# quotes (a line break; an apostrophe where a plain scalar cannot go; a tab),
# and whether the double-quoted ones hold an escape. The double quotes are the
# control: their escape kept its continuation mark wherever it was folded.
LONG_STRINGS = {
    "one line, LaTeX": ("We fit $\\Delta G$ against $\\log K_d$ per peptide ", 1),
    "one line, apostrophe": ("the peptide's helix holds at room temperature ", 1),
    "one line, apostrophe, colon, LaTeX": ("Peptides' helix: $\\alpha$ ", 1),
    "one line, apostrophe, colon, double quotes": (
        'Peptides\' helix: the "open" state ',
        1,
    ),
    "one line, tab": ("| step\t| value\t| what it means\t", 1),
    "lines": ("the binding energy is computed per replica ", 2),
    "lines, apostrophe": ("the peptide's helix holds at room temperature ", 2),
    "lines, LaTeX": ("We fit $\\Delta G$ against $\\log K_d$ per peptide ", 2),
    "lines, double quotes": ('the so-called "open" state is short-lived ', 2),
}

# Every writer litman has, and a new one given no width.
WRITERS = {
    "add": add._yaml,
    "agent_prefs": agent_prefs._yaml,
    "code": code._yaml,
    "config": config_cmd._yaml,
    "modify": modify._yaml,
    "project": project._yaml,
    "project_link": project_link._yaml,
    "rename": rename._yaml,
    "ripple": ripple._yaml,
    "rm": rm._yaml,
    "supplementary": supplementary._yaml_rt,
    "sync": sync._yaml,
    "trash": trash._yaml_rt,
    "trash sidecar": trash._yaml_dump,
    "vault_registry": vault_registry._yaml,
    "new": ThreadLocalYAML(
        indent={"mapping": 2, "sequence": 4, "offset": 2},
        preserve_quotes=True,
        default_flow_style=False,
    ),
}


def long_strings(phrase: str, lines: int) -> list[str]:
    """``lines`` lines repeating ``phrase``, once per shift of it, so a line
    folded at a fixed column breaks next to every character of it."""
    return [
        "\n".join(("x" * shift + phrase * 5).rstrip() for _ in range(lines))
        for shift in range(len(phrase))
    ]


@pytest.mark.parametrize("kind", list(LONG_STRINGS))
def test_every_writer_reads_back_a_long_string_as_written(kind: str) -> None:
    """ruamel folded a long string at 80 columns, and a double-quoted one
    folded right after an escape read back with a space added: `$\\Delta`
    as `$\\ Delta`, a tab with a space after it."""
    values = {
        f"k{i:03d}": value
        for i, value in enumerate(long_strings(*LONG_STRINGS[kind]))
    }
    changed = []
    for name, writer in WRITERS.items():
        buf = io.StringIO()
        writer.dump(values, buf)
        if YAML(typ="safe").load(buf.getvalue()) != values:
            changed.append(name)

    assert changed == []


def test_a_new_writer_does_not_fold_past_4096_columns_either() -> None:
    """A width of 4096 still folded a longer string, and broke it as 80 did."""
    phrase, _ = LONG_STRINGS["lines, LaTeX"]
    values = {
        f"k{shift:02d}": ("x" * shift + phrase * 100 + "\n") * 2
        for shift in range(len(phrase))
    }
    buf = io.StringIO()
    WRITERS["new"].dump(values, buf)

    assert YAML(typ="safe").load(buf.getvalue()) == values
