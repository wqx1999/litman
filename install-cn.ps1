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
# Windows PowerShell 5.1 on an older .NET may offer only TLS 1.0 / 1.1, which
# the mirrors refuse. Add TLS 1.2 to whatever is enabled; never remove any.
[Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12

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

# uv is also published on PyPI, as wheels that carry the uv binary as-is - and
# the PyPI mirrors inside China hold every release in full. They come first:
# a wheel is a zip, so getting uv out of one needs no Python and no installer
# script. Both mirrors serve files at PyPI's own paths.
$PypiMirrors = @("https://pypi.tuna.tsinghua.edu.cn", "https://mirrors.aliyun.com/pypi")
$UvVersion   = "0.13.6"

# A mirror counts as usable if it answers a HEAD within ten seconds. The probe
# asks for a directory rather than a file: mirrors prune old releases, and
# pinning a filename would read a routine prune as an outage.
function Test-Mirror($url) {
    try {
        $null = Invoke-WebRequest -Uri $url -Method Head -TimeoutSec 10 -UseBasicParsing
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

# The uv wheel for this machine. Pinned rather than looked up: the version
# listing is megabytes long and sorts as text (0.9.9 after 0.13.6), and a
# checksum kept here - not one the mirror publishes beside its own file - is
# what makes the download trustworthy. PyPI never deletes a release, so a pinned
# wheel stays fetchable; bump $UvVersion and both entries together (paths and
# digests from https://pypi.org/pypi/uv/<ver>/json). Anything else skips the
# PyPI mirrors and uses uv's own installer. A 32-bit PowerShell on 64-bit
# Windows reports x86 here and the real machine in PROCESSOR_ARCHITEW6432.
function Get-UvWheel {
    $arch = $env:PROCESSOR_ARCHITECTURE
    if ($env:PROCESSOR_ARCHITEW6432) { $arch = $env:PROCESSOR_ARCHITEW6432 }
    switch ($arch) {
        "AMD64" {
            return @{
                Path   = "packages/7c/9d/714523188aea3054644d8e69ae8089aac70325a3cee2d3d05509d4f23398/uv-0.13.6-py3-none-win_amd64.whl"
                Sha256 = "7c9bc35ccb65932d8dc10a0669f9c805eaa3c9827ce18ecbecb1bd67944b2cdf"
            }
        }
        "ARM64" {
            return @{
                Path   = "packages/6a/7d/5ebb4cccbb11ae9e10f6cda704ce382722ab9dc68c9280a5b810378aee3e/uv-0.13.6-py3-none-win_arm64.whl"
                Sha256 = "722c7f96803f115e53918b8ee8858d1680ea6077c614b6db750747d709ab8d9a"
            }
        }
    }
    return $null
}

# One attempt at one PyPI mirror; throws on any failure. uv reaches $ToolBin
# only after it has run and printed the pinned version - a half-written file
# must never look like an install - and lands there by a rename within that
# directory, so an interrupted copy leaves only a stray *.new behind.
function Install-UvFromPypi($mirror, $wheel) {
    $exes = @("uv.exe", "uvx.exe", "uvw.exe")
    $tmp = Join-Path ([IO.Path]::GetTempPath()) ("litman-uv-" + [guid]::NewGuid().ToString("N"))
    New-Item -ItemType Directory -Path $tmp | Out-Null
    try {
        # Windows PowerShell 5.1 redraws its progress bar for every chunk, which
        # slows a download this size many times over. Local to this function.
        $ProgressPreference = "SilentlyContinue"
        $whl = Join-Path $tmp "uv.whl"
        Invoke-WebRequest -Uri "$mirror/$($wheel.Path)" -OutFile $whl -TimeoutSec 20 -UseBasicParsing
        if ((Get-FileHash -Algorithm SHA256 $whl).Hash -ne $wheel.Sha256) {
            throw "checksum mismatch"
        }
        # Expand-Archive refuses the .whl extension on 5.1; read the zip directly.
        Add-Type -AssemblyName System.IO.Compression.FileSystem
        $zip = [IO.Compression.ZipFile]::OpenRead($whl)
        try {
            foreach ($name in $exes) {
                $entry = $zip.GetEntry("uv-$UvVersion.data/scripts/$name")
                if ($null -eq $entry) { throw "$name is not in the download" }
                [IO.Compression.ZipFileExtensions]::ExtractToFile($entry, (Join-Path $tmp $name), $true)
            }
        } finally {
            $zip.Dispose()
        }
        $version = & (Join-Path $tmp "uv.exe") --version
        if ($LASTEXITCODE -ne 0 -or "$version" -notmatch "^uv $([regex]::Escape($UvVersion))( |$)") {
            throw "the downloaded uv does not run here"
        }
        New-Item -ItemType Directory -Force -Path $ToolBin | Out-Null
        foreach ($name in $exes) {
            $dest = Join-Path $ToolBin $name
            Copy-Item -Force (Join-Path $tmp $name) "$dest.new"
            Move-Item -Force "$dest.new" $dest
        }
    } finally {
        Remove-Item -Recurse -Force $tmp -ErrorAction SilentlyContinue
    }
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
# Set when uv's own installer ran: it has already put its bin dir on the user
# PATH, so that step is skipped below.
$uvInstallerRan = $false

if (Test-Cmd uv) {
    Write-Host "uv already installed - skipping."
} else {
    Write-Host "Installing uv..."
    # uv's bin dir is not on PATH until the shell is reopened; prepend it so the
    # rest of THIS script run can call uv and, later, lit.
    $env:Path = "$ToolBin;$env:Path"
    $wheel = Get-UvWheel
    if ($wheel) {
        foreach ($mirror in $PypiMirrors) {
            Write-Host "  from $mirror"
            try {
                Install-UvFromPypi $mirror $wheel
                if (Test-UvWorks) {
                    $installedUv = $true
                    break
                }
            } catch {
                Write-Host "  ($($_.Exception.Message))"
            }
            Write-Host "  failed - trying the next source."
        }
    }
    if (-not $installedUv -and (Test-Mirror "$UstcUv/uv-installer.ps1")) {
        Write-Host "  from $UstcUv"
        # Tolerate failure here: Test-UvWorks decides whether it worked.
        try { Install-UvFromMirror } catch { }
        $uvInstallerRan = $true
        $installedUv = Test-UvWorks
    }
    if (-not $installedUv) {
        Write-Host "  from $Worker"
        try { Install-UvFromWorker } catch { }
        $uvInstallerRan = $true
        $installedUv = Test-UvWorks
    }
    if (-not $installedUv) {
        Write-Host ""
        Write-Host "error: could not download uv from any source." -ForegroundColor Red
        Write-Host "Install uv by hand, then run this command again:"
        Write-Host "  - with Python:  pip install uv -i https://pypi.tuna.tsinghua.edu.cn/simple"
        Write-Host "  - or download   https://pypi.tuna.tsinghua.edu.cn/simple/uv/  (pick the"
        Write-Host "    win_amd64 wheel; it is a zip) and copy uv.exe, uvx.exe and uvw.exe from"
        Write-Host "    its uv-<version>.data\scripts\ folder into $ToolBin"
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

# Put that dir on the user PATH for good when nothing else has: uv came from
# somewhere else (pip, a package manager), or out of a wheel above. `uv tool
# update-shell` writes the user PATH in the registry; it only looks at the PATH
# it is given, so it gets the user's own - handed the one this script extended,
# it would find the dir already there and do nothing. It also fails when the
# registry already carries the dir, which is fine: a new terminal will have it.
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
