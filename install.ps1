# litman installer (Windows) — installs uv (if missing), then litman as a uv tool.
#
# Usage:
#   powershell -ExecutionPolicy ByPass -c "irm https://get.litman.dev/install.ps1 | iex"
#
# Idempotent: re-running upgrades an existing install. No admin rights —
# everything lands under your user profile (uv's default tool location), and uv
# fetches its own Python, so the system Python is never touched.
$ErrorActionPreference = "Stop"

# uv places tool executables here by default.
$ToolBin = Join-Path $env:USERPROFILE ".local\bin"
# PATH as the user's session has it, before this script prepends anything: it
# is what decides whether `lit` will be found once the script is done.
$OrigPath = $env:Path

function Test-Cmd($name) {
    $null -ne (Get-Command $name -ErrorAction SilentlyContinue)
}

# uv and lit write UTF-8 into a pipe, but PowerShell decodes what it captures
# from them with the console's code page - GBK on a Chinese Windows - which
# garbles any non-ASCII path or message, a Chinese user name for one. Run a
# block with that decoder switched to UTF-8; with no console to switch, as is.
function Invoke-Utf8([scriptblock]$Block) {
    $prev = $null
    try {
        $prev = [Console]::OutputEncoding
        [Console]::OutputEncoding = New-Object System.Text.UTF8Encoding $false
    } catch {
        $prev = $null
    }
    try { & $Block } finally {
        if ($prev) { try { [Console]::OutputEncoding = $prev } catch { } }
    }
}

# Run a native command and return everything it printed, stderr included, as
# text. Windows PowerShell 5.1 turns redirected native stderr into errors, which
# ErrorActionPreference=Stop would make fatal - so relax it for this call only
# (the assignment is local to the function).
function Invoke-Native {
    $ErrorActionPreference = "Continue"
    $exe, $rest = $args
    Invoke-Utf8 { & $exe @rest 2>&1 | ForEach-Object { "$_" } }
}

# Ask uv itself rather than an exit code. A nested `irm | iex` does not reliably
# surface failure as a nonzero exit, so only a uv that runs proves anything.
function Test-UvWorks {
    if (-not (Test-Cmd uv)) { return $false }
    try {
        $null = & uv --version
        return ($LASTEXITCODE -eq 0)
    } catch {
        return $false
    }
}

# Set when uv's own installer ran: it has already put its bin dir on the user
# PATH, so that step is skipped below.
$uvInstallerRan = $false

if (Test-Cmd uv) {
    Write-Host "uv already installed - skipping."
} else {
    Write-Host "Installing uv (astral.sh)..."
    # Tolerate failure here: Test-UvWorks decides whether it worked.
    try { powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex" } catch { }
    $uvInstallerRan = $true
    # uv's bin dir is not on PATH until the shell is reopened; prepend it so the
    # rest of THIS script run can call uv and, later, lit.
    $env:Path = "$ToolBin;$env:Path"
    if (-not (Test-UvWorks)) {
        Write-Host ""
        Write-Host "error: could not install uv." -ForegroundColor Red
        Write-Host "Install it by hand (https://docs.astral.sh/uv/getting-started/installation/),"
        Write-Host "then run this command again."
        # Not `exit`: under `irm | iex` that closes the user's PowerShell window,
        # message and all. A terminating error ends the script and leaves it open.
        throw "litman was not installed: uv is missing."
    }
}

# Where uv puts lit: its tool bin dir, which UV_TOOL_BIN_DIR can move. It goes
# on this run's PATH too, ahead of the install (or uv warns that it is missing):
# only a fresh uv install above has put it there, and `lit gui --make-shortcut`
# below finds the program the shortcut should start by looking lit up on PATH.
$LitBin = $ToolBin
try {
    $dir = Invoke-Utf8 { & uv tool dir --bin }
    if ($LASTEXITCODE -eq 0 -and "$dir".Trim()) { $LitBin = "$dir".Trim() }
} catch { }
if (-not (($env:Path -split ';' | ForEach-Object { $_.TrimEnd('\') }) -contains $LitBin.TrimEnd('\'))) {
    $env:Path = "$LitBin;$env:Path"
}

# uv prints "No tools installed" to stderr; under ErrorActionPreference=Stop,
# Windows PowerShell 5.1 turns redirected native stderr into a terminating
# error, so hand the redirect to cmd.exe instead of PowerShell.
$toolList = cmd /c "uv tool list 2>nul"
if ($toolList -match '(?m)^litman') {
    Write-Host "Upgrading litman..."
    # Clear the launcher paths so uv's entrypoint copy lands on empty names,
    # then settle afterwards: re-created by the upgrade -> drop the .old; not
    # re-created (failure, or uv's "Nothing to upgrade" fast path that skips
    # entrypoints) -> rename it back, so a launcher never disappears.
    # This cannot rescue an upgrade run while litman is open: Windows refuses
    # even to RENAME a live exe, so the move is skipped and uv dies copying the
    # new stub over the running one. Close litman first.
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

# Put that dir on the user PATH for good when uv came from somewhere else (pip,
# a package manager) and nothing has. `uv tool update-shell` writes the user
# PATH in the registry; it only looks at the PATH it is given, so it gets the
# user's own - handed the one this script extended, it would find the dir
# already there and do nothing. It also fails when the registry already carries
# the dir, which is fine: a new terminal will have it.
$pathPending = -not (($OrigPath -split ';' | ForEach-Object { $_.TrimEnd('\') }) -contains $LitBin.TrimEnd('\'))
if ($pathPending -and -not $uvInstallerRan) {
    $uvExe = (Get-Command uv).Source
    $sessionPath = $env:Path
    $env:Path = $OrigPath
    try {
        $updateOut = Invoke-Native $uvExe tool update-shell
        if ($LASTEXITCODE -eq 0) { $updateOut | ForEach-Object { Write-Host $_ } }
    } catch {
    } finally {
        $env:Path = $sessionPath
    }
}

# Verify the CLI runs, falling back to its absolute location should PATH still
# miss it.
$litExe = Join-Path $LitBin "lit.exe"
function Get-Lit {
    if (Test-Cmd lit) { "lit" }
    elseif (Test-Path $litExe) { $litExe }
    else { throw "could not locate the 'lit' executable" }
}
function Invoke-Lit { $lit = Get-Lit; & $lit @args }

try {
    Invoke-Lit --version
} catch {
    Write-Host "warning: could not locate the 'lit' executable to verify it."
}

# Drop a desktop shortcut so the user can just double-click to start — no
# `lit setup` needed (the app builds the library and picks the agent itself).
# Best-effort: a native exe's nonzero exit does not throw, so check $LASTEXITCODE
# - and keep what it printed, or nobody can tell what went wrong.
$shortcutOk = $false
$shortcutOut = $null
try {
    $shortcutOut = Invoke-Native (Get-Lit) gui --make-shortcut
    $shortcutOk = ($LASTEXITCODE -eq 0)
} catch {
    $shortcutOut = $_.Exception.Message
}
if ($shortcutOk) {
    Write-Host "Created a 'litman' shortcut on your Desktop."
} else {
    Write-Host "note: could not create the desktop shortcut; run 'lit gui --make-shortcut' later."
    # Its last line says why, even under a whole Python traceback.
    $reason = @($shortcutOut | Where-Object { "$_".Trim() }) | Select-Object -Last 1
    if ($reason) { Write-Host "  $reason" }
}

Write-Host ""
if ($pathPending) {
    Write-Host "Open a new terminal so that 'lit' is on your PATH."
    Write-Host "If it still is not found, add $LitBin to your PATH."
}
if ($shortcutOk) {
    Write-Host "Done. Double-click the Desktop 'litman' icon to start."
} else {
    Write-Host "Done. Run 'lit gui' to start."
}
Write-Host "(Optional) 'lit setup' adds shell completion and the agent skills."
