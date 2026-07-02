"""Thread-safe sharing of ruamel ``YAML`` instances.

A ruamel ``YAML`` object is stateful and **not thread-safe**: ``.load()`` /
``.dump()`` mutate instance-level ``tags`` / ``doc_infos`` and reuse cached
reader / scanner / parser objects across calls (see ``ruamel/yaml/main.py``).
A single ``lit`` CLI process is single-threaded and never trips on this, but the
``lit gui`` server runs its sync request handlers in a threadpool. A module-level
YAML shared across those worker threads can have one request's parse clobber
another's mid-flight, surfacing as a baffling ``AttributeError: 'NoneType'
object has no attribute 'anchor'`` from deep inside ruamel (the parser's state is
reset to end-of-stream by the other thread). Because the same ``core`` /
``commands`` modules are imported by the server, every module-level shared
instance is a latent hazard, not just the one that happened to crash first.

:class:`ThreadLocalYAML` is a drop-in replacement: it hands each thread its own
real ``YAML`` instance (constructed once per thread with identical configuration)
and forwards all attribute access to it. Existing ``_yaml.load(...)`` /
``_yaml.dump(...)`` call sites are unchanged. Within one thread the instance is
reused, so the single-threaded CLI pays no per-call construction cost.
"""

from __future__ import annotations

import threading
from typing import Any

from ruamel.yaml import YAML


class ThreadLocalYAML:
    """A ``YAML``-compatible proxy that is safe to share across threads.

    Pass the same configuration the equivalent ``YAML()`` instance used:

    * ``typ="safe"`` for read-only safe loaders, or
    * ``indent`` / ``preserve_quotes`` / ``default_flow_style`` for the
      comment-preserving round-trip dumpers.

    Each thread lazily builds and caches its own underlying instance.
    """

    def __init__(
        self,
        *,
        typ: str | None = None,
        indent: dict[str, int] | None = None,
        preserve_quotes: bool | None = None,
        default_flow_style: bool | None = None,
    ) -> None:
        self._typ = typ
        self._indent = indent
        self._preserve_quotes = preserve_quotes
        self._default_flow_style = default_flow_style
        self._local = threading.local()

    def _build(self) -> YAML:
        yaml = YAML(typ=self._typ) if self._typ is not None else YAML()
        if self._indent is not None:
            yaml.indent(**self._indent)
        if self._preserve_quotes is not None:
            yaml.preserve_quotes = self._preserve_quotes
        if self._default_flow_style is not None:
            yaml.default_flow_style = self._default_flow_style
        return yaml

    @property
    def _yaml(self) -> YAML:
        instance = getattr(self._local, "yaml", None)
        if instance is None:
            instance = self._build()
            self._local.yaml = instance
        return instance

    def __getattr__(self, name: str) -> Any:
        # Only reached for names not found on the proxy itself (load/dump/...).
        return getattr(self._yaml, name)
