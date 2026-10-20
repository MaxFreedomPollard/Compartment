# Compartment as an MCP server in a container.
#
# The transport is stdio, so nothing is exposed and no port is published. The
# container talks to its host over the pipe it was started with, which is the
# same security property the native install has.
#
# The default embedding model ships inside the wheel, so the network is needed
# only while this image is being built. At runtime the container makes no
# outbound connection, and it is held to that twice: the offline guard is on
# (COMPARTMENT_ASSERT_OFFLINE=1, below), and the run line gives it no network.
#
#   Build:  docker build -t compartment .
#           docker buildx build --platform linux/amd64,linux/arm64 -t compartment .
#   Run:    docker run -i --rm --network none \
#             --user "$(id -u):$(id -g)" \
#             -v "$HOME/.compartment:/data" \
#             -e COMPARTMENT_PASSPHRASE \
#             compartment
#
# The vault is the user's data and is never baked into the image: mount it at
# /data. Create it once on the host with `compartment init` before the first
# container run, because init prompts for a passphrase and that passphrase is
# the only key to the vault.
#
# --user: the vault is mode 0600 and owned by you, so on a Linux host the
# image's own unprivileged user (uid 10001) cannot read a bind-mounted one;
# running as your own uid can. (Docker Desktop on macOS and Windows maps
# ownership itself and needs no --user.) A uid the image has never heard of
# gets HOME=/, which is fine: with the passphrase in the environment nothing
# is written outside /data.
#
# linux/amd64 and linux/arm64 only: onnxruntime, which Compartment cannot run
# without, publishes Linux wheels for x86_64 and aarch64 and nothing else.

FROM python:3.13-slim AS build

WORKDIR /src
# Only what the wheel is built from. `COPY .` also brought in whatever the
# checkout had lying about - a local build/ directory, whose stale build/lib
# setuptools folds into the wheel, a virtualenv, .git - into the build.
COPY pyproject.toml README.md LICENSE /src/
COPY src /src/src
RUN pip install --no-cache-dir build \
 && python -m build --wheel --outdir /dist

FROM python:3.13-slim

# Unbuffered so the stdio transport is not held up by Python's block buffering.
# The offline guard is sealed on for every process in the container. The
# shared embedding daemon is for a machine full of agents sharing one model;
# a container runs one server, and the daemon would be one more process
# wanting a writable session directory.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    COMPARTMENT_ASSERT_OFFLINE=1 \
    COMPARTMENT_EMBED_DAEMON=0

COPY --from=build /dist/*.whl /tmp/
RUN pip install --no-cache-dir /tmp/*.whl \
 && rm -rf /tmp/*.whl

# Runs unprivileged. /data is the mount point for the host's vault, so the uid
# here has to be able to read and write whatever is mounted there (see --user
# above).
RUN useradd --create-home --uid 10001 compartment \
 && mkdir -p /data \
 && chown compartment:compartment /data
USER compartment

VOLUME ["/data"]

# A stdio server has no listener for a probe to reach, and its health is the
# client's pipe: the MCP client sees at once when the server stops answering.
HEALTHCHECK NONE

# No EXPOSE and no port: stdio only, zero listeners.
ENTRYPOINT ["compartment", "--vault", "/data/memory.vault", "--caller", "docker", "serve"]
