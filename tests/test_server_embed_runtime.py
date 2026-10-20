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
