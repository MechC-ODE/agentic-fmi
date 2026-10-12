# Run-store protocol

> **Status: Accepted (v1).** The models live in [`src/agentic_fmi/store/protocol.py`](../src/agentic_fmi/store/protocol.py). The JSON Schemas generated from them are committed in [`docs/schemas/`](schemas/), and a drift test keeps the two in sync. Version 1 grew additively: build jobs (`BuildSpec`, `build.json`, the `fmu_built` event, the `build_failed` code; [ADR 0008](adr/0008-fmu-builds-as-supervised-jobs.md)). System runs (`SystemSpec` with `kind: "system"`, the `oms_changed` code; [ADR 0009](adr/0009-systems-with-omsimulator.md)). Live runs ([ADR 0010](adr/0010-live-runs-and-the-pane.md)): `live` and `idle_limit_s` in both run specs, the `paused` state, `heartbeat_mono`, `speed` and `reason` in `status.json`, the `command` event, and three documents: commands in the [inbox](#inbox), their outcomes, and [`live.json`](#livejson). The pane's chart picker adds the `chart` action and `Command.names` (additive, protocol stays 1). The worker (`live.py`, `store/inbox.py`) writes and reads them; the server (`control.py`, behind `run_control` and `run_status(values)`) writes the commands and reads the outcomes and `live.json`. Response styles add one more additive file, [`preferences.json`](#preferencesjson).

The run store is the only contract between the MCP server, the workers and the pane ([ADR 0003](adr/0003-file-based-run-store-protocol.md)). [ADR 0007](adr/0007-run-store-location-ids-and-supervision.md) decides its location, the run-ID grammar and supervision.

## Location

- The store is `<workspace>/.agentic-fmi/`, where `<workspace>` is passed as `afmi mcp --workspace` (the Claude Code project directory). It is gitignored.
- `AFMI_STORE_DIR` overrides the location. A relative value is resolved against the workspace. `config.py` is the only reader.
- Keep the store on a Linux filesystem, such as a workspace under `/lab` (see [Performance](#performance)).

## Layout

```text
.agentic-fmi/
  index.json                 every run with its cached state (MCP servers only, under .lock)
  events.jsonl               append-only lifecycle events, one JSON object per line
  .lock                      flock target: index.json read-modify-write and the server's terminal status writes
  preferences.json           Preferences: the response style (talkative or ninja); the MCP server only; optional
  runs/
    r12/                     one batch run; the directory is created by the ID claim
      spec.json              everything needed to reproduce the run; immutable
      status.json            authoritative state, progress, heartbeat, pid, error
      summary.json           compact results (finished or stopped runs)
      result.csv             full time series; never returned raw to Claude
      log.txt                worker stdout and stderr, including FMU log messages
      fmu/                   the extracted FMU while the worker runs; removed when it exits
      plots/*.png            PNGs rendered by run_plot
    r13/                     one build job (spec kind "build")
      spec.json              BuildSpec: source path and sha256, name, backend, options; immutable
      status.json            as for a run; t_start = t_stop = t = 0, progress 0 until finished
      source/<file>          private copy of the hashed source bytes; the worker builds from it
      work/                  the build tool's cwd and TMPDIR; removed when the worker exits
      build.log              the build tool's stdout and stderr
      build.json             BuildRecord of a successful build (the same record as below)
      log.txt                the worker's own output (tracebacks)
    r14/                     one system run (spec kind "system")
      spec.json              SystemSpec: components with FMU refs, connections, parameters, master, step; immutable
      status.json            as for a run
      summary.json           as for a run; variables are <component>.<variable>
      result.csv             as for a run; one row per communication step, post-exchange values
      log.txt                worker and OMSimulator output (info and warning lines, error lines)
      work/                  fmus/<component>.fmu (hashed private copies), ssp/ (OMSimulator's model copies) and tmp/ (its TMPDIR); removed when the worker exits
      plots/*.png            as for a run
    r15/                     one live run of either kind (spec live: true): the files of a run or system run, plus
      live.json              LiveSnapshot: current values and chart history; the worker's heartbeat thread only; kept after the run
      commands/<id>.json     Command, <ms13>-<server|pane>-<rand8>.json; written once by the server or the pane
      outcomes/<id>.json     CommandOutcome, one per command file; the worker only

<workspace>/generated/<name>/  written only by a successful build job
  <name>.fmu                 the built FMU
  build.json                 its BuildRecord; replaced only by an overwrite build

<workspace>/imported/<name>/   written only by system_import, never replaced
  <name>.json                a system file (not a run-store document; schema below); its stem names the system in run_list
  <component>.fmu            private copies of the FMUs the SSP held, one per component
<workspace>/imported/.<name>.tmp-<pid>-<thread>/   system_import's staging directory: renamed to <name>/ when complete, removed on any failure
```

The server's temporary command files (`commands/.<id>.tmp-<pid>-<thread>`) start with a dot and never match the command grammar.

## Run IDs

- Run IDs match `^r[1-9]\d*$` (`r1`, `r12`).
- A server claims an ID by creating its directory with an exclusive `os.mkdir`, starting at one above the highest existing number and incrementing on `FileExistsError`. This keeps IDs unique across threads and across servers that share a store.
- Never parse a directory that doesn't match the grammar as a run.
- Build jobs and system runs take IDs from the same sequence (`kind` in the spec and the index tells them apart), so `run_status`, `run_list` and `run_stop` serve them unchanged.
- The prefix `st` is reserved. There are no sessions: a live run is an ordinary run with `live: true` ([ADR 0010](adr/0010-live-runs-and-the-pane.md)).

## Documents and schemas

| File | Model | Schema | Writer | Readers |
|---|---|---|---|---|
| `runs/<id>/spec.json` | `RunSpec` | [spec.schema.json](schemas/spec.schema.json) | MCP server, once, before spawn | Worker, server |
| `runs/<id>/status.json` | `RunStatus` | [status.schema.json](schemas/status.schema.json) | See [Status ownership](#status-ownership) | Server, pane |
| `runs/<id>/summary.json` | `RunSummary` | [summary.schema.json](schemas/summary.schema.json) | Worker, with `result.csv` | Server |
| `index.json` | `Index` (`IndexEntry` items) | [index.schema.json](schemas/index.schema.json) | MCP servers, under the lock | Server, pane |
| `events.jsonl` (each line) | `Event`, or `BuildEvent` when `type` is `fmu_built` | [event.schema.json](schemas/event.schema.json), [build-event.schema.json](schemas/build-event.schema.json) | Server and workers (append) | Humans (the pane reads no events: [ADR 0010](adr/0010-live-runs-and-the-pane.md)) |
| `runs/<id>/spec.json` of a build job | `BuildSpec` (`kind: "build"`) | [build-spec.schema.json](schemas/build-spec.schema.json) | MCP server, once, before spawn | Worker, server |
| `runs/<id>/build.json`, `generated/<name>/build.json` | `BuildRecord` | [build.schema.json](schemas/build.schema.json) | Worker, on success only | Server, pane |
| `runs/<id>/spec.json` of a system run | `SystemSpec` (`kind: "system"`) | [system-spec.schema.json](schemas/system-spec.schema.json) | MCP server, once, before spawn | Worker, server, pane |
| A system file (anywhere in the workspace; not in the store) | `system.SystemFile` | [system-file.schema.json](schemas/system-file.schema.json) | Claude, or `system_import` | Server |
| `runs/<id>/commands/<id>.json` (live runs) | `Command` | [command.schema.json](schemas/command.schema.json) | Server (exclusive create) or pane (write once) | Worker |
| `runs/<id>/outcomes/<id>.json` (live runs) | `CommandOutcome` | [command-outcome.schema.json](schemas/command-outcome.schema.json) | Worker, once per command file | Server, pane |
| `runs/<id>/live.json` (live runs) | `LiveSnapshot` | [live.schema.json](schemas/live.schema.json) | Worker's heartbeat thread | Server (`run_status(values)`), pane |
| `preferences.json` | `Preferences` | [preferences.schema.json](schemas/preferences.schema.json) | MCP server only (the `response_style` tool), atomically | Server |

A reader of `spec.json` dispatches on `kind` (`run`, `build` or `system`); a reader of `events.jsonl` dispatches on `type`. System runs write the ordinary lifecycle events.

The main fields of each document:
- **`RunSpec`:**
  - The FMU: its absolute resolved path, `sha256`, size, FMI version and model name.
  - `fmi_type` and `solver`.
  - The **resolved** experiment: `start_time`, `stop_time` and `output_interval`, plus `relative_tolerance` and `step_size` when they are set. FMPy's defaults are made explicit.
  - `start_values` and `inputs`, exactly as given.
  - `outputs`: the variables recorded in `result.csv`.
  - `timeout_s` (wall time spent stepping; time a live run's gate waits is excluded), `estimated_steps` and `force`.
  - `versions`: `agentic_fmi`, `fmpy`, `numpy`, `python` and `platform`.
  - `live` (default `false`) and, for a live run only, `idle_limit_s`. A live `RunSpec` has `fmi_type` `CoSimulation` (validated).
- **`RunStatus`:**
  - `state`, plus `t`, `t_start`, `t_stop` and `progress`.
  - `updated_at`, `started_at`, `finished_at` and `heartbeat_at`, plus `heartbeat_mono` (the worker's `time.monotonic()` at that heartbeat).
  - `pid` and `pid_start_time`.
  - `error` (`code`, `message`, `log_tail`).
  - Live runs: `speed` (the pacing factor) and `last_cmd` (the ID of the last command with an outcome; a display field).
  - `reason`: why a run ended where its state alone doesn't say (the idle limit; an FMU that ended the run before `t_stop`).
- **`RunSummary`:** `state`, `t_end`, `n_points`, `wall_time_s`, `columns`, and `variables` (per variable: `min`, `max`, `final`, `min_at`, `max_at` and `unit`).
- **`Event`:** `at`, `run_id`, `type` (`created`, `started`, `finished`, `failed`, `stopped`, or `command` for each command outcome of a live run), `source` (`server` or `worker`) and `message`. Build jobs write these lifecycle events too.
- **`SystemSpec`** gains `live` and `idle_limit_s` exactly as `RunSpec` does.
- **`BuildSpec`:**
  - `workspace` (absolute) and `source` (`path` relative to it, `sha256`, `size_bytes`).
  - `name` (`^[A-Za-z][A-Za-z0-9_]{0,63}$`) and `fmu_path` (`generated/<name>/<name>.fmu`).
  - `backend` (`pythonfmu` or `omc`) and `options` (fixed: `fmi_version` `2.0`, `fmu_type` `cs` or `me_cs`, for omc `fmi_flags` `s:cvode`).
  - `overwrite`, `timeout_s` (`Timing.build_timeout_s`) and `versions`.
- **`BuildRecord`** (`build.json`):
  - `run_id`, `name`, `built_at`, `backend`, `source` and `options` as in the spec.
  - `versions`: `agentic_fmi`, `python`, `platform`, plus `pythonfmu`, or `omc` and `msl`.
  - `fmu`: `path`, `sha256` (of the file written with it), `size_bytes`, `fmi_version` `2.0`, `model_name`, `model_identifier`, `interfaces`.
  - `duration_s`, `warnings` (diagnostics lines), `replaced_sha256` (overwrite only) and `summary` (variable counts, input, output and parameter names, default experiment).
- **`BuildEvent`:** `at`, `run_id` (the build job), `type` `fmu_built`, `source` `worker`, `fmu_path`, `fmu_sha256` and `message`. A successful build appends exactly one, after both files are written; a failed, stopped or timed-out build appends none.
- **`IndexEntry` of a build job:** `kind` `build`, `fmu_name` the build name, `fmu_path` the absolute output FMU path.
- **`SystemSpec`** ([ADR 0009](adr/0009-systems-with-omsimulator.md)): everything the worker needs to build the OMSimulator model, and the pane to draw it, without reading any other file.
  - `name` (the system file's stem), `system` (the system file: absolute `path`, `sha256`, `size_bytes`) and `paramset` (the `.ssv`, same shape, or null).
  - `components`: name (`^[A-Za-z_][A-Za-z0-9_]{0,63}$`) to `{"fmu": FmuRef}`.
  - `connections`: `[{"from": "PI.u", "to": "MSD.F"}]`; a reference splits at the first dot.
  - `parameters`: `"<component>.<variable>"` to `{"value", "source"}`, where `source` is the layer that won: `system` < `paramset` < `start_values` (the FMU's own start values are the implicit lowest layer). Keys are sorted. Each `value` is already converted to its variable's type (a Real is a float, `10.0` for `10`; an Integer or Enumeration an int; a Boolean a bool; a String a string), so the worker can set it as it stands. Only parameters, inputs no connection drives, and states with `initial` exact or approx are ever recorded: never outputs, calculated or structural parameters, constants or arrays.
  - `master`: `{"method": "oms_ma", "coupling": "jacobi", "step_size": h}`. `experiment` is a run's `Experiment` with `output_interval` equal to h (validated); `relative_tolerance` and `step_size` stay null.
  - `outputs` (`<component>.<variable>`), `timeout_s`, `estimated_steps` (communication steps), `force`, `versions`, and `omsimulator` (`version`, `sha256` of the installed package tree).
- **`IndexEntry` of a system run:** `kind` `system`, `fmu_name` the system's name, `fmu_path` the system file's absolute path.

Regenerate the schemas after changing a model, then commit both:

```bash
uv run python -c "from pathlib import Path; from agentic_fmi.store.protocol import export_schemas; export_schemas(Path('docs/schemas'))"
uv run python -c "from pathlib import Path; from agentic_fmi.system import export_system_file_schema; export_system_file_schema(Path('docs/schemas'))"
```

## States and errors

![Run states: a run starts as queued. It becomes running when the worker starts, failed if the worker never started, or stopped if run_stop arrives first. A running run ends as finished, stopped or failed. A live run switches between running and paused (at its start time, on pause, and at the end of a step) and a paused run ends as stopped or failed. Finished, failed and stopped are terminal. Each arrow names who writes the transition, the worker or the server.](diagrams/run-states.svg)

*Source: [docs/diagrams/run-states.mmd](diagrams/run-states.mmd) (edit, then run scripts/render-diagrams.sh)*

`finished`, `failed` and `stopped` are terminal. `paused` (live runs only) is active, like `queued` and `running`: the worker writes `running` when it takes the run over, `paused` once the gate waits at the start time, and then switches between the two on `pause`, `resume` and `step`; a paused run ends as `stopped` (a `stop` command, `run_stop`, or the idle limit with `reason` set), `failed` (`worker_lost` like a running run) or, after `resume`, any way a running run ends. A run that ends `finished` at least one communication step before `t_stop` has `reason` "ended early at t=… of t_stop=…" (the FMU requested it; batch runs too). Less than a step short is not early: FMPy stops at the last grid point when the next one would pass `t_stop` and the FMU can't shorten its step. Build jobs use the same states; `stopped` means cancelled, with nothing written to `generated/`. System runs use them exactly as runs do.

| `error.code` | Meaning | Written by |
|---|---|---|
| `timeout` | More than `timeout_s` of non-paused wall time | Worker watchdog, which then kills its own process group |
| `worker_lost` | The worker died, never started, or its heartbeat went stale (for example, SIGSTOP). A pythonfmu component that raises inside a system run aborts the worker within OMSimulator, so the run ends here with the abort line in `log_tail`; test each component alone with `run_start(fmu=...)` first, where FMPy reports the exception as `simulation_error` | Server, after kill, reap and confirm |
| `fmu_changed` | The FMU's sha256 at worker start differs from `spec.fmu.sha256` (for a system run, any component's `fmu.sha256`) | Worker |
| `not_runnable` | No binary for this platform, or an unsupported interface | Worker |
| `simulation_error` | FMPy or the FMU raised an error; for a system run, OMSimulator or the system engine did (a refused parameter, a step that made no progress, a start time other than 0) | Worker |
| `internal_error` | A bug in Agentic FMI | Worker or server |
| `build_failed` | A build job produced no FMU: tool errors, an FMU that failed the checks, or an output directory taken meanwhile. `message` is the diagnostics text (at most 2000 characters) and `log_tail` is null | Worker |
| `oms_changed` | A system run's worker found no OMSimulator, or an installed package tree whose sha256 or version differs from `spec.omsimulator` or from the pin its own code carries (`config.OMSIMULATOR_PIN`), and refused to import it. `message` prints both digests | Worker |

`error.message` is one or two sentences, never a traceback, except for `build_failed`, whose message is the diagnostics: a count line, at most 10 lines `path:line[:col]: error: text` (the source's workspace-relative path, never a temporary path), and the path of `build.log`. `error.log_tail` holds at most the last 2 KB of `log.txt`.

## Status ownership

There is exactly one writer at a time:
1. The server writes `queued` before it spawns the worker.
2. The worker takes over with `running`. From then on, while the worker is alive, it is the only writer, `paused`, `speed` and `last_cmd` included: commands never write `status.json`. A dedicated thread refreshes `heartbeat_at` and `heartbeat_mono` every 0.5 s, independent of `t`, also while a live run is paused. The server judges staleness by `time.monotonic() - heartbeat_mono` when present (same machine; immune to wall-clock jumps), else by `heartbeat_at`.
3. A worker that finds any state other than `queued` exits without writing.
4. The server writes a terminal state only when one of these holds:
   - The worker has been confirmed gone after `killpg(SIGKILL)`. The server's own child counts as gone only once reaped through its `Popen` handle; a process it didn't spawn counts as gone once `/proc/<pid>` is absent or a zombie. It signals only a pid whose start time equals `pid_start_time`.
   - The run was still `queued` 15 s after creation.
   - The worker process could not be started (`spawn` raised `OSError`). The server writes `failed{internal_error}` itself, because no worker will.
5. Every terminal write by a server is a compare-and-set under the store lock (`<store>/.lock`): it re-reads `status.json` and writes only if the run is still in the state the decision was based on, so a worker's own last word, a worker that has just started and a second server's takeover are never overwritten. The scratch directories (`fmu/`, a build job's `work/`) are removed only after that write succeeds or once the worker is confirmed dead. The lock is never held while waiting for a worker.

## Inbox

Live runs only ([ADR 0010](adr/0010-live-runs-and-the-pane.md); code: `store/inbox.py`, `live.py`, `control.py`).

- **Names.** `runs/<id>/commands/<ms13>-<server|pane>-<rand8>.json`: 13 digits of Unix milliseconds, the writer, 8 lowercase hex digits (`^\d{13}-(server|pane)-[0-9a-f]{8}\.json$`). The worker ignores every other entry, every non-regular file and symlinks.
- **Content.** One `Command`: `protocol`, `id` (the stem), `source` (equal to the name's writer), `issued_at`, `action` and its argument: `pause`; `resume` with an optional `speed` (> 0); `step` with `n` (whole communication steps, ≥ 1; the server turns `run_control(dt=...)` into `n = ceil(dt/h)`, the pane sends 1); `set` with `values` (`{name: number | boolean}`; `<component>.<variable>` in a system); `stop`; `chart` with `names` (the recorded outputs to chart, in order: a list of at most 16 unique names, empty for no chart; only the pane sends it, and `run_control` has no `chart` action). `n` is allowed only with `step`, `values` only with `set`, `speed` only with `resume` and `names` only with `chart`, which requires it. At most 4096 bytes.
- **Writers.** The server creates exclusively: a dot-prefixed temporary file in `commands/`, fsync, `os.link` to the final name (fails if taken), unlink; on a file system without hard links (`EPERM`, `ENOTSUP`, `ENOSYS`, `EXDEV` or `EMLINK` from the link) it creates the final name with `O_CREAT | O_EXCL` and writes it in place, which the grace below covers. The pane writes the final name once with `$.fs.write`, which is not atomic. Nobody rewrites or deletes a command file.
- **The worker** reads `commands/` every `Timing.inbox_poll_s` (0.1 s) while the run steps and every wait slice (50 ms) while it waits. It handles files in name order, each at most once (it remembers what it handled; it never skips a file because its name sorts below `status.last_cmd`, so a late lower name is still applied). A file that is empty or doesn't parse is retried until `Timing.inbox_grace_s` (2 s) after the worker first saw it, then rejected; later names wait behind it. A file over 4096 bytes is rejected unread. Malformed bytes never crash the worker.
- **Outcomes.** For each command file the worker writes exactly one `CommandOutcome` to `runs/<id>/outcomes/<id>.json` (atomic): `id`, `source`, `action` (null if unreadable), `status` (`applied` or `rejected`), `t` (the simulation time it took effect or was rejected; for `step`, the time the last step reached), `at` and `message` (the reason, or a note such as "already paused" or "3 of 5 steps before the run ended"). Then it appends a `command` event and updates `last_cmd`. When the run ends (finished, stopped or failed by the worker), a final sweep gives every remaining command file a rejected outcome, before the terminal status is written; a step still running gets its applied outcome ("2 of 5 steps before the run ended"). A run killed by its timeout, or lost, gets no sweep: a reader waiting for an outcome also watches for a terminal state. The terminal status carries the final `speed` and `last_cmd`. A store error while writing an outcome is logged to `log.txt` and never ends the run. A reader finds an outcome in O(1) from the command ID.
- **Rules.** `step` only from `paused` and not while another `step` runs (both rejected); it ends paused (a `pause`, `resume` or `stop` during the steps ends them early, and the step's outcome says how many ran). `pause` while paused and `resume` while running are applied with a note; `resume` with a `speed` over `Timing.live_max_speed` is rejected. Every command after a `stop` is rejected. A paused run is stopped by the idle limit after `idle_limit_s` without any command (time spent running never counts). `set` accepts scalar inputs that nothing drives (not the run's `inputs` table, not a connection) and tunable parameters, within the declared `min` and `max` and the range of their integer type, with a value of the variable's kind; the server checks before writing and the worker checks again with the same function (`live.settable`), so a bad pane set is rejected and the run continues. A `set` is not atomic: if the FMU or OMSimulator refuses a value after the checks passed, the values set before it stay set, and the rejected outcome's `message` ends with `; already applied: PI.Kp=8, P.K=2` (at most 10 values, then `and N more`). FMPy sets one FMI type group at a time, so the failing group's own variables are not listed. `stop` ends the run as `stopped` with its partial result. `chart` replaces the charted signals of `live.json` (the keys of `series`, in the order given) and is applied or rejected at once in any state, paused, running or stepping: it never pauses, resumes, steps, stops or changes the speed. It is rejected, leaving the selection as it was, if a name is not one of the columns in `live.json`'s `values` (the message gives close matches and the first names), a name repeats, or more than `Limits.live_variables` (3) are named. It counts as a command for the idle limit, like every command.
- **The server** (`run_control`) validates a request first (the run is live and not ended; `set` with `live.check_sets` against the model descriptions it parses itself; `dt` becomes `n = ceil(dt/h - 1e-9)`), writes one command, then polls `outcomes/<id>.json` (O(1)) and the run's status every `Timing.poll_interval_s` until the outcome exists and `status.last_cmd` has caught up with it (the heartbeat publishes state a moment after the outcome; for `stop` until the run has ended), the run is terminal, or `wait_s` has passed (default `Timing.control_default_wait_s`, 5 s; capped at `Timing.max_wait_s`), never longer. A rejected outcome is an error carrying the worker's message; no outcome is not an error (the command stays queued; a terminal run without one was probably not applied).
- **Replay.** `spec.json` plus the command files and their outcomes (with `t`) reproduce a live run.

## live.json

`runs/<id>/live.json` is a `LiveSnapshot` of a live run: `run_id`, `seq` (increases with every write), `t`, `values` (every scalar recorded output in `spec.outputs` order, at most `Limits.live_max_columns` (200), each `{name, unit, value}` at `t`, null when non-finite) and the ring buffer `time` plus `series` (the last `Limits.live_points` (500) communication points of the charted outputs, the current chart selection; every series has one value per time). Values are rounded to 6 significant digits.

- **The chart selection** is what `series` holds: by default the first `Limits.live_variables` (3) names of `values`, replaced by each applied `chart` command. The worker keeps the last 500 points of every column in memory (one read of all columns per communication point) and publishes the history of the selected ones only, so a signal charted later has its full history in the next `live.json`. A reader takes the selection as the keys of `series`; `values` stays complete.
- Written only by the worker's heartbeat thread, atomically, at most 5 times a second and only when a new sample arrived; the oldest history points are dropped until the file is under `Limits.live_max_bytes` (100,000 bytes; the worst case the limits allow is about 58 KB, a fixture checked by `tests/store/test_pane_fixtures.py`).
- Written once more as the worker stops, with the values of the result's last row, and kept after the run ends, so a reopened pane still draws the last window. Batch runs have no `live.json`, and they never read the inbox.
- `run_status(values=[...])` answers from it (at most 25 names, recorded outputs only).

## preferences.json

`<store>/preferences.json` is a `Preferences`: `protocol` (1) and `response_style`, `"talkative"` (the default) or `"ninja"`. It records how the user wants Claude to answer in this workspace; the file is additive, so the protocol stays 1.

- **Writer.** The MCP server only, atomically, when Claude calls `response_style` with a `style`. Workers and the pane never write it.
- **Readers.** The server, on each `run_start`, `run_status`, `run_stop`, `run_control`, `fmu_build` and `response_style` call, so a change takes effect at once and survives a restart. A missing, unreadable or invalid file (including another `protocol`) reads as `"talkative"`; a preference never fails a tool.
- **Effect.** In `"ninja"` mode those five tools return their `RunView` without `summary`, `build` and the error's `log_tail`, and with a one-line status in `hint` (see `style.py`). Nothing in a run's own files changes, and refusals (`ToolError`) are the same in both modes.

## Versioning rule

- Every JSON document and every `events.jsonl` line carries a top-level `"protocol": 1`.
- **Additive changes keep the version.** These include new optional fields, new files, new event types, new error codes and new states. Readers in other languages must treat an unknown enum value as generic. Python readers match their writer's version.
- **Breaking changes bump the version.** These include renamed or removed fields, changed types or meaning, and moved files. A bump also needs an ADR and a CHANGELOG entry.
- **Readers ignore unknown fields.** A reader that sees any other `protocol` value reports "unsupported protocol version" and doesn't guess.

## Writing rules

- **Atomic replace:** whole-file JSON is written to `<name>.tmp-<pid>-<thread>` in the same directory, flushed and fsynced, then moved into place with `os.replace`. `result.csv`, PNGs and, in `generated/<name>/`, the built FMU and its `build.json` are written the same way (never through `TMPDIR`, which may be another filesystem). Readers skip `*.tmp-*` files. A reader that fails to parse a file keeps the previous value and retries.
- **Validate before writing:** writers re-validate each model, because `model_copy(update=...)` skips validation.
- **Finite numbers only:** JSON never contains `NaN` or `Infinity`. Non-finite results become `null`. All timestamps are ISO 8601 UTC (`...Z`).
- **Events:** each `events.jsonl` record is one line written with a single `O_APPEND` `write`, at most 4 KiB including the newline (a longer `message` is cut and ends with `…`). Readers ignore a trailing partial line.
- **Build commit:** with its watchdog disarmed and under `flock(<store>/.lock)`, the worker re-checks `generated/<name>/` (absent, or holding a readable `build.json` when `overwrite` is set), then replaces the FMU, then `build.json`, then writes `runs/<id>/build.json`, appends the `fmu_built` event and writes `finished`. A reader that finds `build.json`'s `fmu.sha256` different from the FMU saw the gap between the two replaces.
- **`index.json`:** read-modify-write happens under `flock(<store>/.lock)`. If the index is missing or corrupt, it is rebuilt from `runs/*/spec.json` and `status.json`. `status.json` stays authoritative.
- **`result.csv`:** a header row of plain column names with `time` first, then `%.17g` numbers, UTF-8 and LF. Duplicate times mark events. A name that contains a comma or a quote is CSV-quoted (`"m[1,2]"`). Every column is numeric (Boolean as 0 and 1), and a non-finite value is written as `nan`, `inf` or `-inf`, because CSV is not JSON.

## Limits

| Item | Limit |
|---|---|
| Heartbeat | Every 0.5 s while running. The run is stale after 5 s (`config.Timing`) |
| Event line | 4 KiB |
| `error.log_tail` | 2 KB |
| `live.json` | Under 100,000 bytes, written at most 5 Hz, only on change (`Limits.live_*`) |
| Command file | 4096 bytes; an unparsable one is retried for 2 s (`Timing.inbox_grace_s`) |
| Live run | Paused for 30 min stops it (`Timing.live_idle_limit_s`, recorded as `idle_limit_s`); speed up to 1000 (`Timing.live_max_speed`) |
| Build source | 256 KiB (`Limits.build_source_max_bytes`) |
| Build diagnostics | 2000 characters, 10 lines (`Limits.build_diagnostics_max_*`) |
| Build job | 30 min (`Timing.build_timeout_s`) |
| System file, `.ssv` parameter set | 256 KiB (`Limits.system_file_max_bytes`); 64 components, 1024 connections |
| `system_import` | 2 GiB archive, 1000 zip entries (and a zip directory of at most 1 KiB per permitted entry, checked before it is parsed), 8 MiB per SSD or SSV, 1 GiB per FMU, 2 GiB in total (`Limits.ssp_*`, checked against the declared sizes and counted again while streaming) |

## Performance

The step gate polls the store, so its filesystem matters. Spike (b) measured:

| Run store location | Cost per poll | Inbox command latency |
|---|---|---|
| Windows bind mount (`/workspace`) | about 8 ms | 10–19 ms |
| Container-local storage (`/tmp`) | about 0.33 ms | 2–4 ms |

Batch runs only write status on the heartbeat and never poll the inbox, so the bind mount works but is slower. A live run lists its inbox at most 10 times a second while stepping (20 while paused) and writes `live.json` at most 5 times a second. Spike (i) measured the pane's `$.fs` costs: a small read 0.96 ms on container-local storage and 3.2 ms on the bind mount, a `stat` 0.54 and 1.5 ms. The worker's inbox poll was measured too (`InboxReader.poll`, the median of 200): 3.5 ms on the bind mount (`/workspace/.cache`) with an empty or a 50-file `commands/`, 2.0 ms when `commands/` doesn't exist yet, against 0.012 ms and 0.05 ms on `/tmp`. At 10 polls a second a stepping live run on the bind mount spends about 3.5 % of its wall time listing the inbox; on container-local storage nothing measurable. For demos and live runs, use a workspace under `/lab` or set `AFMI_STORE_DIR`. `/lab` is a named volume. It is expected to behave like `/tmp`, but this hasn't been measured (it was in use).

## Resolved and deferred questions

1. **Store location:** resolved by ADR 0007 (workspace default, `AFMI_STORE_DIR`, demos in `/lab`).
2. **Run-ID allocation with several writers:** resolved by ADR 0007 (exclusive `mkdir`).
3. **Command file names with two writers:** resolved by ADR 0010: `<ms13>-<server|pane>-<rand8>.json`, applied once each in name order, with one outcome file per command; `status.last_cmd` is a display field.
4. **Pane write primitive:** resolved by ADR 0010 (spike (i)): `$.fs` has no rename and `write` is not atomic, so the pane writes each name once and the worker retries an unparsable file until a grace period.
5. **Retention and cleanup of old runs:** deferred. A cleanup tool must not let run IDs be reused (ADR 0007).
