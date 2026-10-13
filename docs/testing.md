# Testing

> The pytest configuration and markers live in `pyproject.toml`. The suite lives in `tests/`: the smoke tests, the `fmu/` tests (`tests/fmu/`), the tests for signals, analysis and plots, the run store (`tests/store/`), the engines and the worker (`tests/engines/`, `tests/worker/`), the supervisor (`tests/supervisor/`), the output budgets (`tests/test_budget.py`), the run use cases (`tests/test_runs.py`), the MCP tools (`tests/mcp/`), `fmu_build` (`tests/builds/`), system files and system runs (`tests/system/`), SSP import and export (`tests/ssp/`), live runs (`tests/live/`, `tests/test_control.py`, `tests/test_runs_live.py`), response styles (`tests/test_style.py`) and the pane (`pane/hooks/*.test.ts`). Layers marked *(planned)* don't exist yet.

## Test layers

| Layer | What it proves | Tooling | Phase |
|---|---|---|---|
| Smoke | The package imports, `afmi` runs, the cached Reference FMUs parse (XML and zip only), and the OMSimulator API resolves | pytest | 0 (done) |
| Unit | Pure functions: parsing, metrics, downsampling, the signals DSL, protocol models | pytest | 1 (done) |
| Integration | Inspection, validation and runs on real Reference FMUs (FMI 2.0 and 3.0, ME and CS). The protocol round-trip against a real worker. A killed worker marks its run `failed`. Suites: `tests/fmu`, `tests/store`, `tests/engines`, `tests/worker`, `tests/supervisor`, `tests/mcp` and `tests/test_runs.py`. The command inbox, the live gate and live control against real workers (`tests/live`, `tests/test_control.py`, `tests/mcp/test_mcp_live.py`) | pytest + the Reference FMUs | 1, 2, 4 (done) |
| Engine contract | The FMPy and OMSimulator engines, each run by a real worker, pass the same tests: finish, stop with a partial result, column order and units, errors as `simulation_error` ([ADR 0009](adr/0009-systems-with-omsimulator.md)). Implemented in `tests/worker/test_engine_contract.py`, which runs each test once per engine on the same Reference FMU (an FMU run, and a one-component system) | pytest (`oms` on the OMSimulator half) | 3 (done) |
| Live contract | The same two engines through real workers, for live runs ([ADR 0010](adr/0010-live-runs-and-the-pane.md)): start paused at t0, steps, pause and resume, speed, sets, stop, timeout and idle limit. Implemented in `tests/live/test_live_contract.py` (each test once per engine, on Feedthrough 2.0 as an FMU run and as a one-component system) | pytest (`oms` on the OMSimulator half) | 4 (done) |
| MCP contract | Tool schemas, `ToolError` text and the output size budget, tested through the MCP SDK v2 client (in memory, and a stdio subprocess) | pytest | 1 (done) |
| Plugins | Plugin manifests are valid. The pane mounts on the `terminal` and `desktop` surfaces with fixture files, and its buttons write command files (golden files). There is no `tsc` in the container, so these tests are the pane's only check | `claude plugin validate .`, `claude plugin validate pane`, `claude plugin test pane` | 1, 4 (done) |
| End to end | Manual sessions in Claude Code following the user guide; the pane can only be judged by eye | `docs/user-guide.md` | by hand, before merging a feature that changes what users see |

## Markers

Markers are registered in `pyproject.toml`:

| Marker | Meaning | Skip with |
|---|---|---|
| `oms` | Runs OMSimulator (in a worker or a child process; it is a core dependency, so it is always installed and the marker only groups these tests) | `-m "not oms"` |
| `omc` | Needs OpenModelica (`omc`). The dev image installs it, so these run in the container | `-m "not omc"` |
| `slow` | Long-running test | `-m "not slow"` |

`--strict-markers` is set in `addopts` (`[tool.pytest.ini_options]`), so a typo in a marker name is an error.

A test whose optional package is missing **skips with a reason**; it doesn't fail. The `oms` smoke test calls `pytest.importorskip("OMSimulator", reason=...)`, and the reason names the install command.

## Fixtures

### The Reference FMUs

- `reference_fmus` (session scope, `tests/conftest.py`): returns `<repo>/.cache/reference-fmus/0.0.41` as a `Path`. The repo root comes from the location of `conftest.py`. The fixture requires the `.complete` marker that `scripts/fetch-reference-fmus.py` writes after a verified extraction. If the marker is missing, each dependent test **skips** with the message `Reference FMUs not cached; run: uv run python scripts/fetch-reference-fmus.py`. Tests never download.
- Read-only exception: `reference_fmus` hands out the real cache. The smoke tests use it only to read `modelDescription.xml` through FMPy and to check each archive's CRC. Nothing loads an FMU binary and nothing writes to the cache.

### Workspaces, FMUs and runs

- `workspace` (`tests/conftest.py`): an empty directory `<tmp_path>/workspace`, already resolved (no symlinks in its path). `load_config(workspace)` turns it into a `Config` whose only allowed root it is. It doesn't create a run store; the store creates `.agentic-fmi/` on first use.
- `fmu` (`tests/conftest.py`): a factory `fmu(fmi_version, name)`, for example `fmu("3.0", "BouncingBall")`. It copies the cached Reference FMU to `<workspace>/<fmi_version>/<name>.fmu`, returns that path, and reuses the copy on a second call. Tests may mutate the copy; the cache is only read. An unknown name fails the test with the missing path. It needs `reference_fmus`, so it skips when the cache is missing.
- `tests/fmu/conftest.py` (visible to the `fmu/` tests only): `config` (`load_config(workspace)`), `write_fmu(name, members)` (a zip in the workspace with the given members), `fmi2_xml(inputs=, outputs=, parameters=, description=, same_name=, variable_description=)` (a schema-valid synthetic FMI 2.0 `modelDescription.xml`) and `fmi1_xml`. The synthetic archives hold no binaries.

- `tests/engines/conftest.py` (engine tests only): `extracted(fmu_path)` unpacks a copy into the test's temp dir, and `engine_spec(fmu_path, fmi_type, outputs=, start_values=, inputs=, solver=, **experiment)` builds the `RunSpec` the server would write. The engine tests load FMU binaries into the test process, on these copies only.
- `tests/worker/conftest.py` (worker tests only): `new_run(fmu_path, fmi_type, ..., timeout_s=, force=, **experiment)` writes a `queued` run directory into the workspace's store. `spawn(run_dir)` starts `python -m agentic_fmi worker` as the supervisor will (new session, output to `log.txt`) with `TMPDIR` set to `worker_tmp`, and kills and reaps every child at teardown. `worker_tmp` is a private, initially empty directory under `tmp_path`: the temp-dir leak checks assert it is still empty, instead of diffing the shared `/tmp`, which other processes use. `run_worker(run_dir)` runs one to completion, and `await_status(run_dir, condition)` polls `status.json`.

The worker tests run real processes. Only `test_timeout_kills_a_hung_native_step` (Roberts 3.0 CS with `timeout_s=5`, about 6 s) is marked `slow`. It checks the watchdog's precision from the first `running` it sees to `failed{timeout}`, on the test's monotonic clock, so interpreter start (several seconds under CPU load) isn't counted. Engine and worker tests together take about 20 s.

- `tests/mcp/conftest.py` and `tests/mcp/mcp_support.py` (MCP tests only; `mcp_support` is importable because pytest puts the directory on `sys.path`): `session` is the real server behind the SDK's in-memory `Client`, driven through an anyio blocking portal so tests make plain calls (`session.ok(tool, **args)` returns the structured content, `session.error(...)` the message of an `is_error` result, `session.wait_for(run_id, states)` polls `run_status`). At teardown every run still queued or running is stopped. Any worker still alive is then killed, the teardown waits until it has exited, and a `run_status` call has the server reap its exited workers, so no worker outlives its test, not even as a zombie. `open_session(config)` builds one with another `Config` (for example `max_workers=1`); `open_stdio_session(config)` talks to a real `python -m agentic_fmi mcp --workspace` subprocess. `synth_fmu(name, variables=)` writes a schema-valid FMI 2.0 archive with no binaries, for output-budget tests. `kill_worker(session, run_id)` (in `test_mcp_runs.py`) SIGKILLs a run's worker and waits until it is gone or a zombie before the test asks the server anything.
- `tests/test_runs.py` (own fixtures): `add_run(columns, ...)` fabricates a run with an exact result table, spec, summary and index entry, so result, comparison and plot logic is checked without a worker.
- Optional dependencies: pythonfmu is a core dependency. The pythonfmu stdio test skips only when the `pythonfmu` command isn't installed next to the interpreter.

The MCP tests run real workers: about 35 s for `tests/mcp` (the Roberts timeout is `slow`).

### FMU builds

- `tests/builds/` runs real workers and real build tools. The directory is `builds`, not `build`: pytest skips directories named `build` by default, so a bare `pytest` would not collect it. `conftest.py` gives `config` (fast poll), `supervisor` and `write_source`; at teardown `build_support.kill_leftovers` kills every worker the supervisor spawned and any process still naming the workspace. `processes_in(dir)` finds the processes working under a directory (omc, cmake, make, clang), which is how the stop test proves nothing is left.
- The **`omc` marker** is on the tests that start the real `omc` (`test_build_omc.py`, and the Modelica tests of `test_build_examples.py`); they skip with the reason "omc (OpenModelica) is not installed" when `shutil.which("omc")` is empty. They run in the dev image, which installs omc 1.27.1 and Modelica Standard Library 4.1.0. Tests that need no omc (the parser on captured logs, the `.mos` script, the install hint, the class-name pre-check) are not marked, so they run everywhere. A build takes 6 to 10 s warm; the whole `omc` set takes about 45 s.
- **Builds are offline.** `omc` runs with an unroutable proxy (`http://127.0.0.1:9`), so its libcurl never reaches the network: with a warm `HOME` it makes no attempt, and with a fresh one (the `slow` test builds with an empty `HOME`, copying the 154 MB library cache from `omlibrary`, about 12 s) the build log shows only the failed package-index download. Tests never download anything.
- `test_build_examples.py` copies `skills/fmu-authoring/examples/` into a temporary workspace (the originals are never modified) and builds each once per module. `test_build_python_examples.py` does the same for the Python examples Claude uses when a user asks for an example (`bouncing_ball.py`, `MSD.py` and `pi_controller.py`), with no `omc`, and checks their physics against exact solutions.
- `test_build_jobs.py` also pins two races and a cleanup: two threads starting the same build name at once (a `Barrier` holds both after `check_request`, so the old check outside the `admission` lock would let both start; exactly one now does), and a commit whose write fails (`execute_build` runs in the test process with `_copy_hashed` or `write_build_record` broken: a directory the commit created is removed again so the name stays free, while an overwrite leaves the earlier build as it was). `tests/mcp/test_mcp_build.py` runs `fmu_build` through the in-memory client: a success, a failure (`Error executing tool fmu_build: Build failed (pythonfmu)`) and a `name` that breaks the schema pattern.

### Systems and SSP

- `tests/worker/test_engine_contract.py` runs every engine-contract test once per engine through a real worker, on the same Reference FMU: as an FMU run (FMPy) and as a one-component system `M` (OMSimulator). `tests/worker/test_worker_system.py` (`oms`) runs system runs through real workers against the exact Jacobi recurrence of the pythonfmu PI controller and plant (`tests/worker/system_support.py`), and checks the pin (`oms_changed`, also against the current pin), the FMU re-hash, the cleanup, OMSimulator's refusals and what it does with an FMU's terminate request (Stair 2.0 stalls the clock, Stair 3.0 is ignored until the FMU fails).

#### `tests/system/`

System files and system runs. `tests/system_fmus.py` writes synthetic FMUs (a zip with a `modelDescription.xml` and, where a platform binary is wanted, an empty placeholder), so the checks read XML and the zip directory only and **no binary is loaded**. `tests/system/conftest.py` gives `config`, `supervisor`, `fmus` (`fmus/pi.fmu`, `fmus/plant.fmu`), `write_system`, `oms` (a fake OMSimulator install that matches the pin), `spawned` (`Supervisor.spawn` is replaced: no worker starts, so `spec.json` is inspected as written) and `add_system_run` (a finished run with an exact result table).

| Module | Proves |
|---|---|
| `test_system_load.py` | Size, JSON and schema errors, path containment (symlinks included), FMU resolution |
| `test_system_check.py` | Every error and warning class of `system_check` |
| `test_system_params.py` | The parameter layers and their recorded precedence, type conversion, what may be set |
| `test_system_run.py` | The fully resolved `spec.json`, the refusals (the step-count and the result-size guards, each overridden by `force`), admission, the views |
| `test_system_compare.py` | `run_compare` and `run_plot` on system runs, and that every `SystemSpec` field is either compared or listed as ignored |
| `test_exit_demo.py` | The closed-loop demo, below |

`test_exit_demo.py` is the closed-loop demo as a test (markers `omc`, `oms` and `slow`, about 15 s; it skips without `omc`). In a temporary workspace with real workers it builds the two `fmu-authoring` examples, checks and runs the shipped `skills/system-builder/examples/loop.json`, pins the overshoot (10.42 % at h = 0.01 within 0.1, 7.88 % at h = 0.001 within 0.02), checks that a pure-numpy sampled-data model of the loop agrees with the real runs, tunes to under 5 %, compares and plots the runs, and exports and re-imports the system for bit-identical results. The skill examples are copied, never modified.

#### `tests/ssp/`

SSP and SSV import and export (`ssp.py`). Most tests run on synthetic FMUs and read XML only; `ssp_support.py` writes SSD and SSV text and raw archives (malformed ones included), and `conftest.py` has `make_ssp`, `config`, the pythonfmu FMUs and a `supervisor` whose workers are killed at teardown.

| Module | Proves |
|---|---|
| `test_ssp_refusals.py` | The refusal suite for untrusted SSPs: unsafe references, archive tricks, XML bombs (a DOCTYPE or an undeclared entity), size caps that refuse before reading, encrypted entries, lying sizes and counts, unsupported SSP features. Each refusal names the cause, leaves nothing under `imported/` and leaves the source untouched |
| `test_ssp_import.py` | Well-formed SSPs: the system file and FMU copies written, SSP 1.0 parameter precedence with `prefix`, names, problems passed on as warnings |
| `test_ssp_ssv.py` | `parse_ssv` (types, units, refusals) and `read_paramset` |
| `test_ssp_export.py` | The archive layout, byte-identical repeated exports (also from a fresh process), the SSD version (1.0 holds only SSP 1.0 connector kinds; a `local` connector or an FMI 3.0 type gives 2.0), validity against OMSimulator's bundled XSDs (skipped when they aren't installed) and the file-level round trip |
| `test_ssp_roundtrip.py` (`oms`) | A system exported, imported and run through the run machinery gives exactly the original results. OMSimulator's own importer, in a **child process**, loads our export without schema warnings and reproduces the worker's run |

The **`oms` marker** is on the tests that run OMSimulator. Those tests do it in a worker or in a child process (`oms_child.py`, run by `ssp_support.run_oms_child` with `-I -B`, `TMPDIR` and the working directory in a scratch directory), never in the test process: importing OMSimulator loads its native library at once. The other SSP and system tests need no OMSimulator and run without the marker.

### Live runs and the pane

- **`tests/live/`** (91 tests) holds the live run machinery (ADR 0010). It uses the worker fixtures of `tests/worker/conftest.py` (loaded by path) and its own `conftest.py` (`new_live_run` for an FMU, `new_live_system_run` for a system, `discarder`) and `live_support.py` (command writers in the server's and the pane's way, outcome and event readers, the Discarder's C source).

| Module | Proves |
|---|---|
| `test_live_inbox.py` | The command inbox on the file system only: names, the server's exclusive create (also where `os.link` is refused), fuzzed parsing, strict name order with a grace for torn pane files, each file handed out once (a late lower name included), oversized and odd entries, outcomes, the final sweep |
| `test_live_gate.py` | The gate's one wait loop on a fake clock: the pause at t0, resume, pacing and re-anchoring, steps, sets checked again in the worker (a refusal partway names the values already applied), stop, the idle limit, waiting excluded from the watchdog's clock, exactly one outcome per command |
| `test_live_settable.py` | What a `set` may change (`settable`, `check_sets`, `driven_inputs`), which outputs `live.json` carries, and the `LiveBuffer` ring (change-only snapshots, rounding, the size cap, the worst case) |
| `test_live_worker.py` | Real workers on BouncingBall CS 2.0 and 3.0: paused at t0, `heartbeat_mono`, `run_stop` while paused, a set of `e` after single steps, bad pane sets rejected, the idle limit, leftovers rejected, batch runs never touching the inbox, monotonic staleness |
| `test_live_contract.py` | The **engine contract for live runs**: every test runs once per engine on Feedthrough 2.0, as an FMU run (FMPy) and as a one-component system (OMSimulator). Start paused with `live.json`, `step n`, pause and resume, speed, sets of Real, Integer, Boolean and Enumeration inputs, stop while paused, waiting excluded from the timeout, the idle limit |
| `test_live_engine_rules.py` | The engine-neutral end rules on FMPy: a step that never advances is `simulation_error` (the stall check), and an FMU-requested terminate is `finished` with "ended early at t=..." (batch and live); `_FmpyLive.set` names the groups it set before a refusal. OMSimulator's side is in `tests/worker/test_worker_system.py` |
| `test_live_oms.py` (`oms` on the worker tests) | The OMSimulator live handle against a fake instance, and real workers on the pythonfmu PI + plant loop: a mid-run Kp and Ki change equals the Jacobi recurrence switched at that step (1e-12), a set on a connected input, an output or a fixed parameter is rejected and the run goes on |

- **The Discarder.** The end-rule tests need an FMU that discards a step without terminating, which no Reference FMU does. `live_support.py` holds its C source and `conftest.py` compiles it once per session with `gcc` against FMPy's FMI 2.0 headers (`build-essential` in the dev image supplies `gcc`). Without `gcc` the tests that use it **skip** with the reason "gcc is needed to build the Discarder test FMU". Only worker processes load it.
- **`tests/test_control.py`** (68 tests) is the server side of `run_control` and `run_status(values)` in `control.py` on fabricated runs and a stand-in worker (no process runs): the refusals (batch run, build, ended run, `step` while running, stray arguments), the command a request becomes (`dt` to whole steps), how long the wait lasts (`wait_s` is never exceeded), how outcomes and rejections are reported, and the 25-value limit.
- **`tests/test_runs_live.py`** (14 tests) covers the live path of `runs.py`: what `start_run(live=True)` records and refuses (an ME-only FMU, `fmi_type="ModelExchange"`), the wait that ends at the pause, and the views of live runs. `tests/system/test_system_live_plan.py` (5 tests) does the same for `run_start(system=..., live=True)`.
- **`tests/mcp/test_mcp_live.py`** (14 tests) drives the live tools through the in-memory client with real workers: `run_start(live=true)` pauses at t0, every `run_control` action on BouncingBall, `run_status(values=[...])`, the wait bounds and the refusal texts, and a live system on OMSimulator through the same tools. `tests/store/test_pane_fixtures.py` is the drift test of the pane's fixtures.
- **The pane suite** (`claude plugin test pane`, 65 tests in `pane/hooks/*.test.ts`, no login, no real file system). `pane.test.ts` mounts the pane on both surfaces (`terminal` and `desktop`) against the fixture store `pane/fixtures/store.ts`, which `scripts/pane_fixtures.py` generates from the pydantic models: the picker, status, the values table (grouped by component, cut to 90 cells), charts and the chart picker (a click on a signal, ✕), the buttons' command files (against the **golden commands** in `PANE_COMMANDS`), toasts, torn files, unknown versions, build cards and store discovery. `diagram.test.ts` pins the connection diagram's **golden Svg** and its limits. `build.test.ts` covers the build card, and `units.test.ts` the pure helpers (the downsampler, both chart renderers within their limits, the terminal's interpolated glyphs, the signal groups and rows, the Set field, the status line). There is no `tsc` in the container, so these tests are the only check of the pane's TypeScript.

The live and server-side tests take about 90 s together (192 tests; real workers and paced runs on short horizons); run them one suite at a time like the other worker tests.

### Response styles

- **`tests/test_style.py`** (31 tests) checks `style.py` on hand-built `RunView`s: the line for every state, a system run's name, the build lines, numbers with at most four significant digits, and `ninja_view` (no summary, build facts or log tail; error code, message and requested `values` kept; the input view unchanged).
- **`tests/store/test_preferences.py`** (9 tests) covers `preferences.json`: the default, an atomic round trip, and a missing, torn, wrong-type, wrong-protocol or empty file reading as ninja, the default. The other MCP tests run in talkative mode (their `mcp_config` fixture stores it), because they read full summaries.
- **`tests/mcp/test_mcp_style.py`** (15 tests) drives `response_style` and the styled tools through the in-memory client: set, report and persistence (a second server on the same store), a corrupt or unwritable preferences path, ninja `run_start`, `run_status`, `run_stop`, live `run_control` and `fmu_build` (the `pi_controller` example, one real pythonfmu build), talkative output unchanged, and refusals identical in both modes. `test_mcp_tools.py` pins the `tools/list` size (31,822 of 32,000 bytes with 18 tools).

## Smoke tests (`tests/test_smoke.py`)

| Test | Proves | Needs |
|---|---|---|
| `test_package_imports_and_version_matches_installed_metadata` | `agentic_fmi.__version__` is a non-empty string equal to the installed `agentic-fmi` metadata | package installed |
| `test_version_flag_prints_afmi_version_and_exits_zero` | `main(["--version"])` prints `afmi <version>` and raises `SystemExit` with code 0 | nothing extra |
| `test_no_arguments_prints_the_help_and_returns_zero` | `main([])` returns 0 and prints the usage with the `mcp` and `worker` commands | nothing extra |
| `test_afmi_console_script_runs_version_flag` | The `afmi` script found next to `sys.executable` runs `--version` and exits 0 (subprocess, 60 s timeout) | package installed (`uv sync`) |
| `test_bouncing_ball_model_description_parses[2.0]` and `[3.0]` | FMPy reads BouncingBall's `fmiVersion`, and `e` is `causality="parameter"` and `variability="tunable"` | `reference_fmus` |
| `test_every_cached_fmu_is_a_zip_with_model_description` | Every `*.fmu` under the cache is a zip with an intact CRC and a `modelDescription.xml`. All bad files are listed in one failure | `reference_fmus` |
| `test_omsimulator_ssp_api_resolves` (`oms`) | `import OMSimulator` works and `from OMSimulator import SSP, CRef` resolves. Nothing is simulated | OMSimulator (a core dependency) |

Expected result in the container: `8 passed`.

Skip paths, checked by hand without moving the cache:

- Missing cache: copy `tests/` and `pyproject.toml` to a scratch directory that has no `.cache/` and run `python -m pytest -q -rs` there. Expect `5 passed, 3 skipped` with the `reference_fmus` skip message.
- Missing OMSimulator: put a module named `OMSimulator.py` that raises `ModuleNotFoundError` first on `PYTHONPATH`, then run `uv run pytest -q -m oms -rs`. Expect the oms test to skip with `OMSimulator not installed; run: uv sync --locked`.

## `fmu/` tests (`tests/fmu/`)

They read `modelDescription.xml` only and copy every Reference FMU into a temporary workspace first. They run in about 4 seconds. The module names carry an `fmu` prefix because the test directories have no `__init__.py`, so test file names must be unique across `tests/`.

| Module | Proves |
|---|---|
| `test_fmu_catalog.py` | A relative and an absolute path resolve alike. Absolute paths, `../` traversal, symlinks and dangling symlinks that point outside the workspace raise `UserError`. A missing name suggests close matches. The scan skips `SKIP_DIRS`, stops at depth 8 and ignores outside symlinks. `list_fmus` filters (substring and glob, case-insensitive), pages (default 50, `page_max` cap, `next_offset`) and lists unreadable archives and FMI 1.0 with a `problem` |
| `test_fmu_inspect.py` | `inspect_fmu` works on all 15 Reference FMUs (hash, counts, JSON within the 8 KiB budget). Clocks gets `runnable: false` with the reason `ScheduledExecution only`. A synthesized FMI 1.0 archive raises `UserError` ("FMI 1.0 is not supported"). Lists are cut at 25 and descriptions at 2048 characters. `resolve_experiment` gives 0.004 for BouncingBall 3.0 CS and 0.01 for 2.0 CS. `check_run` refuses Roberts 3.0 CS (about 1e11 steps) with a pointer to ModelExchange, and passes Roberts ME and every other default experiment. `list_variables` filters (name, causality, variability, type) and pages (default 50, max 200, `total`). `require_variables` gives the documented message |
| `test_fmu_validate.py` | The Reference FMUs validate clean. Clocks, FMI 1.0, broken archives and schema violations are problems, not errors. Problem lists are capped at 25 and each at 300 characters. Path errors raise |
| `test_fmu_no_native_code.py` | After every `fmu/` function ran on all 15 FMUs in the test process, `/proc/self/maps` has no new file under a `/binaries/` directory (Linux only) |
| `test_fmu_fixtures.py` | `workspace` and `fmu` hand out private copies; mutating a copy never changes the cache |

## How to run

Inside the container (or prefix with `MSYS_NO_PATHCONV=1 docker exec -w /workspace agentic-fmi-dev bash -lc '...'` from Git Bash):

```bash
uv run pytest -q                            # full suite (OMSimulator and omc are installed in the container)
uv run pytest -q -m "not omc"               # the default suite
uv run pytest -q tests/test_smoke.py        # smoke tests
uv run pytest -q -m "not omc and not slow"  # quick loop
uv run pytest -x --pdb -k status            # debug one test
uv run ruff check . && uv run ruff format --check .
claude plugin validate .                    # core plugin manifest
claude plugin validate pane                 # pane plugin
claude plugin test pane                     # pane suite (no login needed)
uv run pytest -q tests/live tests/test_control.py tests/test_runs_live.py tests/mcp/test_mcp_live.py   # live runs, about 90 s
```

`claude plugin validate .` checks the core plugin manifest (`.claude-plugin/plugin.json`, including its `mcpServers` entry) and the marketplace file.

`claude plugin validate pane` checks the pane plugin: its manifest, its `$.state` contract and its hooks module, including the rule that `$` is passed only to functions declared at the top of `register.tsx`. `claude plugin test pane` runs the pane's `*.test.ts` files (65 tests) against the fixture store in `pane/fixtures/store.ts`, generated by `scripts/pane_fixtures.py` and kept in sync by `tests/store/test_pane_fixtures.py`; it needs no login. The container has no `tsc`, so type errors in the pane show up only when a test reaches that code.

Container environment notes:

- The container sets `UV_PROJECT_ENVIRONMENT=/home/dev/.venvs/agentic-fmi`, so the virtualenv is not `/workspace/.venv`. The `afmi` smoke test finds the script next to `sys.executable`, so it works wherever the environment lives.
- OMSimulator is a core dependency (ADR 0009), so `uv sync --locked` keeps it. Its sdist fetches unhashed nightly binaries at build time (see [development.md](development.md)); installing it fresh needs network access. When the uv cache already holds the build, `uv sync --locked --offline` restores it without network.
- The `oms` marker selects tests that run OMSimulator (in a worker or a child process); OMSimulator is always installed, so the marker only groups them.

Tests that run workers spawn real processes, so run test suites one at a time, never concurrently.

## Timing and process rules

These rules come from flakes seen in repeated full runs, idle and under CPU load (24 busy loops on 16 cores, where a full run takes about 5 minutes instead of 2):

- **Never measure a duration with the store's timestamps.** They are wall-clock times, and WSL2 steps the container's wall clock back by up to about 1.1 s every 30 s or so. Time durations with `time.monotonic()` in the test.
- **Wait for a SIGKILLed process to die.** `os.killpg(pid, SIGKILL)` returns while the target still shows `R` in `/proc` for a few milliseconds, or tens under load. A reconciliation in that window still sees a live worker. Wait until the process is a zombie or gone (`kill_worker` in the MCP tests, `until(... == "Z")` in the supervisor tests).
- **Wait for a process state; don't assert a transient one.** A child that has just printed "ready" may not be asleep yet.
- **Don't bound interpreter start.** Starting a worker or `afmi mcp` (imports included) takes about 1 s idle and up to 7 s under load. Bound what the code under test controls, such as the watchdog tick or the stop grace, and give spawns generous timeouts.
- **Read a failed run's `log.txt` only after the traceback is in it.** The worker prints the traceback just after it records `failed`, so the status's log tail has none.
- **Temp-dir leak checks use a private directory**: `worker_tmp` (as `TMPDIR`) for workers, and `monkeypatch.setattr(tempfile, "tempdir", ...)` in process. Never diff the shared `/tmp`.

To hunt flakes, run the full suite several times with `-rf -p no:cacheprovider`, both idle and with busy loops (`python -c "while 1: pass" &`, killed afterwards). Check `ls -A /tmp` and `ps -eo pid,stat,cmd` before and after.

## How to add a test

1. Put it next to its subject, mirroring `src/agentic_fmi/`. For example, `src/agentic_fmi/store/atomic.py` is tested in `tests/store/test_atomic.py`.
2. Name tests after the behavior, such as `test_stale_heartbeat_marks_run_failed`, and cover the error paths and the acceptance criteria from the task brief.
3. Use the fixtures for FMUs and run stores. `reference_fmus` is read-only: use it only for reads that load no binaries and write nothing. Anything that simulates, writes or might mutate an FMU copies it first with the `fmu` fixture.
4. Add the matching marker (`oms`, `omc`, `slow`) when the test needs an optional tool or takes more than a few seconds. Optional tools skip with a reason, they don't fail.
5. For MCP tools, assert the output size budget and that every `ToolError` message says what to do next.
6. Run it in the container. If you added a fixture, marker or layer, update this page.
