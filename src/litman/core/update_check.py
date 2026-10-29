"""PyPI update-check core.

A silent, cached, non-blocking probe of the newest litman release. The result
feeds three surfaces: the interactive CLI nudge (``cli.py``), the
``lit self-update`` command, and the webUI version badge (``GET /api/version``).

Where it asks: the package index this copy was installed from, when uv recorded
one (``uv-receipt.toml`` beside the tool's interpreter — the China installers
pin the Tsinghua mirror), else the public PyPI JSON. Asking the install index
means a release is only announced once ``lit self-update`` can fetch it, and
mainland users are not left asking pypi.org, which often does not answer there
in time.

Discipline baked in (task-self-update red lines):

* **Standard library only** — ``urllib`` for the HTTP GET, no new dependency.
* **Zero telemetry** — a plain GET of a public index page; nothing is uploaded.
* **Never blocks / never raises** — the CLI's fetch has a ≤2s timeout (the
  GUI's runs off-thread and may wait longer) and any failure (offline, timeout,
  malformed body) resolves to ``None`` silently, not even a warning.
* **Cached with a 24h TTL** — a network request fires only when the cache is
  stale; every other read touches the local JSON file. A failed attempt is
  recorded too (``failed``), so an offline box does not re-hit the network on
  every command; the GUI retries it after an hour, the CLI after the full TTL.
* **Opt-out** — ``LITMAN_NO_UPDATE_CHECK=1`` skips everything, including reading
  the cache.

The cache lives next to the vault registry (``<registry_dir>/update-check.json``)
and honours ``$LITMAN_REGISTRY_DIR`` through :func:`vault_registry.registry_path`,
so tests isolate it via the same env override the registry uses.
"""

from __future__ import annotations

import json
import re
import sys
import tomllib
import urllib.request
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import litman
from litman.core.vault_registry import registry_path

PYPI_JSON_URL = "https://pypi.org/pypi/litman/json"

# 24h TTL for both the network refresh and the per-nudge frequency cap.
REFRESH_TTL_SECONDS = 24 * 3600
NUDGE_TTL_SECONDS = 24 * 3600

# Hard ceiling on the GET so the CLI nudge can never stall a command.
NETWORK_TIMEOUT_SECONDS = 2.0

# The GUI asks from a background thread, so it can afford a slow link (a
# mainland user reaching pypi.org), and retries a failure sooner than the CLI.
GUI_NETWORK_TIMEOUT_SECONDS = 10.0
GUI_RETRY_SECONDS = 3600

# Written by `uv tool install` into the tool's environment root (= sys.prefix of
# the interpreter litman runs on), on every platform.
UV_RECEIPT_FILENAME = "uv-receipt.toml"

# The Simple API page lists files; JSON (PEP 691) when the index speaks it,
# HTML otherwise (Aliyun answers HTML only).
_SIMPLE_ACCEPT = "application/vnd.pypi.simple.v1+json, text/html;q=0.1"
_RELEASE_RE = re.compile(r"\d+(?:\.\d+)*")

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


def _tmp_path(path: Path) -> Path:
    return path.with_suffix(path.suffix + ".tmp")


def cache_files() -> list[Path]:
    """The cache, and the temp file a write killed mid-way left beside it —
    those that exist. What ``lit uninstall`` removes."""
    path = cache_path()
    return [p for p in (path, _tmp_path(path)) if p.is_file()]


def remove_cache() -> dict[str, object]:
    """Delete :func:`cache_files`, and the config dir too once it is empty.

    Counterpart of :func:`_write_cache` for ``lit uninstall``. Returns
    ``{"path", "removed", "dir_removed"}``; ``removed`` lists the files
    actually deleted.
    """
    path = cache_path()
    removed: list[Path] = []
    for target in (path, _tmp_path(path)):
        try:
            target.unlink()
        except FileNotFoundError:
            continue
        removed.append(target)
    dir_removed = False
    parent = path.parent
    try:
        if parent.is_dir() and not any(parent.iterdir()):
            parent.rmdir()
            dir_removed = True
    except OSError:
        pass
    return {"path": path, "removed": removed, "dir_removed": dir_removed}


def _current_version() -> str:
    # Read through the package object (not a bound import) so a test can
    # monkeypatch ``litman.__version__`` to drive the comparison.
    return litman.__version__


def _utcnow() -> datetime:
    return datetime.now(UTC)


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
        parsed = parsed.replace(tzinfo=UTC)
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
    tmp = _tmp_path(path)
    tmp.write_text(json.dumps(data), encoding="utf-8")
    tmp.replace(path)


def uv_receipt_path() -> Path:
    """Where uv keeps this installation's receipt, if uv installed it."""
    return Path(sys.prefix) / UV_RECEIPT_FILENAME


def install_index() -> str | None:
    """The default package index uv installed litman from, or ``None``.

    ``None`` when litman was not installed by ``uv tool`` (pipx, pip, a dev
    checkout), when the install named no index (plain PyPI), or when the
    receipt cannot be read. Never raises.
    """
    try:
        with uv_receipt_path().open("rb") as f:
            options = tomllib.load(f)["tool"]["options"]
        for entry in options.get("index", []):
            if entry.get("default") and isinstance(entry.get("url"), str):
                return str(entry["url"])
        legacy = options.get("index-url")  # receipts from uv before `index`
        return legacy if isinstance(legacy, str) else None
    except Exception:
        return None


def _version_of(filename: str) -> str | None:
    """``litman-1.3.9-py3-none-any.whl`` / ``litman-1.3.9.tar.gz`` → ``1.3.9``.

    Only plain ``X.Y.Z`` releases count — what PyPI's ``info.version`` reports
    too — so a pre-release is never announced.
    """
    name = filename.lower()
    if not name.startswith("litman-"):
        return None
    rest = name[len("litman-"):]
    if rest.endswith(".whl"):
        version = rest.split("-", 1)[0]
    else:
        version = rest.removesuffix(".tar.gz").removesuffix(".zip")
    return version if _RELEASE_RE.fullmatch(version) else None


def _simple_page_versions(body: bytes) -> list[str]:
    """Release versions on a Simple API project page, yanked files left out."""
    text = body.decode("utf-8")
    if text.lstrip().startswith("{"):
        files = json.loads(text).get("files", [])
        names = [f["filename"] for f in files if not f.get("yanked")]
    else:
        names = [
            m.group(2)
            for m in re.finditer(r"<a\b([^>]*)>([^<]+)</a>", text)
            if "data-yanked" not in m.group(1)
        ]
    return [v for v in map(_version_of, names) if v is not None]


def _fetch_from_index(index: str, *, timeout: float) -> str | None:
    request = urllib.request.Request(
        index.rstrip("/") + "/litman/", headers={"Accept": _SIMPLE_ACCEPT}
    )
    with urllib.request.urlopen(request, timeout=timeout) as resp:
        versions = _simple_page_versions(resp.read())
    return max(versions, key=_parse_version) if versions else None


def _fetch_latest_version(*, timeout: float = NETWORK_TIMEOUT_SECONDS) -> str | None:
    """The newest release on the install index (:func:`install_index`), else
    on PyPI (``info.version`` of its JSON).

    Only the install index is asked when there is one: a release it does not
    have yet is one ``lit self-update`` could not install.

    Any failure (network down, timeout, non-200, malformed body) returns
    ``None`` — never raises, never prints. This is the ONLY function that
    touches the network.
    """
    try:
        index = install_index()
        if index is not None:
            return _fetch_from_index(index, timeout=timeout)
        with urllib.request.urlopen(PYPI_JSON_URL, timeout=timeout) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
        version = payload["info"]["version"]
        return version if isinstance(version, str) else None
    except Exception:
        return None


def refresh_cache_if_stale(
    *,
    now: datetime | None = None,
    timeout: float = NETWORK_TIMEOUT_SECONDS,
    retry_after: int = REFRESH_TTL_SECONDS,
) -> None:
    """Fire at most one request when the cache is older than the TTL.

    No-op when opted out or when the cache is fresh. On any outcome the attempt
    timestamp is recorded; a failure also sets ``failed``, and is retried once
    ``retry_after`` seconds have passed instead of the 24h TTL. The CLI keeps
    the default (an offline machine does not re-hit the network every command);
    the GUI's background thread passes an hour. Fully silent: a write failure
    is swallowed just like a fetch failure.
    """
    if opt_out():
        return
    now = now or _utcnow()
    cache = read_cache()
    if cache is not None:
        ttl = retry_after if cache.get("failed") else REFRESH_TTL_SECONDS
        if not _is_stale(cache.get("checked_at"), now=now, ttl_seconds=ttl):
            return

    latest = _fetch_latest_version(timeout=timeout)
    updated = dict(cache or {})
    updated["checked_at"] = now.isoformat()
    if latest is not None:
        updated["latest"] = latest
        updated.pop("failed", None)
    else:
        updated["failed"] = True
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
