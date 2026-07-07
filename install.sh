#!/bin/sh
# litman installer — installs uv (if missing), then litman as a uv tool.
#
# Usage:
#   curl -LsSf https://raw.githubusercontent.com/wqx1999/litman/main/install.sh | sh
#
# Idempotent: re-running upgrades an existing install and exits 0. No sudo —
# everything lands under $HOME (uv's default tool location), and uv fetches its
# own Python, so the system Python is never touched.
set -eu

UV_INSTALLER_URL="https://astral.sh/uv/install.sh"
# uv places tool executables here by default (honours XDG_BIN_HOME).
TOOL_BIN="${XDG_BIN_HOME:-$HOME/.local/bin}"

info() { printf '%s\n' "$*"; }

installed_uv=0

if command -v uv >/dev/null 2>&1; then
    info "uv already installed — skipping."
else
    info "Installing uv (astral.sh)…"
    curl -LsSf "$UV_INSTALLER_URL" | sh
    installed_uv=1
    # uv's bin dir is not on PATH until the shell is reopened; prepend it so the
    # rest of THIS script run can call uv and, later, lit.
    PATH="$TOOL_BIN:$PATH"
    export PATH
fi

if uv tool list 2>/dev/null | grep -q '^litman'; then
    info "Upgrading litman…"
    uv tool upgrade litman
else
    info "Installing litman…"
    uv tool install litman
fi

# Verify the CLI runs. PATH may not include the tool bin dir yet in this run, so
# fall back to its absolute location.
if command -v lit >/dev/null 2>&1; then
    lit --version
elif [ -x "$TOOL_BIN/lit" ]; then
    "$TOOL_BIN/lit" --version
else
    info "warning: could not locate the 'lit' executable to verify it."
fi

info ""
if [ "$installed_uv" -eq 1 ]; then
    info "uv was just installed. Open a new shell (or 'source' your shell rc)"
    info "so that 'lit' is on your PATH."
fi
info "Next step:  lit setup"
