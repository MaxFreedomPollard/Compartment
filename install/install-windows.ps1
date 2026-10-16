# Compartment installer - Windows (PowerShell)
# Installs the package, creates + unlocks a vault,
# and prints the Hermes selection step. Fully offline after this script.
$ErrorActionPreference = "Stop"

Write-Host "Compartment installer (Windows)"
$py = if (Get-Command py -ErrorAction SilentlyContinue) { "py" } else { "python" }
& $py --version *> $null
if ($LASTEXITCODE -ne 0) { Write-Error "Python not found. Install from python.org (check 'Add to PATH')."; exit 1 }

$repoRoot = Split-Path -Parent $PSScriptRoot
if (Test-Path (Join-Path $repoRoot "pyproject.toml")) {
    & $py -m pip install --user $repoRoot
} else {
    & $py -m pip install --user compartment
}

# `pip install --user` puts compartment.exe in the interpreter's own user
# Scripts directory (%APPDATA%\Python\PythonXY\Scripts), which is not on PATH
# on a clean machine. Ask the interpreter where that is and use it, so the
# rest of this script runs instead of dying on "compartment is not
# recognized".
$userBin = & $py -c "import sysconfig; print(sysconfig.get_path('scripts', scheme='nt_user'))"
$userBin = "$userBin".Trim()
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
        & $py -c "import compartment.cli" *> $null
        $importable = ($LASTEXITCODE -eq 0)
    } catch {
        $importable = $false
    }
    if (-not $importable) {
        Write-Host "compartment was installed but cannot be found."
        Write-Host "Expected the command in: $userBin"
        Write-Host "Try:  $py -m pip install --user --force-reinstall compartment"
        exit 1
    }
}

function Invoke-Compartment {
    if ($compartment) { & $compartment @args } else { & $py -m compartment.cli @args }
}

Write-Host ""
Write-Host "Creating your encrypted vault..."
Invoke-Compartment init

if (-not $onPathAlready) {
    Write-Host ""
    Write-Host "Note: $userBin is not on your PATH."
    Write-Host "Run this once in PowerShell so 'compartment' works in new terminals:"
    Write-Host "  [Environment]::SetEnvironmentVariable('Path', '$userBin;' + [Environment]::GetEnvironmentVariable('Path', 'User'), 'User')"
}

Write-Host @"

Done. Compartment is installed and your vault is unlocked (it stays unlocked
through logins until the next restart, then asks for your passphrase once).

To use it with Hermes:
  1. python -m pip install --user compartment    # into the Hermes venv if separate
  2. Copy-Item -Recurse integrations\hermes\compartment `$env:USERPROFILE\.hermes\plugins\compartment
  3. hermes memory setup      # pick "compartment" in the list

Verify anytime:  compartment selftest
"@
