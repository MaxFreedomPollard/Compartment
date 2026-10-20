#!/usr/bin/env bash
# Compartment installer - Linux, WSL and ChromeOS (Crostini). On Alpine,
# Termux and the BSDs it says what to install first.
#
# Installs the package, creates and unlocks a vault, and connects the agents
# it finds. Fully offline after this script.
#
#   bash install/install-linux.sh                 from a checkout
#   PYTHON=python3.12 bash install-linux.sh       to choose the interpreter
#
# Running it again upgrades Compartment and leaves an existing vault alone.
# Written for bash 3.2 and up, so the same constructs run on macOS's bash.
set -euo pipefail

echo "Compartment installer (Linux)"

# --- 1. A Python that can run Compartment ------------------------------------
#
# Compartment runs on Python 3.11 to 3.14 with x86_64 or aarch64 and glibc
# 2.28 or newer: that is where every compiled dependency publishes wheels,
# onnxruntime above all, which Compartment cannot embed anything without
# (tools/wheel_coverage.py checks the matrix in CI). Elsewhere - musl
# (Alpine), Android (Termux), the BSDs, 32-bit, armv7, riscv64, ppc64le,
# s390x, an older glibc, a Python newer than its wheels - pip fails with a
# bare "No matching distribution found for onnxruntime". So this is checked
# first, and where the operating system packages onnxruntime instead, that
# copy is used.
#
# The probe runs inside each candidate. First line: "ok", or why this
# platform has no onnxruntime wheel. Second line: whether onnxruntime is
# importable already. Exit 0: usable. Exit 1: not Python 3.11 to 3.14.
# Exit 2: no wheel and no onnxruntime.
PROBE='
import os, platform, sys
if not (3, 11) <= sys.version_info[:2] <= (3, 14):
    print("Python %d.%d" % sys.version_info[:2])
    sys.exit(1)
reason = "ok"
machine = platform.machine().lower()
if hasattr(sys, "getandroidapilevel") or sys.platform == "android":
    reason = "android"
elif platform.system() != "Linux":
    reason = platform.system() or "unknown"
elif machine not in ("x86_64", "amd64", "aarch64", "arm64"):
    reason = "arch " + machine
else:
    try:
        libc = os.confstr("CS_GNU_LIBC_VERSION") or ""
    except (ValueError, OSError):
        libc = ""
    if not libc.startswith("glibc "):
        reason = "musl"
    else:
        parts = libc.split()[1].split(".")
        if (int(parts[0]), int(parts[1])) < (2, 28):
            reason = "glibc " + libc.split()[1]
try:
    import importlib.util
    present = importlib.util.find_spec("onnxruntime") is not None
except Exception:
    present = False
print(reason)
print("present" if present else "absent")
sys.exit(0 if reason == "ok" or present else 2)
'

PY=""
PLATFORM=""
NO_WHEEL=""
NO_WHEEL_PY=""
TOO_OLD=""
if [ -n "${PYTHON:-}" ]; then
  set -- "$PYTHON"
else
  set -- python3 python3.14 python3.13 python3.12 python3.11
fi
for cand in "$@"; do
  command -v "$cand" >/dev/null 2>&1 || continue
  rc=0
  out="$("$cand" -c "$PROBE" 2>/dev/null)" || rc=$?
  first="$(printf '%s\n' "$out" | sed -n 1p)"
  case "$rc" in
    0) PY="$(command -v "$cand")"; PLATFORM="$first"; break ;;
    2) if [ -z "$NO_WHEEL" ]; then NO_WHEEL="$first"; NO_WHEEL_PY="$cand"; fi ;;
    *) TOO_OLD="${TOO_OLD:+$TOO_OLD, }$cand is ${first:-not runnable}" ;;
  esac
done

if [ -z "$PY" ] && [ -n "$NO_WHEEL" ]; then
  pyxy="$("$NO_WHEEL_PY" -c 'import sys; print("%d%d" % sys.version_info[:2])')"
  echo
  echo "onnxruntime, which Compartment needs, publishes no build for this system ($NO_WHEEL)."
  case "$NO_WHEEL" in
    android)
      echo "On Termux, install it from Termux's own packages, then run this script again:"
      echo "  pkg install python onnxruntime python-numpy rust"
      echo "(rust lets pip build tokenizers and pydantic-core, which have no Android wheels.)" ;;
    musl)
      echo "On Alpine, install it from the community repository, then run this script again:"
      echo "  apk add python3 py3-onnxruntime"
      echo "Every other dependency has musllinux wheels." ;;
    FreeBSD)
      echo "On FreeBSD, install it and the other compiled dependencies from packages,"
      echo "then run this script again:"
      echo "  pkg install py${pyxy}-onnxruntime py${pyxy}-tokenizers py${pyxy}-pydantic-core \\"
      echo "              py${pyxy}-pynacl py${pyxy}-argon2-cffi py${pyxy}-numpy" ;;
    arch\ *)
      echo "onnxruntime ships wheels for x86_64 and aarch64 only. On a Raspberry Pi,"
      echo "install the 64-bit OS. Elsewhere, install onnxruntime for $NO_WHEEL_PY from"
      echo "your distribution if it packages it, then run this script again." ;;
    glibc\ *)
      echo "Its wheels need glibc 2.28 or newer (Debian 10, Ubuntu 20.04, RHEL 8"
      echo "and later). Use a newer distribution, or a container of one."
      exit 1 ;;
    *)
      echo "Build onnxruntime's Python package for $NO_WHEEL_PY from source"
      echo "(https://github.com/microsoft/onnxruntime), then run this script again." ;;
  esac
  echo "This script then installs Compartment in its own environment that uses"
  echo "that onnxruntime."
  exit 1
fi

if [ -z "$PY" ]; then
  echo
  if [ -n "$TOO_OLD" ]; then
    echo "Compartment needs Python 3.11 to 3.14; found: $TOO_OLD."
  else
    echo "Compartment needs Python 3.11 to 3.14, and none was found."
  fi
  echo "Install one with your package manager, for example"
  echo "  sudo apt install python3.12 python3.12-venv    (Ubuntu 22.04: python3.11)"
  echo "  sudo dnf install python3.12"
  echo "then run this script again, or name it: PYTHON=python3.12 bash ${BASH_SOURCE[0]:-install-linux.sh}"
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

# Where to put it. `pip install --user` into the system Python fails on
# Debian 12+, Ubuntu 23.04+, Arch, Crostini and Homebrew with
# "externally-managed-environment" (PEP 668), which under `set -e` ended the
# script before anything was installed. So:
#   - inside an activated virtualenv: into it (--user is refused there);
#   - onnxruntime from the OS (Alpine, Termux, FreeBSD): a private venv that
#     can see the system site-packages, so pip uses that copy;
#   - a Python that allows it: `pip install --user`, as before;
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
elif [ "$PLATFORM" != ok ]; then
  METHOD=private
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
    # user bin directory (usually ~/.local/bin), which is not on PATH on a
    # clean machine, so the interpreter is asked where that is.
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
    SITE_FLAG=""
    if [ "$PLATFORM" != ok ]; then SITE_FLAG="--system-site-packages"; fi
    # No --clear: a re-run upgrades in place, under a status bar app that
    # may be running from this very environment.
    if ! "$PY" -m venv $SITE_FLAG "$VENV"; then
      echo
      echo "Could not create a virtual environment with $PY."
      echo "On Debian, Ubuntu and Crostini install the venv module first:"
      echo "  sudo apt install python3-venv     (or python3.X-venv for $PY)"
      echo "or install pipx (sudo apt install pipx), then run this script again."
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

# Staying unlocked until the next restart keeps a per-boot secret in POSIX
# shared memory. Termux has none, and glibc keeps it in /dev/shm, which some
# containers and chroots lack; there `init` would create the vault and then
# fail. Such a vault is created locked and opened with the passphrase.
NO_SESSION=""
if [ -n "${TERMUX_VERSION:-}" ] || case "${PREFIX:-}" in *com.termux*) true ;; *) false ;; esac \
   || [ ! -d /dev/shm ] || [ ! -w /dev/shm ]; then
  NO_SESSION="--no-session"
fi

echo
INIT_RAN=no
if [ -e "$VAULT" ]; then
  echo "Your vault at $VAULT is kept as it is."
  echo "Open it with:  compartment unlock"
else
  INIT_RAN=yes
  echo "Creating your encrypted vault..."
  # `init` asks for a passphrase on the terminal. Run as `curl ... | bash`,
  # stdin is the script itself, so the terminal is handed over explicitly.
  if [ -t 0 ]; then
    run_compartment init $NO_SESSION
  elif (: </dev/tty) 2>/dev/null; then
    run_compartment init $NO_SESSION </dev/tty
  else
    INIT_RAN=no
    echo "No terminal to ask for a passphrase on. Create the vault with:"
    echo "  compartment init $NO_SESSION"
  fi
fi

if [ "$ON_PATH_ALREADY" = "no" ]; then
  echo
  echo "Note: $BIN is not on your PATH."
  echo "Add this line to ~/.bashrc (or your shell's profile) so 'compartment' works in new shells:"
  echo "  export PATH=\"$BIN:\$PATH\""
fi

if [ -n "$NO_SESSION" ]; then
  cat <<'EOF'

This system has no shared memory to keep the vault unlocked between
commands, so it stays locked. Open it per command with
COMPARTMENT_PASSPHRASE, or with `compartment unlock --once`.
EOF
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
