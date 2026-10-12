# Phase 0 spikes

The Phase 0 feasibility spikes check the libraries the design depends on before any product code uses them. Each spike is a throwaway script in [`spikes/`](../spikes/README.md). It prints a JSON report and writes that report, with every generated FMU, SSP and result file, to the gitignored `.cache/spikes/<letter>/`. This page records the verdicts, the evidence and what each one means for the design. The spikes ran in the order c, b, a, d, e. Spike (f) ran later, in Phase 2 (task 2.5a), and has no script in `spikes/`: it drives `omc` directly from a scratch directory in the `/lab` volume, and section (f) lists the model and the commands. Spikes (g) and (h) prepared and accompanied Phase 3, and spike (i) prepared Phase 4. Each section below is self-contained.

FMI 1.0 is out of scope by project decision. The project supports FMI 2.0 and FMI 3.0 only, so no spike covers FMI 1.0.

## Summary

| Spike | Verdict | Key finding | Design implication |
|---|---|---|---|
| (c) OMSimulator 3 | **PASS** | A 2-FMU SSP exports, re-imports, simulates and passes the connected signal through. The installed API can step: `InstantiatedModel.stepUntil(t)` and `doStep()`, with `setValue` and `getValue` between steps. FMI 3.0 needs Binary signals excluded from the result file. | OMSimulator can run live systems, not only batch ones. Run it only in a worker process: one model per process, it logs to stdout, and one `Capi` call segfaults. |
| (b) FMPy step control | **PASS** | Through `step_finished`, the worker can pause (sim time frozen 2.2 s while the heartbeat continued), set `e` (read back as 0.5, apex ratio 0.49 → 0.25) and stop cleanly (`Terminate -> OK`, `FreeInstance`). Command latency is 10–19 ms on the bind mount and 2–4 ms on a container-local directory. In ME, `e` can only be set inside Event Mode. | The worker applies commands at `step_finished`. For ME it wraps parameter changes in Event Mode. Keep the command inbox off the Windows bind mount. Don't pass FMPy's `timeout`. |
| (a) FMPy matrix | **PARTIAL** | 27 of 29 FMU × interface runs pass on x86_64-linux with the default experiment. Clocks is ScheduledExecution-only, which `simulate_fmu` can't run. Roberts in CS doesn't finish in 120 s, and FMPy's `timeout=5` doesn't stop it either. | Report ScheduledExecution as unsupported. Enforce run timeouts by killing the worker process. Estimate the step count (horizon divided by `fixedInternalStepSize`) before a run and warn. |
| (d) FMPy container FMU | **PARTIAL** | FMI 3.0 works, with workarounds: Stair → Feedthrough inside the container gives y = u exactly and the same counter transitions as a standalone Stair. FMI 2.0 works only until a nested FMU terminates (it fails at t = 8.9). The documented import path doesn't exist. | Use OMSimulator for systems (c works). If containers are ever used, generate them as FMI 3.0 with our own config builder, which must follow the binary's value-reference-0 = time convention. |
| (e) pythonfmu | **PASS** | The Gain FMU runs in FMPy and in OMSimulator (y = k·u, and k is tunable mid-run) with no `LD_PRELOAD`. The FMU bundles its own `pythonfmu`. Only a non-Python host needs `LD_PRELOAD` of libpython. | Generated FMUs work in our Python-hosted engines as they are. Document `LD_PRELOAD` and Python 3.12 as runtime requirements for external tools. |
| (f) OpenModelica `omc` | **PASS** (caveats) | `buildModelFMU` turns a Modelica model into a self-contained FMI 2.0 ME+CS FMU in 6 s warm and 20 s on one core (budget: 60 s). It works offline. FMPy runs it as ME and as CS. The default CS solver is forward Euler (max error 8e-3, against 2.6e-7 with `--fmiFlags=s:cvode`). `omc` exits 0 on every failure, and the real result is the returned path. | `fmu_build` must decide success from a non-empty returned path plus the file on disk, not from the exit code or from error text. Pass `s:cvode`, run in a fresh scratch directory with a writable `HOME`, and parse `getErrorString()` with one regex. FMI 3.0 output isn't available. |
| (g) Closed loop in OMSimulator | **PASS** (caveats) | The pythonfmu PI and the omc MSD close the loop in OMSimulator inside a Python process, with no `LD_PRELOAD` and no GIL problem. The result equals an FMPy Jacobi master on OMSimulator's time grid to 5e-12. `Kp` changes between `stepUntil` calls take effect at the next step. An SSP round trip reproduces the CSV byte for byte. At h = 0.1 the Jacobi loop is unstable. `stepUntil(t)` can overshoot `t` by one step, exports aren't byte-reproducible, and a component-level SSV can be lost on re-export. | Put OMSimulator's temp dirs on a container-local path (instantiate 2.6 s → 0.13 s). Step with `doStep()` or `stepUntil(t − h/2)`. Deduplicate result rows. Write SSV ourselves and bind it at the root. Rezip SSPs deterministically. Keep `import OMSimulator` out of the MCP process. Pin the nightly tarball by sha256. |
| (h) OMSimulator engine facts | **PASS** (caveats) | `setResultFile("")` after `instantiate()` stops `default_res.mat`; `removeSignalsFromResults(".*")` doesn't. `doStep()` then `getValue()` gives post-exchange values equal to the exact Jacobi recurrence to 2.4e-15. `setValue` before `initialize()` works, and a refusal is only a `RuntimeWarning`. Hand-written SSD/SSV files validate and re-import. The clock always starts at 0. A discarded step stalls the clock silently, and a pythonfmu slave that raises aborts the process. | Build the model through the API, turn the result file off, check every `setValue`, step with `doStep()` only, refuse a non-zero start time, raise when a step makes no progress, pin the package tree (`660de535…07fdd5`). A raising pythonfmu component ends as `worker_lost`. |
| (i) Live control and the pane API | **PASS** (design changes) | FMPy CS: a gate in `step_finished` and our own doStep loop give bit-identical results, but `step_finished` first fires after step 1. FMPy ME: `step_finished` can't reset CVode or see new event times, so a live `k` or input change is off by 3.6e-2 and 1.1e-1, a state jump is lost and a requested termination time is missed; our own loop on FMPy's `CVodeSolver` is exact to the tolerance. OMSimulator after `initialize()`: tunable parameters and unconnected inputs apply at the next `doStep()`, a connected input holds for one step only, outputs and fixed parameters are refused. Discard and terminate end differently in each engine. Heartbeat gaps stay at most 51 ms while paused. `$.fs` has no rename and its `write` isn't atomic. `claude plugin test` mounts terminal and desktop with no login. | Own the FMPy loops for live runs (ME must), with a gate at the start time. Validate every set against the modelDescription and the system's wiring. Give discard and terminate one outcome in both engines. The pane writes create-once, uniquely named command files, and the worker skips files that don't parse. The pane finds the store from configuration, the environment or `run_start`'s result, not from the session root. |

## Environment

All spikes ran inside the dev container `agentic-fmi-dev` (see [development.md](development.md)), on a Windows 11 host with the repository bind-mounted at `/workspace`.

| Item | Version |
|---|---|
| CPU architecture | x86_64 (16 cores visible) |
| OS | Ubuntu 24.04.5 LTS, kernel 6.6.87.2-microsoft-standard-WSL2, Docker 29.3.0 |
| Python | CPython 3.12.13 (uv-managed python-build-standalone), uv 0.11.14 |
| FMPy | 0.3.32 |
| OMSimulator | 3.0.0.post198 (native library reports `OMSimulator v3.0.0.post198-gf14b85b-linux`) |
| pythonfmu | 0.7.0 |
| Others | numpy 2.5.3, lxml 6.1.3, gcc 13.3.0 (spike e only) |
| Reference FMUs | v0.0.41, `Reference-FMUs.zip` sha256 `62babca76b9c23a51c3096be4bb5930ff8b4388659056be3c0c4ef7a3aeb5403`, extracted to `.cache/reference-fmus/0.0.41/{2.0,3.0}` |

To rerun a spike from the Windows host (Git Bash):

```bash
MSYS_NO_PATHCONV=1 docker exec -w /workspace agentic-fmi-dev bash -lc 'uv run python spikes/<script>.py'
```

### Input FMUs are never modified

Every spike hashes the Reference FMUs it reads (SHA-256) before and after it runs, and records both sets in its `result.json` under `input_sha256`. In every spike `unchanged` is `true`. Spike (a) reads all 15 FMUs. These were the hashes before and after:

| FMU | FMI 2.0 sha256 | FMI 3.0 sha256 |
|---|---|---|
| BouncingBall | `3959d0dea751e788b7b9591da6beb11955352a1ac46138244ee5343efb7ce5e4` | `f14f1f46c97c21bc208c139fa30d19ebce5b1d0fa0789f09a1ff1b9a6b9ee8c5` |
| Clocks | n/a | `03d07fe5aa6304747584db02169e1a9f202bc5a1d1e0db3113218b60cbf8923d` |
| Dahlquist | `cecf1fb0f04cbb9de102c783dd7d905a17aec0c6e8ce4641f54a0b26673c2c7f` | `228feadfa5706dc76746bee3cf2a4bb25b42971708b0d06435f142284215d0c6` |
| Feedthrough | `2d4ac516803691be0bb69e92a85d5ba70abab795323903bbb3e719af6964ddad` | `cbb038007286be266707fb919ed4614ea3c5eba3c18941e3d50482a2b8e467c9` |
| Resource | `11760ee03d097a459dbd513bc26fb21656f72a4b6fd25514de10d61eba68a6c4` | `3bf97e1a5b97b24c7705080da96ca3d3766d3f12a4a5fd79b86914ccac6187b7` |
| Roberts | n/a | `dd177f02bc85def137d1859431de3bb42a8549ea6e310d77c46b9bd4c167e0c1` |
| Stair | `917f5a7123cf5b2857ac6bc7a121fe96e0a37a9a734b84d0166fd0554298789c` | `031dc8028fae3d8c48c7f3c11ab4f7db5ab1df81f9e0a8dc1f5610c69d1939d4` |
| StateSpace | n/a | `ec38d2467a446b0519983f029668648de74bdab2b53afbe9274e8d2013751a4f` |
| VanDerPol | `c22b935903fab007c57319d4134b6a9c08a7139d90c97150310dc05afe823a3d` | `f4c50e976db5b732809641f91f183abb3488b3e01cbe35f032bfd297da82d3d9` |

The libraries always work on copies. OMSimulator copies each resource into its SSP temp directory. FMPy and the container builder extract the FMU into a temp directory. The output of `pythonfmu build` goes to `.cache/spikes/e`.

---

## (c) OMSimulator 3: SSP round trip and step-wise control

**Verdict: PASS.** It passes on FMI 2.0 with no workaround. It also passes on FMI 3.0 once Binary signals are excluded from the result file.

**Command:** `uv run python spikes/c_omsimulator.py`. Output: `.cache/spikes/c/` (`result.json`, `feedthrough_chain_fmi{20,30}.ssp`, `batch_*.csv`, `stepped_*.csv`).

**What it does.**
1. It builds an SSP with `from OMSimulator import SSP, CRef`. The SSP has two Feedthrough instances, A and B, and one connection, `A.Float64_continuous_output -> B.Float64_continuous_input`, plus an inline binding `A.Float64_continuous_input = 3.0`.
2. It exports the SSP with `SSP.export`, re-imports it with `SSP(path)` and instantiates it with `SSP.instantiate()`, which returns an `InstantiatedModel`.
3. It sets `A.Float64_continuous_input = 4.0`, simulates to the SSD default stop time of 1.0 and reads B's output from the CSV result file.
4. It steps the same system in 10 increments, changing A's input to 7.0 after the fifth increment. It does this once through the high-level API and once through `Capi`, the wrapper around the `oms_*` C functions.

The API was checked against the installed source in `site-packages/OMSimulator/` (`ssp.py`, `ssd.py`, `instantiated_model.py`, `capi.py`), not against the online docs.

**Results (FMI 2.0).**
- After re-import, the connection list is `A.Float64_continuous_output -> B.Float64_continuous_input` and the binding reads back as 3.0.
- Batch: 1001 result rows (the default `oms_ma` step is 1 ms). `B.Float64_continuous_output` is 4.0 in every row, so the connection works and the runtime value overrides the SSD binding. `initialize()` plus `simulate()` took 0.023 s.
- Stepping: one `doStep()` advanced from 0 to 0.001. The ten `stepUntil` calls took 1.9–2.3 ms each.

  | t | 0.1–0.5 | 0.6–1.0 |
  |---|---|---|
  | A input (`getValue`) | 4.0 | 7.0 |
  | B output (`getValue`) | 4.0 | 7.0 |

  In the result file, B first shows 7.0 at t = 0.501. The change takes effect at the next communication step.
- `Capi` stepping gives the same trace with every status `Status.ok`.

**Results (FMI 3.0 Feedthrough).**
- As is, with no workaround, the CSV has its header and **0 rows**. stderr gets 2024 error lines, and `simulate` takes 1.40 s, against 0.034 s with the workaround. Each 0.1 s `stepUntil` takes about 140 ms, against about 2.7 ms. The getters still return correct values.

  ```
  warning: Unknown FMI3 base type for var : Binary_input
  error:   [getReal] UnSupported Numeric Type:
  error:   [updateSignals] failed to fetch variable Binary_input
  ```
- With `inst.removeSignalsFromResults(".*Binary.*")` before `initialize()`: 1001 rows, B = 4.0 throughout, and the same stepping trace as FMI 2.0 (switch at t = 0.501).
- After re-import, the inline FMI 3.0 binding comes back as the string `'3.0'`, where FMI 2.0 gives the float `3.0`. The value is still applied.

**Can a system be advanced step by step with set/get between steps? Yes.** These calls work in 3.0.0.post198:

- High level, on the object returned by `SSP(path).instantiate()`: `setResultFile(path)`, `setValue(CRef(...), v)`, `initialize()`, then any mix of `stepUntil(t)`, `doStep()`, `setValue(...)`, `getValue(...)` and `getTime()`, then `terminate()` and `delete()`. `setValue` after `initialize()` is allowed for inputs and tunable parameters. For any other variable, the library issues a `RuntimeWarning` and skips the set (`checkfmiRequirements`).
- Low level, the `oms_*` C API through `from OMSimulator import Capi`, on the model cref `"model"` and the variable cref `"model.root.<component>.<var>"`: `Capi.setReal`, `Capi.initialize`, `Capi.stepUntil`, `Capi.doStep`, `Capi.getTime`, `Capi.getReal`, `Capi.terminate` and `Capi.delete`. Each returns a `Status`.
- Legacy API: the package has **no** `OMSimulator` class and no `oms` module (`hasattr(OMSimulator, "OMSimulator")` is `False`). The public names are `CRef`, `Capi`, `SSP`, `SSD`, `SSV`, `SSM`, `System`, `FMU`, `InstantiatedModel` and so on. The native library still exports the legacy functions (`oms_importFile`, `oms_export`, `oms_getState`, `oms_setState`, and others), reachable through `Capi.obj` by ctypes, but nothing needs them.

**Other findings, quoted verbatim.**
- **One model per process.** `InstantiatedModel` hard-codes the model name `"model"`. A second `instantiate()` while the first is alive fails with `RuntimeError: Failed to create new model: Status.error`, logged natively as `error:   [newModel] "model" already exists in the scope`.
- **A segfault.** `Capi.getResultFileSignals("model")` kills the process (exit -11), so the spike runs it in a child process. The child prints `warning: Unknown result file type: ` and then `Fatal Python error: Segmentation fault`.
- **Logging goes to stdout.** OMSimulator writes its `info:` and `warning:` lines to stdout (53 lines in this run) and its `error:` lines to stderr. In the MCP process this would corrupt the stdio channel.
- **Temp directory leak.** Each `instantiate()` calls `tempfile.mkdtemp()` and never removes the directory. This run leaked 10 empty directories; the spike redirects them to `.cache/spikes/c/process-tmp`.
- **Deleting an uninitialized model.** `delete()` on a model that was instantiated but never initialized logs `error:   [fmi2logger] [fmi2Error] B/logStatusError: fmi2Terminate: Illegal call sequence.` It doesn't raise.

**Packaging.** PyPI publishes OMSimulator 3.0.0.post198 as an 8937-byte **sdist only**. `uv.lock` pins the sdist hash (`sha256:7365aa92…`), but its build hook (`build_helpers.py`, `OMSInstaller.fetch_oms`) downloads the native binaries at install time from `https://build.openmodelica.org/omsimulator/nightly/linux-amd64/OMSimulator-linux-amd64-v3.0.0.post198-gf14b85b.tar.gz`. It picks that URL for Ubuntu 24.04 and a `linux-focal-amd64` one for anything else. The download uses `requests.get(url, verify=False)`, so TLS is not verified, and it checks no hash. The locally built wheel is tagged `py3-none-any` even though it contains `libOMSimulator.so` (sha256 `818b054f…02b0fb` here). The first install needs network access, and `uv.lock` can't vouch for the binary.

**Pin.** Because (c) passed, `pyproject.toml` pins `oms = ["OMSimulator==3.0.0.post198"]` and `uv lock` was rerun. Only the `requires-dist` specifier changed, and the locked version was already 3.0.0.post198.

**Design implication.** ADR 0004 holds, and it doesn't need its fallbacks: OMSimulator 3 can serve live systems as well as batch ones, so the `Instance` protocol can require `step` from it. Drive it only from a worker process (ADR 0002), with one system per process. The worker must:
- exclude Binary signals from results for FMI 3.0 (String signals are already skipped, with an `info:` line);
- never call `Capi.getResultFileSignals`;
- treat a second instantiate in the same process as a bug;
- clean up the temp directories itself.

Pin the version exactly, and record the binary's sha256 at install time, because the lock file can't.

---

## (b) FMPy step control through `step_finished`

**Verdict: PASS.**

**Command:** `uv run python spikes/b_step_control.py`. Output: `.cache/spikes/b/` (`result.json`, plus one run directory per variant with `state.json` and the `inbox/`).

**What it does.** It runs BouncingBall in four variants: FMI 2.0 and 3.0, each in CS and ME. The `step_finished` callback (FMPy calls it as `cb(time, recorder)`) works like this:
- After every step it writes `state.json` atomically, containing `t`, `paused` and a wall-clock `heartbeat`.
- It polls `inbox/` for JSON command files.
- It paces the simulation to wall-clock time so another process can react.

A separate **writer process** (the same script, run with `--writer`) watches `state.json` and drops commands into the inbox. Each command is written to a `.tmp` file, renamed into place and stamped with `written_at`. The commands are:

1. `pause` at t ≥ 0.5, then `resume` 2.2 s later.
2. `set e = 0.5` at t ≥ 1.0.
3. `stop` at t ≥ 2.5.

Latency is the time from `written_at` until the callback applies the command. The callback reaches the live FMU through **`recorder.fmu`**: `FMU2Slave`, `FMU2Model`, `FMU3Slave` or `FMU3Model`. Passing `fmu_instance=` to `simulate_fmu` would work too.

**`e` in the modelDescription.** In both FMI 2.0 and FMI 3.0, `e` is `causality="parameter" variability="tunable" initial="exact"`, with start 0.7, min 0.5 and max 1.

**Results.**

| Variant | Callbacks | Pause: sim t frozen at / heartbeats / span | `e` read back | Latency pause / resume / set / stop (ms) | Stopped at | Last FMI calls |
|---|---|---|---|---|---|---|
| FMI 2.0 CS | 251 | 0.51 / 157 / 2.21 s | 0.5 | 11.1 / 12.3 / 19.2 / 11.5 | 2.51 | `fmi2Terminate -> OK`, `fmi2FreeInstance` |
| FMI 2.0 ME | 632 | 0.508 / 155 / 2.21 s | 0.5 | 15.7 / 11.1 / 13.8 / 11.0 | 2.504 | `fmi2Terminate -> OK`, `fmi2FreeInstance` |
| FMI 3.0 CS | 627 | 0.504 / 153 / 2.21 s | 0.5 | 9.8 / 11.2 / 15.4 / 18.1 | 2.508 | `fmi3Terminate -> OK`, `fmi3FreeInstance` |
| FMI 3.0 ME | 633 | 0.508 / 154 / 2.21 s | 0.5 | 15.4 / 14.4 / 12.1 / 15.6 | 2.504 | `fmi3Terminate -> OK`, `fmi3FreeInstance` |
| FMI 3.0 CS, IPC on container-local `/tmp` | 626 | 0.504 / 403 / 2.20 s | 0.5 | 3.9 / 2.5 / 2.2 / 2.7 | 2.504 | `fmi3Terminate -> OK`, `fmi3FreeInstance` |

- **Pause.** During the pause the published simulation time stayed at one value while 153–157 heartbeats advanced over 2.2 s of wall clock.
- **Effect of `e` on bounce apexes.** Apexes are the local maxima of `h`. The baseline is the same variant with no commands.

  | | Apex 1 (before the set) | Apex 2 | Apex 3 | Apex 2 / apex 1 |
  |---|---|---|---|---|
  | CS baseline (e = 0.7) | 0.4948 | 0.2455 | 0.1221 | 0.496 ≈ 0.7² |
  | CS, e = 0.5 at t ≈ 1.0 | 0.4948 | 0.1255 | 0.0320 | 0.254 ≈ 0.5² |
  | ME baseline (e = 0.7) | 0.4900 | 0.2401 | 0.1176 | 0.490 |
  | ME, e = 0.5 at t ≈ 1.0 | 0.4900 | 0.1225 | 0.0306 | 0.250 |

  FMI 2.0 and FMI 3.0 give the same apex values.
- **Stop.** Returning `False` from the callback ends the loop. FMPy then calls `terminate()` and `freeInstance()`, which the `fmi_call_logger` tail shows, and returns the partial result: 252–639 rows ending at t ≈ 2.5 of the stop time 3.0.
- **Callback overhead.** On the bind mount, one callback (atomic state write plus inbox poll) takes about 8.1–8.2 ms. With a 4 ms output interval (FMI 3.0 CS and ME) pacing can't keep up, and the runs take 7.4–7.6 s of wall time instead of about 4.7 s. On container-local `/tmp` a callback takes 0.33 ms and the run takes 4.71 s.
- **Model Exchange needs Event Mode for parameter changes.** Setting `e` straight from `step_finished` in ME (the `naive_me_set` probe) is rejected by the FMU. The rejection also leaves the instance in an error state, so the whole run aborts. The same happens in FMI 2.0 and FMI 3.0:

  ```
  [ERROR] Variable e can only be set after instantiation, in initialization mode or event mode.
  [ERROR] fmi2SetTime: Illegal call sequence.
  [ERROR] fmi2SetContinuousStates: Illegal call sequence.
  FMICallException: fmi2SetContinuousStates failed with status 3 (error).
  ```

  The FMI 3.0 run gives the same messages with `fmi3SetFloat64`, `fmi3SetTime` and `fmi3SetContinuousStates`. CVode also logs `At t = 1, the rootfinding routine failed in an unrecoverable manner.` What works, and what the ME rows above use:
  1. `fmu.enterEventMode()`
  2. set the value
  3. loop `newDiscreteStates()` (FMI 2.0) or `updateDiscreteStates()` (FMI 3.0) until no further update is needed
  4. `fmu.enterContinuousTimeMode()`

  CS accepts the set directly.
- **FMPy's `timeout` and pauses.** `simulate_fmu(timeout=1.0)` with a 2 s sleep in `step_finished` at t = 0.5 returns **normally, with no exception and no warning**. The result is cut off at t = 0.500 of the stop time 3.0, after 2.04 s of wall time. FMPy checks the timeout at the top of each loop iteration, using wall time since start, so time spent paused counts against it. Spike (a) shows the opposite failure as well: the check can't interrupt a long native `doStep`.

**Design implication.** This confirms ADR 0002 and the step gate in the architecture doc. Specifically:
- The worker applies inbox commands inside `step_finished`, using `recorder.fmu`.
- For ME engines, the worker wraps every parameter change in Event Mode, then the discrete-state update, then Continuous-Time Mode. Otherwise one `set` kills the run.
- Don't pass FMPy's `timeout`. The worker's own watchdog must exclude paused time, and only a process kill can stop a hung step.
- Keep the command inbox and the live-state files on a container-local filesystem, not on the Windows bind mount, or throttle polling. On the bind mount each poll costs about 8 ms, which sets the latency floor and slows fast models.

---

## (a) FMPy `simulate_fmu` matrix

**Verdict: PARTIAL.** 27 of 29 runs pass. The two failures come from the FMUs and FMPy's scope, not from the platform.

**Command:** `uv run python spikes/a_fmpy_matrix.py`. Output: `.cache/spikes/a/result.json`.

**What it does.** For every Reference FMU in v0.0.41, FMI 2.0 and FMI 3.0, and for every interface the modelDescription declares (CoSimulation and ModelExchange), the spike calls `simulate_fmu(path, fmi_type=...)` with the default experiment. Each run happens in a fresh child process with a 120 s hard timeout. An FMU that declares neither CS nor ME gets one run with FMPy's automatic choice. The child reports `sim_wall_s`, the time inside `simulate_fmu`. The parent reports `proc_s`, the whole child process including interpreter start and imports.

**Results** (x86_64-linux).

| FMI | FMU | Interface | OK | Rows | t_end | sim_wall_s | proc_s |
|---|---|---|---|---|---|---|---|
| 2.0 | BouncingBall | CS | yes | 301 | 3 | 0.0482 | 0.275 |
| 2.0 | BouncingBall | ME | yes | 773 | 3 | 0.0651 | 0.303 |
| 2.0 | Dahlquist | CS | yes | 101 | 10 | 0.0505 | 0.281 |
| 2.0 | Dahlquist | ME | yes | 1001 | 10 | 0.0596 | 0.3 |
| 2.0 | Feedthrough | CS | yes | 501 | 2 | 0.0729 | 0.283 |
| 2.0 | Feedthrough | ME | yes | 1001 | 2 | 0.0644 | 0.284 |
| 2.0 | Resource | CS | yes | 501 | 1 | 0.0525 | 0.274 |
| 2.0 | Resource | ME | yes | 1001 | 1 | 0.0653 | 0.319 |
| 2.0 | Stair | CS | yes | 46 | 9 | 0.0424 | 0.287 |
| 2.0 | Stair | ME | yes | 909 | 9 | 0.0606 | 0.298 |
| 2.0 | VanDerPol | CS | yes | 1001 | 20 | 0.0513 | 0.275 |
| 2.0 | VanDerPol | ME | yes | 1001 | 20 | 0.0656 | 0.282 |
| 3.0 | BouncingBall | CS | yes | 751 | 3 | 0.0604 | 0.311 |
| 3.0 | BouncingBall | ME | yes | 773 | 3 | 0.0792 | 0.299 |
| 3.0 | Clocks | auto (SE only) | **no** |  |  | 0.0622 | 0.288 |
| 3.0 | Dahlquist | CS | yes | 101 | 10 | 0.072 | 0.289 |
| 3.0 | Dahlquist | ME | yes | 1001 | 10 | 0.0756 | 0.299 |
| 3.0 | Feedthrough | CS | yes | 21 | 2 | 0.0558 | 0.283 |
| 3.0 | Feedthrough | ME | yes | 1001 | 2 | 0.0901 | 0.313 |
| 3.0 | Resource | CS | yes | 2 | 1 | 0.0512 | 0.295 |
| 3.0 | Resource | ME | yes | 1001 | 1 | 0.0792 | 0.313 |
| 3.0 | Roberts | CS | **no** |  |  |  |  |
| 3.0 | Roberts | ME | yes | 1005 | 1e+08 | 0.1009 | 0.41 |
| 3.0 | Stair | CS | yes | 46 | 9 | 0.0593 | 0.314 |
| 3.0 | Stair | ME | yes | 909 | 9 | 0.0632 | 0.319 |
| 3.0 | StateSpace | CS | yes | 11 | 10 | 0.0597 | 0.308 |
| 3.0 | StateSpace | ME | yes | 1001 | 10 | 0.077 | 0.33 |
| 3.0 | VanDerPol | CS | yes | 1001 | 20 | 0.0628 | 0.299 |
| 3.0 | VanDerPol | ME | yes | 1001 | 20 | 0.0672 | 0.306 |

Failures, verbatim:
- **Clocks (FMI 3.0)** declares only ScheduledExecution: `Exception: FMI type "ModelExchange" is not supported by the FMU`. `simulate_fmu` accepts only `ModelExchange` or `CoSimulation`.
- **Roberts (FMI 3.0) in CS**: `hard timeout after 120 s (child killed)`. The default experiment runs from 1e-5 to 1e8 s, and the CS interface has `fixedInternalStepSize="1e-3"`. FMPy doubles the output interval until there are at most 1000 steps, which gives about 1.3e5 s per `doStep`. That makes about 1.3e8 internal steps per call. The same FMU in ME (CVode) finishes in 0.10 s.
- **FMPy timeout probe**: rerunning Roberts CS with `simulate_fmu(timeout=5)` was killed at the 60 s hard limit: `hard timeout after 60 s (child killed)`. An exploratory run outside the script was still running after 400 s. FMPy checks `timeout` only between `doStep` calls.

Notes:
- The row counts depend on the interface and the FMI version. For FMI 3.0 CS, FMPy samples at `fixedInternalStepSize`, so Resource gives 2 rows, Feedthrough 21 and StateSpace 11. FMI 2.0 CS uses the default experiment's `stepSize`, or else an automatic interval: Resource gives 501 rows.
- Stair stops itself at t = 9 (`t_end` 9.0 in both interfaces).
- Starting a worker process costs about 0.2–0.3 s on top of the simulation (`proc_s` minus `sim_wall_s`).
- When `simulate_fmu` raises, the directory it extracted the FMU into stays in `/tmp`, because FMPy has no `try/finally`. Each failing call left one directory: 2 in (b) and 5 in (d). Killing a child process leaks its directory too, so (a) left 3: Clocks and the two killed Roberts runs.

**Design implication.**
- FMPy is a sound single-FMU engine for FMI 2.0 and 3.0 in both CS and ME on x86_64-linux.
- `inspect` should mark ScheduledExecution-only FMUs as not runnable.
- Before starting a run, estimate the internal step count (the horizon divided by `fixedInternalStepSize`) and warn or refuse when it's huge.
- Enforce wall-clock limits by killing the worker (ADR 0002), because FMPy's `timeout` can't interrupt native code.
- The worker should extract FMUs into its own run directory and remove it in a `finally` block, rather than trust FMPy's cleanup.

---

## (d) FMPy container FMU

**Verdict: PARTIAL.** FMI 3.0 passes once the workarounds below are applied. FMI 2.0 works only until a nested FMU terminates.

**Command:** `uv run python spikes/d_container_fmu.py`. Output: `.cache/spikes/d/` (`StairFeedthrough_fmi{30,20}_{naive,fixed}.fmu`, `result.json`).

**API.** In FMPy 0.3.32 the function is `fmpy.container_fmu.cli.create_container_fmu(config, unzipdir, filename)`. The package `fmpy.container_fmu` exports only `write_configuration`, and `hasattr(fmpy.container_fmu, "create_container_fmu")` is `False`. The config schema comes from the installed source (`container_fmu/config.py`, attrs classes):
- `Configuration(parallelDoStep, modelDescription, components, connections, variableMappings)`
- `Component(name, filename, modelDescription)`
- `Connection(startElement, startConnectors, endElement, endConnectors)`
- `variableMappings: dict[container ModelVariable, list[(Component, component ModelVariable)]]`

The caller builds the container's own `ModelDescription`, including a `CoSimulation` with `fixedInternalStepSize`. `create_container_fmu` first runs **`shutil.rmtree(unzipdir)`**: the directory must exist, and its contents are deleted. It then extracts the nested FMUs to `resources/<stem>/`, writes `resources/container.json` and copies a prebuilt Rust `container_fmu` binary for four platforms. The Linux binary exports both the `fmi2*` and the `fmi3*` functions.

**What it does.** The container wires Stair.counter to Feedthrough.Int32_input internally. It also has an input `u` (mapped to Feedthrough.Float64_continuous_input) and outputs `y` (Feedthrough.Float64_continuous_output) and `counter` (Feedthrough.Int32_output). The spike builds the container for FMI 3.0 and FMI 2.0, in a naive variant and a fixed variant. It validates each one with `fmpy.validation.validate_fmu` and `read_model_description(validate=True)`. It then simulates each one with a ramp on `u` and compares the counter transitions against a standalone Stair run.

**Results.**

| Variant | `validate_fmu` problems | Simulation |
|---|---|---|
| FMI 3.0 naive | `ModelStructure/InitialUnknowns does not contain the expected set of variables. Expected {counter, y} but was {}.` | `Exception: Failed to instantiate FMU`, container log `[ERROR] Failed to read configuration file …/resources/container.json. Variable with index 0 has no mappings.` |
| FMI 3.0 fixed | none | 91 rows to t = 9.0 (Stair terminates), 0.11 s. `max|y − u| = 0.0`. Counter transitions at t = 1, 2, … 8 match the standalone Stair exactly |
| FMI 2.0 naive | same InitialUnknowns problem | `Exception: Failed to instantiate model`, container log ``[ERROR] Failed to read configuration file …/resources/container.json. Failed to parse configuration file: unknown variant `Integer`, expected one of `Float32`, `Float64`, `Int8`, `UInt8`, `Int16`, `UInt16`, `Int32`, `UInt32`, `Int64`, `UInt64`, `Boolean`, `String`, `Binary`, `Clock` at line 60 column 37`` |
| FMI 2.0 fixed | none | To the default stop time: `FMICallException: fmi2DoStep failed with status 3 (error).` at t = 8.9, where the nested Stair returns `fmi2Discard` with `fmi2Terminated`. To 8.5 s: 86 rows, `max|y − u| = 0.0`, counter transitions identical to the standalone Stair |

The internal connection works: the container's `counter` output is Feedthrough's `Int32_output`, which is driven only by the internal Stair → Feedthrough connection.

**Workarounds in the fixed variant. Each one is a bug or an undocumented convention in FMPy 0.3.32.**
1. **Value reference 0 is time.** The binary maps container value reference *i* to `container.json` `variables[i-1]`. So `time` must be the first model variable (value reference 0) but must *not* appear in `variableMappings`. Mapping it with an empty list fails with "Variable with index 0 has no mappings". Leaving it out of the model variables fails FMI 3.0 validation with `Exactly one independent variable must be defined.`
2. **FMI 2.0 connection types.** `write_configuration` maps `Real`/`Integer` to `Float64`/`Int32` for container variables but copies connection types verbatim. The spike passes `attrs.evolve(variable, type="Int32")` as the connector.
3. **InitialUnknowns.** Set `modelDescription.initialUnknowns` to the outputs, or `validate_fmu` reports the problem shown above.

Bugs without a workaround:
- The FMI 2.0 container turns a nested `fmi2Discard`/terminate into `fmi2Error`. The FMI 3.0 container propagates terminate correctly.
- `shutil.make_archive(base_name=unzipdir/"Container", root_dir=unzipdir)` writes the zip *inside* the directory it is zipping, so every container FMU contains a stray `Container.zip` entry. The FMI 3.0 container is 2.3 MB.

**Design implication.** Spike (c) passed, so container FMUs are not needed as the live-systems fallback from ADR 0004. If we offer "export a system as one FMU" later:
- Generate FMI 3.0 containers only.
- Build the configuration with our own code, which encodes the value-reference-0 convention and the type mapping.
- Validate the result, and remove the stray `Container.zip`.
- Pin FMPy exactly, because this module is experimental and moved between releases.

The docs that name `fmpy.container_fmu.create_container_fmu` should use `fmpy.container_fmu.cli.create_container_fmu`.

---

## (e) pythonfmu: build a Python FMU and host it

**Verdict: PASS.**

**Command:** `uv run python spikes/e_pythonfmu.py`. The spike runs `uv run --no-sync pythonfmu build -f .cache/spikes/e/src/gain.py -d .cache/spikes/e` itself. Output: `.cache/spikes/e/` (`Gain.fmu`, `result.json`, `native/`).

**What it does.** It writes a tiny `Fmi2Slave` called `Gain`, with a tunable parameter `k` (start 2.0), an input `u` and an output `y = k*u` computed in `do_step`, and a default experiment of 0 to 1 s with a 0.1 s step. It builds the FMU (in 0.17 s) and runs it in four hosts, each in a child process:
1. FMPy `simulate_fmu`, with `u` = 2 then 4 from t = 0.5, and `k = 3` as a start value.
2. FMPy again, with the site-packages copy of `pythonfmu` hidden by a meta-path finder.
3. OMSimulator: an SSP with Reference Feedthrough (FMI 2.0) feeding `Float64_continuous_output` into `gain.u`. Feedthrough's input is 2 and `k` is 3; `k` is set to 6 at t = 0.5.
4. A minimal C host built with gcc. It calls `dlopen` on `binaries/linux64/Gain.so`, then `fmi2Instantiate`, `SetupExperiment`, `Enter/ExitInitializationMode` and `SetReal`, one `DoStep` and `GetReal`.

**FMU contents.** `binaries/linux64/Gain.so`, `binaries/win64/Gain.dll`, `modelDescription.xml` (FMI 2.0 CS, `needsExecutionTool="true"`), `resources/gain.py`, `resources/slavemodule.txt` and **`resources/pythonfmu/*.py`**, a bundled copy of pythonfmu. There are no aarch64 or macOS binaries, and no FMI 3.0.

**Results.**

| Host | LD_PRELOAD needed? | Result |
|---|---|---|
| FMPy (Python process) | No | 11 rows. y = 0 at t = 0, 6.0 for t = 0.1–0.5, and 12.0 from t = 0.6. The output lags `u` by one step because `y` is computed in `do_step`. `pythonfmu` was loaded from `/tmp/…/resources/pythonfmu/__init__.py`, the bundled copy |
| FMPy, site-packages `pythonfmu` hidden | No | Identical result, and again the bundled copy |
| OMSimulator (Python process, `oms_ma`) | No | `gain.y` is 6.0 at t = 0.5, and 12.0 at t = 1.0 after `setValue(k, 6)` mid-run, so tunable `k` works |
| Native C host, plain environment | **Yes** | `dlopen failed: …/binaries/linux64/Gain.so: undefined symbol: _Py_NoneStruct` |
| Native C host, `LD_PRELOAD=…/libpython3.12.so.1.0` | Yes, and it is sufficient | `fmi2Instantiate: instance created`, `fmi2DoStep status 0, y after one step with k=3, u=2: 6`. Setting `PYTHONHOME` and `PYTHONPATH` as well changed nothing |

**Why.** The uv Python reports `sysconfig.get_config_var("Py_ENABLE_SHARED") == 1`. Its shared library is `/home/dev/.local/share/uv/python/cpython-3.12.13-linux-x86_64-gnu/lib/libpython3.12.so.1.0`, with `LDLIBRARY` `libpython3.12.so`. But the `python3.12` executable doesn't link it (`ldd` lists no libpython). It embeds the interpreter and exports 1013 `Py*` symbols itself. `libpythonfmu-export.so` (`Gain.so`) has no `NEEDED` entry for libpython and 43 undefined `Py*` symbols. It works in any process that already exports CPython symbols, which means any Python host, FMPy and OMSimulator included. Any other process needs libpython preloaded. The export library puts the FMU's `resources/` on `sys.path` and imports the module named in `slavemodule.txt`, so **the runtime environment does not need `pythonfmu` installed**. It needs the slave script's own third-party imports and a CPython 3.12 runtime.

**Design implication.**
- FMUs generated through the `gen` extra run in our FMPy and OMSimulator workers without `LD_PRELOAD` or environment tweaks, and they are self-contained apart from the Python runtime.
- For users who take a generated FMU to an external, non-Python tool, document two requirements: `LD_PRELOAD` of a matching `libpython3.12.so`, and the slave's own dependencies.
- The generated FMUs are FMI 2.0 CS only, declare `needsExecutionTool="true"`, and ship linux64 and win64 binaries only.

---

## Corrections for other docs

**Resolved on 2026-10-09:** all of these were applied to ADR 0002, ADR 0004 (now Accepted), architecture.md, protocol.md, the codebase map and the glossary. The list stays as a record.

The spikes contradict some statements elsewhere. Those files aren't owned by this task, so they are listed here for their owners:

- [ADR 0004](adr/0004-omsimulator-as-multi-fmu-orchestrator.md), the open question: the installed 3.0.0.post198 API *does* step (`InstantiatedModel.stepUntil` and `doStep`, `Capi.stepUntil` and `doStep`). There's no legacy `OMSimulator` class to fall back to. Spike (c) passed, so the ADR can move to Accepted.
- ADR 0004 and the FMPy references: the import path is `fmpy.container_fmu.cli.create_container_fmu`, not `fmpy.container_fmu.create_container_fmu`.
- The ADR 0004 consequence "pythonfmu FMUs need Python at runtime inside OMSimulator (`LD_PRELOAD`)" is wrong for our setup. OMSimulator runs inside Python, and the FMU works there without `LD_PRELOAD`. Only non-Python hosts need it.
- [architecture.md](architecture.md), on mid-run changes: "a command takes effect at the next communication step" holds for CS. For ME, tunable parameters must be set inside Event Mode, or the run aborts.
- The run store and command inbox ([protocol.md](protocol.md), ADR 0003): if `.agentic-fmi/` sits on the Windows bind mount, file-based IPC costs about 8 ms per poll and 10–19 ms per command. A container-local path costs 0.3 ms and 2–4 ms.

---

## (f) OpenModelica omc: build an FMU from Modelica

**Verdict: PASS, with caveats that `fmu_build` has to handle** (listed under "Design implication").

**Setup.** Task 2.5a added OpenModelica to the dev image (`omc` and `omlibrary` from `build.openmodelica.org/apt noble stable`, `--no-install-recommends`; see [development.md](development.md)). The spike drives `omc` directly in scratch directories under `/lab/omc-spike/` (no script in `spikes/`, nothing generated in the repo), as user `dev` inside the container.

| Item | Value |
|---|---|
| `omc --version` | `OpenModelica 1.27.1~2-g6db4671` (apt `omc` and `omlibrary` 1.27.1~2-g6db4671-1) |
| Modelica Standard Library | `loadModel(Modelica)` loads **4.1.0** (`getVersion(Modelica)`). The `omlibrary` cache also holds 3.2.3 and 4.0.0 |
| Image size | 1.74 GB before, 2.34 GB after (content size 520 MB → 692 MB); about 135 MB downloaded |
| Host | 16 cores visible, Ubuntu 24.04, FMPy 0.3.32 in the project venv |

**The model** (`MSD.mo`, a mass-spring-damper with a force input; the port names are the ones Phase 3 will wire: plant input `F`, plant outputs `x` and `v`; the Python controller will take `y` and give `u`):

```modelica
model MSD "Mass-spring-damper with a force input"
  parameter Real m(unit="kg", min=0.001) = 1.0 "mass";
  parameter Real k(unit="N/m") = 10.0 "spring stiffness";
  parameter Real d(unit="N.s/m") = 0.5 "damping";
  input Real F(unit="N") = 0 "external force";
  output Real x(start=0, fixed=true, unit="m") "position";
  output Real v(start=0, fixed=true, unit="m/s") "velocity";
equation
  der(x) = v;
  m*der(v) = F - k*x - d*v;
  annotation(experiment(StartTime=0, StopTime=10, Tolerance=1e-6));
end MSD;
```

**The script** (`build.mos`, run with `omc build.mos`):

```
loadModel(Modelica); getErrorString();
loadFile("MSD.mo"); getErrorString();
buildModelFMU(MSD, version="2.0", fmuType="me_cs"); getErrorString();
```

**Timings** (wall clock, the 60 s budget being the point where Claude Desktop cancels a stdio MCP call):

| Case | Time |
|---|---|
| First `omc` in a fresh `HOME` (installs the library cache, downloads the package index) | 13.1 s |
| Warm, MSD | 6.3 s |
| Warm, MSD with `--fmiFlags=s:cvode` | 6.4 s |
| Warm, an MSL-based model (`Modelica.Mechanics.Translational` components) | 6.4 s |
| Offline (`--network none`), fresh `HOME`, the MSL-based model | 10.1 s |
| Pinned to one core (`taskset -c 0`), MSD and the MSL-based model | 20.6 s and 20.9 s |

User CPU is about 33 s on 16 cores and 14 s on one: the build runs a parallel `make`. A trivial model is about three times inside the budget even on one core. Larger or MSL-heavy models weren't measured, so treat a build as a job that can exceed 60 s.

**Result layout.** On success `omc` writes `<cwd>/MSD.fmu` (1.06 MB) and these side files next to it: `MSD.log`, `MSD_FMU.log`, `MSD_FMU.makefile`, `MSD_FMU.libs`, `MSD_info.json`, `MSD_external_functions.json` and `index.html`. The temporary `.fmutmp` directory is removed. `fileNamePrefix="plant"` renames all of them, and `cd("out")` in the script moves them to another directory.

The FMU holds:
- `binaries/linux64/MSD.so` (640 KB) and nothing for other platforms, because the default is `platforms={"static"}`.
- `modelDescription.xml`: FMI 2.0 with both `<ModelExchange>` and `<CoSimulation>` on `modelIdentifier="MSD"`, `canHandleVariableCommunicationStepSize="true"`, `canInterpolateInputs="true"`, `needsExecutionTool="false"`, and a `DefaultExperiment` taken from the `experiment` annotation (0 to 10 s, tolerance 1e-6, step size 0.02).
- `resources/MSD_JacA.bin`.
- `sources/`, the 273 C files of the generated model and the OpenModelica runtime (4.4 MB uncompressed; it is why the zip is 1 MB).

Variables: `F` (input), `x` and `v` (outputs), `m`, `k`, `d` (parameters, variability `fixed`, with their start values, units and `min`), and four extra `local` variables, `_D_outputAlias_x`, `_D_outputAlias_v` and their derivatives. These come from states that are declared `output`; they are harmless, but they show up in `fmu_variables`. The MSL-based model gave `F`, `x`, `v` and parameters with dotted names (`mass.m`, `sd.c`, `sd.d`).

**The default FMU is self-contained.** `ldd binaries/linux64/MSD.so` lists only `libm.so.6` and `libc.so.6`, with `RUNPATH $ORIGIN`. It ran identically from `cwd=/` with `LD_LIBRARY_PATH` unset.

**FMPy results** (project venv, `uv run --no-sync python`; `F = 1`, `output_interval = 0.01`, 0 to 10 s). The analytic solution is the underdamped step response, `x(10) = 0.091769`.

| Run | Wall time | `x(10)` | Max `abs(x - analytic)` |
|---|---|---|---|
| Model Exchange (FMPy's CVode) | 0.045 s | 0.091779 | 1.5e-5 |
| Co-Simulation, default | 0.024 s | 0.086542 | **8.2e-3** |
| Co-Simulation, built with `--fmiFlags=s:cvode` | 0.028 s | 0.091769 | 2.7e-7 |

The default CS solver is **forward Euler**: the error is first-order in the communication step (1.11 at 0.1 s, 8.2e-3 at 0.01 s, 7.5e-4 at 0.001 s). A force step supplied as an FMPy input signal (F = 0 until t = 1 s, then 1) behaves the same way in ME and CS: `x(0.99) = 0`, then it moves. At t = 3 s `x` is 0.039266 in ME, 0.039258 in CS with CVode and 0.032876 in CS with Euler.

`setCommandLineOptions("--fmiFlags=s:cvode")` before `buildModelFMU` writes `resources/MSD_flags.json` (`{"s": "cvode"}`) and also bundles `libsundials_cvode.so.5` and `libsundials_nvecserial.so.5` in `binaries/linux64` (FMU 1.32 MB; the `.so` finds them through `$ORIGIN`, so it is still self-contained). `--fmuRuntimeDepends=modelica` changed nothing for this model; it presumably matters for models with external C functions, which weren't tried.

**FMI 3.0 isn't available.** `buildModelFMU(MSD, version="3.0", ...)` fails with `Error: Unknown FMU version 3.0. Only version 1.0 & 2.0 are supported.` Generated FMUs are FMI 2.0 only.

**Messages and errors.** In every case below `omc` exited **0** with an empty stderr. Every statement in the `.mos` prints its value on stdout (`true`, `""`, a quoted string), `buildModelFMU` prints the quoted path on success and `""` on failure, and the errors are only visible through `getErrorString()`. That function drains the message buffer, so each call returns what happened since the previous one.

| Case | `buildModelFMU` result | What `getErrorString()` returned |
|---|---|---|
| Success | `"/lab/omc-spike/ok/MSD.fmu"` | `""` |
| Syntax error (missing `;`), at `loadFile` | `""` (`loadFile` printed `false`) | `[/lab/omc-spike/err/Syn.mo:5:3-5:3:writable] Error: Missing token: SEMICOLON`; after the build also `Error: Failed to load package Syn (default) using MODELICAPATH /home/dev/.openmodelica/libraries/.` and `Error: Class Syn not found in scope <top>.` |
| Undeclared variable (`der(x) = -y`) | `""` | `[/lab/omc-spike/err/Sem.mo:4:3-4:14:writable] Error: Variable y not found in scope Sem.` |
| Type mismatch (`parameter Real p = "text"`) | `""` | `[…/Typ.mo:2:3-2:28:writable] Error: Type mismatch in binding p = "text", expected subtype of Real, got type String.` |
| Under-determined (`Real z` with no equation) | `""` | `Error: Too few equations, under-determined system. The model has 1 equation(s) and 2 variable(s).` then `[…/Under.mo:3:3-3:9:writable] Warning: Variable z does not have any remaining equation to be solved in.` with an indented continuation line |
| Missing file | `loadFile` printed `false` | `Error: Failed to load file DoesNotExist.mo: file does not exist.` |
| Unknown class | `""` | `Error: Failed to load package NoSuchModel (default) using MODELICAPATH …` and `Error: Class NoSuchModel not found in scope <top>.` |

The format is `[<file>:<line1>:<col1>-<line2>:<col2>:<writable|readonly>] <Error|Warning|Notification>: <text>`, where the bracketed location is missing for some messages (`Error: …` alone), and a message can continue on indented lines. `getMessagesStringInternal()` returns the same messages as verbose Modelica records (`filename`, `lineStart`, `columnStart`, `message`, `kind`, `level`), which is more work to parse for no extra information we use.

**Offline and `HOME`.** With `docker run --network none` (only `lo`, DNS fails), `loadModel(Modelica)` returns `true`, loads MSL 4.1.0 from the `omlibrary` cache and the MSL-based model builds in 10.1 s. `omc` copies the cache into `~/.openmodelica/libraries/` on first use (154 MB, about 7 s once per `HOME`; the directory isn't in a volume, so a recreated container repeats it). Two things to know:
- Offline, the `getErrorString()` after `loadModel(Modelica)` contains `Error: Curl error for URL https://libraries.openmodelica.org/index/v1/index.json: Couldn't resolve host name` and `Error: Failed to download package index …`, **although the load succeeded and the FMU builds**. Online, the same call returns `Notification` lines (including "Downloaded package index") instead.
- With an unwritable `HOME` (`HOME=/nonexistent`), the library install fails (`Failed to open file for writing: /nonexistent/.openmodelica/libraries/index.json.tmp1`), `loadModel(Modelica)` doesn't load MSL, and a model using MSL fails with `Error: Class Modelica.Blocks.Interfaces.RealInput not found in scope MSLMSD.` So `HOME` must be writable. Concurrent first-time installs into one `HOME` weren't tested.

**Design implication for `fmu_build`.**
1. **Decide success from the returned path, not from errors or the exit code.** Success means `buildModelFMU` returned a non-empty path and that file exists. The exit code is 0 on failure, stderr is empty, and `getErrorString()` can hold harmless `Error:` lines on a successful build (offline library index).
2. **Build in a fresh scratch directory and copy the FMU out.** `omc` writes seven side files into the working directory, and a stale `.fmu` from an earlier build would otherwise look like a result. Use absolute paths for `loadFile`, and don't write into the workspace except the final artifact.
3. **Run it as a subprocess with a timeout, and don't assume it fits in 60 s.** Six seconds for a toy model is comfortable, but MSL-heavy models were not measured. If the architecture wants builds to survive a cancelled call, they need the same job treatment as runs.
4. **Give it a writable, persistent `HOME`.** The 154 MB library install costs about 7 s per fresh `HOME`. A persistent directory (or a volume for `~/.openmodelica`) pays it once.
5. **Always set `--fmiFlags=s:cvode`.** Otherwise CS FMUs integrate with forward Euler, which is inaccurate at coarse communication steps (and unstable at 0.1 s here). Pass `version="2.0", fmuType="me_cs"` explicitly. This costs about 260 KB for the bundled SUNDIALS libraries.
6. **Parse `getErrorString()` with one regex** (the format above), keep `Error` lines first and `Warning` lines after them, drop `Notification`, cap the count, and show the `file:line:column` plus the text. Replace the scratch path by the file name. When `loadFile` returned `false`, report only its message: the "Failed to load package … MODELICAPATH" and "Class … not found" lines that follow are noise (and the first leaks a home path).
7. **Set expectations in the tool description.** The FMU is FMI 2.0 only (no 3.0 export in omc 1.27.1), linux64 only (`platforms={"static"}`), and carries its C sources. A model whose states are declared `output` shows four extra `local` variables.

---

## (g) Closed loop in OMSimulator

**Verdict: PASS, with caveats.** This spike prepares Phase 3. A pythonfmu PI controller and an omc mass-spring-damper plant close the loop inside OMSimulator, hosted in a Python process with no `LD_PRELOAD`. OMSimulator's result matches an independent FMPy co-simulation master to 5e-12 once that master uses OMSimulator's scheme and time grid. Stepping, mid-run `Kp` changes, the SSP round trip and SSV parameter files all work. Three caveats need handling in Phase 3 code: the `stepUntil` overshoot, the slow FMU extraction on the bind mount, and the SSV export bug.

| Item | Verdict |
|---|---|
| 1. Build both skill examples | PASS |
| 2. Closed loop in OMSimulator, in a Python process | PASS |
| 3. Independent reference | PASS: OMSimulator is a Jacobi master on an accumulated time grid, max abs(Δx) 5e-12 |
| 4. Stepping and a mid-run `Kp` change | PASS, but `stepUntil(t)` can overshoot `t` by one communication step |
| 5. SSP and SSV | PARTIAL: the round trip gives a byte-identical result, but exports aren't byte-reproducible, and a component-level SSV next to an inline binding is lost on re-export |
| 6. Binary provenance | PARTIAL: the venv binary is pinned down, but the apt option can't be inspected offline |
| 7. FMI 3.0 Binary signals | PASS: confirms (c) |

**Command:** `uv run --no-sync python spikes/g_closed_loop.py` (about 100 s). Output goes to `.cache/spikes/g/`:
- `result.json`, `fmus/`, `results/*.csv` and `*.mat`, `ssp/`, `ssv/`;
- `logs/`, with each child's stdout and stderr;
- `children/`, with each child's config and report.

The parent process never imports OMSimulator. Every OMSimulator and FMPy run happens in a fresh child process (`python -I -B spikes/g_closed_loop.py --child <probe> <cfg.json>`) with `LD_PRELOAD` removed from the environment and one model per process, the way a worker would run it. Versions are as in the Environment table, plus pythonfmu 0.7.0 and `OpenModelica 1.27.1~2-g6db4671`. The venv has no scipy.

### 1. Builds

- **PI.** The spike copies `skills/fmu-authoring/examples/pi_controller.py` to `.cache/spikes/g/src/` and runs `uv run --no-sync python -I -B -m pythonfmu build -f <copy> -d .cache/spikes/g/fmus`, which takes 0.2 s. The result, `PIController.fmu`, is 660 KB: FMI 2.0 CS only, `needsExecutionTool="true"`, with `binaries/linux64` and `binaries/win64`. `PIController.so` has `NEEDED` entries for `libstdc++.so.6`, `libgcc_s.so.1` and `libc.so.6`, and none for libpython.
- **MSD.** The spike runs `omc build.mos` in a fresh `.cache/spikes/g/omc/`, with this script:

  ```
  setCommandLineOptions("--fmiFlags=s:cvode"); getErrorString();
  loadFile("<absolute path>/MSD.mo"); getErrorString();
  buildModelFMU(MSD, version="2.0", fmuType="me_cs"); getErrorString();
  ```

  It took 16–18 s. The container was shared with another session at the time; (f) measured 6.4 s warm. `MSD.fmu` is 1.32 MB, has both ME and CS, and bundles `libsundials_cvode.so.5` and `libsundials_nvecserial.so.5`.
- **Neither build is reproducible.** Each one embeds a new GUID (pythonfmu a time-based `uuid1`, omc a random one) and a `generationDateAndTime`, so both FMUs got a new sha256 on each of the three runs. The inputs stayed unchanged: the sha256 of both example files and of the FMI 3.0 Feedthrough was the same before and after.

### 2. The loop in OMSimulator

The system, built with the oms3 API (`from OMSimulator import SSP, CRef`):

```python
model = SSP(temp_dir=tmp)
model.addResource(pi_fmu, new_name="resources/PIController.fmu")
model.addResource(msd_fmu, new_name="resources/MSD.fmu")
model.addComponent(CRef("default", "PI"), "resources/PIController.fmu")
model.addComponent(CRef("default", "MSD"), "resources/MSD.fmu")
model.addConnection(CRef("default", "PI", "u"), CRef("default", "MSD", "F"))
model.addConnection(CRef("default", "MSD", "x"), CRef("default", "PI", "y"))
model.setValue(CRef("default", "PI", "r"), 1.0)
model.newSolver({"name": "ma", "method": "oms_ma", "fixedStepSize": h})
model.setSolver(CRef("default", "PI"), "ma")
model.setSolver(CRef("default", "MSD"), "ma")
model.activeVariant.stopTime = 10.0
inst = model.instantiate()
inst.setResultFile(".../result.csv")
inst.initialize()
inst.simulate()
```

`inst.dumpApiCalls()` shows `oms_addSystem("model.root", "oms_system_wc")`, `oms_setSolver("model.root", "oms_ma")` and `oms_setFixedStepSize("model.root", 0.01)`. MSD declares both interfaces (the SSD says `implementation="any"`), and in the WC system OMSimulator uses its CS interface. Two things show it: the FMU's own CVode logs its settings to stdout (`LOG_SOLVER | info | CVODE use equidistant time grid YES`, `CVODE Using relative error tolerance 1.000000e-06`), and the result matches the FMPy CS master.

| h | `instantiate()` | of which `addSubModel` PI / MSD | `initialize()` | `simulate()` | `delete()` | Child process |
|---|---|---|---|---|---|---|
| 0.01 | 2.65 s | 0.26 / 2.23 s | 2 ms | 71 ms | 0.47 s | 3.9 s |
| 0.1 | 2.57 s | 0.28 / 2.14 s | 2 ms | 9.5 ms | 0.44 s | 3.4 s |
| 0.1, temp dirs on container-local `/tmp` | 0.13 s | 0.019 / 0.079 s | 2 ms | 9.3 ms | 0.016 s | 0.37 s |

The time goes into extracting the FMUs onto the Windows bind mount: MSD carries 268 source files (3.9 MB) under `sources/`. On a container-local temp directory, `instantiate()` is 20 times faster.

- **pythonfmu inside OMSimulator works, with no `LD_PRELOAD`.** All 16 PI + MSD children ran with `LD_PRELOAD` unset, exited 0 and wrote nothing to stderr. The FMU takes its CPython symbols from the host interpreter. It loaded its **bundled** `pythonfmu` (`…/temp/0001_PI/resources/pythonfmu/__init__.py`) and `pi_controller.py` into the worker's own interpreter, and it added `…/0001_PI/resources` to the worker's `sys.path`.
- **The GIL is not a problem.** `simulate()` ran on a second thread while the main thread ticked every millisecond: 62 ticks during the 71 ms run, with a largest gap of 1.25 ms. ctypes releases the GIL around the `oms_*` call, and the FMU, whose binary imports `PyGILState_Ensure` and `PyGILState_Release`, takes it for each `do_step`. The FMU was instantiated on the main thread and stepped from the other one. OMSimulator started no threads of its own: the peak was 2 OS threads, the main thread and ours.
- **Result file.** The file is CSV if its name ends in `.csv` and MAT otherwise (the SSD default is `default_res.mat`).
  - Columns are `time` and `default.default.<component>.<variable>`, for example `default.default.MSD.x` and `default.default.PI.u`. omc's `_D_outputAlias_*` and `der(...)` locals are included.
  - The CSV has 12 significant digits and no parameters. `addSignalsToResults(".*Kp")` doesn't add `Kp`.
  - The MAT file is OpenModelica's `Atrajectory` / `1.1` / `binTrans` v4 format: `name`, `description`, `dataInfo`, `data_1` with the parameters (`Kp`, `Ki`, `r`, `m`, `k`, `d`) and `data_2` with signals × rows. The spike reads it with about 20 lines of numpy and no scipy. It agrees with the CSV to 5e-12.
- **Two rows per communication point.** Every time stamp appears twice: first after `doStep`, with new outputs and old inputs, then after the input exchange, with updated inputs. So 1001 communication points give 2003 rows: 999 pairs, one row at t = 0 and four at the stop time. A reader must deduplicate, keeping the first row per time.
- **A sliver step at the stop time.** OMSimulator advances time with `t += h`. After 1000 steps of 0.01 it stands at 9.999999999999831, so it takes one more step of 1.7e-13 s to reach 10.0. With h = 0.1 it stands at 9.99999999999998 and the last step is 2e-14 s. The MAT time column shows this (`'9.99999999999998', '9.99999999999998', '10.0', '10.0'`), while the CSV prints all four as `10`. The PI computes `u` in `do_step`, so the sliver step changes its output in the last two rows: u goes from 10.00212 to 10.00316 at h = 0.01, and from 1.068 to −2.175 at h = 0.1.
- **The host tolerance doesn't reach the FMU.** Setting `relativeTolerance` 1e-6 on the solver (which emits `oms_setTolerance("model.root", 1e-06)`) changed nothing: max abs(Δx) was 0. The omc CS FMU uses its own tolerance, 1e-6 from its `experiment` annotation. FMPy's `setupExperiment(tolerance=1e-4)` didn't change it either.

### 3. Comparison with independent references

Three references:
- **An FMPy master.** It runs in a child process, instantiates both FMUs as `FMU2Slave` and steps them at the same h. It can use Jacobi coupling (both FMUs step with the inputs from the previous exchange) or Gauss-Seidel coupling (PI first, then MSD with the new u), on either a `k·h` grid or OMSimulator's accumulated grid.
- **The exact sampled-data loop.** This is the PI as coded plus a zero-order-hold plant, computed with the matrix exponential in numpy.
- **The continuous-time loop**, computed exactly. Its poles are −0.606 ± 4.32j and −0.788.

The table gives max abs(Δx) between OMSimulator and each reference, and each run's step metrics (overshoot, 2 % settling time, x(10)):

| Run | h = 0.01: max abs(Δx) | h = 0.01: overshoot / settling / x(10) | h = 0.1: max abs(Δx) | h = 0.1: overshoot / settling / x(10) |
|---|---|---|---|---|
| OMSimulator | — | 10.43 % / 6.07 s / 0.99839 | — | 148.7 % / never / 2.487 (unstable) |
| FMPy Jacobi, accumulated grid | **5.0e-12** (abs(Δu) 5.0e-11) | identical | **5.0e-12** | identical |
| FMPy Jacobi, `k·h` grid | 2.1e-5 | 10.43 % / 6.07 s / 0.99837 | 7.3e-4 | 148.7 % / never / 2.487 |
| Exact sampled-data, Jacobi | 2.2e-4 | 10.42 % / 6.07 s / 0.99846 | 4.0e-3 | 148.4 % / never / 2.484 |
| FMPy Gauss-Seidel, `k·h` grid | 2.3e-2 | 8.55 % / 5.97 s / 0.99885 | 1.50 | 18.2 % / 8.9 s / 0.9866 |
| Exact sampled-data, Gauss-Seidel | 2.3e-2 | 8.55 % / 5.97 s / 0.99892 | 1.50 | 18.2 % / 8.9 s / 0.9866 |
| Continuous loop | 3.0e-2 | 7.56 % / 5.89 s / 0.99921 | 1.49 | 7.56 % / 5.9 s / 0.99921 |

What explains the differences:
- **`oms_ma` is a Jacobi master.** Both FMUs step from the same communication point with the inputs from the previous exchange, and then all outputs are copied to inputs. In the post-step rows of the result, `MSD.F` equals the previous row's `PI.u` exactly. The PI computes u from the y it had at the start of its step, so the plant sees the controller react one to two steps late.
- **On OMSimulator's accumulated time grid, our FMPy Jacobi master reproduces OMSimulator** to the CSV's 12 digits, including the sliver step. On a `k·h` grid, the same master agrees to 1e-15 for the first 20 steps and then drifts, by up to 2.1e-5 at h = 0.01. The time stamps differ in the last bits, so the FMU's internal CVode takes different steps, and the difference stays at its tolerance level.
- **The gap to the exact sampled-data loop, 2.2e-4, is the omc FMU's CVode error.** It accumulates over 1000 restarts with a step input, and both hosts share it.
- **The coupling lag separates co-simulation from the continuous loop.** At h = 0.01 it adds 2.9 points of overshoot (10.4 % against 7.6 %). At **h = 0.1 the Jacobi loop is unstable**, with an oscillation that grows to x(10) = 2.49, while Gauss-Seidel stays stable at 18 % overshoot. `oms_ma` offers no Gauss-Seidel ordering.

### 4. Stepping and a mid-run `Kp` change

The probe runs the same system at h = 0.01. It calls `initialize()` and one `doStep()`, which advances to 0.01, one communication step. It then calls `stepUntil(k·0.1)` for k = 1…100, calls `setValue(CRef("default", "PI", "Kp"), 40.0)` once `getTime()` reaches 1.0, and reads `getValue` of `PI.Kp`, `PI.u` and `MSD.x` between calls.

- `setValue` returned `Status.ok`. `getValue(PI.Kp)` read 10.0 at t = 1.0 and 40.0 from t = 1.1. `u` went from 5.08 at t = 1.0 to 9.31 at t = 1.1 and 13.10 at t = 1.2.
- Up to t = 1.0 the stepped run equals the batch run exactly: max abs(Δx) = 0 over 101 points. After the switch it matches an FMPy Jacobi master that sets `Kp` = 40 at t = 1.0 to 5.0e-12. It differs from the batch run by up to 0.34, and settles at 6.83 s instead of 6.07 s.
- Each `stepUntil` over a 0.1 s chunk (10 steps) took 0.69 ms median and 0.98 ms at most.
- **`stepUntil(t)` overshoots by one step** when the accumulated time stops a hair below `t`. 80 of the 100 targets were missed: `stepUntil(0.1)` ended with `getTime()` = `0.10999999999999999`, and `stepUntil(2.1)` with `2.109999999999999`. It doesn't clip its last step to the target, although it does clip to the stop time, which is what produces the sliver step. The data stays on the same grid, so the results are unaffected. But "pause at t" and "change at t" land one step late.

### 5. SSP and SSV

**Export.** `model.export(path)`, called before instantiating, writes a 1.48 MB SSP:

| Entry | Bytes | Compressed |
|---|---|---|
| `SystemStructure.ssd` | 5109 | 1089 |
| `resources/MSD.fmu` | 1318753 | 1261840 |
| `resources/PIController.fmu` | 659946 | 213135 |

The FMUs are embedded byte for byte (same CRC). The SSD records:
- the solver as an OpenModelica annotation: `<oms:SimulationInformation name="ma" method="oms_ma" fixedStepSize="0.01"/>` on the system, and `name="ma"` on each component;
- the stop time in `DefaultExperiment`;
- `implementation="CoSimulation"` for PI and `implementation="any"` for MSD.

**Re-import and run.** `SSP(path).instantiate()` gives the same 2003 rows, and the CSV is byte-identical to the original run's.

**Byte reproducibility: no.**
- Two exports of the same object 0.1 s apart differ only in `SystemStructure.ssd`.
- An export from a fresh process also differs in every zip entry's timestamp.
- With `generationDateAndTime` blanked out, the SSD text is identical, and so is the entry order.

The installed source explains it. `SSD.export` always writes `generationDateAndTime=datetime.now().isoformat()`, and its `dirty` property is always `True`. `SSP.export` zips the temp directory in `rglob` order with `ZipFile.write`, which stores each file's mtime.

**SSV.** oms3 has an `SSV` class: `SSV().setValue("PI.Kp", 40.0)` and `.export(path)` write a file with `version="2.0"`. To apply a file, list it as a parameter binding of a system or component with `model.addResource(path, new_name="resources/x.ssv")` and `model.addSSVReference(CRef(...), "resources/x.ssv")`. At instantiation each parameter becomes an `oms_setReal("model.root.PI.Kp", …)` call. A hand-written SSV 1.0 file works the same way. Both kinds of file validate against the SSP 2.0 XSD that OMSimulator bundles, checked with lxml.

| Case | `Kp` after `initialize()` | max abs(Δx) against the exact sampled-data Jacobi loop with that `Kp` | After export and re-import |
|---|---|---|---|
| oms3 `SSV` writer, bound at the root, `PI.Kp` = 40 | 40.0 | 8.2e-5 (overshoot 46.5 %) | 40.0, Δx = 0 |
| Hand-written SSV 1.0, bound at the root, `PI.Kp` = 2.5 | 2.5 | 6.1e-4 | 2.5, Δx = 0 |
| Hand-written, bound at component PI, `Kp` = 2.5; PI also has the inline `r` binding | 2.5 | 6.1e-4 | **10.0: the SSV is lost**, Δx = 0.46 |
| Hand-written, bound at component PI, no inline binding | 2.5 | 6.1e-4 | 2.5, Δx = 0 |

The lost case is an OMSimulator export bug. A component with an inline binding and an SSV reference gets two `<ssd:ParameterBindings>` elements. The import ignores the second one, and the only sign of trouble is a `UserWarning`:

```
Failed to validate SystemStructure.ssd against schemafile SystemStructureDescription.xsd
ERROR (line 36, column 0): Element '{http://ssp-standard.org/SSP1/SystemStructureDescription}ParameterBindings': This element is not expected. Expected is one of ( {http://ssp-standard.org/SSP1/SystemStructureCommon}MetaData, {http://ssp-standard.org/SSP1/SystemStructureCommon}Signature, ...
```

Writing SSV files ourselves is simple: the spike's template is ten lines of XML, and OMSimulator applies it.

### 6. Binary provenance

| Item | Value |
|---|---|
| Library | `/home/dev/.venvs/agentic-fmi/lib/python3.12/site-packages/OMSimulator/libOMSimulator.so`, 9849096 bytes |
| sha256 | `818b054fdacbef14d4fac379cab77d17b57d26f6bbd8e095811047cb4b02b0fb`. It equals the dist-info `RECORD` entry and the two copies in the uv cache (`archive-v0/YmeY67JrF1YeaR7_/` and the sdist build directory) |
| Version string, in the binary and in `OMSimulator.version` | `OMSimulator v3.0.0.post198-gf14b85b-linux` |
| Wheel | built locally, tagged `py3-none-any`, sha256 `a2e0e9118ed5996bead3753531c36d53722aeada0d148c3445778dc7d573a427`, cached at `~/.cache/uv/sdists-v9/pypi/omsimulator/3.0.0.post198/…/omsimulator-3.0.0.post198-py3-none-any.whl` |
| `ldd` | `libstdc++.so.6`, `libm.so.6`, `libgcc_s.so.1`, `libc.so.6` only |

**What the install downloads.** The PyPI sdist ships an empty `OMSimulator/` directory and a `build_py` hook, `build_helpers.OMSInstaller`. The hook downloads `https://build.openmodelica.org/omsimulator/nightly/linux-amd64/OMSimulator-linux-amd64-v3.0.0.post198-gf14b85b.tar.gz` with `requests.get(url, verify=False)` and checks no hash. It copies these from the tarball:
- `lib/x86_64-linux-gnu/libOMSimulator.so`;
- `lib/x86_64-linux-gnu/OMSimulator`, which is **the whole Python package** (25 `.py` files);
- `OMSimulatorGui` (32 files);
- `share/OMSimulator/schema` (25 XSDs).

So the lock file vouches for neither the library nor the Python code we import. The hook deletes the tarball after the build, and it runs `pip install requests` if `requests` is missing.

**`import OMSimulator` loads the native library.** `__init__.py` ends with `version = Capi.getVersion()`, and `SSD.export` calls `capi.capi().getVersion()` again. Every probe that checked `/proc/self/maps` found `libOMSimulator.so` mapped right after the import. So the MCP server can't use the oms3 API at all, not even to author an SSP.

**The apt option can't be assessed offline.**
- The image's apt lists are empty (`rm -rf /var/lib/apt/lists/*` in the Dockerfile), so `apt-cache policy omsimulator libomsimulator` prints nothing and `apt-cache show omsimulator` prints `E: No packages found`. An answer needs `apt-get update`, which downloads, so the spike didn't run it.
- The configured source is `deb [arch=amd64 signed-by=/usr/share/keyrings/openmodelica-keyring.gpg] https://build.openmodelica.org/apt noble stable`.
- The installed OpenModelica packages are `omc`, `omc-common`, `libomc`, `libomcsimulation` and `omlibrary`, all 1.27.1~2-g6db4671-1. No other `libOMSimulator*` exists on the image.

What the loader in `capi.py` implies:
- **Search order.** It looks for `OMSimulator/libOMSimulator.so` first, then `OMSimulator/../libOMSimulator.so`, then walks up to six parent directories looking for `bin/libOMSimulator.so` or the library itself. No environment variable overrides it.
- **Symbols.** At import it eagerly binds 49 `oms_*` symbols. All 49 are present in this build, which exports 104.
- **A Debian-style install.** The tarball's layout, `lib/x86_64-linux-gnu/libOMSimulator.so` next to `lib/x86_64-linux-gnu/OMSimulator/`, matches the loader's second rule. So an install with that layout would load its own library without tricks.
- **This PyPI package with an apt library.** That would need a symlink in site-packages and an apt build that exports all 49 symbols with the same signatures: feasible, but fragile.
- **What would settle it.** The apt route helps only if the apt package also ships the matching Python package. Checking that needs `apt-get update` and `apt-get download`, without installing.

### 7. FMI 3.0 note

The spike re-checked (c) with one FMI 3.0 Feedthrough in a system. As is, the result file has 0 rows and stderr gets 2006 `error:` lines (`error:   [getReal] UnSupported Numeric Type:` and `error:   [updateSignals] failed to fetch variable Binary_input`). With `inst.removeSignalsFromResults(".*Binary.*")` before `initialize()`, it has 1001 rows and no errors. This is the same as in (c).

### Design implications for Phase 3

1. **Workers can host pythonfmu FMUs in OMSimulator as they are.** They need no `LD_PRELOAD` and no GIL handling, and stepping on a separate thread is fine. But the FMU's Python code runs inside the worker's interpreter and changes it: it adds its `resources/` to `sys.path`, and it imports its bundled `pythonfmu` and its slave module into `sys.modules`. Two pythonfmu FMUs with the same module names, or with different bundled `pythonfmu` versions, would share whichever was imported first. That wasn't tested, and one system per process limits the damage.
2. **Put OMSimulator's temp directories on a container-local path**, as (b) found for the inbox. `instantiate()` drops from 2.6 s to 0.13 s and `delete()` from 0.45 s to 0.016 s. Set `tempfile.tempdir` (or `TMPDIR`) and `SSP(temp_dir=…)` per run, and remove them afterwards: every OMSimulator child left a directory behind, as in (c).
3. **The step gate must not use `stepUntil(t)` for exact times.** Use `doStep()` once per communication step, or `stepUntil(t − h/2)`, and compute targets as `k·h`. Report the communication point actually reached (`getTime()`), not the target.
4. **The result reader keeps the first row per time and drops the sliver step at the stop time.** Read the MAT file when parameters must be shown, because the CSV has none. A numpy-only MAT v4 reader is enough.
5. **The communication step and the coupling scheme matter for closed loops.** `oms_ma` is Jacobi with a lag of one to two steps, and this loop is unstable at h = 0.1. The run spec should state h explicitly (the `oms_ma` default is 1 ms), run summaries should show it, and the authoring skill should warn that a controller computing its output in `do_step` adds a step of delay. Our FMPy Jacobi master on OMSimulator's accumulated grid reproduces OMSimulator to 5e-12, so it makes a good regression test for the OMSimulator engine.
6. **Write SSV files ourselves and bind them at the root system,** with parameter names `<component>.<parameter>`. A component-level SSV is lost on export when the component also has an inline binding. Validate exported SSDs against the bundled XSD, and treat a validation warning as an error.
7. **A reproducible SSP export needs our own post-processing.** Either rezip with fixed timestamps and sorted entries and drop or fix `generationDateAndTime`, or identify a system by a content hash: the SSD without the timestamp plus the sha256 of each embedded FMU. Generated FMUs aren't reproducible either (a new GUID per build), so provenance must hash the artifacts actually used.
8. **Keep OMSimulator out of the MCP process entirely,** including for SSP authoring, because `import OMSimulator` loads `libOMSimulator.so`. The MCP side can write the SSD and SSV XML with lxml, which is all an SSP needs.
9. **Record the binary's identity with every run:** the `libOMSimulator.so` sha256 (`818b054f…02b0fb` here) and `OMSimulator.version`. Treat the install as unverified until the image fetches the nightly tarball itself against a pinned sha256, or installs a locally built wheel pinned by hash. The apt route first needs an online check with `apt-cache` and `apt-get download`.

## (h) OMSimulator engine facts

**Verdict: PASS, with caveats.** This spike is the Phase 3 design probe, kept as [`spikes/h_oms_engine_probe.py`](../spikes/h_oms_engine_probe.py) and extended while the OMSimulator engine was built (`engines/oms_engine.py`, ADR 0009). It checks every OMSimulator 3.0.0.post198 behaviour the engine relies on. Three findings shape the engine beyond the design: the clock ignores the start time, a discarded step stalls silently, and a pythonfmu slave that raises aborts the process.

**Command:** `uv run --no-sync python spikes/h_oms_engine_probe.py` (about 15 s). It builds the shipped `pi_controller.py`, an exact zero-order-hold plant (`x <- a·x + K·(1 − a)·F`, `a = exp(−h/τ)`) and two stepping slaves with pythonfmu into `.cache/spikes/h/fmus`, then runs each case in a fresh child process (`python -I -B … --child NAME CFG`, `LD_PRELOAD` removed, `TMPDIR` on the container-local `/tmp/afmi-h`). The parent never imports OMSimulator. The report is `.cache/spikes/h/report.json`, with each child's stdout and stderr next to it.

| Item | Result |
|---|---|
| 1. Result file | `inst.setResultFile("")` after `instantiate()` logs `info: No result file will be created`, and no `.mat` or `.csv` appears. With `removeSignalsFromResults(".*")` only, or with neither, `default_res.mat` appears in the process's working directory |
| 2. Closed loop, `doStep()` + `getValue()` | 101 rows for h = 0.01 up to 1 s. Post-exchange values: `PI.y − P.x` and `P.F − PI.u` are 0.0 in every row. Against the exact Jacobi recurrence, max abs(Δx) 2.2e-16 and max abs(Δu) 2.4e-15. `getTime()` stays within 6.7e-16 of k·h. A `doStep()` at the stop time leaves the time at 1.0. Median `doStep()` 0.018 ms |
| 3. `setValue` before `initialize()` | `PI.Kp` and `PI.r` return `Status.ok` and take effect. An output with `initial="exact"` (`PI.u`) is accepted too, so the server must limit what may be set. A variable that is neither an input nor `initial="exact"` is refused with only a `RuntimeWarning`, and `setValue` returns `None` |
| 4. FMI 3.0 Feedthrough, two instances | Setters return `Status.ok`. `getVariableType` maps Float32 and Float64 to Real (0), the integer types to Integer (1), Boolean to 2 and String to 3. A Binary read returns 0.0 and logs `error: [getReal] UnSupported Numeric Type`. Mean `doStep()` 0.007 ms with the result file off |
| 5. Hand-written SSD and SSV (1.0 and 2.0) | Written twice, byte-identical. Both validate against OMSimulator's bundled `SystemStructureDescription.xsd` and `SystemStructureParameterValues.xsd` (`SystemStructureDescription11.xsd` itself doesn't parse). `SSP(path)` re-imports them: `Kp` = 7 from the root SSV, max abs(Δx) 2.2e-16 against the recurrence with `Kp` = 7 |
| 6. Package-tree digest | `660de5354584aa509806736d6e6f292429f5ebb19282308caaa998e79407fdd5` over 49 files (`__pycache__` skipped), 12 ms. `libOMSimulator.so` sha256 `818b054f…02b0fb`, metadata version `3.0.0.post198`. `find_spec` and `importlib.metadata` don't import the package |
| 7. Start time | With `activeVariant.startTime = 0.5` (and `setStartTime(0.5)` again), `getStartTime()` and `getTime()` say 0.5 after `instantiate()`, but `initialize()` resets the clock to 0.0 and the first `doStep()` reaches 0.1 |
| 8. A discarded step | A pythonfmu `do_step` that returns `False` past t = 0.5: OMSimulator logs `info: fmi2_doStep discarded for FMU "model.root.S"`, `doStep()` doesn't raise, the time reaches 0.6 once and then stays there on every later `doStep()` |
| 9. A slave that raises | A pythonfmu `do_step` that raises past t = 0.5 aborts the process inside OMSimulator: `terminate called after throwing an instance of 'std::logic_error'`, `what(): basic_string: construction from null is not valid`, exit status −6 (SIGABRT). A slave that raises in `__init__` aborts the same way at `instantiate()`. No OMSimulator logging level (0, 1, 2) avoids it. FMPy turns the same exception into an FMI error |
| 10. Temp directories | Each `instantiate()` leaves one `mkdtemp()` directory under `TMPDIR` (the extracted FMUs and the slaves' Python sources). `SSP(temp_dir=…)` makes another below the directory it is given, and removes it when the object is collected |

The engine and the worker apply this as follows (ADR 0009):
- The model is built through the API, never by loading a user's SSP. `setResultFile("")` follows `instantiate()`.
- Every `setValue` runs under `warnings.catch_warnings`, and a `RuntimeWarning` or a status other than `Status.ok` fails the run with `simulation_error`, naming the variable.
- The engine steps with `doStep()` only, while `getTime() < stop_time − h/2`. So there's no sliver step at the stop time, a remainder shorter than h/2 is dropped, and a longer one becomes one last step that OMSimulator clips to the stop time. A step that doesn't advance the clock raises `simulation_error` instead of spinning until the timeout.
- A clock that doesn't start at `start_time` raises, because OMSimulator always starts at 0. The server should refuse a non-zero `start_time` for systems before the run starts.
- `String`, `Binary` and `Clock` outputs are left out of the result, as FMPy does.
- The worker points `TMPDIR` and `tempfile.tempdir` at `work/tmp`, and the SSP's temp directory is `work/ssp`. The worker removes `work/` when it exits, so the leaked directories go with it.
- A raising pythonfmu component can't be caught. The worker dies, and the server's takeover records `worker_lost` with the abort line in `log_tail`. Test such a component alone with `run_start(fmu=...)` first, where FMPy reports the exception.

---

## (i) Live control and the pane API

**Verdict: PASS, with design changes.** This spike is the Phase 4 probe. It settles how a worker steps, pauses, sets and stops a live run in each engine, and what the Claude Code 2.1.295 plugin API gives the pane. Live sets in FMPy Co-Simulation work from `step_finished`. In Model Exchange they don't, because FMPy keeps the solver to itself. OMSimulator applies a set at the next step, but it treats each kind of variable differently.

**Commands.**
- Engines: `uv run --no-sync python spikes/i_live_control.py`, about 60 s (13 s of builds, then 50 child processes). It builds these FMUs into `.cache/spikes/i/fmus`:
  - with pythonfmu: an integrator (`x <- x + k·u·h`, `k` tunable, `u` an input), the exact zero-order-hold plant of spike (h) with an extra unconnected input `d`, a slave whose `do_step` returns `False` past t = 0.5, and the shipped PI example;
  - with omc: the shipped `MSD.mo` (`s:cvode`);
  - with gcc against FMPy's headers: **LiveProbe**, a C FMI 2.0 ME and CS FMU for `x' = −k·x + u`. Its `k`, `jump`, `t_term` and `t_discard` are tunable and, in ME, settable only in initialization or Event Mode, as in the Reference FMUs. In Event Mode a non-zero `jump` is added to `x` and reported as `valuesOfContinuousStatesChanged`. `t_term` is a time event that requests termination. In CS, a step that reaches `t_term` ends there with `fmi2Discard` and `fmi2Terminated`, and a step that would pass `t_discard` returns `fmi2Discard` without terminating.

  Each probe runs in a fresh child (`python -I -B … --child NAME CFG`, `TMPDIR` on the container-local `/tmp/afmi-i`). The parent imports neither FMPy's FMU classes nor OMSimulator. Every child runs a step gate as the Phase 4 worker would. Commands are applied in the simulation thread after a step. A paused gate blocks in `queue.get(timeout=0.05)` while a controller thread (the inbox reader's role) holds 1 s, single-steps, sets and resumes. A heartbeat thread beats every 50 ms. The report is `.cache/spikes/i/report.json`, with each child's config, stdout and stderr in `.cache/spikes/i/runs/`.
- Pane: a throwaway probe mod in `.cache/spikes/i/probe-pane/`, checked with `claude plugin validate` and `claude plugin test` (5 tests). A second mod, `.cache/spikes/i/fsprobe-mod/`, and `fs_poller.py` measure `$.fs` and `$.clock` in a real headless session. The 2.1.295 typings are saved in `.cache/spikes/i/types-2.1.295/`.

### Engines

| Item | Result |
|---|---|
| 1. FMPy CS: `step_finished` against our own loop | The integrator, BouncingBall 2.0 and 3.0 CS and LiveProbe CS each ran the same script both ways: pause at t = 0.25, hold 1 s, three single steps, a set while paused, resume, sets of `k` and `u`, stop at 1.5. The callback reaches the FMU through `recorder.fmu`, and our loop drives `FMU2Slave` or `FMU3Slave` itself. Rows are bit-identical between the two. Against the closed form the integrator is within 3.1e-15 and LiveProbe (`k`, `u`, `jump`) within 1.4e-15. BouncingBall apexes are 0.4948, 0.1255 and 0.0320 after `e` = 0.5 (ratio 0.254 ≈ 0.5²). Each single step advanced exactly one communication step (0.25 → 0.26 → 0.27 → 0.28) and blocked again. A stop ends with `Terminate -> OK` and `FreeInstance`. Code size: 47 lines for the callback runner and 71 for our CS loop, plus a 63-line gate that both share |
| 2. Pause and set at the start time | `step_finished` first fires after step 1: the first gate call sees t = 0.01, so a pause or set scheduled at t = 0 lands at 0.01 (`x(0.01)` = 0.01 with `k` still 1). Two mechanisms work, both exact to the reference (5.6e-16). One passes `simulate_fmu(fmu_instance=...)` an instance from `instantiate_fmu` whose `exitInitializationMode` is wrapped to run the gate. The other is our own loop calling the gate before its first `doStep`. With either, the run pauses at 0.0, single steps go 0.0 → 0.01 → 0.02, and `x(0.01)` = 0.02 with `k` = 2. In ME the wrapped call returns while the FMU is still in its initial Event Mode, so a set there goes in directly, without `enterEventMode`. `k` and `jump` set at t0 match the closed form in both approaches (within 1.7e-5 after t = 1.05). The caller must free an instance it passes in |
| 3. Input table against a live set | The integrator took `u` = 1 from FMPy's input table and got a live `u` = 5 at t = 1.0. The slope afterwards stays 1.0: `Input.apply` overwrites the live value before every `doStep` (in ME, also inside every root-function call) |
| 4. FMPy ME: `step_finished` with Event Mode against our own loop | BouncingBall 2.0 and 3.0 with `e` = 0.5 at t = 1, set in Event Mode: both approaches give apexes 0.49, 0.1225 and 0.0306, within 1.9e-5 of each other on the grid, because `e` acts only at the bounce. LiveProbe ME against the closed form, max abs error after t = 1.05 (callback / own loop): `k` = 3, 3.6e-2 / 2.5e-5; `u` = 2 (a continuous input, set directly), 1.1e-1 / 2.5e-5; `jump` = 0.5, 4.7e-1 (the jump is lost) / 5.7e-5. With `t_term` = 2.0 set at t = 1, the callback run goes on to 3.0 and misses the time event, while our loop stops at 2.0 (`terminated by the FMU at t=2`). The cause: FMPy's CVode integrates past the output point and interpolates back (`CV_NORMAL`), so its internal state and history are already ahead with the old dynamics. Only `simulateME`'s local `solver.reset()` and `next_event_time` follow an event, and `step_finished` can reach neither. Our ME loop (114 lines) uses FMPy's `CVodeSolver` and `Input`. After any command it resets the solver and takes the next event time and any termination from the command's event iteration |
| 5. VanDerPol 3.0 ME | A pause at t = 1 (1 s hold, then single steps 1.0 → 1.01 → 1.02 → 1.03) gives rows bit-identical to an unpaused run. Setting the **fixed** `mu` = 2 in Event Mode is accepted (no error status) and changes the trajectory. The Reference FMU doesn't enforce variability there, so the server must |
| 6. Discard and terminate in FMPy | All of these return normally, with no error, before the stop time: LiveProbe CS with `t_term` = 2.05 (`fmi2Discard` + `fmi2Terminated`) and LiveProbe ME (`terminateSimulation`) both end at t = 2.05 of 3. Stair 2.0 and 3.0, CS and ME, end at 9.0 of 10. The pythonfmu slave whose `do_step` returns `False` ends at 0.5, because pythonfmu answers `fmi2Terminated` = true for it. LiveProbe CS with `t_discard` = 1.05 (`fmi2Discard`, not terminated) is different: FMPy retries the same step forever and calls `step_finished` with the same t each time (1,000 calls at t = 1.0 until the probe's stall check stopped it). Without `step_finished` it hung and was killed after 15 s. Today's engine reports every terminating case as finished (`stopped` false) with a result that ends before `stop_time` |
| 7. OMSimulator `setValue` after `initialize()` | In the PI and exact-plant loop (h = 0.01, set after `doStep` 100), each case matches the Jacobi recurrence under its own hypothesis to 2.6e-15: <br>• Tunable `PI.Kp` = 40 and `P.K` = 4, and the unconnected input `P.d` = 0.5: `Status.ok`, effective from the next `doStep()`, and they persist. (`Kp` = 40 diverges, so its match is relative.) <br>• Connected inputs `P.F` = 5 and `PI.y` = 0: `Status.ok`, and `getValue` reads the value back. The next step uses it, then the exchange overwrites it, so it acts as a one-step pulse. <br>• The output `PI.u`, the fixed `MSD.k` (omc) and the fixed BouncingBall `g` are refused with `RuntimeWarning: Cannot set variable 'u' after the FMU is initialized. Only variables with causality='input' or causality='parameter' and variability='tunable' can be set at this time`, status `None`, with no effect. Before `initialize()`, spike (h) found the same output accepted. <br>• BouncingBall 2.0 and 3.0 CS (FMI 3.0 too) with `e` = 0.5 give apexes 0.4948, 0.1255 and 0.0320, as FMPy does. LiveProbe `k` = 3 matches the closed form to 1.4e-15 |
| 8. OMSimulator pause | 3 s without `doStep()` after step 100: 60 heartbeats with a 50.6 ms maximum gap. The result equals the unpaused run, and nothing timed out |
| 9. Discard and terminate in OMSimulator | The raw `Capi.doStep` status (the `inst.doStep()` wrapper raises on anything but ok): in FMI 2.0, a discard without terminate (LiveProbe `t_discard`, the pythonfmu slave) and a discard with `fmi2Terminated` (LiveProbe `t_term`, Stair 2.0) both return `Status.ok`. Both log the same `info: fmi2_doStep discarded for FMU "…"`. The clock moves to the end of the discarded step once and then stays. No status, time or OMSimulator line tells the two apart; only the FMU's own log text does. In FMI 3.0, OMSimulator ignores `terminateSimulation`: Stair 3.0 kept stepping from 9.0 to 9.8 with `Status.ok` until the FMU failed (`Variable "counter" cannot be incremented for values >= 10`). `doStep` then returned `Status.error`, and `terminate` failed with `fmi3Terminate: Illegal call sequence` |
| 10. Heartbeat while paused | Across 23 children with a 50 ms heartbeat thread, the worst gap was 51.0 ms. That holds while the gate blocks (`queue.get` with a timeout releases the GIL) and while steps run, in both engines and with pythonfmu slaves |

### Pane API (Claude Code 2.1.295)

| Item | Result |
|---|---|
| 1. Typings | `claude plugin validate` writes no typings. A `--plugin-dir` load writes `.claude-plugin/types/{claude-code,claude-code-tools,claude-code-mcp}/index.d.ts` only when the user owns the folder, which the root-owned bind mount `/workspace` isn't. In `-p`, it also needs `CLAUDE_CODE_PLUGIN_DIR_WATCH=1`. The saved file starts `// Written by Claude Code 2.1.295.` Its `$.fs`, `$.clock`, `Raster`, `Svg`, `invalidate` and `blit` declarations equal 2.1.293's |
| 2. Probe mod and test harness | `/fmi-probe` opens a Pane. `$.clock.every(250)` polls `live.json` and `status.json` of a fixture run. The pane draws a braille `Raster` on the terminal and an `Svg` polyline elsewhere, and a Button writes `commands/<unix_ms>-pane-<8 hex>.json`. `claude plugin validate` passes. It requires `$` to be passed only to functions declared at the top of the module: a closure inside `register` is refused with `$ is passed to "startPolling", which is not a function declared at the top of this file`. `claude plugin test` passes 5 tests in 0.7 s: mounts on both `terminal` and `desktop`, a button press, the mocked clock (4 polls per 1,000 ms advanced), a fake filesystem from `on('fs.read' / 'fs.write', …)` answering `{ value }` or `{ deny }`, a torn `live.json`, and the limits in item 5. **No login is needed**: the run is identical with an empty `CLAUDE_CONFIG_DIR` and `HOME`. A headless `claude -p --plugin-dir <dir> "/fmi-probe"` also runs the plugin's own command with no login and no model call |
| 3. `$.fs` | `read`, `write`, `list`, `exists`, `stat` (`resolve: true` adds `realPath`) and `ancestors`. **There is no rename, delete, append, mkdir or exclusive create.** `write` creates parent directories and is **not atomic**. A poller read the file while the mod rewrote it 20 times with about 4 MB each. It saw torn content (mixed versions, NUL tails) 4 times on container-local `/tmp` and 6 times on the bind mount, and an empty file 24 times. The inode never changed: the file is truncated and rewritten in place. 4,194,304 bytes is accepted, and one byte more is refused (`4194305 bytes is over the 4194304-byte limit`). Reading a missing file rejects with `ENOENT`. Costs on container-local `/tmp` and on the bind mount: a small read 0.96 and 3.2 ms, a 1 MB read 6.6 and 16 ms, `stat` 0.54 and 1.5 ms, a 4 MB write 9.8 and 37 ms |
| 4. Finding the run store | `$.session.cwd()` and `$.session.root()` both answer the launch directory, and a relative `$.fs` path resolves against the cwd. `$.env.get(name)` reads the engine's process environment. The name must be a string literal, `claude plugin validate` lists every name read (`AFMI_STORE_DIR`), and `mock.env` answers it in tests. A `userConfig` field (`storeDir`) reaches `register(on, options)`. A `tool.call` hook sees an MCP tool's result (`ToolCallResult.result`), so the pane could learn the run directory from `run_start`. The session root is not the store in general: the user's own container session runs in `/workspace`, while its MCP server uses `--workspace /lab` |
| 5. Surfaces and limits | The 2.1.295 typings give the terminal `Raster` and `Image` but no `Svg`, and the remote surfaces `Svg` but no `Raster`. At run time, though, every surface's `$.ui.resolve` table holds all 12 elements, and the one a surface can't draw becomes an empty fragment instead of a refusal. `Raster`: 1–512 columns and 1–256 rows (513 and 257 are refused, each naming its limit). Each cell must be one printable width-1 BMP code point (U+4E00 is refused), and colours are `0x00RRGGBB` or `0x01000000` for the terminal default, 1,024 colour pairs at once. `$.ui.blit` repaints a mounted Raster without a render pass, taking up to 120 a second and showing about 60. `Svg`: `source` up to 131,072 characters (131,073 is refused), `alt` required, size in CSS pixels. `Text` drops its `key`, so tests find text by content |
| 6. Timers and redraws | `$.clock.every(ms)` takes at least 1 ms. Measured in a real session: 250 ms gives 251–262 ms, 10 ms gives 10–22 ms, and 1 ms gives 1–2 ms. Each period is one dispatch, and a reload cancels the timers. A hook's own budget is 10 s per dispatch (`.catch` 1 s, linger 5 s). `$` calls don't count against it, but `$.clock.sleep` does. `ui.render` re-runs for the readers of a `$.state` value when it is set, at the redraw rate. `$.ui.invalidate` redraws at most 10 times a second (30 for the terminal's shown pane). A width change redraws every hooked site, and a height change alone redraws nothing. A render hook can't write `$.state` |

### Design implications

1. **FMPy CS:** both approaches are correct. We recommend our own `doStep` loop: 71 lines, bit-identical to `simulate_fmu`, with a natural gate at the start time and explicit handling of discard, terminate and early return. It can reuse FMPy's `Input`, `Recorder` and `apply_start_values`. If `simulate_fmu` stays, pass `fmu_instance=` with a wrapped `exitInitializationMode` for the start-time gate, and make the gate fail a step that doesn't advance.
2. **FMPy ME:** own the loop on FMPy's `CVodeSolver`, because `step_finished` can't apply a live set correctly. After every command, reset the solver and take the next event time and any termination from the command's event iteration. A set at the start time goes straight into the initial Event Mode.
3. **Validate every set on the server against the modelDescription and the system's wiring:** allow tunable parameters and inputs only, because an FMU may accept a fixed parameter (VanDerPol `mu`). For systems, refuse connected inputs, which OMSimulator holds for one step only. Refuse inputs the run's `inputs` table drives, which FMPy overwrites.
4. **Give discard and terminate one outcome in both engines.** FMPy returns early without an error on terminate and loops forever on a discard without terminate. OMSimulator returns ok and stalls in both FMI 2.0 cases, and in FMI 3.0 ignores the termination until the FMU errors. Only our FMPy loop can tell discard from terminate. The engines must report "ended early at t" uniformly instead of `finished` (FMPy today) against `simulation_error` (OMSimulator today).
5. **Pause blocks the gate** in FMPy and **skips `doStep()`** in OMSimulator. The heartbeat is unaffected and OMSimulator has no internal timeout. The watchdog must keep excluding paused time.
6. **Pane commands:** `$.fs.write` isn't atomic and nothing can rename. The pane writes create-once, uniquely named files (`commands/<unix_ms>-pane-<8 hex>.json`) and never overwrites one. The worker treats a file that doesn't parse as a complete JSON object as still being written: it retries on the next poll and sets the file aside after a few seconds. Server and worker files stay atomic (temp file and rename), so the pane's reads never tear, though the pane still parses tolerantly. The pane should poll with `$.fs.stat` and read only when `mtimeMs` or `size` changes.
7. **Store discovery order for the pane:** the `userConfig` field, then `$.env.get("AFMI_STORE_DIR")`, then the run directory from `run_start`'s result (`run_start` should return it as an absolute path), then `<session root>/.agentic-fmi`.
8. **Drawing:** branch on `e.surface` (`Raster` on the terminal, `Svg` elsewhere), because the wrong element silently draws nothing. Size to `e.props.bodyColumns`, at most 512. Downsample the polyline to about 600 points to stay well under 131,072 characters. Repaint the chart with `$.ui.blit` instead of re-rendering. Declare every helper that takes `$` at module level.
