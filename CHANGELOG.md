# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project follows
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.1.0] - 2026-10-12

First public release.

### Added

- **MCP server `fmi`** with 18 tools for Claude: list, inspect, validate and simulate FMUs (FMI 2.0 and 3.0, Co-Simulation and Model Exchange), compare runs, read results and plot them, with compact summaries and PNG plots instead of raw time series.
- **Isolated workers**: every run and build executes in its own process with a heartbeat, a timeout and a kill; the server never loads FMU binaries.
- **Reproducible run store** `.agentic-fmi/`: each run records its settings, the FMU's sha256 and the tool versions; a versioned file protocol with JSON Schemas in `docs/schemas/`.
- **FMU generation** with `fmu_build`: Python sources through pythonfmu and Modelica sources through OpenModelica, as supervised background jobs with compiler diagnostics and build provenance (`build.json`).
- **Connected systems** with OMSimulator: JSON system files, `system_check`, closed-loop runs, parameter sets, and SSP/SSV import and export.
- **Live runs**: `run_start(live=true)` and `run_control` to pause, resume at a speed, step, set tunable parameters and stop a running simulation, for single FMUs and systems; `run_status(values=...)` for current values.
- **The live pane** (`agentic-fmi-pane` plugin, `/fmi`): run picker, tabs for the live run, the system's connections, built FMUs and all runs; progress, controls, values grouped by component, and up to three line charts chosen with a click; a status line and notifications.
- **Response styles**: talkative (default) or ninja (one status line per run), switched by asking Claude.
- **Skills** for Claude: `fmi-expert`, `fmu-authoring`, `system-builder` and `control-engineer`.
- **Dev container** (Ubuntu 24.04 with Claude Code, uv, FMPy, OMSimulator and OpenModelica), a user guide, architecture and protocol docs, and decision records.
