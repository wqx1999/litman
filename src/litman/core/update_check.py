"""PyPI update-check core.

A silent, cached, non-blocking probe of the newest litman release on PyPI. The
result feeds three surfaces: the interactive CLI nudge (``cli.py``), the
``lit self-update`` command, and the webUI version badge (``GET /api/version``).

Discipline baked in (task-self-update red lines):

* **Standard library only** — ``urllib`` for the HTTP GET, no new dependency.
* **Zero telemetry** — a plain GET of the public PyPI JSON; nothing is uploaded.
* **Never blocks / never raises** — the network fetch has a ≤2s timeout and any
  failure (offline, timeout, malformed JSON) resolves to ``None`` silently, not
  even a warning.
* **Cached with a 24h TTL** — a network request fires only when the cache is
  stale; every other read touches the local JSON file. The attempt timestamp is
  recorded even when the fetch fails, so an offline box does not re-hit the
  network on every command.
* **Opt-out** — ``LITMAN_NO_UPDATE_CHECK=1`` skips everything, including reading
  the cache.

The cache lives next to the vault registry (``<registry_dir>/update-check.json``)
and honours ``$LITMAN_REGISTRY_DIR`` through :func:`vault_registry.registry_path`,
so tests isolate it via the same env override the registry uses.
"""

from __future__ import annotations

import json
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import litman
from litman.core.vault_registry import registry_path

PYPI_JSON_URL = "https://pypi.org/pypi/litman/json"

# 24h TTL for both the network refresh and the per-nudge frequency cap.
REFRESH_TTL_SECONDS = 24 * 3600
NUDGE_TTL_SECONDS = 24 * 3600

# Hard ceiling on the PyPI GET so the CLI nudge can never stall a command.
NETWORK_TIMEOUT_SECONDS = 2.0

OPT_OUT_ENV = "LITMAN_NO_UPDATE_CHECK"

CACHE_FILENAME = "update-check.json"


def opt_out() -> bool:
    """True when ``LITMAN_NO_UPDATE_CHECK=1`` disables the whole feature."""
    import os

    return os.environ.get(OPT_OUT_ENV, "").strip() == "1"


def cache_path() -> Path:
    """Path to the update-check cache file, beside the vault registry.

    Honours ``$LITMAN_REGISTRY_DIR`` because it is derived from
    ``registry_path().parent`` — so a test that redirects the registry also
    redirects this cache.
    """
    return registry_path().parent / CACHE_FILENAME


def _current_version() -> str:
    # Read through the package object (not a bound import) so a test can
    # monkeypatch ``litman.__version__`` to drive the comparison.
    return litman.__version__


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _parse_version(v: str) -> tuple[int, ...]:
    """Split ``"1.2.10"`` into ``(1, 2, 10)``; non-numeric suffixes stop a part.

    litman ships plain ``X.Y.Z`` releases, so a numeric-tuple compare is enough
    (no PEP 440 pre/post/dev ordering). A trailing non-digit (``"1.2.0rc1"``)
    truncates that segment to its leading digits rather than raising.
    """
    parts: list[int] = []
    for chunk in v.strip().split("."):
        digits = ""
        for ch in chunk:
            if ch.isdigit():
                digits += ch
            else:
                break
        parts.append(int(digits) if digits else 0)
    return tuple(parts)


def is_newer(latest: str, current: str) -> bool:
    """True iff ``latest`` is a strictly higher version than ``current``."""
    try:
        a = _parse_version(latest)
        b = _parse_version(current)
    except (AttributeError, TypeError):
        return False
    width = max(len(a), len(b))
    a = a + (0,) * (width - len(a))
    b = b + (0,) * (width - len(b))
    return a > b


def _is_stale(ts: Any, *, now: datetime, ttl_seconds: int) -> bool:
    """True when ISO timestamp ``ts`` is older than ``ttl_seconds`` before ``now``.

    A missing / unparseable timestamp counts as stale (so the refresh / nudge
    fires). A legacy naive string is assumed UTC rather than raising.
    """
    if not isinstance(ts, str) or not ts:
        return True
    try:
        parsed = datetime.fromisoformat(ts)
    except (ValueError, TypeError):
        return True
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return (now - parsed).total_seconds() > ttl_seconds


def read_cache() -> dict[str, Any] | None:
    """Return the parsed cache dict, or ``None`` when absent / unreadable."""
    try:
        raw = json.loads(cache_path().read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return raw if isinstance(raw, dict) else None


def _write_cache(data: dict[str, Any]) -> None:
    """Persist ``data`` atomically (tmp + replace) beside the registry."""
    path = cache_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data), encoding="utf-8")
    tmp.replace(path)


def _fetch_latest_version(*, timeout: float = NETWORK_TIMEOUT_SECONDS) -> str | None:
    """Plain GET of the public PyPI JSON → ``info.version``.

    Any failure (network down, timeout, non-200, malformed body) returns
    ``None`` — never raises, never prints. This is the ONLY function that
    touches the network.
    """
    try:
        with urllib.request.urlopen(PYPI_JSON_URL, timeout=timeout) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
        version = payload["info"]["version"]
        return version if isinstance(version, str) else None
    except Exception:
        return None


def refresh_cache_if_stale(*, now: datetime | None = None) -> None:
    """Fire at most one PyPI request when the cache is older than the TTL.

    No-op when opted out or when the cache is fresh. On any outcome (success or
    failure) the attempt timestamp is recorded so the 24h TTL also throttles a
    failed fetch — an offline machine will not re-hit the network every command.
    Fully silent: a write failure is swallowed just like a fetch failure.
    """
    if opt_out():
        return
    now = now or _utcnow()
    cache = read_cache()
    if cache is not None and not _is_stale(
        cache.get("checked_at"), now=now, ttl_seconds=REFRESH_TTL_SECONDS
    ):
        return

    latest = _fetch_latest_version()
    updated = dict(cache or {})
    updated["checked_at"] = now.isoformat()
    if latest is not None:
        updated["latest"] = latest
    try:
        _write_cache(updated)
    except OSError:
        pass


def available_update(*, current: str | None = None) -> tuple[str, str] | None:
    """``(current, latest)`` when the cache shows a newer release, else ``None``.

    Pure read (never fetches). Returns ``None`` when opted out, when there is no
    cache, or when the cached latest is not strictly newer than ``current``.
    Used by the webUI version route.
    """
    if opt_out():
        return None
    cache = read_cache()
    if not cache:
        return None
    latest = cache.get("latest")
    current = current or _current_version()
    if isinstance(latest, str) and is_newer(latest, current):
        return current, latest
    return None


def consume_nudge(
    *, now: datetime | None = None, current: str | None = None
) -> tuple[str, str] | None:
    """``(current, latest)`` when a CLI nudge is due, stamping ``last_nudged_at``.

    Due = not opted out, the cache shows a newer release, and the last nudge was
    more than :data:`NUDGE_TTL_SECONDS` ago (or never). Records the nudge so the
    caller prints at most once per 24h. Returns ``None`` when no nudge is due;
    reads the (possibly-freshened) cache but never fetches.
    """
    if opt_out():
        return None
    now = now or _utcnow()
    cache = read_cache()
    if not cache:
        return None
    latest = cache.get("latest")
    current = current or _current_version()
    if not isinstance(latest, str) or not is_newer(latest, current):
        return None
    if not _is_stale(
        cache.get("last_nudged_at"), now=now, ttl_seconds=NUDGE_TTL_SECONDS
    ):
        return None
    cache["last_nudged_at"] = now.isoformat()
    try:
        _write_cache(cache)
    except OSError:
        return None
    return current, latest
