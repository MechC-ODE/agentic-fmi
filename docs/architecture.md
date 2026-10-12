# Architecture

> **Status:** the MCP server, `fmu/`, the run store, the worker, the FMPy and OMSimulator engines, the supervisor, signals, analysis, plots, `fmu_build` with its build jobs ([ADR 0008](adr/0008-fmu-builds-as-supervised-jobs.md)), system files, system runs and SSP import and export ([ADR 0009](adr/0009-systems-with-omsimulator.md)), live runs (the gate, the command inbox, `live.json`, `run_control`; [ADR 0010](adr/0010-live-runs-and-the-pane.md)), response styles and the core and pane plugins exist. Parts not built yet are marked *(planned)*.

## Goal

Let Claude users explore FMUs (FMI 2.0 and 3.0) in natural language. They can inspect, validate, simulate, compare, report on, modify, compose and generate FMUs, and watch or steer simulations live from inside Claude Code. Claude stays in the loop: it reads compact results, decides the next experiment and acts again.

## System context

![System context: the user talks to Claude Code, which calls the MCP server over stdio. The server spawns workers and, with them, shares the run store. Workers run the FMUs' native code. The pane plugin reads and writes the run store.](diagrams/system-context.svg)

*Source: [docs/diagrams/system-context.mmd](diagrams/system-context.mmd) (edit, then run scripts/render-diagrams.sh)*

The orange box is the only place FMU native code and build tools run.

## Containers (processes)

Everything runs inside the dev container `agentic-fmi-dev`. See [ADR 0006](adr/0006-dedicated-dev-container-ubuntu-24-04.md) and [development.md](development.md).

![Containers: inside the dev container, the Claude Code process hosts the core plugin and the pane mod. The core plugin starts the afmi mcp server over stdio, which spawns one afmi worker per run. The server and the worker share the run store, and the pane mod polls it and writes commands to it.](diagrams/containers.svg)

*Source: [docs/diagrams/containers.mmd](diagrams/containers.mmd) (edit, then run scripts/render-diagrams.sh)*

The orange box is the only place FMU native code and build tools run.

| Component | Responsibility | Never does |
|---|---|---|
| Claude Code + core plugin | Hosts the conversation, starts the MCP server from the `mcpServers` entry in `.claude-plugin/plugin.json` (through `scripts/afmi-mcp`; a root `.mcp.json` is avoided because Claude Code would also load it as project config), and provides skills such as `fmi-expert` | n/a |
| MCP server `afmi mcp` | Validates requests, reads `modelDescription.xml`, creates runs, spawns and supervises workers, summarizes results and renders plots | Load FMU binaries, or print to stdout (stdout carries MCP) |
| Worker `afmi worker` | Loads the FMU through an engine, simulates, writes the heartbeat and results. For a build job (`kind: "build"`) it runs the build tool instead and commits the FMU (Phase 2). For a system run (`kind: "system"`, Phase 3) it verifies the OMSimulator pin and builds the OMSimulator model from the spec. For a live run (Phase 4) its gate pauses, paces, steps, applies inbox commands (one outcome each) and feeds `live.json` | Talk to Claude or the pane directly, or open a user's SSP |
| Build tool (pythonfmu or `omc`) *(Phase 2)* | Turns the private copy of one source into an FMU, inside the build job's worker process group, in `runs/<id>/work/` | Run in the MCP process, read the original source, or write outside its work directory |
| Run store | Shared state for runs (batch and live), as files ([protocol.md](protocol.md)) | Hold anything that isn't reproducible from `spec.json` |
| Pane `agentic-fmi-pane` *(Phase 4, early access)* | Shows a run picker, status, values with units and charts of the signals the user picks, at most 3 (`live.json`), built FMUs (`build.json`) and a system's connections (`spec.json`), a status line and toasts. Its buttons write command files | Compute metrics, call Python, read anything outside the run store (XML, SSP, `result.csv`) or `events.jsonl`, or write anything but `commands/` |

## Principles

1. **Native code is isolated.** FMU native code never runs in the MCP process, and neither do build tools, which execute the source's code. Workers are isolated, with a heartbeat, a timeout and a kill. ([ADR 0002](adr/0002-isolated-worker-process-per-run.md), [ADR 0008](adr/0008-fmu-builds-as-supervised-jobs.md))
2. **Context economy.** Tools return summaries, metrics and plots, never raw series. In ninja mode (the `response_style` tool; the choice is kept in the store's `preferences.json`) run, build and live-control results shrink further to a one-line status that Claude relays, and nothing else changes.
3. **Reproducibility.** Every run records its spec, the FMU's sha256 and the tool versions.
4. **One contract.** The versioned file protocol is the only contract between Python and the pane. ([ADR 0003](adr/0003-file-based-run-store-protocol.md))
5. **Originals are never mutated.** Built and derived FMUs, parameter sets and systems are new files. Builds write only `generated/<name>/<name>.fmu` and its `build.json`.
6. **Docs are part of done.**

Engineering practice: apply SOLID pragmatically, with KISS and YAGNI. The one real seam is the engine signature in `engines/base.py`: each engine is one function `simulate(spec, directory, gate) -> EngineResult`, FMPy for single FMUs and OMSimulator for systems, held to the same shared contract tests ([ADR 0009](adr/0009-systems-with-omsimulator.md)). There is no `Engine` class or `Instance` protocol. Everything else is plain functions and data models. See [CONTRIBUTING.md](../CONTRIBUTING.md#code-conventions).

## Inside the Python package

![Package dependencies: cli.py starts either the server side (mcp/server.py, runs.py, control.py, ssp.py, style.py, budget.py, plots.py, supervisor.py) or the worker side (worker.py, which imports engines/ lazily). Both sides import the shared modules live.py, system.py, build.py, fmu/, analysis/, signals.py and store/. The server side never imports the worker side.](diagrams/package-dependencies.svg)

*Source: [docs/diagrams/package-dependencies.mmd](diagrams/package-dependencies.mmd) (edit, then run scripts/render-diagrams.sh)*

Arrows are imports, drawn for the main dependencies only. The orange boxes are the worker side. `supervisor.py` starts the worker as a separate process (`python -m agentic_fmi worker`), which is not an import. The worker imports `proc_stat` from `supervisor.py`, and `engines/fmpy_engine.py` reads `fmu/inspect.py` for variable types. `config.py` and `errors.py`, which nearly every module imports, are left out.

The dependency rule: `mcp/`, `fmu/`, `runs.py`, `system.py`, `ssp.py` and `supervisor.py` never import `engines/` or `worker.py`, so the server can't load a binary by accident. Only `engines/` and `worker.py` may use FMPy's loading and simulation APIs (`simulate_fmu`, `instantiate_fmu`, `extract`, `fmpy.simulation`, `fmpy.fmi1/2/3`). The ruff rule TID251 enforces this, with per-file ignores for `engines/` and `worker.py`. It also bans importing `pythonfmu` anywhere in `src/` (builds run it as a subprocess) and `OMSimulator` outside `engines/` (`import OMSimulator` loads `libOMSimulator.so`): only `engines/oms_engine.py` imports it, and the worker imports that module lazily, after `oms_pin.py` has verified the installed package (ADR 0009). A test checks that the server's modules leave `OMSimulator` out of `sys.modules`.

`build.py` *(Phase 2)* serves both sides without running anything on the server (the log parsers and the diagnostics text are in `build_diagnostics.py`, pure functions over text): `runs.start_build` calls its validation (`check_request`, `build_spec`), and `worker.py` calls `execute_build` for a job whose spec has `kind: "build"`. It imports no server-only module, so the worker stays free of `runs.py` and matplotlib. Core functions raise `errors.UserError`, which `mcp/server.py` turns into `ToolError`. The MCP adapters stay thin: the core functions and `budget.py` cut every result to the output budget (8 KB, 16 KB hard cap), and the adapters only format it.

**Engine seam** *(Phase 3)*. `engines/base.py` holds `LiveGate`, `Live`, `EngineResult`, `ended_early` and `TIME_COLUMN`. `engines.fmpy_engine.simulate(spec: RunSpec, fmu_dir, gate)` and `engines.oms_engine.simulate(spec: SystemSpec, work_dir, gate)` share that signature and return the same result (time first, then the outputs in order, with units), so `result.csv`, summaries, `run_results`, `run_plot` and `run_compare` don't know which engine ran. `worker.py` picks one by the spec's `kind`. Shared contract tests hold both engines to finishing, stopping cleanly with a partial result, column order and units, and reporting errors as `simulation_error`.

**Live control** *(Phase 4, [ADR 0010](adr/0010-live-runs-and-the-pane.md))*. The gate becomes `gate(t, live)`: `Live` (`engines/base.py`) is the engine's handle on the running simulation, with `get` and `set`, implemented by both engines (FMPy over `recorder.fmu`, OMSimulator over its instance). `live.py` holds the gate of live runs (one wait loop for pause, pacing, single steps and inbox polling; waiting is excluded from the watchdog), the `live.json` ring buffer and `settable`, the one rule for what a live `set` may change, which `control.py` (server: `run_control`, `run_status(values)`) also uses. `store/inbox.py` holds the command and outcome files. `live.py` imports no engine library at run time, so the server may import it.

`system.py` *(Phase 3)* owns the system file model, its checks (reusing `fmu/inspect.py`) and the parameter layers. `ssp.py` owns SSP import and export and the one SSV reader; it parses XML only, with a hardened lxml parser. Both run on the server. `oms_pin.py` hashes the installed OMSimulator package without importing it, for the server and the worker alike.

## Flow: a batch run

![Batch run sequence: Claude calls run_start on the MCP server, which validates the request, writes the run's spec and queued status to the run store and spawns a worker. The worker records running with a heartbeat, simulates, and writes the results and the finished status. The server returns a summary or the run id, and Claude then reads, downsamples and plots the results.](diagrams/batch-run-sequence.svg)

*Source: [docs/diagrams/batch-run-sequence.mmd](diagrams/batch-run-sequence.mmd) (edit, then run scripts/render-diagrams.sh)*

If the worker crashes or hangs, its heartbeat goes stale or its pid disappears; the server kills and reaps its process group, confirms it is gone, and only then marks the run `failed{worker_lost}` with the tail of `log.txt`. A run past `timeout_s` is ended by the worker's own watchdog (`failed{timeout}`). `run_stop` sends SIGTERM, so the step gate stops the run cleanly, and SIGKILL after a grace period; a live run can also be ended with the `stop` command of `run_control`. See [ADR 0007](adr/0007-run-store-location-ids-and-supervision.md).

## Flow: a build job *(Phase 2)*

Claude writes a source with its own Write or Edit tool (the user sees it in the write prompt), then calls `fmu_build(source, name?, overwrite?, wait_s?)` ([ADR 0008](adr/0008-fmu-builds-as-supervised-jobs.md)):
1. **Validate, without a subprocess.** `build.check_request` resolves the source inside the workspace (outside the store and `generated/`) and reads it once (at most 256 KiB). It checks the extension (`.py` for pythonfmu, `.mo` for `omc`), the `Fmi2Slave` marker and module name or the Modelica class name, that `omc` is installed, and the output name and directory (the `overwrite` rule). A refusal is a `ToolError` with the fix.
2. **Create the job.** Under the same admission lock and `max_workers` limit as runs, the server checks that no build of the same name is queued or running, claims a run ID and writes `source/<file>` (the hashed bytes), a `BuildSpec` (`kind: "build"`), the `queued` status, the index entry and a `created` event. Then it spawns the ordinary worker.
3. **Build in the worker.** The worker takes the job over like a run (heartbeat, watchdog with `Timing.build_timeout_s`, SIGTERM stops it) and calls `build.execute_build`. The tool runs in `work/` with argv only, an allowlisted environment and output to `build.log`, inside the worker's process group, so every group kill reaches it.
4. **Commit or report.** On success the worker checks the FMU's `modelDescription.xml` and disarms the watchdog. Under the store lock it writes `generated/<name>/<name>.fmu`, then `build.json` (each atomically), a copy of the record in the run directory, one `fmu_built` event and `finished`. On failure it writes `failed{build_failed}`, whose message is the diagnostics (`path:line[:col]: error: text` lines and the log's path); `generated/` is untouched.
5. **Return.** `fmu_build` waits up to `wait_s` (default 45 s). It returns the built FMU's path, sha256, interfaces and counts, or raises the diagnostics as a `ToolError`, or returns the run ID to poll with `run_status` (`run_stop` cancels). The new FMU works at once with `fmu_inspect`, `fmu_validate` and `run_start`.

This isolates the build, but it is not a sandbox: the tool runs as the user. pythonfmu builds still have network access. Only `omc` is cut off: its environment holds an unroutable proxy (`OMC_OFFLINE_ENV`), which blocks its own network access, so it can't refresh its package index or fetch a library. Compiled model code is not restricted.

## Flow: a system run *(Phase 3)*

Claude writes a system file (JSON: components with FMU paths and parameters, connections from outputs to inputs, an experiment with the communication step) with its own Write or Edit tool, then ([ADR 0009](adr/0009-systems-with-omsimulator.md)):
1. **Check.** `system_check(system)` loads the file and every FMU's `modelDescription.xml` and lists errors and warnings, each with its fix: unknown names (close matches), causality, more than one driver, types, units, runnability, unconnected inputs.
2. **Run.** `run_start(system=..., paramset=..., start_values={"PI.Kp": 12}, output_interval=h)` refuses early if the installed OMSimulator doesn't match `config.OMSIMULATOR_PIN`, repeats the checks, resolves the parameter layers (FMU start < system file < `.ssv` < `start_values`) and writes a fully resolved `SystemSpec` (`kind: "system"`). Then it spawns the ordinary worker, like any run.
3. **Simulate in the worker.** The worker re-verifies the pin (`failed{oms_changed}` on a mismatch), copies each FMU to `work/fmus/<component>.fmu` while hashing it, sets `TMPDIR` to `work/tmp` and imports the OMSimulator engine. The engine builds the model through the API, turns the result file off, sets the parameters, initializes, and then calls `doStep()` once per communication step, reading every output with `getValue()` (post-exchange values) and calling the gate. The result is written like a run's.
4. **Read.** `run_status`, `run_results`, `run_plot` and `run_compare` work unchanged; columns are `<component>.<variable>`.
5. **Exchange.** `system_export(system, dest, paramset?)` writes a byte-reproducible `.ssp` that OMSimulator and other SSP tools open. `system_import(source, name?)` turns a flat `.ssp` from any tool into `imported/<name>/<name>.json` plus private FMU copies. It treats the archive as untrusted: it reads only referenced members, with streamed caps, and refuses unsafe references and any DOCTYPE.

OMSimulator's `oms_ma` master is Jacobi-style: all components step from the same point with the previous exchange's inputs, which adds up to a step of delay around a loop. The communication step is therefore part of the system file, and the authoring skill tells Claude to choose it consciously.

## Flow: a live run *(Phase 4, [ADR 0010](adr/0010-live-runs-and-the-pane.md))*

A live run is an ordinary run with `live: true`, for a Co-Simulation FMU or a system. Model Exchange runs stay batch: spike (i) showed that FMPy's `step_finished` can't apply a set correctly while CVode runs, so `live=true` with an ME-only FMU is refused with a hint.

![Live run sequence (Phase 4): Claude starts a live run and the worker pauses at t0. Claude sets a gain through run_control: the server checks it, writes a command file, the worker's gate applies it and writes an outcome with t, which the server reads. Claude resumes at speed 2; the worker paces its steps and writes live.json. The pane polls the store, draws charts, writes a pause command and toasts its outcome. Claude reads values with run_status and stops the run.](diagrams/live-session-sequence.svg)

*Source: [docs/diagrams/live-session-sequence.mmd](diagrams/live-session-sequence.mmd) (edit, then run scripts/render-diagrams.sh)*

1. **Start.** `run_start(..., live=true)` writes a spec with `live` and `idle_limit_s`, spawns the worker and waits until the run is `paused` at its start time. The engine calls the gate once after initialization (FMPy: a wrapped `exitInitializationMode` on an instance passed to `simulate_fmu`; OMSimulator: after `initialize()`), and the gate waits there.
2. **Control.** `run_control(run_id, action, n?, dt?, values?, speed?, wait_s)` checks the request (`set`: only scalar inputs nothing drives and tunable parameters, within `[min, max]`; `step`: only from `paused`), creates a command file exclusively in `runs/<id>/commands/` and waits for its outcome in `runs/<id>/outcomes/`, the run's end or `wait_s`. The pane's buttons write command files too; the worker checks those again and rejects a bad one without stopping the run.
3. **Gate.** One wait loop in the gate handles pause, pacing (speed factor, 1x real time by default, changed by `resume(speed=...)`), single steps and inbox polling, sleeping at most 50 ms at a time; the watchdog counts only time spent stepping. Each command gets exactly one outcome with its simulation time `t`, plus a `command` event. A run paused longer than its idle limit (30 min) is stopped with its partial result and a reason.
4. **Watch.** The heartbeat thread writes `status.json` (`paused`/`running`, `speed`, `last_cmd`, `heartbeat_mono`) and, at most 5 times a second, `live.json` (current values with units, the history of the charted signals). `run_status(values=[...])` and the pane read it.
5. **End.** `stop` (a command), `run_stop` (SIGTERM, seen within one wait slice) or the idle limit ends the run as `stopped` with its partial result; a resumed run can also finish normally. Results, plots and comparisons work as for any run; the command and outcome files are its replay log.

**Engine outcomes** shared by both engines: a step that makes no progress (an FMU discarding without terminating) is `simulation_error` in batch and live runs alike; FMPy's stall check ends what used to be an endless retry. An FMU that requests termination ends `finished` with "ended early at t" in FMPy. OMSimulator never reports it: with FMI 2.0 the clock stops, so it is `simulation_error` like a discard; with FMI 3.0 it ignores the request and the run goes on until the FMU itself fails (`simulation_error`) or reaches `t_stop`.

**ScheduledExecution-only FMUs** (such as Reference `Clocks`) can't be run by `simulate_fmu`. `inspect` reports them as unsupported.

**Container FMUs** (`fmpy.container_fmu.cli.create_container_fmu`) are partial: FMI 3.0 only, with workarounds, and not on the critical path. OMSimulator orchestrates systems ([ADR 0004](adr/0004-omsimulator-as-multi-fmu-orchestrator.md)). `create_container_fmu` runs `shutil.rmtree` on its `unzipdir` first, so pass it an empty directory that we own.

## How the three loops close

- **Agent loop** *(Phase 1)*: Claude calls a tool, gets compact metrics and plots, and picks the next experiment. Context economy keeps this loop affordable.
- **Control loop** *(Phases 3–4)*: controller and plant FMUs are co-simulated inside one worker by OMSimulator, which is fast and deterministic. Alternatively, Claude or the user steers a live run through `run_control` or the pane's buttons (pause, resume at a speed, step, set, stop), at roughly 0.1 s per hop.
- **Event loop**: worker events go to `events.jsonl` (including `fmu_built` from Phase 2 and `command` from Phase 4) for humans and Claude; the pane turns state transitions in `index.json` and `status.json`, and the outcomes of its own commands, into toasts and status-line updates (it never tails `events.jsonl`, ADR 0010).

## Security

FMUs and build sources are executable. The safeguards:
- FMUs and sources load only from allowed roots: the workspace, with symlinks resolved before the check. More roots (`fmu_dir`) come later.
- Tools never download anything. A pythonfmu build is not offline: it runs the user's code with network access, and builds are not sandboxed ([ADR 0008](adr/0008-fmu-builds-as-supervised-jobs.md)). The proxy blocks omc's network access, so it can't refresh its package index.
- Claude writes every source with its own Write or Edit tool, so the user sees the code before approving `fmu_build`. That review is the trust boundary for builds.
- Build tools run only in workers, from a private copy, with argv only and an allowlisted environment. The server never imports `pythonfmu` or the source.
- Originals are never overwritten. `overwrite=true` replaces only an earlier `fmu_build` output (a directory that holds `build.json`). `system_import` and `system_export` only create new files.
- SSP files are untrusted input *(Phase 3)*. `system_import` reads only the members the SSD references, through streamed caps, and names every output itself. It refuses unsafe references, duplicate or encrypted entries and any DOCTYPE. The worker never opens a user's SSP ([ADR 0009](adr/0009-systems-with-omsimulator.md)).
- The OMSimulator install is pinned by the sha256 of its package tree, because its sdist downloads binaries without a hash. The server refuses a mismatch, and the worker fails closed before importing it. This detects a change; it doesn't prove authenticity.

## Key decisions

| ADR | Decision | Status |
|---|---|---|
| [0001](adr/0001-mcp-server-as-integration-layer.md) | An MCP server is the integration layer | Accepted |
| [0002](adr/0002-isolated-worker-process-per-run.md) | Each run gets an isolated worker process (live runs included) | Accepted |
| [0003](adr/0003-file-based-run-store-protocol.md) | A file-based, versioned run-store protocol | Accepted |
| [0004](adr/0004-omsimulator-as-multi-fmu-orchestrator.md) | OMSimulator orchestrates multi-FMU systems | Accepted |
| [0005](adr/0005-pane-as-separate-early-access-plugin.md) | The pane is a separate early-access plugin | Accepted (amended by 0010) |
| [0006](adr/0006-dedicated-dev-container-ubuntu-24-04.md) | A dedicated dev container on Ubuntu 24.04 | Accepted |
| [0007](adr/0007-run-store-location-ids-and-supervision.md) | Run-store location, run IDs claimed by mkdir, worker supervision | Accepted |
| [0008](adr/0008-fmu-builds-as-supervised-jobs.md) | One `fmu_build` tool, builds as supervised jobs, `build.json` provenance | Accepted |
| [0009](adr/0009-systems-with-omsimulator.md) | Systems with OMSimulator: one run path, SSP and SSV as exchange formats, a pinned install | Accepted (amended by 0010) |
| [0010](adr/0010-live-runs-and-the-pane.md) | Live runs are ordinary runs: a `gate(t, live)` with one wait loop, a command inbox with one outcome per command, `live.json`; the pane reads only the run store | Accepted |
