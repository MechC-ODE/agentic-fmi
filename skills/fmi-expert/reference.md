# fmi-expert reference

Details for the [fmi-expert skill](SKILL.md): the tools in one table, the input signals, the budgets and the defaults.

## Tools

| Tool | Use it to | Main parameters |
|---|---|---|
| `workspace_info` | See the workspace, run store, platform, versions, FMU count, active runs and output limits | none |
| `fmu_list` | Find FMUs (paged, sorted by path) with FMI version, interfaces and runnability | `pattern`, `offset`, `limit` |
| `fmu_inspect` | Describe one FMU: version, interfaces, platforms, runnability and reasons, default experiment, counts, up to 25 inputs, outputs and parameters | `fmu` |
| `fmu_variables` | List variables with causality, variability, type, unit, start, min and max | `fmu`, `causality`, `variability`, `name`, `type`, `offset`, `limit` |
| `fmu_validate` | Check an FMU against the FMI standard (schema, names, structure, start values, units) | `fmu` |
| `run_start` | Simulate an FMU, or a system file (several connected FMUs, run by OMSimulator), in an isolated worker. `live=true` starts a Co-Simulation run paused at its start time, for `run_control` | `fmu`, `system`, `paramset`, `start_values`, `stop_time`, `start_time`, `output_interval`, `relative_tolerance`, `inputs`, `outputs`, `fmi_type`, `solver`, `timeout_s`, `wait_s`, `force`, `live` |
| `run_status` | State, progress and time of a run; summary when done, error and log tail when failed. `values` gives the current value and unit of up to 25 recorded outputs of a live run | `run_id`, `values` |
| `run_stop` | Stop a queued, running or paused run (SIGTERM, then SIGKILL after a grace period) | `run_id` |
| `run_control` | Steer a live run: `pause`; `resume` (optional `speed`); `step` (paused runs only; `n` steps or `dt` seconds, rounded up to whole steps); `set` (`values`: unconnected inputs and tunable parameters); `stop`. Waits for the outcome; a refused command is an error with the reason | `run_id`, `action`, `n`, `dt`, `values`, `speed`, `wait_s` |
| `run_list` | List runs, newest first | `state`, `offset`, `limit` |
| `run_results` | Downsampled series of a finished or stopped run | `run_id`, `variables`, `max_points`, `start_time`, `stop_time` |
| `run_plot` | PNG with one panel per variable and one line per run | `run_ids`, `variables`, `title` |
| `run_compare` | Spec differences and per-variable min, max, final and max and RMS difference against the first run | `run_ids`, `variables` |
| `fmu_build` | Build an FMI 2.0 FMU from a `.py` (pythonfmu) or `.mo` (OpenModelica) file into `generated/<name>/`, as a job polled with `run_status` | `source`, `name`, `overwrite`, `wait_s` |
| `system_check` | Check a system file without running it: FMU paths, names, connection direction, types, units, drivers, parameters and the experiment; errors and warnings come with their fixes | `system` |
| `system_import` | Import a flat SSP from any tool as `imported/<name>/<name>.json` plus private FMU copies; never modifies the `.ssp` | `source`, `name` |
| `system_export` | Export a system file as a standard `.ssp` (SSD, FMUs, one SSV, the step); byte-identical on repeat, never overwrites | `system`, `dest`, `paramset` |
| `response_style` | Set the reply style: `ninja` (the default: run, build and live-control results come as one 🥷 status line, which you reply with) or `talkative` (summaries). Without `style`, reports the current one. Kept in the workspace | `style` |

Run IDs look like `r3`. `fmu` is a path relative to the workspace, as `fmu_list` shows it.

## Input signals

`inputs` maps an input variable's name to one signal:

| Signal | Example |
|---|---|
| Constant | `{"kind": "constant", "value": 1}` |
| Step | `{"kind": "step", "time": 1, "before": 0, "after": 2}` |
| Ramp | `{"kind": "ramp", "start": 0, "end": 2, "before": 0, "after": 1}` |
| Table | `{"kind": "table", "points": [[0, 0], [1, 2]], "interpolation": "linear"}` (or `"hold"`) |

A step or a `hold` change is a jump: two samples at the same time. Table times must not decrease.

## Budgets

| Item | Limit |
|---|---|
| Points per variable in `run_results` | 100 by default, 500 at most (LTTB keeps the endpoints and the extrema) |
| Variables per `run_results` call | 8 |
| Runs and variables per `run_plot` or `run_compare` | 6 and 6 (`run_compare` needs at least 2 runs) |
| Page size | 50 by default, 200 at most |
| Items per list in `fmu_inspect` | 25 |
| One tool's JSON text | about 8 KB (up to 16 KB when you ask for more points) |
| One PNG | about 200 KB |
| Runs queued, running or paused at once | 4 (a live run counts until it ends) |
| `wait_s` of `run_start` | 10 s by default, 60 s at most |
| `wait_s` of `run_control` | 5 s by default, 60 s at most |
| `values` of `run_status` and `set` of `run_control` | 25 names |
| `speed` of a live run | 1 (real time) by default, 1000 at most |
| A live run paused without a command | stopped after 30 min (`idle_limit_s`), partial result kept |
| `timeout_s` | 600 s by default, 86400 s at most |
| Step-count estimate | above 1e8 internal steps `run_start` refuses unless `force` |
| Result table of a system run | one row per step: above 5e6 values (rows times columns, `time` included) `run_start` refuses unless `force`; raise `output_interval`, record fewer `outputs` or shorten `stop_time` |

`workspace_info` reports the limits that apply to the current server.

Lists that don't fit the budget are cut and say so: `fmu_list`, `fmu_variables` and `run_list` continue at `next_offset`, and `fmu_inspect` and `fmu_validate` set `truncated` when they drop variables or problems.

## Defaults

- `fmi_type`: CoSimulation if the FMU has it, else ModelExchange.
- `solver`: `CVode`; only ModelExchange uses it.
- `start_time`, `stop_time`, `relative_tolerance`: the FMU's default experiment, else FMPy's defaults. The resolved values are recorded in the run's spec.
- `output_interval`: FMPy's choice, at most about 1000 rows.
- `outputs`: the FMU's outputs; if it has none, its continuous non-parameter variables.
- `run_results` without `variables`: the first 4 recorded variables. The result's `note` says how many were recorded and names the first 12 to ask for.
- Result rows depend on the interface and the FMI version (a CS FMU in FMI 3.0 samples at its `fixedInternalStepSize`), so a CS run and an ME run of the same model have different time grids. `run_compare` interpolates onto the baseline's grid.

## Runs and files

Every run lives in `<workspace>/.agentic-fmi/runs/<id>/` with `spec.json`, `status.json`, `summary.json`, `result.csv`, `log.txt` and `plots/`. The user can open these; the full series is in `result.csv`. Don't read it into the conversation; use the tools above. A live run also has `live.json` (current values and chart history, replaced as it runs), `commands/` (one file per command) and `outcomes/` (the worker's answer to each, with the simulation time); together they replay the run.
