#!/usr/bin/env bash
# Compartment installer - macOS
#
# Installs the package, creates and unlocks a vault, connects the agents it
# finds, and puts Compartment in the menu bar. Fully offline after this
# script. (No command line at all: Compartment.pkg from the latest release.)
#
#   bash install/install-macos.sh                 from a checkout
#   PYTHON=python3.12 bash install-macos.sh       to choose the interpreter
#
# Running it again upgrades Compartment and leaves an existing vault alone.
# macOS ships bash 3.2, so nothing here needs a newer one.
set -euo pipefail

echo "Compartment installer (macOS)"

# --- 1. A Python that can run Compartment ------------------------------------
#
# Compartment runs on macOS 13 or newer, with a Python its compiled
# dependencies publish wheels for (tools/wheel_coverage.py checks the
# matrix in CI). onnxruntime, which it cannot embed anything without,
# decides it:
#   - Apple silicon, macOS 14 or newer: Python 3.11 to 3.14.
#   - Apple silicon on macOS 13: Python 3.11 to 3.13 - onnxruntime's
#     builds for 3.14 need macOS 14.
#   - Intel Macs (and an x86_64 Python under Rosetta): Python 3.11 to 3.13 -
#     onnxruntime stopped building for Intel after 1.23.2, which stops at
#     3.13.
# Any other Python fails in pip with a bare "No matching distribution found
# for onnxruntime", so each candidate is checked first and the first one
# that fits is used.
#
# The probe runs inside each candidate. Exit 0: usable. Exit 1: older than
# Python 3.11. Exit 2: Python 3.11+, but this Mac has no wheels for it; its
# output names the newest Python that would do, or "none" for a macOS
# older than 13.
MACOS_VERSION="$(sw_vers -productVersion 2>/dev/null || true)"
export CMPT_MACOS_VERSION="$MACOS_VERSION"
PROBE='
import os, platform, sys
v = sys.version_info[:2]
if v < (3, 11):
    print("Python %d.%d" % v)
    sys.exit(1)
text = os.environ.get("CMPT_MACOS_VERSION") or platform.mac_ver()[0] or "0"
parts = [int(x) for x in text.split(".")[:2] if x.isdigit()] + [0, 0]
mac = (parts[0], parts[1])
if mac == (10, 16):
    mac = (11, 0)
if mac < (13, 0):
    print("none")
    sys.exit(2)
intel = platform.machine().lower() not in ("arm64", "aarch64")
newest = (3, 13) if (intel or mac < (14, 0)) else (3, 14)
if v > newest:
    print("%d.%d %s" % (newest + (platform.machine(),)))
    sys.exit(2)
'

# /usr/bin/python3 on a Mac without the Command Line Tools is a stub that
# opens an installer dialog instead of running, so it is only tried where
# the tools are installed.
usable_name() {
  case "$(command -v "$1" 2>/dev/null)" in
    "") return 1 ;;
    /usr/bin/python3) xcode-select -p >/dev/null 2>&1 ;;
    *) return 0 ;;
  esac
}

PY=""
NEWEST=""
PYARCH=""
TOO_OLD=""
if [ -n "${PYTHON:-}" ]; then
  set -- "$PYTHON"
else
  set -- python3 python3.13 python3.12 python3.11 python3.14 \
    /opt/homebrew/bin/python3.13 /opt/homebrew/bin/python3.12 /opt/homebrew/bin/python3.11 \
    /usr/local/bin/python3.13 /usr/local/bin/python3.12 /usr/local/bin/python3.11 \
    /Library/Frameworks/Python.framework/Versions/3.13/bin/python3 \
    /Library/Frameworks/Python.framework/Versions/3.12/bin/python3 \
    /Library/Frameworks/Python.framework/Versions/3.11/bin/python3
fi
for cand in "$@"; do
  usable_name "$cand" || continue
  rc=0
  out="$("$cand" -c "$PROBE" 2>/dev/null)" || rc=$?
  case "$rc" in
    0) PY="$(command -v "$cand")"; break ;;
    2) if [ -z "$NEWEST" ]; then NEWEST="${out%% *}"; PYARCH="${out#* }"; fi
       TOO_OLD="${TOO_OLD:+$TOO_OLD, }$cand has no onnxruntime build here" ;;
    *) TOO_OLD="${TOO_OLD:+$TOO_OLD, }$cand is ${out:-not runnable}" ;;
  esac
done

if [ -z "$PY" ]; then
  echo
  if [ "$NEWEST" = none ]; then
    echo "Compartment needs macOS 13 or newer; this Mac has macOS $MACOS_VERSION,"
    echo "for which onnxruntime and the menu bar app's libraries publish no builds."
    exit 1
  elif [ -n "$NEWEST" ]; then
    echo "For an $PYARCH Python on macOS $MACOS_VERSION, onnxruntime, which"
    echo "Compartment needs, publishes builds only for Python 3.11 to $NEWEST."
    echo "Found: $TOO_OLD."
    echo "Install Python $NEWEST, then run this script again:"
    echo "  brew install python@$NEWEST       (or the installer from python.org)"
    echo "  PYTHON=python$NEWEST bash ${BASH_SOURCE[0]:-install-macos.sh}"
  else
    if [ -n "$TOO_OLD" ]; then
      echo "Compartment needs Python 3.11 or newer; found: $TOO_OLD."
    else
      echo "Compartment needs Python 3.11 or newer, and none was found."
    fi
    echo "Install it from python.org or with Homebrew (brew install python@3.13),"
    echo "then run this script again."
  fi
  exit 1
fi
echo "Using $PY"

# --- 2. Install ---------------------------------------------------------------
#
# This checkout when the script is run from one, otherwise the release on
# PyPI. Only a checkout whose pyproject names compartment counts: run as
# `curl ... | bash` there is no script file, and `dirname "$0"` named the
# directory the user happened to be in, so the pyproject one level up was
# somebody else's project, which got installed instead.
SRC="compartment"
SCRIPT="${BASH_SOURCE[0]:-}"
if [ -n "$SCRIPT" ] && [ -f "$SCRIPT" ]; then
  ROOT="$(cd "$(dirname "$SCRIPT")/.." && pwd)"
  if [ -f "$ROOT/pyproject.toml" ] && grep -q '^name = "compartment"$' "$ROOT/pyproject.toml"; then
    SRC="$ROOT"
  fi
fi

# Where to put it. Homebrew's Python is externally managed (PEP 668), so
# `pip install --user` there fails with "externally-managed-environment",
# which under `set -e` ended the script before anything was installed. So:
#   - inside an activated virtualenv: into it (--user is refused there);
#   - a Python that allows it (python.org's): `pip install --user`, as before;
#   - an externally managed one, or one without pip (minimal containers):
#     pipx, then uv, then a private venv, which brings its own pip.
KIND="$("$PY" -c '
import importlib.util, os, sys, sysconfig
if sys.prefix != sys.base_prefix:
    print("venv")
elif os.path.isfile(os.path.join(sysconfig.get_path("stdlib"), "EXTERNALLY-MANAGED")):
    print("managed")
elif importlib.util.find_spec("pip") is None:
    print("nopip")
else:
    print("plain")
')"
if [ "$KIND" = venv ]; then
  METHOD=venv-active
elif [ "$KIND" = plain ]; then
  METHOD=user
elif command -v pipx >/dev/null 2>&1; then
  METHOD=pipx
elif command -v uv >/dev/null 2>&1; then
  METHOD=uv
else
  METHOD=private
fi

LOCAL_BIN="$HOME/.local/bin"
case "$METHOD" in
  venv-active)
    "$PY" -m pip install --upgrade "$SRC"
    BIN="$("$PY" -c 'import sysconfig; print(sysconfig.get_path("scripts"))')" ;;
  user)
    # `pip install --user` puts the console script in the interpreter's own
    # user bin directory (~/Library/Python/3.X/bin), which is not on PATH on
    # a clean machine, so the interpreter is asked where that is.
    "$PY" -m pip install --user --upgrade "$SRC"
    BIN="$("$PY" -c 'import sysconfig; print(sysconfig.get_path("scripts", scheme="posix_user"))')" ;;
  pipx)
    pipx install --force --python "$PY" "$SRC"
    BIN="$(pipx environment --value PIPX_BIN_DIR 2>/dev/null || true)"
    BIN="${BIN:-$LOCAL_BIN}" ;;
  uv)
    uv tool install --force --upgrade --python "$PY" "$SRC"
    BIN="$(uv tool dir --bin 2>/dev/null || true)"
    BIN="${BIN:-$LOCAL_BIN}" ;;
  private)
    VENV="${XDG_DATA_HOME:-$HOME/.local/share}/compartment/venv"
    mkdir -p "$(dirname "$VENV")"
    # No --clear: a re-run upgrades in place, under a menu bar app that may
    # be running from this very environment.
    if ! "$PY" -m venv "$VENV"; then
      echo
      echo "Could not create a virtual environment with $PY."
      echo "Install pipx (brew install pipx) and run this script again."
      exit 1
    fi
    "$VENV/bin/python" -m pip install --upgrade "$SRC"
    mkdir -p "$LOCAL_BIN"
    for cmd in compartment cmpt engram; do
      if [ -x "$VENV/bin/$cmd" ]; then ln -sf "$VENV/bin/$cmd" "$LOCAL_BIN/$cmd"; fi
    done
    BIN="$LOCAL_BIN" ;;
esac

ON_PATH_ALREADY=no
case ":$PATH:" in *":$BIN:"*) ON_PATH_ALREADY=yes ;; esac
if [ -d "$BIN" ]; then
  PATH="$BIN:$PATH"
  export PATH
fi

if [ -x "$BIN/compartment" ]; then
  COMPARTMENT="$BIN/compartment"
elif command -v compartment >/dev/null 2>&1; then
  COMPARTMENT="$(command -v compartment)"
elif "$PY" -c 'import compartment.cli' >/dev/null 2>&1; then
  COMPARTMENT=""      # no console script on PATH, but the module is importable
else
  echo "compartment was installed but cannot be found."
  echo "Expected the command in: $BIN"
  exit 1
fi

run_compartment() {
  if [ -n "$COMPARTMENT" ]; then
    "$COMPARTMENT" "$@"
  else
    "$PY" -m compartment.cli "$@"
  fi
}

# --- 3. The vault ---------------------------------------------------------------
#
# Where `compartment` looks by default (home.py): COMPARTMENT_VAULT, then
# the legacy ENGRAM_VAULT, then memory.vault in ~/.compartment, or in
# ~/.engram when only that exists. A vault that is there is never touched:
# `init` refuses to overwrite one, and a re-run should not fail on that.
if [ -n "${COMPARTMENT_VAULT:-}" ]; then
  VAULT="$COMPARTMENT_VAULT"
elif [ -n "${ENGRAM_VAULT:-}" ]; then
  VAULT="$ENGRAM_VAULT"
elif [ ! -e "$HOME/.compartment" ] && [ -e "$HOME/.engram" ]; then
  VAULT="$HOME/.engram/memory.vault"
else
  VAULT="$HOME/.compartment/memory.vault"
fi

echo
INIT_RAN=no
if [ -e "$VAULT" ]; then
  echo "Your vault at $VAULT is kept as it is."
  echo "Open it with:  compartment unlock   (or from the menu bar)"
else
  INIT_RAN=yes
  echo "Creating your encrypted vault..."
  # `init` asks for a passphrase on the terminal. Run as `curl ... | bash`,
  # stdin is the script itself, so the terminal is handed over explicitly.
  if [ -t 0 ]; then
    run_compartment init
  elif (: </dev/tty) 2>/dev/null; then
    run_compartment init </dev/tty
  else
    INIT_RAN=no
    echo "No terminal to ask for a passphrase on. Create the vault with:"
    echo "  compartment init"
  fi
fi

if [ "$ON_PATH_ALREADY" = "no" ]; then
  echo
  echo "Note: $BIN is not on your PATH."
  echo "Add this line to ~/.zshrc so 'compartment' works in new shells:"
  echo "  export PATH=\"$BIN:\$PATH\""
fi

echo
echo "Done. Compartment is installed."
if [ "$INIT_RAN" = yes ]; then
  echo "\`init\` connected the agents it found (Claude Code, Hermes Agent,"
  echo "OpenClaw); restart them to load it."
fi
cat <<'EOF'

To connect an agent:
  compartment integrate claude       # Claude Code + Claude Desktop
  compartment integrate hermes       # Hermes Agent
  compartment integrate --list       # every MCP client it can wire

Verify anytime:  compartment selftest
EOF
