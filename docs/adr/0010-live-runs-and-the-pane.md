# 0010. Live runs and the pane: ordinary runs with a gate, a command inbox and live.json

**Status:** Accepted, implemented · **Date:** 2026-10-11 · **Amends** [0005](0005-pane-as-separate-early-access-plugin.md) and [0009](0009-systems-with-omsimulator.md)

## Problem

Claude and the user want to pause a running simulation, change a value and continue, for single FMUs and systems, while a pane inside Claude Code draws it. Spike (i) set the limits:

| Where | What the spike found |
|---|---|
| FMPy, Co-Simulation | A gate between steps can pause and set values, identical to our own loop |
| FMPy, Model Exchange | Changing a value mid-run is wrong unless we own the solver loop |
| OMSimulator | Tunable parameters and unconnected inputs can be set after start |
| The pane's file API | No rename, no exclusive create, writes aren't atomic |

## Decision

- **Live runs are ordinary runs.** `run_start(live=true)` starts paused at t0; one tool, `run_control`, pauses, resumes at a speed, steps, sets and stops. Co-Simulation only.
- **One wait loop** in the worker handles pause, pacing, single steps and new commands. Waiting doesn't count toward the timeout; an idle paused run is stopped.
- **Commands are files.** The server and the pane write one file per command; the worker applies each once, in order, and writes one answer per command.
- **`live.json`** holds current values and the recent history of up to three charted signals, written at most 5 times a second.
- **The pane only reads the store and writes commands.**

![Claude's run_control (through the server) and the pane's buttons each write one file per command into commands/. The worker's wait loop applies each once, in name order, writes one answer per command into outcomes/ (Claude gets the answer, the pane shows a toast), and writes live.json at most 5 Hz and 100 KB, which the pane draws as values and charts.](../diagrams/adr-0010-inbox.svg)

![Claude starts a live run, which waits at t = 0; Claude changes a gain and resumes at double speed; the worker applies each command and reports back; the pane draws the run and can pause it; Claude reads the current values and stops the run.](../diagrams/live-session-sequence.svg)

## Options considered

| Option | Verdict | Why |
|---|---|---|
| **A gate inside FMPy's `simulate_fmu`** | Chosen | Same behaviour as our own loop, far less code |
| Our own Model Exchange loop | Rejected | Model Exchange runs stay batch |
| **One answer file per command** | Chosen | Every command is answered; the files are a replay log |
| Acknowledgements in `status.json` | Rejected | A second writer, and no history |
| **Live runs as ordinary runs** | Chosen | Reuses store, supervision, results and plots |
| Separate session tools | Rejected | A second tool family for the same thing |

## Trade-offs

**Gains**
- No new run kind or tool family; the core works without the pane.
- One loop owns all waiting, so pause, pacing and the watchdog can't disagree.
- The command and answer files replay what happened.

**Costs, and how we handle them**
- Co-Simulation only; no live rewiring or Gauss-Seidel coupling.
- A command lands after one poll and one step (about 0.1 s), so tight loops belong inside a co-simulation.
- A run killed by its timeout leaves its pending commands unanswered; readers also watch for the run's end.
- The pane depends on Claude Code's early-access mod API.

## Chart selection (added later)

The user picks which signals to chart, up to three. The worker keeps the recent history of every recorded output and publishes only the selected ones in `live.json`, so a newly picked signal appears with its full history. A pane-only `chart` command replaces the selection; it never pauses or steps the run, and a bad one is rejected while the run goes on.

| Option | Verdict | Why |
|---|---|---|
| **Worker keeps every history, publishes the selection** | Chosen | A new chart starts with its full history |
| Publish every signal's history | Rejected | 200 signals of 500 points don't fit in 100 KB |
| Keep the selection in the pane | Rejected | A new chart would start empty |

Cost: the worker reads every output at every step, about 0.7 µs per extra signal; measured step times didn't change.

<details>
<summary><b>Details:</b> the spike, the engine seam, the inbox, live.json and the tools</summary>

- **More spike facts.** FMPy's gate in `step_finished` reaches the FMU through `recorder.fmu`; it first fires after step 1, and wrapping `exitInitializationMode` of an instance passed as `simulate_fmu(fmu_instance=...)` gates the start time. A value FMPy's input table drives is overwritten before every step. A discard without terminate makes FMPy retry the same step forever; a terminate returns early without an error. In OMSimulator a connected input holds a set value for one step; outputs and fixed parameters are refused with a warning; with FMI 2.0 discard and terminate both stall the clock, and FMI 3.0 ignores terminate. The pane's `$.env.get` reads the engine's environment and `$.session.root()` is the launch directory; `Raster` draws only on the terminal and `Svg` only elsewhere.
- **Engine seam.** `simulate(spec, dir, gate)` and `EngineResult` stay; the gate becomes `gate(t, live)`, where `Live` (`engines/base.py`) has `get(names)` and `set(values)`. For a live spec both engines call the gate once at the start time after initialization.
- **Engine-neutral outcomes**, contract-tested. A step that never advances is `simulation_error` (FMPy: `Limits.stall_repeats` gate calls at one `t`, which also ends the batch discard hang; OMSimulator: its no-progress check). An FMU-requested terminate ends `finished` with "ended early at t" where the engine can tell (FMPy). OMSimulator never reports it: with FMI 2.0 the run is a no-progress `simulation_error`; with FMI 3.0 it keeps stepping.
- **Live specs.** `RunSpec.live` and `SystemSpec.live`, with `idle_limit_s`, and the state `paused` (additive, protocol 1). A live run counts toward `max_workers` until it ends; `run_start(live=true)` waits until it is paused at its start time. A Model-Exchange-only FMU is refused with a hint.
- **Wait loop.** A step may start when `wall >= anchor + (t - t_anchor) / speed`, re-anchored on every resume and speed change. Single steps are whole steps `n`, unpaced, paused again afterwards. The loop sleeps at most 50 ms at a time; waiting is excluded from the watchdog's active clock. The heartbeat adds `heartbeat_mono` (CLOCK_MONOTONIC), which the supervisor prefers.
- **Inbox.** Names are `<ms13>-<server|pane>-<rand8>.json`, at most 4 KiB, one `Command` each: `pause`, `resume{speed?}`, `step{n}`, `set{values}`, `stop`, and the pane's `chart{names}`. The server creates files exclusively (temporary file plus `os.link`); the pane writes once with `$.fs.write`. The worker applies files in name order, retries an unparsable one until `Timing.inbox_grace_s` and then rejects it (so a torn pane file can delay later commands by up to the grace), and writes one `CommandOutcome` per file plus a `command` event. Only the worker writes `status.json`; `status.last_cmd` is for display.
- **What `set` accepts.** Scalar inputs that nothing drives and tunable parameters within `[min, max]`. One function, `live.settable`, decides for the server (before writing) and for the worker (a pane's set may be wrong; it is rejected and the run goes on).
- **live.json.** `LiveSnapshot`: the current value and unit of every scalar recorded output (at most `Limits.live_max_columns`) and the last `Limits.live_points` points of the charted ones. Written only by the heartbeat thread, atomically, at most 5 Hz, only on change, under `Limits.live_max_bytes` (100 KB). Batch runs don't write it. `run_status(values=[...])` (at most 25 names) answers from it.
- **Chart command.** `Command.names` is the whole new selection, in order, at most `Limits.live_variables` (3), which is also the default selection (the first outputs). Unknown names (with close matches), repeats and more than 3 are rejected. `run_control` doesn't offer `chart`, so `tools/list` is unchanged. Two clicks before the next `live.json` write act on the same selection.
- **Tools.** `run_control(run_id, action, n?, dt?, values?, speed?, wait_s)`: `dt` becomes `n = ceil(dt / h)`; it waits for the outcome, the run's end or `wait_s`. The server side lives in `control.py`. `tools/list` stayed under 32,000 bytes by trimming descriptions (31,125 bytes for 17 tools). `run_compare` lists `live` as a spec difference but can't show the commands.
- **The pane** finds the store as `config.py` does (`AFMI_STORE_DIR`, else `<session root>/.agentic-fmi`), because it inherits Claude Code's environment. It reads `index.json` and per run `spec.json`, `status.json`, `live.json`, `build.json` and its own command outcomes; toasts come from state changes, not `events.jsonl`. One downsampler per signal feeds the terminal and desktop drawings; the system diagram is a fixed grid. Fixtures are generated from the pydantic models, with a drift test.

</details>
