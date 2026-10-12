# 0004. OMSimulator as the multi-FMU orchestrator

- Status: Accepted (Phase 0 spike (c) passed; evidence in [spikes.md](../spikes.md)). Partly superseded by [0009](0009-systems-with-omsimulator.md): there is no `Instance` protocol (each engine is one `simulate(spec, dir, gate)` function), OMSimulator is a core dependency pinned by its installed tree, and the worker uses `doStep` with the result file off.
- Date: 2026-10-09

## Context

Systems of FMUs, such as a controller plus a plant, need a master algorithm that handles connections, step coordination and SSP import and export. The options were:

- **OMSimulator 3.** It's SSP-native, has a Python API and installs with pip.
- **FMPy container FMUs** (`fmpy.container_fmu.cli.create_container_fmu`). These wrap FMUs into one co-simulation FMU, which the FMPy engine can step, but they have no SSP.
- **Our own master algorithm.** Rejected as scope creep.

The open question was whether OMSimulator 3 can advance a system step by step, which live sessions need (step, pause, set). The online docs for v3.0.0.post199 show no `stepUntil`.

## Decision

- OMSimulator 3 is the multi-FMU orchestrator, used through the `oms` extra and locked exactly (`OMSimulator==3.0.0.post198`).
- It sits behind the `Engine`/`Instance` protocol in `engines/oms_engine.py`, and `Instance` can require `step` from it.
- FMPy stays the single-FMU engine.

Spike (c) showed that 3.0.0.post198 steps, so none of the planned fallbacks is needed. There's no legacy `OMSimulator` class in this version, and the FMPy container route stays out of the critical path (spike (d) is only partial).
- **High level:** `inst = SSP(path).instantiate()`, then `setResultFile`, `setValue(CRef(...), v)`, `initialize()`, then any mix of `stepUntil(t)`, `doStep()`, `setValue`, `getValue` and `getTime()`, then `terminate()` and `delete()`.
- **Low level:** `from OMSimulator import Capi` wraps the native `oms_*` calls.

## Consequences

- (+) Standard SSP and SSV import and export, so systems are portable to other tools.
- (+) One engine seam with two real implementations, and shared contract tests.
- (+) Live systems and batch systems use the same engine.
- (-) OMSimulator 3 changes quickly. We pin it exactly and the contract tests catch drift.
- (-) Run OMSimulator only inside worker processes, one model per process ([ADR 0002](0002-isolated-worker-process-per-run.md)). `InstantiatedModel` hard-codes the model name, a second `instantiate()` fails, it logs `info:` and `warning:` lines to stdout, and `Capi.getResultFileSignals` segfaults (never call it).
- (-) For FMI 3.0 systems the worker must call `removeSignalsFromResults(".*Binary.*")` before `initialize()`. Otherwise the result file has 0 rows and every step is about 50 times slower.
- (-) The PyPI sdist downloads an unhashed nightly tarball from `build.openmodelica.org` at build time, with `requests.get(..., verify=False)`. `uv.lock` doesn't cover the binary, so the exact version pin is the only control, the first install needs network access, and the binary isn't hash-verified. This is an accepted risk to revisit: record the binary's sha256 at install time, or vendor a wheel.
- (-) pythonfmu FMUs run inside OMSimulator and FMPy (both Python hosts) without `LD_PRELOAD`, because the FMU bundles its own `pythonfmu` under `resources/`. Only non-Python hosts need `LD_PRELOAD` of libpython (spike (e)).
