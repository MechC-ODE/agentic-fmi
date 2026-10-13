---
name: fmi-expert
description: How to explore and simulate FMUs (Functional Mock-up Units, FMI 2.0 and 3.0) with the Agentic FMI tools. Use whenever the user mentions an FMU, a .fmu file, FMI, co-simulation or model exchange, a Modelica or exported dynamic model, or asks to inspect a model, change a parameter and re-simulate, compare simulation runs or plot results, or asks for a simulation example or demo.
---

# Working with FMUs

The `fmi` MCP server runs FMUs in isolated worker processes and returns compact summaries and plots, never raw time series. Use its tools as described here. Details such as the input signals, limits and tool parameters are in [reference.md](reference.md). To create a new FMU from Python or Modelica source with `fmu_build`, see the [fmu-authoring skill](../fmu-authoring/SKILL.md). To connect FMUs into a system, write a JSON system file, check it with `system_check`, run it with `run_start(system=...)`, and convert SSP files with `system_import` and `system_export`; the [system-builder skill](../system-builder/SKILL.md) has the workflow. To steer a run while it runs, see [Live runs](#live-runs). For status-only replies, see [Response style](#response-style).

## Examples, and nothing downloaded

- **Never download or install anything on your own:** no FMUs or models from the web, no `pip` or `uv` installs, no new Python environments, no scripts that fetch files. The plugin manages its own environment, and the user's client may block such calls anyway. If something is missing, say what it is and where it would come from, and let the user decide.
- **Asked for an example or a demo, with no FMU at hand?** Use the plugin's ready-made examples ([fmu-authoring](../fmu-authoring/SKILL.md#ready-made-examples)). They are tested and build in about a second, offline and without OpenModelica:
  - One FMU: [bouncing_ball.py](../fmu-authoring/examples/bouncing_ball.py) (outputs `h` and `v`, tunable `e`).
  - A closed loop: [pi_controller.py](../fmu-authoring/examples/pi_controller.py) and [MSD.py](../fmu-authoring/examples/MSD.py), wired by [loop.json](../system-builder/examples/loop.json) (copy it to the workspace root).

  Read the file, Write it unchanged into the workspace's `models/`, `fmu_build` it, then run it. Don't write a model of your own for an example.
- The Modelica Reference FMUs (BouncingBall, VanDerPol and others, used in the user guide) are a download from github.com/modelica/Reference-FMUs. Mention them if the user wants third-party FMUs; the user fetches them.

## FMI in one minute

- An **FMU** is a zip with `modelDescription.xml` (the interface) plus compiled binaries per platform. Only Linux x86-64 binaries run here (`linux64` in FMI 2.0, `x86_64-linux` in FMI 3.0).
- **Interfaces.** *Co-Simulation (CS)*: the FMU brings its own solver and advances in communication steps. *Model Exchange (ME)*: the FMU supplies equations and the tool supplies the solver (`solver`: `CVode` by default, or `Euler`). *Scheduled Execution (SE)*, FMI 3.0 only, is not supported. `run_start` picks CS if the FMU has it; set `fmi_type` to choose.
- **Causality** is a variable's role. `parameter`: set before the run, through `start_values`. `input`: varies in time, through `inputs`. `output`: recorded by default. `local`: internal, recorded only if you name it in `outputs`. `calculatedParameter`: derived, not settable.
- **Variability** is when a value may change. `fixed`: only before the run. `tunable`: also between steps, so a live run can `set` it with `run_control`. `constant`, `discrete` and `continuous` describe the signal. BouncingBall's `e` is `tunable`, VanDerPol's `mu` is `fixed`. For a batch run, both go in `start_values`.

## Workflow

1. **`workspace_info`** if you don't know where the FMUs are or why a run was refused.
2. **`fmu_list`** (optional `pattern`). Pass the returned path as `fmu` everywhere else.
3. **`fmu_inspect`**. Read `runnability` first: `runnable`, the per-interface `reason` and the platform. Then the default experiment, and the inputs, outputs and parameters (tunable first, at most 25 each).
4. **`fmu_variables`** only when you need the full list or a filter (`causality`, `variability`, `name`, `type`, paged). `total` is the number of matches.
5. **`fmu_validate`** if inspect fails or the FMU misbehaves. Problems come back as data, not as an error.
6. **`run_start`** with `start_values`, `stop_time`, `inputs`, `outputs` as needed. It waits up to `wait_s` (default 10 s). If the run finishes in time you get the summary (min, max and final per variable); otherwise you get the run ID.
7. **`run_status`** to poll a run that is still `queued`, `running` or `paused` (a live run). **`run_stop`** ends one early and keeps the partial result. **`run_list`** finds earlier runs.
8. **`run_compare`** for differences between runs, **`run_plot`** to see shapes, **`run_results`** for a few numbers.

To test a parameter, start one run per value (ask for no more than a handful), then compare. The first run in `run_compare` is the baseline.

## Live runs

A live run is an ordinary run that you (and the user) can steer while it runs. Use one to step through a model, to change a tunable parameter or an unconnected input mid-run, or to let the user watch. For a plain answer, a batch run is simpler and runs at full speed.

- **Start.** `run_start(fmu=..., live=true)`, or `system=...` with `live=true`. Co-Simulation only: a Model Exchange-only FMU, or `fmi_type: "ModelExchange"`, is refused with a hint (run it as a batch run). The call returns once the run is `paused` at its start time, with the run ID. A live run counts towards the four at a time until it ends, so stop the ones you are done with. A run paused longer than its `idle_limit_s` (30 min, in its spec) without a command is stopped with its partial result, and `reason` says so.
- **Steer.** `run_control(run_id, action, ...)` with one action:
  - `pause`: stops at the next step; the state becomes `paused`.
  - `resume`: continues. `speed` is a resume option (1 is real time, at most 1000) and stays until you change it.
  - `step`: only while paused (otherwise the error says to pause first). `n` steps (default 1), or `dt` seconds, rounded up to whole communication steps. The run is paused again afterwards.
  - `set`: `values` such as `{"e": 0.6}`, or `{"PI.Kp": 12}` for a system; applied before the next step.
  - `stop`: ends the run and keeps the partial result.
- **Outcomes.** `run_control` waits up to `wait_s` (default 5 s) for the worker's outcome and returns the run with a line such as "Applied step at t=2.5". A command the worker or the server refuses comes back as an error with the reason, and the run goes on; fix the call and send it again. If no outcome arrives in time the command is queued: check `run_status`.
- **What `set` accepts.** Scalar inputs that nothing drives, and tunable parameters, inside their `min` and `max`. It refuses outputs, `fixed` parameters (VanDerPol's `mu`), an input driven by `inputs` or by a connection, arrays and strings, each with the reason. List the candidates with `fmu_variables(variability="tunable")` first; BouncingBall's `e` is tunable.
- **Values.** `run_status(run_id, values=["h", "v"])` gives the current value and unit of up to 25 recorded outputs, from the last snapshot; live runs only, and an unrecorded name is refused with the recorded ones listed. Plain `run_status` shows state, time, progress and speed. When the run has ended it is an ordinary run: `run_results`, `run_plot` and `run_compare` work (`run_compare` lists `live` as a difference; the commands are in the run's `commands/` and `outcomes/`).
- **Pacing.** A live run goes at 1x real time unless you resume with another `speed`, so a 10 s model takes 10 s. `step` is not paced. Time spent paused or waiting does not count towards `timeout_s`. A command takes effect within about a step plus 0.1 s.
- **The pane.** With the `agentic-fmi-pane` plugin loaded, `/fmi` opens a pane for the user: the run picker, state, values with units, live charts, and Pause, Resume, Step, Stop and Set controls for the same run, plus new FMUs and a system's connections. It reads and writes only the run store, so the user's buttons and your `run_control` calls steer the same run (commands apply in the order they were written). The pane is optional: suggest `/fmi`, but don't depend on it.

## Context economy

- Prefer the summary in `run_start` or `run_status`, then `run_compare`, then `run_plot`. Use `run_results` last, and ask for few variables (`variables`, at most 8) and a small `max_points` (the default 100 is usually plenty; 500 is the cap).
- `run_plot` takes 1 to 6 runs and up to 6 variables: one panel per variable, one line per run. Choose variables on purpose; the plot is also saved under the first run's `plots/` directory.
- Page with `offset` and `limit` instead of asking for everything. Don't call `fmu_variables` on a large model without a filter.
- Record only what you need (`outputs`). Quote numbers from tool output as they are (6 significant digits) and give units when the summary has them.

## Pitfalls

- **Platform.** An FMU needs a binary for Linux x86-64. A Windows-only FMU shows `runnable: false` with the reason; a different FMU build is the only fix.
- **FMI 1.0 is not supported.** Ask for an FMI 2.0 or 3.0 export.
- **ScheduledExecution-only FMUs** (such as Reference `Clocks`) can't run here. Inspect and validate still work.
- **Huge step counts.** For a CS FMU with a `fixedInternalStepSize`, the tool estimates horizon divided by step size. Above 1e8 steps `run_start` refuses and names the options. Prefer `fmi_type: "ModelExchange"` (the solver picks its steps) or a shorter `stop_time`. Use `force: true` only with a `timeout_s`. Example: Reference `Roberts` as CoSimulation needs about 1e11 steps; as ModelExchange it finishes in under a second.
- **Parameters go in `start_values`, not `inputs`.** `inputs` accepts only variables with causality `input`. A name that doesn't exist is rejected with close matches.
- **The experiment defaults come from the FMU.** `stop_time`, `start_time` and `relative_tolerance` fall back to its default experiment. Roberts' default horizon is 1e8 seconds, so say what you want.
- **Timeouts kill the run.** `timeout_s` (default 600 s of wall time) is enforced by killing the worker, and a timed-out run keeps no results. Choose a timeout that fits the question.
- **Four runs at a time** can be queued or running. A fifth start is refused until one ends.
- **Originals stay untouched.** Tools never modify an FMU. Each run records its spec, the FMU's sha256 and the tool versions, so a run can be reproduced.
- **Don't guess variable names.** Read them from `fmu_inspect` or `fmu_variables`. Array variables appear as `y[1]`, `y[2]`.

## Reading a failed run

`run_status` shows `state: failed` with `error.code`, a one-line `error.message` and a `log_tail` (the end of the worker's log, including the FMU's own messages).

| `error.code` | Meaning | What to do |
|---|---|---|
| `timeout` | More than `timeout_s` of wall time. | Shorten `stop_time`, raise `output_interval`, switch to `ModelExchange`, or raise `timeout_s` if the model is just slow. |
| `worker_lost` | The worker crashed or hung (no heartbeat for 5 s) and was killed. A Python (pythonfmu) component that raises inside a system run aborts the worker within OMSimulator, so it ends here too. | Read `log_tail`; native FMU crashes land here. For a system run, test each component alone with `run_start(fmu=...)` first: FMPy reports a raising component as a readable `simulation_error`. Otherwise retry once, then try the other `fmi_type` or `fmu_validate`. |
| `fmu_changed` | The file changed between `run_start` and the worker's start. | Don't edit FMUs while runs are queued. Start the run again. |
| `not_runnable` | No binary for this platform, or an unsupported interface. | See `runnability.reasons` in `fmu_inspect`. Parameters won't help. |
| `simulation_error` | The FMU or FMPy reported an error, such as a rejected start value or a solver failure. | Read the message and `log_tail`. Try other start values, a shorter span, `relative_tolerance`, or the other `fmi_type` or `solver`. |
| `oms_changed` | A system run found the installed OMSimulator package different from the pinned one and refused to load it. | Tell the user; the message shows both digests. Reinstall with `uv sync --locked`; don't retry. |
| `internal_error` | A bug in Agentic FMI. | Don't retry blindly; report the run ID and `log_tail` to the user. |

A run in state `stopped` was ended by `run_stop`; its partial result is usable. `run_start` itself raises an error, not a failed run, when it refuses the request (unknown variable, step guard, unsupported FMU). The message names the fix.

## Answering the user

Say which FMU and interface ran, the run IDs, what differed between runs (`run_compare` lists it), and the numbers that matter with units. Show the plot rather than describing it. If a result looks wrong, check the start values against the variable's `min`, `max` and unit in `fmu_variables` before blaming the model.

## Response style

The default is **ninja**: status only.

- For `run_start`, `run_status`, `run_stop`, `run_control` and `fmu_build` results, reply with the result's 🥷 line (its `hint`) and nothing else: no summary, table or suggestion. Example: "🥷 r12 done: BouncingBall, t=3 s."
- An explicit question (compare, plot, results, inspect, "what was the final height?") still gets a brief answer; use `run_results`, `run_compare` and `run_plot`, which return the same data in both modes.
- Errors and refusals come back in full in both modes. Act on them as usual and report only the outcome.

When the user asks for explanations ("talkative mode", "explain the results"), call `response_style("talkative")`: then summarize, explain and suggest the next step, as described above. `response_style("ninja")` switches back; `response_style` without an argument reports the current style. The style persists in the workspace.
