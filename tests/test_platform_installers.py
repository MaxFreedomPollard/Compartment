"""install/install-linux.sh and install/install-macos.sh, run for real by
bash against stand-ins for python, pip, venv, pipx and uv.

Every external program the scripts start is a stub on a PATH that holds
nothing else: each one logs how it was called, and the python stub runs the
scripts' own probes with the platform facts a test gives it (its version,
glibc or musl, CPU, macOS version, PEP 668, an onnxruntime already
installed). So these tests are about the scripts' decisions, on any POSIX
machine, and nothing is installed or downloaded.
"""
import json
import os
import pathlib
import pty
import re
import shutil
import subprocess
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
LINUX = ROOT / "install" / "install-linux.sh"
MACOS = ROOT / "install" / "install-macos.sh"
BASH = shutil.which("bash")

pytestmark = pytest.mark.skipif(os.name == "nt" or BASH is None,
                                reason="the shell installers need bash")

_PYTHON_STUB = r'''#!{python}
import json, os, shutil, stat, sys
me = os.path.abspath(sys.argv[0])
name = os.path.basename(me)
args = sys.argv[1:]
facts = json.loads(os.environ.get("STUB_FACTS", "{{}}"))
f = dict(facts.get("*", {{}}))
f.update(facts.get(name, {{}}))
in_venv = os.path.isfile(os.path.join(os.path.dirname(os.path.dirname(me)),
                                      "pyvenv.cfg"))
kind = "plain" if in_venv else f.get("kind", "plain")

def log(*what):
    with open(os.environ["STUB_LOG"], "a") as fh:
        fh.write(json.dumps([me, *what]) + "\n")

def console_script(where):
    os.makedirs(where, exist_ok=True)
    target = os.path.join(where, "compartment")
    shutil.copy(os.environ["STUB_COMPARTMENT"], target)
    os.chmod(target, 0o755)

if args[:1] == ["-c"]:
    import importlib.util, platform, sysconfig
    if "import compartment.cli" in args[1]:
        sys.exit(1)
    sys.version_info = tuple(f.get("version", [3, 12])) + (0, "final", 0)
    platform.system = lambda: f.get("system", "Linux")
    platform.machine = lambda: f.get("machine", "x86_64")
    libc = f.get("libc", "glibc 2.39")
    def confstr(n):
        if libc == "musl":
            raise OSError(22, "Invalid argument")
        return libc
    os.confstr = confstr
    if f.get("android"):
        sys.getandroidapilevel = lambda: 34
    real_find_spec = importlib.util.find_spec
    def find_spec(n, *a, **k):
        if n == "onnxruntime":
            return object() if f.get("ort") else None
        if n == "pip":
            return None if f.get("no_pip") else object()
        return real_find_spec(n, *a, **k)
    importlib.util.find_spec = find_spec
    stdlib = os.path.join(os.environ["STUB_ROOT"], "stdlib-" + kind)
    os.makedirs(stdlib, exist_ok=True)
    if kind == "managed":
        open(os.path.join(stdlib, "EXTERNALLY-MANAGED"), "w").close()
    real_get_path = sysconfig.get_path
    def get_path(n, scheme=None, *a, **k):
        if n == "stdlib":
            return stdlib
        if n == "scripts":
            return os.environ["STUB_USER_BIN" if scheme == "posix_user"
                              else "STUB_VENV_BIN"]
        return real_get_path(n, *a, **k)
    sysconfig.get_path = get_path
    if kind == "venv":
        sys.prefix, sys.base_prefix = "/venv", "/base"
    else:
        sys.prefix = sys.base_prefix
    exec(compile(args[1], "<probe>", "exec"), {{"__name__": "__main__"}})
    sys.exit(0)
if args[:2] == ["-m", "pip"]:
    log("pip", *args[2:])
    if kind == "managed" and "--user" in args:
        print("error: externally-managed-environment", file=sys.stderr)
        sys.exit(1)
    console_script(os.path.dirname(me) if in_venv
                   else os.environ["STUB_USER_BIN"] if "--user" in args
                   else os.environ["STUB_VENV_BIN"])
    sys.exit(0)
if args[:2] == ["-m", "venv"]:
    log("venv", *args[2:])
    if f.get("no_venv"):
        print("ensurepip is not available", file=sys.stderr)
        sys.exit(1)
    d = args[-1]
    os.makedirs(os.path.join(d, "bin"), exist_ok=True)
    open(os.path.join(d, "pyvenv.cfg"), "w").close()
    shutil.copy(me, os.path.join(d, "bin", "python"))
    sys.exit(0)
log("unexpected", *args)
sys.exit(3)
'''

_TOOL_STUB = r'''#!{python}
import json, os, shutil, sys
args = sys.argv[1:]
tool = os.path.basename(sys.argv[0])
with open(os.environ["STUB_LOG"], "a") as fh:
    fh.write(json.dumps([tool, *args]) + "\n")
if tool == "compartment":
    sys.exit(0)
if tool == "sw_vers":
    print(os.environ.get("STUB_MACOS", "15.1"))
    sys.exit(0)
bindir = os.environ["STUB_TOOL_BIN"]
if args[:1] in (["environment"], ["tool"]) and "install" not in args:
    print(bindir)
    sys.exit(0)
os.makedirs(bindir, exist_ok=True)
shutil.copy(os.environ["STUB_COMPARTMENT"], os.path.join(bindir, "compartment"))
os.chmod(os.path.join(bindir, "compartment"), 0o755)
'''


class Run:
    def __init__(self, proc, log):
        self.rc = proc.returncode
        self.out = proc.stdout + proc.stderr
        self.calls = [json.loads(line) for line in log.read_text().splitlines()]

    def of(self, what):
        """The argument lists of every call to `what` (pip, venv, pipx,
        uv, compartment)."""
        found = []
        for call in self.calls:
            head = call[0] if call[0] in ("pipx", "uv", "compartment", "sw_vers") else call[1]
            if head == what:
                found.append(call[1:] if call[0] in ("pipx", "uv", "compartment", "sw_vers") else call[2:])
        return found

    def interpreter_of(self, what):
        return [c[0] for c in self.calls if len(c) > 1 and c[1] == what]

    def init_args(self):
        """How `init` was run, or how the script told the user to run it
        when there was no terminal to ask for a passphrase on (no pty in
        this sandbox, say). None when it was neither."""
        inits = [c for c in self.of("compartment") if c[:1] == ["init"]]
        if inits:
            return inits[0]
        m = re.search(r"Create the vault with:\n\s+compartment (init[^\n]*)",
                      self.out)
        return m.group(1).split() if m else None


@pytest.fixture()
def installer(tmp_path):
    """Run an installer with the stand-ins. `pythons` names the python
    stubs to put on PATH; `facts` is per-stub (or "*") platform facts;
    `tools` adds pipx and/or uv."""
    def run(script, pythons=("python3",), facts=None, tools=(), env=None,
            tty=True, stdin_script=False, cwd=None):
        root = tmp_path / "stubs"
        bin_ = root / "bin"
        bin_.mkdir(parents=True, exist_ok=True)
        home = tmp_path / "home"
        home.mkdir(exist_ok=True)
        log = root / "log"
        log.write_text("")
        py = _PYTHON_STUB.format(python=sys.executable)
        tool = _TOOL_STUB.format(python=sys.executable)
        comp = root / "compartment-stub"
        comp.write_text(tool)
        comp.chmod(0o755)
        for name in pythons:
            (bin_ / name).write_text(py)
            (bin_ / name).chmod(0o755)
        for name in (*tools, "sw_vers"):
            (bin_ / name).write_text(tool)
            (bin_ / name).chmod(0o755)
        for name in ("sed", "grep", "dirname", "mkdir", "ln", "cat", "uname"):
            real = shutil.which(name, path="/usr/bin:/bin")
            if real:
                (bin_ / name).symlink_to(real)
        e = {"PATH": str(bin_), "HOME": str(home),
             "STUB_LOG": str(log), "STUB_ROOT": str(root),
             "STUB_FACTS": json.dumps(facts or {}),
             "STUB_COMPARTMENT": str(comp),
             "STUB_USER_BIN": str(home / ".local" / "bin"),
             "STUB_VENV_BIN": str(root / "active-venv" / "bin"),
             "STUB_TOOL_BIN": str(home / ".local" / "bin")}
        e.update(env or {})
        master = slave = None
        if stdin_script:
            argv, stdin = [BASH, "-s"], open(script, "rb")
        else:
            argv, stdin = [BASH, str(script)], subprocess.DEVNULL
            if tty:
                try:
                    master, slave = pty.openpty()
                    stdin = slave
                except OSError:
                    pass            # no ptys here: the no-terminal branch
        try:
            # A session of its own, so /dev/tty is never the terminal of
            # whoever runs the suite.
            proc = subprocess.run(argv, env=e, stdin=stdin, cwd=cwd or tmp_path,
                                  capture_output=True, text=True, timeout=120,
                                  start_new_session=True)
        finally:
            if stdin_script:
                stdin.close()
            for fd in (master, slave):
                if fd is not None:
                    os.close(fd)
        return Run(proc, log)
    return run


# --- Linux: PEP 668 -----------------------------------------------------------

def test_an_externally_managed_python_is_installed_with_pipx(installer):
    """Debian 12+, Ubuntu 23.04+, Arch, Crostini: `pip install --user` fails
    with externally-managed-environment, and under `set -e` the script
    ended there with nothing installed."""
    r = installer(LINUX, facts={"*": {"kind": "managed"}}, tools=("pipx",))
    assert r.rc == 0, r.out
    assert not [a for a in r.of("pip") if "--user" in a]
    install = [a for a in r.of("pipx") if a[:1] == ["install"]]
    assert install and str(ROOT) in install[0]
    assert r.init_args()[0] == "init"


def test_uv_is_used_where_there_is_no_pipx(installer):
    r = installer(LINUX, facts={"*": {"kind": "managed"}}, tools=("uv",))
    assert r.rc == 0, r.out
    assert [a for a in r.of("uv") if a[:2] == ["tool", "install"]]


def test_with_neither_a_private_environment_is_made_and_linked(installer,
                                                               tmp_path):
    r = installer(LINUX, facts={"*": {"kind": "managed"}})
    assert r.rc == 0, r.out
    venv = tmp_path / "home" / ".local" / "share" / "compartment" / "venv"
    assert r.of("venv") == [[str(venv)]]
    assert r.interpreter_of("pip") == [str(venv / "bin" / "python")]
    link = tmp_path / "home" / ".local" / "bin" / "compartment"
    assert link.is_symlink() and link.resolve() == (venv / "bin" / "compartment").resolve()
    assert r.init_args()[0] == "init"


def test_a_python_without_the_venv_module_says_which_package_to_add(installer):
    """Debian splits ensurepip into python3-venv, and Crostini has none."""
    r = installer(LINUX, facts={"*": {"kind": "managed", "no_venv": True}})
    assert r.rc == 1
    assert "python3-venv" in r.out and "pipx" in r.out


def test_a_python_that_allows_user_installs_keeps_the_old_route(installer):
    r = installer(LINUX)
    assert r.rc == 0, r.out
    assert r.of("pip") == [["install", "--user", "--upgrade", str(ROOT)]]


def test_a_python_without_pip_gets_a_private_environment(installer, tmp_path):
    """Minimal containers and some distributions ship python3 without pip:
    `python3 -m pip` died with "No module named pip"."""
    r = installer(LINUX, facts={"*": {"no_pip": True}})
    assert r.rc == 0, r.out
    venv = tmp_path / "home" / ".local" / "share" / "compartment" / "venv"
    assert r.of("venv") == [[str(venv)]]
    assert r.interpreter_of("pip") == [str(venv / "bin" / "python")]


def test_an_activated_virtualenv_is_installed_into_without_user(installer):
    """pip refuses --user inside a virtualenv."""
    r = installer(LINUX, facts={"*": {"kind": "venv"}})
    assert r.rc == 0, r.out
    assert r.of("pip") == [["install", "--upgrade", str(ROOT)]]


# --- Linux: which Python ------------------------------------------------------

def test_a_too_old_python3_is_passed_over_for_a_versioned_one(installer):
    """Ubuntu 22.04's python3 is 3.10; python3.11 is a separate package."""
    r = installer(LINUX, pythons=("python3", "python3.11"),
                  facts={"python3": {"version": [3, 10]},
                         "python3.11": {"version": [3, 11]}})
    assert r.rc == 0, r.out
    assert r.interpreter_of("pip")[0].endswith("python3.11")


def test_a_python_newer_than_the_wheels_is_passed_over_too(installer):
    """Python 3.15 is out before onnxruntime builds for it; the supported
    range is 3.11 to 3.14 (tools/wheel_coverage.py)."""
    r = installer(LINUX, pythons=("python3", "python3.14"),
                  facts={"python3": {"version": [3, 15]},
                         "python3.14": {"version": [3, 14]}})
    assert r.rc == 0, r.out
    assert r.interpreter_of("pip")[0].endswith("python3.14")


def test_no_python_in_range_is_explained(installer):
    r = installer(LINUX, facts={"*": {"version": [3, 9]}})
    assert r.rc == 1
    assert "3.11 to 3.14" in r.out and "python3 is Python 3.9" in r.out
    assert not r.of("pip")


# --- Linux: platforms onnxruntime has no wheel for ----------------------------

@pytest.mark.parametrize("facts, says", [
    ({"libc": "musl"}, "apk add python3 py3-onnxruntime"),
    ({"android": True}, "pkg install python onnxruntime"),
    ({"system": "FreeBSD", "version": [3, 12]}, "pkg install py312-onnxruntime"),
    ({"machine": "armv7l"}, "64-bit OS"),
    ({"libc": "glibc 2.17"}, "glibc 2.28 or newer"),
    ({"libc": "glibc 2.27"}, "glibc 2.28 or newer"),
])
def test_a_platform_without_onnxruntime_wheels_is_told_what_to_install(
        installer, facts, says):
    """pip used to fail there with a bare "No matching distribution found
    for onnxruntime", after a long resolve."""
    r = installer(LINUX, facts={"*": facts})
    assert r.rc == 1
    assert says in r.out
    assert not r.of("pip") and not r.of("venv")


def test_onnxruntime_from_the_os_is_used_through_system_site_packages(
        installer, tmp_path):
    """Alpine's py3-onnxruntime, Termux's onnxruntime, FreeBSD's port: pip
    must see that copy, or it goes looking for a wheel that does not
    exist."""
    r = installer(LINUX, facts={"*": {"libc": "musl", "ort": True,
                                      "kind": "managed"}}, tools=("pipx",))
    assert r.rc == 0, r.out
    venv = tmp_path / "home" / ".local" / "share" / "compartment" / "venv"
    assert r.of("venv") == [["--system-site-packages", str(venv)]]
    assert not r.of("pipx")


def test_termux_creates_the_vault_locked_because_it_has_no_shared_memory(
        installer):
    """Termux has no shm_open, so `init` would create the vault and then
    fail storing the session credential."""
    r = installer(LINUX, facts={"*": {"android": True, "ort": True}},
                  env={"TERMUX_VERSION": "0.119"})
    assert r.rc == 0, r.out
    assert r.init_args() == ["init", "--no-session"]
    assert "COMPARTMENT_PASSPHRASE" in r.out


# --- Linux: what gets installed, and re-running ------------------------------

def test_piped_into_bash_it_installs_compartment_not_the_project_next_door(
        installer, tmp_path):
    """`curl ... | bash` has no script file: $0 is "bash", so `dirname $0`
    was the current directory, and a pyproject.toml one level up - any
    other Python project the user was standing in - was installed instead
    of Compartment."""
    other = tmp_path / "someone-elses-project"
    (other / "docs").mkdir(parents=True)
    (other / "pyproject.toml").write_text('[project]\nname = "other"\n')
    vault = tmp_path / "home" / "existing.vault"
    vault.parent.mkdir(parents=True, exist_ok=True)
    vault.write_text("")
    r = installer(LINUX, stdin_script=True, cwd=other / "docs",
                  env={"COMPARTMENT_VAULT": str(vault)})
    assert r.rc == 0, r.out
    assert r.of("pip") == [["install", "--user", "--upgrade", "compartment"]]


def test_running_it_again_leaves_the_vault_alone_and_succeeds(installer,
                                                              tmp_path):
    """`init` refuses to overwrite a vault, so a second run used to end in
    an error after upgrading."""
    (tmp_path / "home" / ".compartment").mkdir(parents=True)
    (tmp_path / "home" / ".compartment" / "memory.vault").write_text("")
    r = installer(LINUX)
    assert r.rc == 0, r.out
    assert r.init_args() is None
    assert "kept as it is" in r.out


def test_a_legacy_engram_vault_counts_as_existing(installer, tmp_path):
    (tmp_path / "home" / ".engram").mkdir(parents=True)
    (tmp_path / "home" / ".engram" / "memory.vault").write_text("")
    r = installer(LINUX)
    assert r.rc == 0, r.out
    assert r.init_args() is None


# --- macOS --------------------------------------------------------------------

def test_homebrew_python_is_installed_with_pipx_on_macos(installer):
    r = installer(MACOS, facts={"*": {"kind": "managed", "machine": "arm64"}},
                  tools=("pipx",))
    assert r.rc == 0, r.out
    assert not [a for a in r.of("pip") if "--user" in a]
    assert [a for a in r.of("pipx") if a[:1] == ["install"]]


@pytest.mark.parametrize("machine, macos", [("x86_64", "15.1"),
                                             ("arm64", "13.6")])
def test_python_3_14_is_passed_over_where_onnxruntime_stops_at_3_13(
        installer, machine, macos):
    """onnxruntime has no Intel Mac build after 1.23.2 and no macOS 13 build
    after it either; 1.23.2 covers Python 3.11-3.13. With Homebrew's
    python3 at 3.14, pip failed on onnxruntime."""
    r = installer(MACOS, pythons=("python3", "python3.13"),
                  facts={"*": {"machine": machine},
                         "python3": {"version": [3, 14]},
                         "python3.13": {"version": [3, 13]}},
                  env={"STUB_MACOS": macos})
    assert r.rc == 0, r.out
    assert r.interpreter_of("pip")[0].endswith("python3.13")


def test_only_python_3_14_on_an_intel_mac_names_the_python_to_install(
        installer):
    r = installer(MACOS, facts={"*": {"machine": "x86_64",
                                      "version": [3, 14]}})
    assert r.rc == 1
    assert "brew install python@3.13" in r.out
    assert "x86_64 Python" in r.out
    assert not r.of("pip")


def test_a_mac_older_than_macos_13_is_told_it_is_unsupported(installer):
    r = installer(MACOS, facts={"*": {"machine": "arm64"}},
                  env={"STUB_MACOS": "12.7"})
    assert r.rc == 1
    assert "macOS 13 or newer" in r.out
    assert not r.of("pip")


def test_apple_silicon_on_a_current_macos_takes_python_3_14(installer):
    r = installer(MACOS, facts={"*": {"machine": "arm64", "version": [3, 14]}},
                  env={"STUB_MACOS": "15.1"})
    assert r.rc == 0, r.out
    assert r.init_args() == ["init"]


def test_both_scripts_parse():
    for script in (LINUX, MACOS):
        subprocess.run([BASH, "-n", str(script)], check=True)
