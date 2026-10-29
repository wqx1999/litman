#!/bin/sh
# litman installer — installs uv (if missing), then litman as a uv tool.
#
# Usage:
#   curl -LsSf https://get.litman.dev/install.sh | sh
#
# Idempotent: re-running upgrades an existing install and exits 0. No sudo —
# everything lands under $HOME (uv's default tool location), and uv fetches its
# own Python, so the system Python is never touched.
set -eu

UV_INSTALLER_URL="https://astral.sh/uv/install.sh"
# uv places tool executables here by default (honours XDG_BIN_HOME).
TOOL_BIN="${XDG_BIN_HOME:-$HOME/.local/bin}"
# PATH as the user's shell has it, before this script prepends anything: it is
# what decides whether `lit` will be found once the script is done.
ORIG_PATH="$PATH"

info() { printf '%s\n' "$*"; }

# Set when uv's own installer ran: it has already put its bin dir on PATH in
# the shell startup files, so that step is skipped below.
uv_installer_ran=0

if command -v uv >/dev/null 2>&1; then
    info "uv already installed — skipping."
else
    info "Installing uv (astral.sh)…"
    # Tolerate failure here: the check below decides whether it worked.
    curl -LsSf "$UV_INSTALLER_URL" | sh || true
    uv_installer_ran=1
    # uv's bin dir is not on PATH until the shell is reopened; prepend it so the
    # rest of THIS script run can call uv and, later, lit.
    PATH="$TOOL_BIN:$PATH"
    export PATH
    # Ask uv itself rather than an exit code. A piped installer reports the
    # status of `sh`, which happily succeeds on empty input when the download
    # failed, so only a uv that runs proves anything.
    if ! { command -v uv >/dev/null 2>&1 && uv --version >/dev/null 2>&1; }; then
        info ""
        info "error: could not install uv."
        info "Install it by hand (https://docs.astral.sh/uv/getting-started/installation/),"
        info "then run this command again."
        exit 1
    fi
fi

# Where uv puts lit: its tool bin dir, which UV_TOOL_BIN_DIR can move. It goes
# on this run's PATH too, ahead of the install (or uv warns that it is missing):
# only a fresh uv install above has put it there, and `lit gui --make-shortcut`
# below finds the program the launcher should start by looking lit up on PATH.
LIT_BIN=$(uv tool dir --bin 2>/dev/null) || LIT_BIN="$TOOL_BIN"
case ":$PATH:" in
    *":$LIT_BIN:"*) ;;
    *) PATH="$LIT_BIN:$PATH"; export PATH ;;
esac

if uv tool list 2>/dev/null | grep -q '^litman'; then
    info "Upgrading litman…"
    uv tool upgrade litman
else
    info "Installing litman…"
    uv tool install litman
fi

# Put that dir on PATH for good when uv came from somewhere else (pip, a package
# manager) and nothing has. `uv tool update-shell` edits the shell startup
# files; it only looks at the PATH it is given, so it gets the user's own —
# handed the one this script extended, it would find the dir already there and
# do nothing. It also fails when the files already carry the line, which is
# fine: either way a new shell will have it.
path_pending=0
case ":$ORIG_PATH:" in
    *":$LIT_BIN:"*) ;;
    *)
        path_pending=1
        if [ "$uv_installer_ran" -eq 0 ]; then
            uv_bin=$(command -v uv)
            if update_out=$(PATH="$ORIG_PATH" "$uv_bin" tool update-shell 2>&1); then
                printf '%s\n' "$update_out"
            fi
        fi ;;
esac

# Resolve how to call lit, falling back to its absolute location should PATH
# still miss it.
if command -v lit >/dev/null 2>&1; then
    lit_cmd="lit"
elif [ -x "$LIT_BIN/lit" ]; then
    lit_cmd="$LIT_BIN/lit"
else
    lit_cmd=""
fi

shortcut_ok=0
if [ -n "$lit_cmd" ]; then
    "$lit_cmd" --version
    # Create the launcher (apps menu / Applications) so litman can be started by
    # double-click — no `lit setup` needed (the app builds the library and picks
    # the agent itself). Best-effort: never fail the install over a shortcut,
    # but say why, or nobody can tell what went wrong.
    if shortcut_out=$("$lit_cmd" gui --make-shortcut 2>&1); then
        shortcut_ok=1
        info "Created a 'litman' launcher you can double-click to start."
    else
        info "note: could not create the launcher; run 'lit gui --make-shortcut' later."
        # Its last line says why, even under a whole Python traceback.
        printf '%s\n' "$shortcut_out" | sed '/^[[:space:]]*$/d' | tail -n 1 | sed 's/^/  /'
    fi
else
    info "warning: could not locate the 'lit' executable to verify it."
fi

info ""
if [ "$path_pending" -eq 1 ]; then
    info "Open a new shell (or 'source' your shell rc) so that 'lit' is on your PATH."
    info "If it still is not found, add $LIT_BIN to your PATH."
fi
if [ "$shortcut_ok" -eq 1 ]; then
    info "Done. Double-click the 'litman' launcher, or run 'lit gui'."
else
    info "Done. Run 'lit gui' to start."
fi
info "(Optional) 'lit setup' adds shell completion and the agent skills."
