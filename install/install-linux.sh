#!/usr/bin/env bash
# engRAM installer - Linux
# Installs the package, creates + unlocks a vault,
# and prints the Hermes selection step. Fully offline after this script.
set -euo pipefail

echo "engRAM installer (Linux)"
PY="${PYTHON:-python3}"
command -v "$PY" >/dev/null || { echo "python3 not found. Install via your package manager (apt/dnf/pacman)."; exit 1; }

if [ -f "$(dirname "$0")/../pyproject.toml" ]; then
  "$PY" -m pip install --user "$(cd "$(dirname "$0")/.." && pwd)"
else
  "$PY" -m pip install --user engram-memory-vault
fi

echo
echo "Creating your encrypted vault…"
engram init

cat <<'EOF'

Done. engRAM is installed and your vault is unlocked (it stays unlocked
through logins until the next restart, then asks for your passphrase once).

To use it with Hermes:
  1. python3 -m pip install --user engram-memory-vault    # into the Hermes venv if separate
  2. cp -r integrations/hermes/engram ~/.hermes/plugins/engram
  3. hermes memory setup      # pick "engram" in the list

Verify anytime:  engram selftest
EOF
