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
# Managed Python (python-build-standalone) is the one uv download that still
# comes from github.com, and github.com answers it with a redirect to
# release-assets.githubusercontent.com, which is blocked. get.litman.dev is a
# Cloudflare Worker that follows that redirect server-side and streams the
# bytes back, so nothing here ever hits a blocked host.
$env:UV_PYTHON_INSTALL_MIRROR = "https://get.litman.dev/gh/astral-sh/python-build-standalone/releases/download"

# The litman wheel comes from the Tsinghua TUNA PyPI mirror — full automatic
# sync of upstream PyPI, hosted inside China. uv records this index in the tool
# receipt, so `lit self-update` (uv tool upgrade litman) keeps using it too.
$env:UV_DEFAULT_INDEX = "https://pypi.tuna.tsinghua.edu.cn/simple"

# The uv binary itself needs no mirror: uv's installer already tries Astral's
# own CDN (releases.astral.sh, Cloudflare-fronted) before github.com. Uncomment
# the next line only if that CDN turns out to be unreachable — it routes the uv
# download through get.litman.dev instead, at the cost of losing the
# CDN-then-GitHub fallback, since this variable replaces the whole list.
# $env:UV_INSTALLER_GITHUB_BASE_URL = "https://get.litman.dev/gh"
# -----------------------------------------------------------------------------

# uv places tool executables here by default.
$ToolBin = Join-Path $env:USERPROFILE ".local\bin"

function Test-Cmd($name) {
    $null -ne (Get-Command $name -ErrorAction SilentlyContinue)
}

$installedUv = $false

if (Test-Cmd uv) {
    Write-Host "uv already installed - skipping."
} else {
    Write-Host "Installing uv (astral.sh)..."
    powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
    $installedUv = $true
    # uv's bin dir is not on PATH until the shell is reopened; prepend it so the
    # rest of THIS script run can call uv and, later, lit.
    $env:Path = "$ToolBin;$env:Path"
}

# uv prints "No tools installed" to stderr; under ErrorActionPreference=Stop,
# Windows PowerShell 5.1 turns redirected native stderr into a terminating
# error, so hand the redirect to cmd.exe instead of PowerShell.
$toolList = cmd /c "uv tool list 2>nul"
if ($toolList -match '(?m)^litman') {
    Write-Host "Upgrading litman..."
    uv tool upgrade litman
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
