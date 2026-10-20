"""What the embedder says when its runtime cannot be loaded: the remedy, in
a ModelError, which the CLI prints as one line and the MCP server passes to
the model, instead of an ImportError neither of them shows usefully."""
import sys

import pytest

from compartment import embed
from compartment.crypto import CryptoError


@pytest.mark.parametrize("platform, installer", [
    ("win-amd64", "vc_redist.x64.exe"),
    ("win-arm64", "vc_redist.arm64.exe"),
    ("win32", "vc_redist.x86.exe"),
])
def test_the_runtime_named_is_the_one_this_python_needs(monkeypatch, platform,
                                                       installer):
    """Windows on ARM with an ARM64 Python was sent to the x64 installer,
    which installs a runtime that Python cannot load."""
    monkeypatch.setattr(embed.os, "name", "nt")
    monkeypatch.setattr(embed.sysconfig, "get_platform", lambda: platform)
    err = embed._missing_runtime_error(ImportError(
        "DLL load failed while importing onnxruntime_pybind11_state: The "
        "specified module could not be found."))
    assert installer in str(err)


@pytest.mark.parametrize("module", ["onnxruntime", "tokenizers"])
def test_a_runtime_that_is_not_installed_says_how_to_install_it(monkeypatch,
                                                                module):
    """onnxruntime has no wheel for musl, 32-bit ARM, the BSDs or Android,
    and an install there can lack it: the CLI printed a traceback and the
    MCP server told the model "Error executing tool" and nothing else."""
    monkeypatch.setitem(sys.modules, module, None)    # import now fails
    with pytest.raises(embed.ModelError) as e:
        embed.Embedder()
    assert isinstance(e.value, CryptoError)          # what the CLI and server report
    assert f"-m pip install {module}" in str(e.value)
    assert sys.executable in str(e.value)


def test_any_other_missing_module_is_left_as_it_is(monkeypatch):
    err = ModuleNotFoundError("No module named 'flatbuffers'",
                              name="flatbuffers")
    assert embed._runtime_not_installed(err) is None


# ------------------------------------------------- CPUs in a container --
def _cgroup(tmp_path, own, files):
    root = tmp_path / "sys" / "fs" / "cgroup"
    for rel, text in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
    proc = tmp_path / "proc_self_cgroup"
    proc.write_text(own)
    return str(root), str(proc)


def test_a_cgroup_v2_quota_is_read_from_the_tightest_level(tmp_path):
    root, own = _cgroup(tmp_path, "0::/user.slice/agent.scope\n", {
        "user.slice/cpu.max": "150000 100000\n",
        "user.slice/agent.scope/cpu.max": "max 100000\n",
    })
    assert embed._cgroup_cpu_limit(root, own) == 1.5


def test_a_container_sees_its_own_quota_at_the_root(tmp_path):
    root, own = _cgroup(tmp_path, "0::/\n", {"cpu.max": "200000 100000\n"})
    assert embed._cgroup_cpu_limit(root, own) == 2.0


def test_a_cgroup_v1_quota_and_none(tmp_path):
    root, own = _cgroup(tmp_path, "4:cpu,cpuacct:/docker/abc\n", {
        "cpu,cpuacct/cpu.cfs_quota_us": "50000\n",
        "cpu,cpuacct/cpu.cfs_period_us": "100000\n",
    })
    assert embed._cgroup_cpu_limit(root, own) == 0.5
    root, own = _cgroup(tmp_path / "unlimited", "0::/\n",
                        {"cpu.max": "max 100000\n"})
    assert embed._cgroup_cpu_limit(root, own) is None


@pytest.mark.parametrize("limit, affinity, threads", [
    (2.0, 64, 2),        # docker run --cpus 2 on a 64-core host
    (0.5, 64, 1),        # never fewer than one
    (None, 64, 0),       # nothing limits it: onnxruntime's own choice
    (48.0, 64, 0),       # above half: its own choice already fits
    (None, 4, 4),        # taskset -c 0-3
])
def test_the_inference_pool_fits_the_cpus_this_process_may_use(
        monkeypatch, limit, affinity, threads):
    """onnxruntime sizes its pool from the host's cores and does not see a
    container's quota: 32 threads sharing two CPUs' worth of time, every
    search throttled until the next period."""
    monkeypatch.setattr(embed.os, "cpu_count", lambda: 64)
    monkeypatch.setattr(embed.os, "process_cpu_count", lambda: affinity,
                        raising=False)
    monkeypatch.setattr(embed.sys, "platform", "linux")
    monkeypatch.setattr(embed, "_cgroup_cpu_limit", lambda: limit)
    assert embed.intra_op_threads() == threads


def test_the_embedder_runs_with_that_many_threads(monkeypatch):
    monkeypatch.setattr(embed, "intra_op_threads", lambda: 2)
    e = embed.Embedder()
    assert e.sess.get_session_options().intra_op_num_threads == 2
    assert e.embed_query("router passphrase").shape == (e.dim,)


def test_a_model_folder_named_in_the_environment_needs_no_home(tmp_path,
                                                               monkeypatch):
    def no_home():
        raise RuntimeError("Could not determine home directory.")
    monkeypatch.setattr(embed, "home", no_home)
    monkeypatch.setenv("COMPARTMENT_MODEL_DIR", str(tmp_path / "models"))
    assert embed.user_model_dir() == tmp_path / "models"
