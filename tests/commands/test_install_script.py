"""Tests for the top-level ``install.sh`` one-line installer.

The real installer downloads uv from astral.sh and runs ``uv tool install
litman`` — neither of which may touch the network in the test suite (red line
#5). So we drive ``sh install.sh`` as a subprocess with a stub ``uv`` and stub
``curl`` on ``PATH``:

* the stub ``curl``, when asked for the astral uv-installer URL, emits a snippet
  (consumed by the script's ``| sh``) that drops a stub ``uv`` into
  ``$HOME/.local/bin`` — mimicking what the real uv installer does;
* the stub ``uv`` records its argv and fakes ``uv tool list/install/upgrade``,
  marking litman "installed" via a state file and dropping a stub ``lit`` so the
  script's ``lit --version`` verification succeeds.

We then assert on the recorded argv which branch the script took.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

INSTALL_SH = Path(__file__).resolve().parents[2] / "install.sh"

# Stub ``uv``: records argv to $UV_LOG and fakes the tool subcommands. On
# ``tool install`` it marks litman installed and drops a stub ``lit``.
_UV_STUB = """#!/bin/sh
printf '%s\\n' "$*" >> "$UV_LOG"
case "${1:-}" in
  tool)
    case "${2:-}" in
      list)
        if [ -f "$UV_STATE/litman_installed" ]; then
          printf 'litman v1.1.0\\n- lit\\n'
        fi
        ;;
      install)
        mkdir -p "$UV_STATE" "$HOME/.local/bin"
        : > "$UV_STATE/litman_installed"
        cat > "$HOME/.local/bin/lit" <<'LIT_EOF'
#!/bin/sh
case "$1" in
  --version) printf 'litman 1.1.0\\n' ;;
esac
exit 0
LIT_EOF
        chmod +x "$HOME/.local/bin/lit"
        ;;
      upgrade)
        : ;;
    esac
    ;;
esac
exit 0
"""

# Stub ``curl``: records argv to $CURL_LOG. For the astral uv-installer URL it
# emits a snippet that installs the stub uv (path in $UV_STUB_SRC) under $HOME.
_CURL_STUB = """#!/bin/sh
printf '%s\\n' "$*" >> "$CURL_LOG"
for a in "$@"; do
  case "$a" in
    *astral.sh/uv/install.sh)
      printf 'mkdir -p "$HOME/.local/bin"\\n'
      printf 'cp "%s" "$HOME/.local/bin/uv"\\n' "$UV_STUB_SRC"
      printf 'chmod +x "$HOME/.local/bin/uv"\\n'
      exit 0
      ;;
  esac
done
exit 0
"""

# Stub ``lit`` for the "already installed" scenario (no install runs to drop
# one), placed on PATH so the script's verification finds it.
_LIT_STUB = """#!/bin/sh
case "$1" in
  --version) printf 'litman 1.1.0\\n' ;;
esac
exit 0
"""


def _write_exe(path: Path, body: str) -> Path:
    path.write_text(body)
    path.chmod(0o755)
    return path


def _read(path: Path) -> str:
    return path.read_text() if path.exists() else ""


def _make_env(tmp_path: Path) -> tuple[dict[str, str], dict[str, Path]]:
    """Build a hermetic subprocess env with stub bins on PATH.

    Returns (env, paths) where ``paths`` exposes the log/state files for
    assertions.
    """
    home = tmp_path / "home"
    stub_bin = tmp_path / "stub_bin"
    home.mkdir()
    stub_bin.mkdir()

    uv_src = _write_exe(tmp_path / "uv_stub_impl", _UV_STUB)  # not named "uv"
    uv_log = tmp_path / "uv.log"
    curl_log = tmp_path / "curl.log"
    uv_state = tmp_path / "uv_state"

    _write_exe(stub_bin / "curl", _CURL_STUB)

    env = {
        "HOME": str(home),
        "PATH": f"{stub_bin}:/usr/bin:/bin",
        "UV_LOG": str(uv_log),
        "CURL_LOG": str(curl_log),
        "UV_STATE": str(uv_state),
        "UV_STUB_SRC": str(uv_src),
    }
    paths = {
        "home": home,
        "stub_bin": stub_bin,
        "uv_log": uv_log,
        "curl_log": curl_log,
        "uv_state": uv_state,
    }
    return env, paths


def _run(env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["sh", str(INSTALL_SH)],
        env=env,
        capture_output=True,
        text=True,
    )


def test_no_uv_installs_uv_then_installs_litman(tmp_path: Path) -> None:
    env, paths = _make_env(tmp_path)  # no uv on PATH, litman not installed

    result = _run(env)

    assert result.returncode == 0, result.stderr
    # bootstrapped uv via the astral installer …
    assert "astral.sh/uv/install.sh" in _read(paths["curl_log"])
    # … then did a fresh install (not an upgrade).
    uv_log = _read(paths["uv_log"])
    assert "tool install litman" in uv_log
    assert "tool upgrade" not in uv_log


def test_uv_present_upgrades_litman(tmp_path: Path) -> None:
    env, paths = _make_env(tmp_path)
    # uv already on PATH …
    _write_exe(paths["stub_bin"] / "uv", _UV_STUB)
    # … litman already installed …
    paths["uv_state"].mkdir()
    (paths["uv_state"] / "litman_installed").touch()
    # … and lit already on PATH for the verification step.
    _write_exe(paths["stub_bin"] / "lit", _LIT_STUB)

    result = _run(env)

    assert result.returncode == 0, result.stderr
    uv_log = _read(paths["uv_log"])
    assert "tool upgrade litman" in uv_log
    assert "tool install litman" not in uv_log
    # uv was present, so the astral installer was never fetched.
    assert "astral.sh/uv/install.sh" not in _read(paths["curl_log"])


def test_two_runs_both_exit_zero(tmp_path: Path) -> None:
    env, paths = _make_env(tmp_path)
    _write_exe(paths["stub_bin"] / "uv", _UV_STUB)
    paths["uv_state"].mkdir()
    (paths["uv_state"] / "litman_installed").touch()
    _write_exe(paths["stub_bin"] / "lit", _LIT_STUB)

    first = _run(env)
    second = _run(env)

    assert first.returncode == 0, first.stderr
    assert second.returncode == 0, second.stderr
