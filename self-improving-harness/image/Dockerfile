FROM python:3.13-slim

# procps gives pgrep. The Windows mount shows files as another user's, and its
# checkouts usually have CRLF line endings.
RUN apt-get update \
    && apt-get install -y --no-install-recommends git procps \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --uid 1000 agent \
    && git config --system safe.directory /workspace \
    && git config --system core.autocrlf input

USER agent

# pip installs here: the agent owns it and has no root.
RUN python -m venv /home/agent/venv \
    && /home/agent/venv/bin/pip install --no-cache-dir pytest

ENV VIRTUAL_ENV=/home/agent/venv \
    PATH=/home/agent/venv/bin:$PATH \
    GIT_OPTIONAL_LOCKS=0
