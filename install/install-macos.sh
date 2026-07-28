#!/usr/bin/env bash
# Compartment installer - macOS
# Installs the package, creates + unlocks a vault,
# and prints the Hermes selection step. Fully offline after this script.
set -euo pipefail

echo "Compartment installer (macOS)"
PY="${PYTHON:-python3}"
command -v "$PY" >/dev/null || { echo "python3 not found. Install from python.org or 'brew install python'."; exit 1; }

# From a source checkout, install this repo; otherwise install from PyPI.
if [ -f "$(dirname "$0")/../pyproject.toml" ]; then
  "$PY" -m pip install --user "$(cd "$(dirname "$0")/.." && pwd)"
else
  "$PY" -m pip install --user compartment
fi

# `pip install --user` puts the console script in the interpreter's own user
# bin directory (on macOS ~/Library/Python/3.X/bin), which is not on PATH on a
# clean machine. Ask the interpreter where that is and use it, so the rest of
# this script runs instead of dying on "compartment: command not found".
USER_BIN="$("$PY" -c 'import sysconfig; print(sysconfig.get_path("scripts", scheme="posix_user"))')"
ON_PATH_ALREADY=no
if command -v compartment >/dev/null 2>&1; then ON_PATH_ALREADY=yes; fi
if [ -d "$USER_BIN" ]; then
  PATH="$USER_BIN:$PATH"
  export PATH
fi

if [ -x "$USER_BIN/compartment" ]; then
  COMPARTMENT="$USER_BIN/compartment"
elif command -v compartment >/dev/null 2>&1; then
  COMPARTMENT="$(command -v compartment)"
elif "$PY" -c 'import compartment.cli' >/dev/null 2>&1; then
  COMPARTMENT=""      # no console script on PATH, but the module is importable
else
  echo "compartment was installed but cannot be found."
  echo "Expected the command in: $USER_BIN"
  echo "Try:  \"$PY\" -m pip install --user --force-reinstall compartment"
  exit 1
fi

run_compartment() {
  if [ -n "$COMPARTMENT" ]; then
    "$COMPARTMENT" "$@"
  else
    "$PY" -m compartment.cli "$@"
  fi
}

echo
echo "Creating your encrypted vault..."
run_compartment init

if [ "$ON_PATH_ALREADY" = "no" ]; then
  echo
  echo "Note: $USER_BIN is not on your PATH."
  echo "Add this line to ~/.zshrc so 'compartment' works in new shells:"
  echo "  export PATH=\"$USER_BIN:\$PATH\""
fi

cat <<'EOF'

Done. Compartment is installed and your vault is unlocked (it stays unlocked
through logins until the next restart, then asks for your passphrase once).

To use it with Hermes:
  1. python3 -m pip install --user compartment    # into the Hermes venv if separate
  2. cp -r integrations/hermes/compartment ~/.hermes/plugins/compartment
  3. hermes memory setup      # pick "compartment" in the list

Verify anytime:  compartment selftest
EOF
