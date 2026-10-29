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

# uv is also published on PyPI, as wheels that carry the uv binary as-is — and
# the PyPI mirrors inside China hold every release in full. They come first:
# a wheel is a zip, so getting uv out of one needs no Python and no installer
# script. Both mirrors serve files at PyPI's own paths.
PYPI_MIRRORS="https://pypi.tuna.tsinghua.edu.cn https://mirrors.aliyun.com/pypi"
UV_VERSION="0.13.6"

# A mirror counts as usable if it answers a HEAD within ten seconds. The probe
# asks for a directory rather than a file: mirrors prune old releases, and
# pinning a filename would read a routine prune as an outage.
mirror_alive() { curl -fsI -m 10 -o /dev/null "$1" 2>/dev/null; }

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
# PATH as the user's shell has it, before this script prepends anything: it is
# what decides whether `lit` will be found once the script is done.
ORIG_PATH="$PATH"

info() { printf '%s\n' "$*"; }

# The uv wheel for this machine, as "<path on the mirror> <sha256>". Pinned
# rather than looked up: the version listing is megabytes long and sorts as text
# (0.9.9 after 0.13.6), and a checksum kept here — not one the mirror publishes
# beside its own file — is what makes the download trustworthy. PyPI never
# deletes a release, so a pinned wheel stays fetchable; bump UV_VERSION and every
# line below together (paths and digests from https://pypi.org/pypi/uv/<ver>/json).
# Anything not listed skips the PyPI mirrors and uses uv's own installer.
uv_wheel() {
    case "$(uname -s)/$(uname -m)" in
        Linux/x86_64)
            if is_musl; then
                echo "packages/3a/10/7a4c287b1d377353c939f451881529528c78815d57f36097a6607187ab1a/uv-0.13.6-py3-none-musllinux_1_1_x86_64.whl d1efed1d15102ffdbf5e0fd49a440643dda2d921a2ad856792ae42ccd43c253f"
            else
                echo "packages/5c/43/0688ff5142934238f2812e348f4a24e09b57d48bfb38dd8f850325a0d9de/uv-0.13.6-py3-none-manylinux_2_17_x86_64.manylinux2014_x86_64.whl 22759b8895e1103578de6fb4cd27e94b4399d021023378d29f01333717751fba"
            fi ;;
        # One wheel for both C libraries: its binary is static.
        Linux/aarch64)
            echo "packages/05/3b/f78583af6504f1270da95fd0d0935e381f58330c007260f450e81a1e5e1b/uv-0.13.6-py3-none-manylinux_2_17_aarch64.manylinux2014_aarch64.musllinux_1_1_aarch64.whl 7ec5486476d67a4214c460b5492393ee2300d84e1de17b293100a019b413568b" ;;
        Darwin/x86_64)
            echo "packages/33/55/ff10d3f756c39a239e492e57262bd22fcda412a9f85a268175447cf33b09/uv-0.13.6-py3-none-macosx_10_12_x86_64.whl 95f042b95e67ef8c3d3569bb6e9d895e592002c5e871cf3f1afe407c9d77a294" ;;
        Darwin/arm64)
            echo "packages/f4/14/77230cc4f6a73a31578cb8070a5f67f58a76946ead643ae8a4155620d6da/uv-0.13.6-py3-none-macosx_11_0_arm64.whl 51361029b7cf91185d0092c4b356d110b4ad629e9cb11593f1e006ac72a72ca9" ;;
        *)
            return 1 ;;
    esac
}

is_musl() {
    ls /lib/ld-musl-* >/dev/null 2>&1 && return 0
    { ldd --version 2>&1 || true; } | grep -qi musl
}

sha256_of() {
    if command -v sha256sum >/dev/null 2>&1; then
        sha256sum "$1" | cut -d' ' -f1
    elif command -v shasum >/dev/null 2>&1; then
        shasum -a 256 "$1" | cut -d' ' -f1
    else
        return 1
    fi
}

# Getting uv out of a wheel takes a checksum tool and something to unzip with;
# without both, the PyPI mirrors are skipped rather than downloaded from in vain.
wheel_tools() {
    { command -v sha256sum >/dev/null 2>&1 || command -v shasum >/dev/null 2>&1; } &&
        { command -v unzip >/dev/null 2>&1 || command -v python3 >/dev/null 2>&1; }
}

# Extract the named members of a zip into a directory, keeping their paths.
unzip_members() {
    zip=$1
    dest=$2
    shift 2
    if command -v unzip >/dev/null 2>&1; then
        unzip -q -o "$zip" "$@" -d "$dest"
    elif command -v python3 >/dev/null 2>&1; then
        python3 -c 'import sys, zipfile; zipfile.ZipFile(sys.argv[1]).extractall(sys.argv[2], sys.argv[3:])' \
            "$zip" "$dest" "$@"
    else
        return 1
    fi
}

# One attempt at one PyPI mirror. A subshell, so the temporary directory is
# always removed and a failure ends only this attempt. Every step checks its own
# result: `set -e` is switched off inside anything called from an `if`, so it
# would let a failed download or a checksum mismatch run on. uv reaches TOOL_BIN
# only after it has run and printed the pinned version — a half-written file
# must never look like an install.
install_uv_from_pypi() (
    mirror=$1
    path=$2
    sha=$3
    tmp=$(mktemp -d) || exit 1
    trap 'rm -rf "$tmp"' EXIT
    curl -fsSL --connect-timeout 10 --speed-limit 1024 --speed-time 30 \
        -o "$tmp/uv.whl" "$mirror/$path" || exit 1
    got=$(sha256_of "$tmp/uv.whl") || { info "  (no sha256sum or shasum to check the download)"; exit 1; }
    [ "$got" = "$sha" ] || { info "  (checksum mismatch)"; exit 1; }
    scripts="uv-$UV_VERSION.data/scripts"
    unzip_members "$tmp/uv.whl" "$tmp/x" "$scripts/uv" "$scripts/uvx" \
        || { info "  (no unzip or python3 to unpack the download)"; exit 1; }
    chmod 755 "$tmp/x/$scripts/uv" "$tmp/x/$scripts/uvx" || exit 1
    case "$("$tmp/x/$scripts/uv" --version 2>/dev/null)" in
        "uv $UV_VERSION"|"uv $UV_VERSION "*) ;;
        *) info "  (the downloaded uv does not run here)"; exit 1 ;;
    esac
    mkdir -p "$TOOL_BIN" || exit 1
    for f in uv uvx; do
        cp "$tmp/x/$scripts/$f" "$TOOL_BIN/.$f.new" && mv -f "$TOOL_BIN/.$f.new" "$TOOL_BIN/$f" || exit 1
    done
)

# uv's installer verifies a sha256 pinned per release inside the script itself,
# so the script and the binary it fetches have to come from the same place: an
# upstream script pointed at a mirror that lags a release fails the checksum and
# installs nothing. Each branch below is therefore self-consistent, and clears
# the other branch's variable — UV_DOWNLOAD_URL outranks
# UV_INSTALLER_GITHUB_BASE_URL, so leaving both set produces exactly the broken
# combination.
#
# The installer runs with the user's own PATH: it adds its bin dir to the shell
# startup files only when that dir is missing from the PATH it sees, and by now
# this script has prepended it.
install_uv_from_mirror() {
    unset UV_INSTALLER_GITHUB_BASE_URL
    UV_DOWNLOAD_URL="$USTC_UV"
    export UV_DOWNLOAD_URL
    curl -LsSf "$USTC_UV/uv-installer.sh" | PATH="$ORIG_PATH" sh
}

install_uv_from_worker() {
    unset UV_DOWNLOAD_URL
    UV_INSTALLER_GITHUB_BASE_URL="$WORKER/gh"
    export UV_INSTALLER_GITHUB_BASE_URL
    curl -LsSf "https://astral.sh/uv/install.sh" | PATH="$ORIG_PATH" sh
}

# Ask uv itself rather than an exit code. A piped installer reports the status
# of `sh`, which happily succeeds on empty input when the download failed, so
# only a uv that runs proves anything.
uv_works() { command -v uv >/dev/null 2>&1 && uv --version >/dev/null 2>&1; }

installed_uv=0
# Set when uv's own installer ran: it has already put its bin dir on PATH in
# the shell startup files, so that step is skipped below.
uv_installer_ran=0

if command -v uv >/dev/null 2>&1; then
    info "uv already installed — skipping."
else
    info "Installing uv…"
    # uv's bin dir is not on PATH until the shell is reopened; prepend it so the
    # rest of THIS script run can call uv and, later, lit.
    PATH="$TOOL_BIN:$PATH"
    export PATH
    if wheel=$(uv_wheel) && wheel_tools; then
        for mirror in $PYPI_MIRRORS; do
            info "  from $mirror"
            if install_uv_from_pypi "$mirror" "${wheel% *}" "${wheel#* }" && uv_works; then
                installed_uv=1
                break
            fi
            info "  failed — trying the next source."
        done
    fi
    if [ "$installed_uv" -eq 0 ] && mirror_alive "$USTC_UV/uv-installer.sh"; then
        info "  from $USTC_UV"
        # Tolerate failure here: uv_works decides whether it worked.
        install_uv_from_mirror || true
        uv_installer_ran=1
        uv_works && installed_uv=1
    fi
    if [ "$installed_uv" -eq 0 ]; then
        info "  from $WORKER"
        install_uv_from_worker || true
        uv_installer_ran=1
        uv_works && installed_uv=1
    fi
    if [ "$installed_uv" -eq 0 ]; then
        info ""
        info "error: could not download uv from any source."
        info "Install uv by hand, then run this command again:"
        info "  - with Python:  pip install uv -i https://pypi.tuna.tsinghua.edu.cn/simple"
        info "  - or download   https://pypi.tuna.tsinghua.edu.cn/simple/uv/  (pick the wheel"
        info "    for your system; it is a zip) and copy uv and uvx from its"
        info "    uv-<version>.data/scripts/ folder into $TOOL_BIN"
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

# Put that dir on PATH for good when nothing else has: uv came from somewhere
# else (pip, a package manager), or out of a wheel above. `uv tool update-shell`
# edits the shell startup files; it only looks at the PATH it is given, so it
# gets the user's own — handed the one this script extended, it would find the
# dir already there and do nothing. It also fails when the files already carry
# the line, which is fine: either way a new shell will have it.
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
