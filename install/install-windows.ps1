# engRAM installer - Windows (PowerShell)
# Installs the package, creates + unlocks a vault,
# and prints the Hermes selection step. Fully offline after this script.
$ErrorActionPreference = "Stop"

Write-Host "engRAM installer (Windows)"
$py = if (Get-Command py -ErrorAction SilentlyContinue) { "py" } else { "python" }
& $py --version *> $null
if ($LASTEXITCODE -ne 0) { Write-Error "Python not found. Install from python.org (check 'Add to PATH')."; exit 1 }

$repoRoot = Split-Path -Parent $PSScriptRoot
if (Test-Path (Join-Path $repoRoot "pyproject.toml")) {
    & $py -m pip install --user $repoRoot
} else {
    & $py -m pip install --user engram-memory-vault
}

Write-Host ""
Write-Host "Creating your encrypted vault..."
engram init

Write-Host @"

Done. engRAM is installed and your vault is unlocked (it stays unlocked
through logins until the next restart, then asks for your passphrase once).

To use it with Hermes:
  1. python -m pip install --user engram-memory-vault    # into the Hermes venv if separate
  2. Copy-Item -Recurse integrations\engram `$env:USERPROFILE\.hermes\plugins\engram
  3. hermes memory setup      # pick "engram" in the list

Verify anytime:  engram selftest
"@
