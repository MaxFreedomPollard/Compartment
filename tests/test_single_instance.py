"""One vault, one icon, however the copies were started.

`compartment init` on a pip install started the app twice: `set_login`
writes a RunAtLoad LaunchAgent and loads it, which starts one, and the next
line started another. The guard that should have caught it only worked for
bundled applications, and a pip install has no bundle, so every pip install
on macOS ended up with two menu bar icons.
"""
import subprocess
import sys
import textwrap
import time

import pytest

from compartment import menubar

HOLDER = textwrap.dedent("""
    import sys, time
    sys.path.insert(0, {src!r})
    from compartment import menubar
    handle, only = menubar.acquire_instance_lock({vault!r})
    print("HELD" if only else "STOOD-DOWN", flush=True)
    time.sleep(30)
""")


def _src_dir():
    import pathlib
    return str(pathlib.Path(menubar.__file__).resolve().parents[2])


@pytest.fixture()
def holder(tmp_path):
    """Another process holding the lock, which is the only way to test it:
    on POSIX a second flock from the SAME process succeeds by design."""
    vault = str(tmp_path / "memory.vault")
    p = subprocess.Popen(
        [sys.executable, "-c", HOLDER.format(src=_src_dir(), vault=vault)],
        stdout=subprocess.PIPE, text=True)
    assert p.stdout.readline().strip() == "HELD"
    yield vault
    p.kill()
    p.wait(timeout=10)


def test_the_first_copy_gets_the_lock(tmp_path):
    handle, only = menubar.acquire_instance_lock(str(tmp_path / "v.vault"))
    assert only is True
    assert handle is not None


def test_a_second_copy_is_told_to_stand_down(holder):
    handle, only = menubar.acquire_instance_lock(holder)
    assert only is False
    assert handle is None


def test_the_lock_is_released_when_the_holder_dies(tmp_path):
    vault = str(tmp_path / "memory.vault")
    p = subprocess.Popen(
        [sys.executable, "-c", HOLDER.format(src=_src_dir(), vault=vault)],
        stdout=subprocess.PIPE, text=True)
    assert p.stdout.readline().strip() == "HELD"
    _, only = menubar.acquire_instance_lock(vault)
    assert only is False          # while it lives
    p.kill()
    p.wait(timeout=10)
    for _ in range(50):           # the kernel drops it, no cleanup to do
        _, only = menubar.acquire_instance_lock(vault)
        if only:
            break
        time.sleep(0.1)
    assert only is True, "a killed copy locked the app out of its own menu bar"


def test_two_vaults_get_two_icons(tmp_path):
    """The lock is per vault, deliberately: someone running a second vault
    is running a second memory, and it needs its own icon."""
    a = tmp_path / "one" / "memory.vault"
    b = tmp_path / "two" / "memory.vault"
    _, first = menubar.acquire_instance_lock(str(a))
    _, second = menubar.acquire_instance_lock(str(b))
    assert first is True and second is True


def test_an_unwritable_home_still_gets_an_app(tmp_path, monkeypatch):
    """Never refuse to start because the lock could not be taken. Two icons
    is a nuisance; no icon is a broken install."""
    def boom(*a, **k):
        raise OSError("read-only file system")
    monkeypatch.setattr(menubar.Path, "mkdir", boom)
    handle, only = menubar.acquire_instance_lock(str(tmp_path / "v.vault"))
    assert only is True
    assert handle is None


def test_the_handle_is_kept_alive_by_the_module(tmp_path):
    """A file object that gets garbage collected closes its descriptor, and
    that drops the lock. Nothing about this works if the handle is dropped."""
    menubar.acquire_instance_lock(str(tmp_path / "v.vault"))
    assert menubar._INSTANCE_LOCK is not None
    assert not menubar._INSTANCE_LOCK.closed


def test_the_lock_lives_beside_the_vault(tmp_path):
    vault = tmp_path / "sub" / "memory.vault"
    menubar.acquire_instance_lock(str(vault))
    assert (tmp_path / "sub" / menubar.INSTANCE_LOCK_NAME).is_file()


def test_the_lock_name_is_hidden_and_not_the_vault_lock(tmp_path):
    """It sits in the same directory as the vault, so it must not collide
    with the vault's own flock file, and it should not clutter the folder."""
    assert menubar.INSTANCE_LOCK_NAME.startswith(".")
    assert "menubar" in menubar.INSTANCE_LOCK_NAME
    assert menubar.INSTANCE_LOCK_NAME != "memory.vault.flock"


# --- the fault that let two icons through -----------------------------------

def test_the_bundle_check_alone_cannot_see_a_pip_install():
    """Documents why the lock exists. A pip install runs as a bare Python
    with no bundle identifier, and the old guard read that identifier first,
    so it stood down before it ever looked for a peer."""
    pytest.importorskip("Foundation")
    from Foundation import NSBundle
    bundle_id = NSBundle.mainBundle().bundleIdentifier()
    if bundle_id == menubar.BUNDLE_ID:
        pytest.skip("running inside the real app bundle")
    # Either None (bare interpreter) or someone else's identifier: in both
    # cases the old check could not have found a running pip copy.
    assert bundle_id != menubar.BUNDLE_ID
