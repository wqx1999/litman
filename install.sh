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

# Resolve how to call lit (PATH may not include the tool bin dir yet this run).
if command -v lit >/dev/null 2>&1; then
    lit_cmd="lit"
elif [ -x "$TOOL_BIN/lit" ]; then
    lit_cmd="$TOOL_BIN/lit"
else
    lit_cmd=""
fi

if [ -n "$lit_cmd" ]; then
    "$lit_cmd" --version
    # Create the launcher (apps menu / Applications) so litman can be started by
    # double-click — no `lit setup` needed (the app builds the library and picks
    # the agent itself). Best-effort: never fail the install over a shortcut.
    if "$lit_cmd" gui --make-shortcut >/dev/null 2>&1; then
        info "Created a 'litman' launcher you can double-click to start."
    else
        info "note: could not create the launcher; run 'lit gui --make-shortcut' later."
    fi
else
    info "warning: could not locate the 'lit' executable to verify it."
fi

info ""
if [ "$installed_uv" -eq 1 ]; then
    info "uv was just installed. Open a new shell (or 'source' your shell rc)"
    info "so that 'lit' is on your PATH."
fi
info "Done. Double-click the 'litman' launcher, or run 'lit gui'."
info "(Optional) 'lit setup' adds shell completion and the agent skills."
