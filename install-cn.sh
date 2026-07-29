#!/bin/sh
# litman installer (mainland China) — installs uv (if missing), then litman as
# a uv tool. Identical to install.sh except that the three downloads that are
# slow or blocked from mainland China are pointed at reachable mirrors.
#
# Usage:
#   curl -LsSf https://get.litman.dev/install-cn.sh | sh
#
# Idempotent: re-running upgrades an existing install and exits 0. No sudo —
# everything lands under $HOME (uv's default tool location), and uv fetches its
# own Python, so the system Python is never touched.
set -eu

# --- mainland-China download sources -----------------------------------------
# Three things get downloaded: the uv binary, the Python runtime uv manages, and
# the litman wheel. From mainland China the upstream hosts for the first two are
# blocked outright — github.com redirects release assets to
# release-assets.githubusercontent.com, and Astral's own CDN is reset at the TLS
# layer — so each is pointed somewhere reachable.
#
# University mirrors serve both, from inside the country, roughly a hundred
# times faster than anything that crosses the border. They are not ours, though:
# they carry a self-chosen subset of GitHub releases and prune it as they like.
# So when a mirror does not answer, the download falls back to get.litman.dev, a
# Cloudflare Worker that follows GitHub's redirect server-side. Mirrors make it
# fast; the Worker makes it certain.
NJU_PYTHON="https://mirror.nju.edu.cn/github-release/astral-sh/python-build-standalone"
USTC_UV="https://mirrors.ustc.edu.cn/github-release/astral-sh/uv/LatestRelease"
WORKER="https://get.litman.dev"

# A mirror counts as usable if it answers a HEAD within five seconds. The probe
# asks for a directory rather than a file: mirrors prune old releases, and
# pinning a filename would read a routine prune as an outage.
mirror_alive() { curl -fsI -m 5 -o /dev/null "$1" 2>/dev/null; }

if mirror_alive "$NJU_PYTHON/"; then
    UV_PYTHON_INSTALL_MIRROR="$NJU_PYTHON"
else
    UV_PYTHON_INSTALL_MIRROR="$WORKER/gh/astral-sh/python-build-standalone/releases/download"
fi
export UV_PYTHON_INSTALL_MIRROR

# The litman wheel comes from the Tsinghua TUNA PyPI mirror — full automatic
# sync of upstream PyPI, hosted inside China. uv records this index in the tool
# receipt, so `lit self-update` (uv tool upgrade litman) keeps using it too.
UV_DEFAULT_INDEX="https://pypi.tuna.tsinghua.edu.cn/simple"
export UV_DEFAULT_INDEX
# -----------------------------------------------------------------------------

# uv places tool executables here by default (honours XDG_BIN_HOME).
TOOL_BIN="${XDG_BIN_HOME:-$HOME/.local/bin}"

info() { printf '%s\n' "$*"; }

# uv's installer verifies a sha256 pinned per release inside the script itself,
# so the script and the binary it fetches have to come from the same place: an
# upstream script pointed at a mirror that lags a release fails the checksum and
# installs nothing. Each branch below is therefore self-consistent, and clears
# the other branch's variable — UV_DOWNLOAD_URL outranks
# UV_INSTALLER_GITHUB_BASE_URL, so leaving both set produces exactly the broken
# combination.
install_uv_from_mirror() {
    unset UV_INSTALLER_GITHUB_BASE_URL
    UV_DOWNLOAD_URL="$USTC_UV"
    export UV_DOWNLOAD_URL
    curl -LsSf "$USTC_UV/uv-installer.sh" | sh
}

install_uv_from_worker() {
    unset UV_DOWNLOAD_URL
    UV_INSTALLER_GITHUB_BASE_URL="$WORKER/gh"
    export UV_INSTALLER_GITHUB_BASE_URL
    curl -LsSf "https://astral.sh/uv/install.sh" | sh
}

installed_uv=0

if command -v uv >/dev/null 2>&1; then
    info "uv already installed — skipping."
else
    info "Installing uv…"
    if mirror_alive "$USTC_UV/uv-installer.sh"; then
        # Tolerate failure here: the check below decides whether it worked.
        install_uv_from_mirror || true
    fi
    # uv's bin dir is not on PATH until the shell is reopened; prepend it so the
    # rest of THIS script run can call uv and, later, lit.
    PATH="$TOOL_BIN:$PATH"
    export PATH
    # Ask the filesystem rather than an exit code. A piped installer reports the
    # status of `sh`, which happily succeeds on empty input when the download
    # failed, so only the presence of uv proves anything.
    if ! command -v uv >/dev/null 2>&1; then
        install_uv_from_worker
    fi
    installed_uv=1
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
