# Glossary

## FMI basics

| Term | Meaning |
|---|---|
| **FMI** | The Functional Mock-up Interface: an open Modelica Association standard for exchanging dynamic models as a C API plus an XML description. Agentic FMI supports **FMI 2.0 and 3.0**, not FMI 1.0 |
| **FMU** | A Functional Mock-up Unit: a zip with `modelDescription.xml`, compiled binaries per platform, and optional sources and resources. Running one executes native code |
| **Platform** | The binary folder an FMU ships, such as `binaries/x86_64-linux/` (FMI 3.0) or `binaries/linux64/` (FMI 2.0). An FMU runs only where it has binaries |
| **modelDescription.xml** | The FMU's interface: variables, types, units, capabilities and default experiment. It can be read without loading binaries, which is all the MCP server does |
| **Model Exchange (ME)** | The FMU provides equations (derivatives and event indicators), and the importer supplies the solver |
| **Co-Simulation (CS)** | The FMU contains its own solver, and the importer advances it with `doStep` over communication steps |
| **Scheduled Execution (SE)** | FMI 3.0 only. The importer activates the model's clocked partitions, for example to emulate an ECU scheduler |
| **Default experiment** | The suggested start time, stop time, tolerance and step size in `modelDescription.xml` |

## Variables

| Term | Meaning |
|---|---|
| **Causality** | A variable's role: `parameter`, `calculatedParameter`, `input`, `output`, `local` or `independent` (time). FMI 3.0 adds `structuralParameter` |
| **Variability** | When a value may change: `constant`, `fixed`, `tunable`, `discrete` or `continuous`. A **fixed** parameter can be set only before initialization; for example, VanDerPol's `mu` can't change mid-run. A **tunable** parameter can change between steps; for example, BouncingBall's `e` (coefficient of restitution) can change during a live run |
| **Initial** | How the start value is used at initialization: `exact`, `approx` or `calculated` |
| **Start value** | A variable's initial value from `modelDescription.xml`, which can be overridden before initialization (`start_values`) |
| **Value reference (VR)** | The integer handle the C API uses to get and set a variable. Tools map names to VRs from `modelDescription.xml` |

## Systems and co-simulation

| Term | Meaning |
|---|---|
| **Master algorithm / orchestrator / importer** | Software that loads FMUs, passes values between connected FMUs and advances time. FMPy runs single FMUs here, and OMSimulator runs systems |
| **Co-simulation step** | A communication (macro) step. Connected FMUs exchange values only at its boundaries |
| **SSP** | System Structure and Parameterization: a Modelica Association standard. An `.ssp` file is a zip of a system description, its FMUs and parameter files |
| **SSD** | The System Structure Description: XML inside an SSP listing components, connectors and connections |
| **SSV** | System Structure parameter Values: a parameter set file. Agentic FMI parameter sets export to SSV |
| **SSM** | System Structure parameter Mapping: maps SSV parameters onto component variables |
| **Container FMU** | An FMU that wraps other FMUs, created with FMPy's `fmpy.container_fmu.cli.create_container_fmu`. Support is partial (FMI 3.0 only, not on the critical path). It's used to derive FMUs with new defaults without touching the original |
| **Reference FMUs** | The Modelica Association's test FMUs (BouncingBall, VanDerPol, Dahlquist, Stair, Feedthrough, …), used as fixtures |

## Claude and MCP

| Term | Meaning |
|---|---|
| **MCP** | The Model Context Protocol, a client-server protocol through which LLM apps use tools. Claude Code is the client, and `afmi mcp` is the server, over stdio |
| **MCP tool** | A named function with a JSON schema that the model can call. Here it returns compact JSON and an optional PNG. Errors for the model are raised as `ToolError` |
| **Plugin** | A Claude Code bundle of skills, agents, hooks and MCP server config, loaded from a marketplace or with `--plugin-dir`. The core plugin is `agentic-fmi` |
| **Skill** | A `SKILL.md` with instructions that Claude loads when relevant, such as `fmi-expert` |
| **Mod / pane** | An early-access Claude Code plugin feature: a TSX module that registers UI (panes, a status line, toasts, commands). Ours is `agentic-fmi-pane`, opened with `/fmi` |

## Project terms

| Term | Meaning |
|---|---|
| **Workspace** | The Claude Code project directory where FMUs are found and the run store lives |
| **Run store** | `<workspace>/.agentic-fmi/`: the file protocol shared by the server, the workers and the pane. See [protocol.md](protocol.md) |
| **Run** | One simulation (`r12`), batch or live (see **Live run**), or one FMU build |
| **Worker** | The isolated process (`afmi worker`) that loads an FMU (or a system) and executes one run |
| **Build job** | A run with `kind: "build"` whose worker runs pythonfmu or `omc` on one source and writes an FMU to `generated/`. Supervised like any run ([ADR 0008](adr/0008-fmu-builds-as-supervised-jobs.md)) |
| **build.json** | The provenance record beside a generated FMU (`generated/<name>/build.json`): source and FMU sha256, tool versions and options |
| **`fmu_built` event** | The event a successful build appends to `events.jsonl` after its FMU is committed to `generated/` |
| **Ninja mode** | A response style (the default; `response_style("talkative")` switches to full summaries): `run_start`, `run_status`, `run_stop`, `run_control` and `fmu_build` return their view without summary, build facts and log tail, with a one-line 🥷 status (for example `🥷 r12 done: BouncingBall, t=3 s.`) as the hint, and Claude replies with that line only. Kept in `preferences.json` in the run store; refusals and the result tools (`run_results`, `run_compare`, `run_plot`) are unchanged |
| **Heartbeat** | The timestamp a worker refreshes in `status.json`. A stale heartbeat means the worker hung or died |
| **Live run** | A run started with `run_start(live=true)` ([ADR 0010](adr/0010-live-runs-and-the-pane.md)): an ordinary run (Co-Simulation only) whose worker starts paused at the start time and then obeys commands while it simulates. Batch runs ignore all of this and run at full speed. State `paused` is active, like `running` |
| **Inbox / command** | `runs/<id>/commands/<ms13>-<server\|pane>-<rand8>.json`: one request per file (`pause`, `resume`, `step`, `set` or `stop`, at most 4 KiB) from `run_control` or the pane. The worker applies each file once, in name order, and ignores other names |
| **Outcome** | `runs/<id>/outcomes/<command id>.json`: the worker's one answer to a command, `applied` or `rejected`, with the simulation time `t` and, for a rejection, the reason. Commands plus outcomes are the run's replay log |
| **Speed factor (pacing)** | How fast a live run goes against the wall clock: 1 (the default) is real time, up to 1000. Set by `resume` with `speed`. Single steps are not paced, and time spent paused or waiting does not count towards `timeout_s` |
| **Idle limit** | `idle_limit_s` in a live run's spec (30 min): a run paused that long without a command is stopped with its partial result, and `status.reason` says so |
| **`live.json`** | The snapshot a live worker keeps up to date: the current value and unit of every recorded scalar output, and the last 500 points of the charted outputs for charts: the first 3 outputs until the pane's chart picker changes the selection (a `chart` command, at most 3 signals). Written at most 5 times a second and only on change; `run_status(values=[...])` and the pane read it |
| **Engine** | A module with one function `simulate(spec, directory, gate) -> EngineResult` (the seam in `engines/base.py`): FMPy for single FMUs, OMSimulator for systems |
| **System file** | A JSON file (version 1) that lists components (FMU paths and parameters), connections from outputs to inputs, and the experiment with the communication step. `system_check` checks it, `run_start(system=...)` runs it (ADR 0009) |
| **SSP / SSD / SSV** | The System Structure and Parameterization standard: an `.ssp` zip holds a `SystemStructure.ssd` (the system) and resources such as FMUs and `.ssv` parameter sets. Agentic FMI imports and exports them; it never runs them directly |
| **Communication step (h)** | The fixed interval at which OMSimulator exchanges values between components (`output_interval` of a system run). Its `oms_ma` master is Jacobi-style, which adds up to a step of delay around a loop. In a live run it is the unit of `step`: `n` steps, or `dt` seconds rounded up to whole steps (for one FMU, the run's `output_interval`) |
