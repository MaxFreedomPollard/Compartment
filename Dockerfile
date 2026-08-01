# Compartment as an MCP server in a container.
#
# The transport is stdio, so nothing is exposed and no port is published. The
# container talks to its host over the pipe it was started with, which is the
# same security property the native install has.
#
# The default embedding model ships inside the wheel, so the network is needed
# only while this image is being built. At runtime the container makes no
# outbound connection.
#
#   Build:  docker build -t compartment .
#   Run:    docker run -i --rm \
#             -v "$HOME/.compartment:/data" \
#             -e COMPARTMENT_PASSPHRASE \
#             compartment
#
# The vault is the user's data and is never baked into the image: mount it at
# /data. Create it once on the host with `compartment init` before the first
# container run, because init prompts for a passphrase and that passphrase is
# the only key to the vault.

FROM python:3.11-slim AS build

WORKDIR /src
COPY . /src
RUN pip install --no-cache-dir build \
 && python -m build --wheel --outdir /dist

FROM python:3.11-slim

# Unbuffered so the stdio transport is not held up by Python's block buffering.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

COPY --from=build /dist/*.whl /tmp/
RUN pip install --no-cache-dir /tmp/*.whl \
 && rm -rf /tmp/*.whl

# Runs unprivileged. /data is the mount point for the host's vault, so the uid
# here has to be able to read and write whatever is mounted there.
RUN useradd --create-home --uid 10001 compartment \
 && mkdir -p /data \
 && chown compartment:compartment /data
USER compartment

VOLUME ["/data"]

# No EXPOSE and no port: stdio only, zero listeners.
ENTRYPOINT ["compartment", "--vault", "/data/memory.vault", "--caller", "docker", "serve"]
