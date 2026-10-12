---
name: system-builder
description: How to connect FMUs into a system and co-simulate it with the Agentic FMI tools - write a JSON system file, check it with system_check, run it with run_start, tune it, and exchange SSP and SSV files with other tools. Use whenever the user asks to connect, wire, couple or co-simulate FMUs, to build a closed loop of a controller and a plant, to tune a gain or parameter in a connected model, to export a system, or mentions SSP, SSD or SSV files.
---

# Building systems from FMUs

You write the system file (JSON) with the Write tool; the tools check, run, export and import it. FMUs themselves: [fmi-expert](../fmi-expert/SKILL.md) to inspect and run one, [fmu-authoring](../fmu-authoring/SKILL.md) to create one. A system runs in OMSimulator inside an isolated worker, and the result is an ordinary run. In ninja mode, reply to a system run with its 🥷 line only ([Response style](../fmi-expert/SKILL.md#response-style)).

## The system file

[examples/loop.json](examples/loop.json) closes the loop of the two `fmu-authoring` examples (PI controller `u` to plant `F`, plant `x` back to controller `y`):

```json
{
  "version": 1,
  "components": {
    "PI": {"fmu": "generated/pi_controller/pi_controller.fmu", "parameters": {"Kp": 10, "Ki": 15, "r": 1}},
    "plant": {"fmu": "generated/MSD/MSD.fmu"}
  },
  "connections": [{"from": "PI.u", "to": "plant.F"}, {"from": "plant.x", "to": "PI.y"}],
  "experiment": {"start_time": 0, "stop_time": 10, "step_size": 0.01}
}
```

- Unknown keys are refused. There is no `outputs` key: `run_start` records every component's outputs (`PI.u`, `plant.x`, `plant.v`) unless you pass `outputs`.
- A component name starts with a letter or `_` (at most 64 characters). A reference is `<component>.<variable>`, split at the first dot. A connection runs from an output to an input of the same type family (Real, Integer, Boolean, Enumeration) and unit.
- `fmu` paths are relative to the system file's folder and must stay inside the workspace. Use the names from `fmu_inspect`; don't guess variable names.
- `parameters` set parameters, inputs that no connection drives, and states with `initial` exact or approx. Outputs and calculated parameters can't be set.
- `step_size` is required. `start_time` must be 0, and `stop_time` a whole number of steps.

## Workflow

1. **Build or find the components** (`fmu_build`, `fmu_list`) and `fmu_inspect` each: note inputs, outputs, units and parameters.
2. **Test each component alone** with `run_start(fmu=...)`. A Python component that raises inside a system aborts the worker (`worker_lost`, the abort in `log_tail`); alone it gives a readable `simulation_error`.
3. **Write the system file** in the workspace.
4. **`system_check`**. Fix every error (each names its fix), then read the warnings: an input nobody drives keeps its start value, and a unit declared on one side only passes through unchanged.
5. **`run_start(system=..., output_interval=h)`**. `output_interval` is the communication step h; `stop_time` and `start_values` may be passed too. The summary lists `<component>.<variable>`. Check a value you can compute by hand.
6. **`run_compare`** and **`run_plot`** (`variables=["plant.x"]`) to judge the dynamics.
7. **Tune** without editing the file: `start_values={"PI.Kp": 8}`, or `paramset` with an `.ssv` file. Precedence, lowest first: the FMU's own start value, the system file, the `.ssv`, `start_values`. Each run records the layer that set every parameter, so `run_compare` shows what differed.
8. **Share** with `system_export` (the system file, a new `.ssp` path, optionally a `paramset`). The `.ssp` holds the FMUs, the step and one SSV (the system file's values, then the paramset's). It never overwrites, and the same input gives the same bytes. For tuned gains, pass an `.ssv` or write them into the system file first.
9. **`system_import(source, name?)`** takes a flat `.ssp` from another tool and writes `imported/<name>/<name>.json` plus private FMU copies, with the SSV values as component parameters. Read its warnings (overridden values, ignored names), then `system_check` and run it like any system file.

A minimal `.ssv` for `paramset` (names are `<component>.<variable>`):

```xml
<ssv:ParameterSet xmlns:ssv="http://ssp-standard.org/SSP1/SystemStructureParameterValues" version="1.0" name="tuned">
  <ssv:Parameters><ssv:Parameter name="PI.Kp"><ssv:Real value="8"/></ssv:Parameter></ssv:Parameters>
</ssv:ParameterSet>
```

## Choosing the step h

OMSimulator couples the components Jacobi-style: all of them step from the same communication point with the inputs of the previous exchange, so a loop gains up to one step of delay. **h changes the dynamics, not only the accuracy.** The example loop (Kp = 10, Ki = 15) overshoots 10.4 % at h = 0.01, 7.9 % at h = 0.001 and about 7.6 % in continuous time, and is unstable at h = 0.1.

- Start at 0.001 to 0.01 for fast dynamics, and h well below the loop's fastest time constant or oscillation period (the example rings with a period of about 1.4 s).
- h must divide the horizon (a whole number of steps), and the result has one row per step: `stop_time / h + 1` rows. 10 s at 0.001 is 10001 rows and a run of a few seconds.
- Halve h and rerun. If the answer moves, the step is part of the model: tell the user which h the numbers are for.

## Tuning a loop

- Read the overshoot from the summary: `(max - setpoint) / setpoint` of the controlled output, and `final` for the steady state. Plot to see settling and ringing.
- Vary **one gain at a time** and compare against the baseline, then adjust the pair. Both Kp and Ki matter, and the effects aren't monotonic. In the example loop at h = 0.01: Kp = 8 alone gives about 9 %, Ki = 12 alone gives 3.7 %, and Kp = 8 with Ki = 12 gives 2.0 % and settles faster.
- Check the winner at a smaller h before reporting it, and keep to four runs at a time.
- Say what changed (`run_compare` lists it), the overshoot with its h, and show the plot.

## Live system runs

`run_start(system=..., live=true, output_interval=h)` starts the loop paused at t = 0 (the [fmi-expert skill](../fmi-expert/SKILL.md#live-runs) has the controls: `run_control` with pause, resume, step, set and stop, and `run_status(values=[...])`). Use it to tune by hand while the user watches.

- **The step unit is the communication step h.** `step` with `n` advances n steps and `dt` is rounded up to whole steps. A paced run (1x by default) spends h of wall time per step; resume with a larger `speed` for a long loop.
- **Change a gain mid-run** with `run_control(run_id, "set", values={"PI.Kp": 12, "PI.Ki": 8})`. It takes effect from the next step, so the plot shows the break. Names are `<component>.<variable>`, and the parameter must be `tunable` (both PI gains are).
- **Refused, with the reason, and the run goes on:** a connected input (`plant.F` is driven by `PI.u` and would be overwritten after one step), an output, and a `fixed` parameter (those go in `start_values` of a new run). Set an unconnected input or a tunable parameter instead.
- The loop is still the Jacobi recurrence, with the gains switched at the step where the set landed (a test pins this to 1e-12), so the tuning rules above hold. Say at which time you changed what; `run_plot` shows the whole run once it has ended.

## Limits

- Flat systems of Co-Simulation FMUs, FMI 2.0 or 3.0 mixed freely. ModelExchange-only and ScheduledExecution FMUs are refused; an FMU that allows one instance per process can be used once. Platform: Linux x86-64 binaries.
- A fixed step and Jacobi coupling only; no algebraic-loop detection (an instantaneous loop is lagged one step) and no unit or type conversion.
- A system run records one row per step: more than 5e6 result values (rows times columns) is refused unless `force`. Raise `output_interval`, record fewer `outputs` or shorten `stop_time` instead.
- `run_start` refuses `inputs`, `fmi_type`, `solver` and `relative_tolerance` for systems. For a time-varying input, add a source component (a Python FMU whose output you connect).
- String, Binary and array variables can't be connected, set or recorded.
- `system_import` refuses nested systems, parameter mappings (SSM), signal dictionaries, system-level connectors and connection transformations by name. An FMI 3.0 component, or a state a value sets (connector kind `local`, which SSP 1.0 lacks), exports as SSD 2.0.
- A system run starts at t = 0 and, for pythonfmu components, imports their slave modules into one interpreter: two different FMUs with the same module name are refused.
