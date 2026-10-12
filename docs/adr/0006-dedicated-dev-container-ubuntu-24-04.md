# 0006. Dedicated dev container on Ubuntu 24.04

- Status: Accepted
- Date: 2026-10-09

## Context

Development happens on a Windows 11 host, but FMUs are run Linux-first, the MCP server and workers are Python, and the pane needs a Claude Code with mod support running in the same environment. OpenModelica (needed by Phase 2) is officially packaged through apt for Ubuntu LTS. Agents must run the same commands the same way. The options were:

- **A WSL distro.** It's mutable and hand-configured, so hard to reproduce or reset.
- **Reusing an existing, unrelated container image.** It carries unrelated tooling, and its changes would couple the two projects.
- **A dedicated container.**

## Decision

The project gets a dedicated image, `.devcontainer/Dockerfile`, based on `ubuntu:24.04`:
- Node 24, copied from `node:24-bookworm-slim`.
- Claude Code, installed with npm.
- uv 0.11.14, copied from `ghcr.io/astral-sh/uv`, with uv-managed Python 3.12.
- Build tools, and a non-root user `dev` (uid 1000).
- OpenModelica (`omc` and the Modelica Standard Library cache `omlibrary`, pinned by `ARG OPENMODELICA_VERSION`) is installed from the official apt repo behind the build argument `WITH_OPENMODELICA` (default `1`). It was added ahead of Phase 2, to run the `omc` spike, and Phase 2's `fmu_build` uses it for Modelica sources.

`compose.yaml` runs service `dev` as container `agentic-fmi-dev` (`init: true`, `sleep infinity`):
- The repo is bind-mounted at `/workspace`.
- Named volumes hold `claude-home` (the login), `venv`, `uv-cache` and `lab` (the demo workspace).

All tooling runs through `docker exec`. `devcontainer.json` serves VS Code users.

## Consequences

- (+) The environment is reproducible and disposable, and identical for every developer and for CI. No Python is needed on the host.
- (+) The non-root user avoids root-owned files in the bind mount. Volumes keep the login, venv and caches fast and across rebuilds.
- (-) Docker Desktop is required.
- (-) Bind mounts from Windows are slow and may not fire inotify, so the venv lives in a volume, demos run in `/lab`, and plugin edits need `/reload-plugins`.
- (-) Git Bash rewrites paths, so commands need `MSYS_NO_PATHCONV=1`. The host path contains a space and must be quoted.
- (-) Claude Code is pinned (`ARG CLAUDE_CODE_VERSION`, now 2.1.295), so the pane's early-access mod API only changes when we bump it deliberately; bumps need a rebuild.
- (-) OpenModelica (`WITH_OPENMODELICA=1`, the default) grows the image from 1.74 GB to 2.34 GB on disk (content size 520 MB to 692 MB, about 135 MB downloaded). `WITH_OPENMODELICA=0` skips it. The version is pinned (`ARG OPENMODELICA_VERSION`), so a rebuild fails with an apt "version not found" error once the repo drops that build, and the pin has to be bumped deliberately. The repo key is not fingerprint-checked.
