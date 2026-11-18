# harness-builder sandbox image. Build with the command from
# harness_builder.executor.image_build.build_command() (context = this directory).
# python:3.11-slim multi-arch index digest, resolved 2026-11-16.
FROM python:3.11-slim@sha256:07d4824036af339fa1b6ef769cc99d9c10a141769538b52b3b597c84f7a7b535

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# Curated package set (OQ-6 ASSUMED): every package, transitive ones included,
# pinned with sha256 hashes in requirements.txt (versions match the host
# uv.lock). Wheels only, so no sdist build can pull unhashed build deps.
COPY requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir --require-hashes --only-binary=:all: \
        -r /tmp/requirements.txt \
    && rm /tmp/requirements.txt

# Non-root user. POSIX hosts override it with --user $(id -u):$(id -g).
RUN useradd --uid 10001 --user-group --no-create-home --home-dir /tmp \
        --shell /usr/sbin/nologin hb

COPY --chmod=0644 hb_entry.py /opt/hb/hb_entry.py

# hb-final is generated here (not copied) so a CRLF checkout cannot break it.
RUN printf '#!/bin/sh\nexec python /opt/hb/hb_entry.py final "$@"\n' \
        > /usr/local/bin/hb-final \
    && chmod 0755 /usr/local/bin/hb-final

USER 10001
WORKDIR /work
