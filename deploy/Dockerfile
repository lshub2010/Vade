# Container image for the Vade core — sample.
#
# There is no upstream image on a registry: this builds from the published
# release tarball. Pin VADE_VERSION and verify the checksum in CI if you
# rebuild; the Dockerfile below installs the tarball as-is.
#
#   docker build -f deploy/Dockerfile -t vade-core:0.0.4-alpha \
#     --build-arg VADE_VERSION=0.0.4-alpha \
#     --build-arg VADE_ARCH=x86_64 .
#
# Build context is the repository root only so the ARGs resolve; no repo files
# are copied into the image.

# syntax=docker/dockerfile:1
FROM python:3.12-slim

ARG VADE_VERSION=0.0.4-alpha
ARG VADE_ARCH=x86_64
ARG VADE_BASE_URL=https://github.com/lshub2010/Vade/releases/download/v${VADE_VERSION}

ENV PYTHONPATH=/opt/vade/core:/opt/vade/protocol/core/python \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

RUN apt-get update \
 && apt-get install -y --no-install-recommends curl ca-certificates \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /opt/vade

RUN curl -fsSL -o /tmp/vade.tar.gz \
        "${VADE_BASE_URL}/vade-core-${VADE_VERSION}-linux-${VADE_ARCH}.tar.gz" \
 && tar -xzf /tmp/vade.tar.gz --strip-components=1 -C /opt/vade \
 && rm -f /tmp/vade.tar.gz \
 && ./install.sh \
 && rm -rf /opt/vade/core/tests

# Unprivileged runtime user. UID/GID 10001 must match `user:` in compose if
# you override it.
RUN groupadd --system --gid 10001 vade \
 && useradd --system --uid 10001 --gid vade --home-dir /var/lib/vade \
        --shell /usr/sbin/nologin vade \
 && install -d -m 0700 -o vade -g vade /var/lib/vade

USER vade

# Data plane inside the container. `--bind` overrides [core].listen, so a
# loopback core.toml can still be published by the container runtime.
# The admin plane stays on 127.0.0.1:8444 (loopback inside the container):
# reach it with `docker compose exec`, not through a published port.
EXPOSE 8443

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
  CMD ["/opt/vade/.venv/bin/python", "-c", "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8443/healthz', timeout=3).status == 200 else 1)"]

ENTRYPOINT ["/opt/vade/.venv/bin/python", "-m", "vade"]
CMD ["--config", "/etc/vade/core.toml", "--bind", "0.0.0.0:8443", "--strict"]
