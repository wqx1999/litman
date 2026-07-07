# litman installer (Windows) — installs uv (if missing), then litman as a uv tool.
#
# Usage:
#   powershell -ExecutionPolicy ByPass -c "irm https://raw.githubusercontent.com/wqx1999/litman/main/install.ps1 | iex"
#
# Idempotent: re-running upgrades an existing install. No admin rights —
# everything lands under your user profile (uv's default tool location), and uv
# fetches its own Python, so the system Python is never touched.
$ErrorActionPreference = "Stop"

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

$toolList = uv tool list 2>$null
if ($toolList -match '(?m)^litman') {
    Write-Host "Upgrading litman..."
    uv tool upgrade litman
} else {
    Write-Host "Installing litman..."
    uv tool install litman
}

# Verify the CLI runs. PATH may not include the tool bin dir yet in this run, so
# fall back to its absolute location.
if (Test-Cmd lit) {
    lit --version
} else {
    $litExe = Join-Path $ToolBin "lit.exe"
    if (Test-Path $litExe) {
        & $litExe --version
    } else {
        Write-Host "warning: could not locate the 'lit' executable to verify it."
    }
}

Write-Host ""
if ($installedUv) {
    Write-Host "uv was just installed. Open a new terminal so that 'lit' is on your PATH."
}
Write-Host "Next step:  lit setup"
