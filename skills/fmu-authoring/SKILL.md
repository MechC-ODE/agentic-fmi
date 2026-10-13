---
name: fmu-authoring
description: How to create FMUs (Functional Mock-up Units) with the Agentic FMI tools by writing a Python or Modelica source file and building it with fmu_build. Use whenever the user asks to create, generate, build or write an FMU, a controller (PI, PID), a plant or process model, a Modelica model, or a Python FMU, or asks to turn a model description into something that can be simulated.
---

# Authoring FMUs

You write the source file yourself with the Write tool (the user sees it in the write prompt), then `fmu_build` turns it into an FMI 2.0 FMU under `generated/`. The tool never edits your source. To simulate the result, follow the [fmi-expert skill](../fmi-expert/SKILL.md). In ninja mode, reply to a build with its 🥷 line only ([Response style](../fmi-expert/SKILL.md#response-style)).

## Python or Modelica?

| | Python (`.py`, pythonfmu) | Modelica (`.mo`, OpenModelica) |
|---|---|---|
| Best for | Controllers, signal logic, discrete-time algorithms, anything written as "each step, compute..." | Physical plants written as equations: mechanics, electrics, thermal, fluids |
| Interfaces | Co-Simulation only | Model Exchange and Co-Simulation |
| Solver | None: you advance the state in `do_step` | The model's own equations, CVODE in Co-Simulation |
| Build time | A second or two | 6 to 20 s for a small model, minutes for a large one |
| FMU binaries | linux64 and win64 (only Linux runs here) | linux64 only |

If the user wants both a plant and a controller, make the plant Modelica and the controller Python (or Python too, when `omc` isn't installed).

## Ready-made examples

Tested sources that build offline. Use them unchanged when the user asks for an example or a demo, and adapt them when writing something similar:

| File | What it is | Interface |
|---|---|---|
| [bouncing_ball.py](examples/bouncing_ball.py) | A ball that falls and bounces; impacts found exactly | outputs `h`, `v`; tunable `e`, fixed `g` |
| [pi_controller.py](examples/pi_controller.py) | PI controller | input `y`, output `u`; tunable `Kp`, `Ki`, `r` |
| [MSD.mo](examples/MSD.mo) | Mass-spring-damper plant in Modelica (needs `omc`) | input `F`, outputs `x`, `v`; parameters `m`, `k`, `d` |
| [MSD.py](examples/MSD.py) | The same plant in Python, for when `omc` is missing | the same as `MSD.mo` |

The controller and the plant are made to be wired together: controller `u` to plant `F`, plant `x` to controller `y` (the system-builder example `loop.json`). `MSD.mo` and `MSD.py` both build to `generated/MSD/MSD.fmu`, so copy only one of them into a workspace. To use an example: Read it, Write it unchanged to `models/<file>` in the workspace, then `fmu_build` it.

## Workflow

1. **Write** the source in the workspace (for example `models/<name>.py`). Not under `generated/` or `.agentic-fmi/`, and at most 256 KiB.
2. **`fmu_build`** with `source`. The result gives the FMU's path, sha256 and interfaces.
3. **Read the diagnostics** if it fails: an error with up to 10 lines `path:line:col: error: message` (errors first) and the path of `build.log` for the rest. Fix the named lines with a **small Edit**, not a rewrite.
4. **Rebuild** with `overwrite=true` (needed once an FMU of that name exists).
5. **`fmu_inspect`** the new FMU: check that the inputs, outputs and parameters are the ones you meant, and the default experiment.
6. **`run_start`** a short run and check the numbers against what the model should do (a steady-state value you can compute by hand). Then iterate: Edit, rebuild with `overwrite=true`, rerun.

Tell the user which file you wrote, which FMU it became, and the numbers that confirm it behaves.

## Long builds

`fmu_build` waits up to `wait_s` (default 45 s, at most 60 s). A build that finished returns its result and a failed one raises its diagnostics. A build still running returns a run ID: poll **`run_status`** with it until `state` is `finished` or `failed`, and use **`run_stop`** to cancel. Builds are jobs like simulation runs: `run_list` shows them, they count toward the four runs at a time, and they time out after 30 minutes. A failed or stopped build leaves `generated/` unchanged.

## Python rules (pythonfmu)

- One `Fmi2Slave` subclass per file; its class name becomes the model name. The file stem must be a valid identifier that is no Python keyword and no existing module (`random.py` or `numpy.py` are refused).
- Set every attribute in `__init__` before `register_variable`: the value is the variable's start value. Register with `Real(name, causality=..., variability=...)`: `Fmi2Causality.parameter` needs `Fmi2Variability.fixed` or `tunable`; `input` and `output` need none. `Integer`, `Boolean` and `String` exist too.
- Give every output `initial=Fmi2Initial.exact` (import `Fmi2Initial`) and a start value (the attribute you set in `__init__`), or the FMU fails `fmu_validate`: pythonfmu writes no `ModelStructure/InitialUnknowns`, and FMPy reports "InitialUnknowns does not contain the expected set of variables".
- Compute in `do_step(self, current_time, step_size)` and return `True`. Outputs set in `do_step` appear after the step, so they lag the inputs by one communication step.
- Set a `default_experiment = DefaultExperiment(start_time=..., stop_time=..., step_size=...)`; `run_start` uses it as the default.
- Top-level code runs at build time and in every instance: no network, no file access, no side effects. Keep to the standard library; the FMU does not bundle third-party packages.
- Builds FMI 2.0 Co-Simulation only. The FMU needs the same CPython minor version (3.12) wherever it runs. It runs in our engines as it is; an external non-Python tool needs `LD_PRELOAD` of `libpython3.12.so`.

## Modelica rules (OpenModelica)

- The file `Name.mo` holds a top-level `model Name` with the same name (a Modelica identifier). Build errors are reported as `path:line:col: error: message` with the workspace-relative path of your file, for example `models/Name.mo:5:1: error: ...`.
- Interface: `input Real F`, `output Real x`, `parameter Real k = 10`. Every unknown needs an equation (as many equations as unknowns), and states need start values: `Real x(start = 0, fixed = true)`.
- Parameters come out as `fixed`, so set them in `start_values`, not `inputs`. A state declared `output` also yields harmless `local` variables such as `_D_outputAlias_x`.
- Add `annotation(experiment(StartTime = 0, StopTime = T, Tolerance = 1e-6))`: it becomes the default experiment, so choose a `StopTime` long enough for the dynamics to settle.
- The Modelica Standard Library 4.1.0 (`Modelica.*`) is installed and works offline. Other Modelica libraries are not installed; avoid external C code.
- FMI 2.0 only, Model Exchange and Co-Simulation. Co-Simulation integrates with CVODE (we always build it so; plain OpenModelica uses forward Euler, which is inaccurate).

## Names and outputs

- `name` is optional and defaults to the file stem: a letter, then letters, digits or `_`, at most 64 characters, matching `^[A-Za-z][A-Za-z0-9_]{0,63}$`. Windows device names (`CON`, `NUL`, ...) and names that differ from an existing one only by case are refused.
- The FMU lands in `generated/<name>/<name>.fmu` next to a `build.json` (source hash, tool versions, options). Pass the path to the other tools as `fmu`. Don't edit anything under `generated/`: change the source and rebuild.
- `overwrite=true` replaces only a directory that an earlier `fmu_build` made. Without it, an existing name is refused; either pick a new `name` or set `overwrite=true`.

## Safety

A build **executes the code**: pythonfmu imports the file, and OpenModelica can run functions and compile external C. This is not a sandbox: the build runs as the user, with network access. The protection is that the user reads the file in the Write prompt and approves the `fmu_build` call. Write only what the user asked for, and don't build files you didn't write or read.
