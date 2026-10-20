"""The Dockerfile's contract, read from the file: Docker is not available to
the suite, so what the image promises is pinned here instead.

The behaviour behind the run line - a uid with no home, HOME=/, the
passphrase in the environment - is exercised for real in
test_platform_session_store.py."""
import pathlib
import re

DOCKERFILE = (pathlib.Path(__file__).resolve().parents[1] / "Dockerfile"
              ).read_text(encoding="utf-8")


def _env():
    """Every ENV key=value the final stage sets."""
    final = DOCKERFILE.split("\nFROM ")[-1]
    pairs = {}
    for block in re.findall(r"^ENV ((?:.*\\\n)*.*)$", final, re.M):
        for key, value in re.findall(r"(\w+)=(\S+)", block):
            pairs[key] = value
    return pairs


def test_the_container_is_held_offline_at_runtime():
    """The header promised no outbound connection and nothing enforced it."""
    assert _env().get("COMPARTMENT_ASSERT_OFFLINE") == "1"
    assert "--network none" in DOCKERFILE


def test_one_server_per_container_runs_no_shared_embedding_daemon():
    """embed_daemon.enabled() names containers as wanting it off; the daemon
    also needs a writable session directory a --user run does not have."""
    assert _env().get("COMPARTMENT_EMBED_DAEMON") == "0"


def test_the_run_line_can_read_a_vault_owned_by_the_host_user():
    """The vault is 0600 and owned by the host user, so the image's uid
    10001 could not open a bind-mounted one on a Linux host."""
    assert '--user "$(id -u):$(id -g)"' in DOCKERFILE


def test_the_wheel_is_built_from_the_package_alone():
    """`COPY . /src` brought a checkout's stale build/ directory, which
    setuptools folds into the wheel, into the image's build."""
    build = DOCKERFILE.split("\nFROM ")[1]
    assert not re.search(r"^COPY \. ", build, re.M)
    assert re.search(r"^COPY src /src/src$", build, re.M)


def test_a_supported_python_and_both_architectures():
    bases = re.findall(r"^FROM python:(\d+\.\d+)-slim", DOCKERFILE, re.M)
    assert bases and all((3, 11) <= tuple(map(int, b.split("."))) <= (3, 14)
                         for b in bases)
    assert len(set(bases)) == 1
    assert "linux/amd64,linux/arm64" in DOCKERFILE
