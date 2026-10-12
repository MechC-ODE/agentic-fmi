# Dev image for Agentic FMI: Ubuntu 24.04 + Node (Claude Code) + uv-managed Python.
# Built via ../compose.yaml; also used by devcontainer.json.

FROM node:24-bookworm-slim AS node
FROM ghcr.io/astral-sh/uv:0.11.14 AS uv

FROM ubuntu:24.04

# Node comes from the official image (binary, npm, npx) so we avoid a NodeSource apt repo.
COPY --from=node /usr/local/bin/node /usr/local/bin/node
COPY --from=node /usr/local/lib/node_modules /usr/local/lib/node_modules
RUN ln -s ../lib/node_modules/npm/bin/npm-cli.js /usr/local/bin/npm \
 && ln -s ../lib/node_modules/npm/bin/npx-cli.js /usr/local/bin/npx
COPY --from=uv /uv /uvx /usr/local/bin/

RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      ca-certificates curl git tmux build-essential cmake pkg-config less nano \
 && rm -rf /var/lib/apt/lists/*

# OpenModelica (omc + Modelica Standard Library) from the official apt repo; set WITH_OPENMODELICA=0 to skip it.
# The version is pinned for reproducible rebuilds. If apt no longer offers it, bump it deliberately.
# The key fingerprints are printed to the build log; no authoritative value is pinned (see docs/development.md).
ARG WITH_OPENMODELICA=1
ARG OPENMODELICA_VERSION=1.27.1~2-g6db4671-1
RUN if [ "$WITH_OPENMODELICA" = "1" ]; then \
      apt-get update \
      && apt-get install -y --no-install-recommends gpg \
      && curl -fsSL https://build.openmodelica.org/apt/openmodelica.asc \
           | gpg --dearmor -o /usr/share/keyrings/openmodelica-keyring.gpg \
      && gpg --show-keys --with-colons /usr/share/keyrings/openmodelica-keyring.gpg | grep '^fpr' \
      && echo "deb [arch=amd64 signed-by=/usr/share/keyrings/openmodelica-keyring.gpg] https://build.openmodelica.org/apt noble stable" \
           > /etc/apt/sources.list.d/openmodelica.list \
      && apt-get update \
      && apt-get install -y --no-install-recommends "omc=${OPENMODELICA_VERSION}" "omlibrary=${OPENMODELICA_VERSION}" \
      && rm -rf /var/lib/apt/lists/*; \
    fi

# Pinned for reproducibility; bump deliberately.
ARG CLAUDE_CODE_VERSION=2.1.295
RUN npm install -g @anthropic-ai/claude-code@${CLAUDE_CODE_VERSION}

# Ubuntu 24.04 ships user "ubuntu" (uid 1000); replace it with "dev" so bind mounts match the host uid.
# The mount points are pre-created and owned by dev so named volumes inherit that ownership.
RUN userdel -r ubuntu \
 && groupadd --gid 1000 dev \
 && useradd --uid 1000 --gid 1000 --create-home --shell /bin/bash dev \
 && mkdir -p /home/dev/.claude /home/dev/.venvs /home/dev/.cache/uv /lab /workspace \
 && chown -R dev:dev /home/dev /lab /workspace \
 && git config --system --add safe.directory /workspace

ENV CLAUDE_CONFIG_DIR=/home/dev/.claude \
    UV_PROJECT_ENVIRONMENT=/home/dev/.venvs/agentic-fmi \
    UV_LINK_MODE=copy \
    UV_PYTHON_PREFERENCE=only-managed

USER dev
WORKDIR /workspace
RUN uv python install 3.12

CMD ["sleep", "infinity"]
