# litman installer (Windows, mainland China) — installs uv (if missing), then
# litman as a uv tool. Identical to install.ps1 except that the downloads that
# are slow or blocked from mainland China are pointed at reachable mirrors.
#
# Usage:
#   powershell -ExecutionPolicy ByPass -c "irm https://get.litman.dev/install-cn.ps1 | iex"
#
# Idempotent: re-running upgrades an existing install. No admin rights —
# everything lands under your user profile (uv's default tool location), and uv
# fetches its own Python, so the system Python is never touched.
$ErrorActionPreference = "Stop"

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
$NjuPython = "https://mirror.nju.edu.cn/github-release/astral-sh/python-build-standalone"
$UstcUv    = "https://mirrors.ustc.edu.cn/github-release/astral-sh/uv/LatestRelease"
$Worker    = "https://get.litman.dev"

# A mirror counts as usable if it answers a HEAD within five seconds. The probe
# asks for a directory rather than a file: mirrors prune old releases, and
# pinning a filename would read a routine prune as an outage.
function Test-Mirror($url) {
    try {
        $null = Invoke-WebRequest -Uri $url -Method Head -TimeoutSec 5 -UseBasicParsing
        return $true
    } catch {
        return $false
    }
}

if (Test-Mirror "$NjuPython/") {
    $env:UV_PYTHON_INSTALL_MIRROR = $NjuPython
} else {
    $env:UV_PYTHON_INSTALL_MIRROR = "$Worker/gh/astral-sh/python-build-standalone/releases/download"
}

# The litman wheel comes from the Tsinghua TUNA PyPI mirror — full automatic
# sync of upstream PyPI, hosted inside China. uv records this index in the tool
# receipt, so `lit self-update` (uv tool upgrade litman) keeps using it too.
$env:UV_DEFAULT_INDEX = "https://pypi.tuna.tsinghua.edu.cn/simple"
# -----------------------------------------------------------------------------

# uv places tool executables here by default.
$ToolBin = Join-Path $env:USERPROFILE ".local\bin"

function Test-Cmd($name) {
    $null -ne (Get-Command $name -ErrorAction SilentlyContinue)
}

# Each branch takes its installer from the same place as the binary it fetches.
# Unlike the POSIX installer, this one does no checksum verification at all, so
# a crossed pair does not fail loudly — it quietly installs whatever version the
# other side happens to carry while recording the version its script was pinned
# to. Keeping the pairs together avoids that mislabelling.
#
# Each branch also clears the other's variable: UV_DOWNLOAD_URL outranks
# UV_INSTALLER_GITHUB_BASE_URL, so leaving both set silently ignores the second.
# The child PowerShell that runs uv's installer inherits this process
# environment, which is how either variable reaches it.
function Install-UvFromMirror {
    Remove-Item Env:UV_INSTALLER_GITHUB_BASE_URL -ErrorAction SilentlyContinue
    $env:UV_DOWNLOAD_URL = $UstcUv
    powershell -ExecutionPolicy ByPass -c "irm $UstcUv/uv-installer.ps1 | iex"
}

function Install-UvFromWorker {
    Remove-Item Env:UV_DOWNLOAD_URL -ErrorAction SilentlyContinue
    $env:UV_INSTALLER_GITHUB_BASE_URL = "$Worker/gh"
    powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
}

$installedUv = $false

if (Test-Cmd uv) {
    Write-Host "uv already installed - skipping."
} else {
    Write-Host "Installing uv..."
    if (Test-Mirror "$UstcUv/uv-installer.ps1") {
        # Tolerate failure here: the check below decides whether it worked.
        try { Install-UvFromMirror } catch { }
    }
    # uv's bin dir is not on PATH until the shell is reopened; prepend it so the
    # rest of THIS script run can call uv and, later, lit.
    $env:Path = "$ToolBin;$env:Path"
    # Ask the filesystem rather than an exit code. A nested `irm | iex` does not
    # reliably surface failure as a nonzero exit, so only the presence of uv
    # proves anything.
    if (-not (Test-Cmd uv) -and -not (Test-Path (Join-Path $ToolBin "uv.exe"))) {
        Install-UvFromWorker
    }
    $installedUv = $true
}

# uv prints "No tools installed" to stderr; under ErrorActionPreference=Stop,
# Windows PowerShell 5.1 turns redirected native stderr into a terminating
# error, so hand the redirect to cmd.exe instead of PowerShell.
$toolList = cmd /c "uv tool list 2>nul"
if ($toolList -match '(?m)^litman') {
    Write-Host "Upgrading litman..."
    # A running litman keeps lit.exe/litw.exe locked and the upgrade would die
    # copying them back (Windows never overwrites a running exe — but it does
    # allow renaming one). Move the launchers aside, then settle afterwards:
    # re-created by the upgrade -> drop the .old; not re-created (failure, or
    # uv's "Nothing to upgrade" fast path that skips entrypoints) -> rename it
    # back, so a launcher never disappears.
    $moved = @()
    foreach ($name in @("lit.exe", "litw.exe")) {
        $stub = Join-Path $ToolBin $name
        if (Test-Path $stub) {
            try {
                Move-Item -Force $stub "$stub.old" -ErrorAction Stop
                $moved += $stub
            } catch {}
        }
    }
    uv tool upgrade litman
    foreach ($stub in $moved) {
        if (Test-Path $stub) {
            Remove-Item "$stub.old" -Force -ErrorAction SilentlyContinue
        } else {
            Move-Item -Force "$stub.old" $stub -ErrorAction SilentlyContinue
        }
    }
} else {
    Write-Host "Installing litman..."
    uv tool install litman
}

# Verify the CLI runs. PATH may not include the tool bin dir yet in this run, so
# fall back to its absolute location.
$litExe = Join-Path $ToolBin "lit.exe"
function Invoke-Lit {
    if (Test-Cmd lit) { & lit @args }
    elseif (Test-Path $litExe) { & $litExe @args }
    else { throw "could not locate the 'lit' executable" }
}

try {
    Invoke-Lit --version
} catch {
    Write-Host "warning: could not locate the 'lit' executable to verify it."
}

# Drop a desktop shortcut so the user can just double-click to start — no
# `lit setup` needed (the app builds the library and picks the agent itself).
# Best-effort: a native exe's nonzero exit does not throw, so check $LASTEXITCODE.
$shortcutOk = $false
try {
    Invoke-Lit gui --make-shortcut | Out-Null
    $shortcutOk = ($LASTEXITCODE -eq 0)
} catch {
    $shortcutOk = $false
}
if ($shortcutOk) {
    Write-Host "Created a 'litman' shortcut on your Desktop."
} else {
    Write-Host "note: could not create the desktop shortcut; run 'lit gui --make-shortcut' later."
}

Write-Host ""
if ($installedUv) {
    Write-Host "uv was just installed. Open a new terminal so that 'lit' is on your PATH."
}
Write-Host "Done. Double-click the Desktop 'litman' icon to start."
Write-Host "(Optional) 'lit setup' adds shell completion and the agent skills."
