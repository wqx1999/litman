"""Tests for ``litman.core.yaml_pool.ThreadLocalYAML`` — thread-safe sharing of
ruamel YAML instances (a ruamel ``YAML`` object is stateful and not thread-safe,
and the ``lit gui`` server parses metadata from a threadpool)."""

from __future__ import annotations

import io
import threading

from ruamel.yaml import YAML

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
