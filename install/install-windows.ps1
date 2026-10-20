# Compartment installer - Windows (Windows PowerShell 5.1 or PowerShell 7)
#
# Installs the package, creates and unlocks a vault, connects the agents it
# finds, and puts Compartment in the tray. Fully offline after this script.
#
#   powershell -ExecutionPolicy Bypass -File install\install-windows.ps1
#
# -ExecutionPolicy Bypass applies to this one run: Windows' default policy
# refuses unsigned scripts, and changing it for good is the user's call.
# Set $env:PYTHON to a python.exe to choose the interpreter. Running it again
# upgrades Compartment and leaves an existing vault alone.
$ErrorActionPreference = "Stop"

Write-Host "Compartment installer (Windows)"

function Stop-Install([string]$message) {
    Write-Host ""
    Write-Host $message
    # `exit` would close the PowerShell window of someone who ran this as
    # `irm ... | iex`, where there is no script file to exit from.
    if ($PSCommandPath) { exit 1 } else { throw "Compartment was not installed." }
}

# --- 1. A Python that can run Compartment ------------------------------------
#
# Compartment's compiled dependencies publish Windows wheels for 64-bit
# Python 3.11 to 3.14 on x64, and 3.12 to 3.14 on ARM64 (no pyyaml wheel for
# 3.11 there); tools/wheel_coverage.py checks that matrix in CI. 32-bit
# Python has no onnxruntime build at all. An x64 Python on an ARM64 machine
# runs under emulation and takes the x64 wheels. Anything else fails in pip
# with a bare "No matching distribution found", so each candidate is probed
# first and the first one inside the matrix is used.
#
# The probe is a file rather than `-c`: Windows PowerShell 5.1 passes an
# argument with double quotes in it to a native program mangled.
$probe = @'
import sys, sysconfig
v = sys.version_info[:2]
plat = sysconfig.get_platform()
if plat == 'win32':
    print('32-bit Python %d.%d' % v)
    sys.exit(2)
low = (3, 12) if plat == 'win-arm64' else (3, 11)
if not (low <= v <= (3, 14)):
    print('Python %d.%d (%s)' % (v[0], v[1], plat))
    sys.exit(1)
print('ok')
'@
$probeFile = Join-Path ([System.IO.Path]::GetTempPath()) "compartment-probe-$PID.py"
Set-Content -LiteralPath $probeFile -Value $probe -Encoding ASCII

function Invoke-Probe($cand) {
    # Local to this function: a candidate that is missing, or the Microsoft
    # Store's python.exe stand-in, writes to stderr, and Windows PowerShell
    # turns redirected stderr into a terminating error under "Stop".
    $ErrorActionPreference = "Continue"
    if (-not (Get-Command $cand.Exe -ErrorAction SilentlyContinue)) { return $null }
    try {
        $a = $cand.Args
        $out = & $cand.Exe @a $probeFile 2>$null
        return @{ Code = $LASTEXITCODE; Out = ((@($out) -join " ").Trim()) }
    } catch {
        return $null
    }
}

$os = if ($env:PROCESSOR_ARCHITEW6432) { $env:PROCESSOR_ARCHITEW6432 } else { $env:PROCESSOR_ARCHITECTURE }
$candidates = @()
if ($env:PYTHON) {
    $candidates += @{ Exe = $env:PYTHON; Args = @() }
} else {
    if ($os -eq "ARM64") {
        foreach ($v in "3.14", "3.13", "3.12") { $candidates += @{ Exe = "py"; Args = @("-$v-arm64") } }
    }
    foreach ($v in "3", "3.14", "3.13", "3.12", "3.11") { $candidates += @{ Exe = "py"; Args = @("-$v") } }
    $candidates += @{ Exe = "python"; Args = @() }
    $candidates += @{ Exe = "python3"; Args = @() }
}

$exe = $null
$pyArgs = @()
$found = @()
try {
    foreach ($cand in $candidates) {
        $r = Invoke-Probe $cand
        if ($null -eq $r) { continue }
        $label = (@($cand.Exe) + $cand.Args) -join " "
        if ($r.Code -eq 0) { $exe = $cand.Exe; $pyArgs = $cand.Args; break }
        $found += "$label is $(if ($r.Out) { $r.Out } else { 'not runnable' })"
    }
} finally {
    Remove-Item -LiteralPath $probeFile -ErrorAction SilentlyContinue
}

if (-not $exe) {
    $range = if ($os -eq "ARM64") { "3.12 to 3.14 (ARM64), or 3.11 to 3.14 (x64)" } else { "3.11 to 3.14" }
    $what = if ($found.Count) { "Found: " + ($found -join "; ") + "." } else { "None was found." }
    Stop-Install ("Compartment needs 64-bit Python $range. $what`n" +
        "Install it from python.org, or: winget install Python.Python.3.13`n" +
        "then run this script again, or name it with `$env:PYTHON.")
}
Write-Host "Using $((@($exe) + $pyArgs) -join ' ')"

function Get-PyText([string]$code) {
    # UTF-8 both ways, so a profile path with a non-ASCII user name (C:\Users\
    # José) comes back as itself rather than in the console's OEM code page.
    $saved = $null
    try { $saved = [Console]::OutputEncoding; [Console]::OutputEncoding = [System.Text.Encoding]::UTF8 } catch { }
    try {
        $out = & $exe @pyArgs -X utf8 -c $code
    } finally {
        if ($saved) { try { [Console]::OutputEncoding = $saved } catch { } }
    }
    return ((@($out) -join "`n").Trim())
}

# --- 2. Install ---------------------------------------------------------------
#
# This checkout when the script is run from one, otherwise the release on
# PyPI. Run as `irm ... | iex` there is no script file and $PSScriptRoot is
# empty, which Split-Path refused, ending the script under "Stop".
$src = "compartment"
if ($PSScriptRoot) {
    $repoRoot = Split-Path -Parent $PSScriptRoot
    $pyproject = Join-Path $repoRoot "pyproject.toml"
    if ((Test-Path -LiteralPath $pyproject) -and
        (Select-String -LiteralPath $pyproject -Pattern '^name = "compartment"$' -Quiet)) {
        $src = $repoRoot
    }
}

# pip refuses --user inside a virtualenv, so an activated one is installed
# into directly; otherwise --user, as before. Windows Pythons are not
# externally managed.
$kind = Get-PyText "import sys; print('venv' if sys.prefix != sys.base_prefix else 'plain')"
$pipArgs = @("-m", "pip", "install", "--upgrade")
if ($kind -ne "venv") { $pipArgs += "--user" }
& $exe @pyArgs @pipArgs $src
if ($LASTEXITCODE -ne 0) {
    Stop-Install "pip could not install Compartment (exit code $LASTEXITCODE); its reason is above."
}

# `pip install --user` puts compartment.exe in the interpreter's own user
# Scripts directory (%APPDATA%\Python\PythonXY\Scripts), which is not on PATH
# on a clean machine. Ask the interpreter where that is and use it, so the
# rest of this script runs instead of dying on "compartment is not
# recognized".
if ($kind -eq "venv") {
    $userBin = Get-PyText "import sysconfig; print(sysconfig.get_path('scripts'))"
} else {
    $userBin = Get-PyText "import sysconfig; print(sysconfig.get_path('scripts', scheme='nt_user'))"
}
$onPathAlready = [bool](Get-Command compartment -ErrorAction SilentlyContinue)
if ($userBin -and (Test-Path -LiteralPath $userBin)) {
    $env:Path = "$userBin;$env:Path"
}

$compartment = $null
if ($userBin -and (Test-Path -LiteralPath (Join-Path $userBin "compartment.exe"))) {
    $compartment = Join-Path $userBin "compartment.exe"
} elseif (Get-Command compartment -ErrorAction SilentlyContinue) {
    $compartment = (Get-Command compartment).Source
} else {
    # No console script on PATH, but the module may be importable. Windows
    # PowerShell turns a native command's redirected stderr into an error
    # under "Stop", which here means only that the import failed.
    try {
        & $exe @pyArgs -c "import compartment.cli" *> $null
        $importable = ($LASTEXITCODE -eq 0)
    } catch {
        $importable = $false
    }
    if (-not $importable) {
        Stop-Install "compartment was installed but cannot be found. Expected the command in: $userBin"
    }
}

function Invoke-Compartment {
    if ($compartment) { & $compartment @args } else { & $exe @pyArgs -m compartment.cli @args }
}

# --- 3. The vault ---------------------------------------------------------------
#
# Where `compartment` looks by default (home.py): COMPARTMENT_VAULT, then
# the legacy ENGRAM_VAULT, then memory.vault in %USERPROFILE%\.compartment,
# or in .engram when only that exists. A vault that is there is never
# touched: `init` refuses to overwrite one, and a re-run should not fail on
# that.
$profileDir = $env:USERPROFILE
if ($env:COMPARTMENT_VAULT) {
    $vault = $env:COMPARTMENT_VAULT
} elseif ($env:ENGRAM_VAULT) {
    $vault = $env:ENGRAM_VAULT
} elseif (-not (Test-Path -LiteralPath (Join-Path $profileDir ".compartment")) -and
          (Test-Path -LiteralPath (Join-Path $profileDir ".engram"))) {
    $vault = Join-Path $profileDir ".engram\memory.vault"
} else {
    $vault = Join-Path $profileDir ".compartment\memory.vault"
}

Write-Host ""
$initRan = $false
if (Test-Path -LiteralPath $vault) {
    Write-Host "Your vault at $vault is kept as it is."
    Write-Host "Open it with:  compartment unlock   (or from the tray icon)"
} else {
    Write-Host "Creating your encrypted vault..."
    Invoke-Compartment init
    if ($LASTEXITCODE -ne 0) {
        Stop-Install "compartment init stopped (exit code $LASTEXITCODE); its reason is above."
    }
    $initRan = $true
}

if (-not $onPathAlready) {
    $quoted = $userBin -replace "'", "''"
    Write-Host ""
    Write-Host "Note: $userBin is not on your PATH."
    Write-Host "Run this once in PowerShell so 'compartment' works in new terminals:"
    Write-Host "  [Environment]::SetEnvironmentVariable('Path', '$quoted;' + [Environment]::GetEnvironmentVariable('Path', 'User'), 'User')"
}

Write-Host ""
Write-Host "Done. Compartment is installed."
if ($initRan) {
    Write-Host "``init`` connected the agents it found (Claude Code, Hermes Agent,"
    Write-Host "OpenClaw); restart them to load it."
}
Write-Host @"

To connect an agent:
  compartment integrate claude       # Claude Code + Claude Desktop
  compartment integrate hermes       # Hermes Agent
  compartment integrate --list       # every MCP client it can wire

Verify anytime:  compartment selftest
"@
