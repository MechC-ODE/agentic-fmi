# Development

All development happens in the dev container `agentic-fmi-dev` ([ADR 0006](adr/0006-dedicated-dev-container-ubuntu-24-04.md)). The host needs only Docker Desktop and Git.

## The container

**Image** (`.devcontainer/Dockerfile`, built by `compose.yaml`):

| Component | Version or source |
|---|---|
| Base | `ubuntu:24.04` |
| Node | 24, copied from `node:24-bookworm-slim` (no apt repo) |
| Claude Code | `npm install -g @anthropic-ai/claude-code@${CLAUDE_CODE_VERSION}` at build time (pinned by `ARG CLAUDE_CODE_VERSION`, currently 2.1.295) |
| uv | 0.11.14, copied from `ghcr.io/astral-sh/uv` |
| Python | 3.12 (3.12.13), installed by uv for user `dev`. `UV_PYTHON_PREFERENCE=only-managed`, so there's no system Python |
| Tools | git, tmux, build-essential, cmake, pkg-config, curl, ca-certificates, less, nano |
| User | `dev` (uid and gid 1000, non-root). `/workspace` is a git `safe.directory` |
| OpenModelica | `omc` 1.27.1~2-g6db4671 and `omlibrary` (Modelica Standard Library 3.2.3, 4.0.0 and 4.1.0), installed with `--no-install-recommends` from the official apt repo `build.openmodelica.org/apt noble stable` and pinned by `ARG OPENMODELICA_VERSION`. Built by default; see below |

The image also sets these environment variables:
- `CLAUDE_CONFIG_DIR=/home/dev/.claude`
- `UV_PROJECT_ENVIRONMENT=/home/dev/.venvs/agentic-fmi`
- `UV_LINK_MODE=copy`

**OpenModelica.** It generates FMUs from Modelica source. It's installed now, and `fmu_build` uses it in Phase 2; see spike (f) in [spikes.md](spikes.md).
- **Build argument.** `WITH_OPENMODELICA` is `1` by default. `WITH_OPENMODELICA=0 docker compose build` skips the whole layer, and `compose.yaml` passes the variable on as a build arg. Without it the image is about 0.6 GB smaller and the OpenModelica tests and tools have no `omc`.
- **Size.** The image grows from 1.74 GB to 2.34 GB on disk (content size 520 MB to 692 MB). The layer downloads about 135 MB: `omc` pulls in clang, BLAS/LAPACK and `unzip`, and `omlibrary` is 53 MB. Rebuilds re-download it, because the layer is rebuilt whenever it or an earlier layer changes.
- **Version pin.** `ARG OPENMODELICA_VERSION` (currently `1.27.1~2-g6db4671-1`) is passed to apt as `omc=<version> omlibrary=<version>`. The apt repo may stop offering an old build once a newer one is published. The image build then fails with an apt "version not found" error, and the fix is to bump the argument deliberately.
- **Repository key.** The Dockerfile saves the repo key as `/usr/share/keyrings/openmodelica-keyring.gpg` and prints its fingerprints to the build log. They were `D229AF1CE5AED74E5F59DF303A59B53664970947` and `5DE86CC050F6623BBA7995B864CE41328E03B30A`. They are **not pinned or checked**, because this task found no authoritative published fingerprint and downloading one wasn't approved. If you can confirm them from an independent source, add a check to the Dockerfile.
- **Architecture.** The apt line is `amd64` only, so the layer is for x86_64 hosts.
- **Use.** `omc` writes into `~/.openmodelica` (a writable `HOME` is required) and copies the library cache there on the first run (154 MB, about 7 s). That directory isn't in a volume, so a recreated container repeats the copy.

**Mounts.** The compose project is named `agentic-fmi`, so the volumes are named `agentic-fmi_<name>`.

| Mount point | Source | Persists |
|---|---|---|
| `/workspace` | Bind mount of the repo (`.`) | Your source files, on the host |
| `/home/dev/.claude` | Volume `claude-home` | The Claude Code login, settings, history and plugin data |
| `/home/dev/.venvs` | Volume `venv` | The project venv `/home/dev/.venvs/agentic-fmi` |
| `/home/dev/.cache/uv` | Volume `uv-cache` | Downloaded wheels, so re-syncs are fast |
| `/lab` | Volume `lab` | A fast scratch workspace for demos and end-to-end sessions, including their run stores |

**Why the venv lives in a volume.**
- Bind mounts from Windows are slow for the many small files a venv holds. A volume stays on the Linux filesystem.
- A Linux venv inside the repo would also confuse Windows tools.
- `UV_LINK_MODE=copy` is needed because the cache and the venv sit on different volumes, so hard links aren't possible.

## First-time setup

From the repo root on the host:

```bash
docker compose up -d --build
docker exec -it agentic-fmi-dev claude      # log in once, then /exit; the login persists in claude-home
MSYS_NO_PATHCONV=1 docker exec -w /workspace agentic-fmi-dev bash -lc 'uv sync'
```

`uv sync` installs the core dependencies (pythonfmu and OMSimulator among them) and the `dev` group. After changing `pyproject.toml`, run `uv lock` and then `uv sync --locked`. There are no extras.

OMSimulator is an sdist whose build downloads its binaries and Python code from `build.openmodelica.org` without a hash and without TLS verification. So the first sync needs network access, and it is slow. **Pre-sync before the first start**: run `uv sync --locked` (dev container) or start the launcher once by hand (`scripts/afmi-mcp <workspace>`, plugin install), so the plugin's first start doesn't spend a minute building OMSimulator. Later syncs reuse the uv cache (`uv sync --locked --offline` works without network). Agentic FMI then pins what was installed: system runs are refused unless the installed `OMSimulator/` package tree hashes to `config.OMSIMULATOR_PIN` ([ADR 0009](adr/0009-systems-with-omsimulator.md)). The pin matches the Ubuntu 24.04 build. On another distribution the sdist fetches another tarball, and system runs stay refused until the pin is reviewed and updated.

VS Code users can choose **Dev Containers: Reopen in Container**, which uses `.devcontainer/devcontainer.json`.

## Rebuilding and resetting

| Goal | Command (host) |
|---|---|
| Rebuild after changing the Dockerfile | `docker compose up -d --build` (volumes are kept) |
| Pick up a newer Claude Code | `docker compose build --no-cache && docker compose up -d` |
| Build the image without OpenModelica | `WITH_OPENMODELICA=0 docker compose build && docker compose up -d` |
| Reset the venv, e.g. after a Python patch change | `docker compose down && docker volume rm agentic-fmi_venv && docker compose up -d`, then `uv sync` |
| Clear the uv cache | Remove `agentic-fmi_uv-cache` the same way |
| Reset everything | `docker compose down -v`. This also deletes `claude-home` (you'll log in again) and `lab` |

## Windows and Git Bash notes

- **Path conversion:** Git Bash rewrites arguments that look like POSIX paths, so `/workspace` turns into `C:/Program Files/Git/workspace`. Prefix `docker exec` with `MSYS_NO_PATHCONV=1`. PowerShell doesn't need this.
- **TTY:** if `docker exec -it` reports "the input device is not a TTY" in mintty, use `winpty docker exec -it ...` or run it from Windows Terminal or PowerShell.
- **Path with a space:** the repo is at `C:\Development\Agentic FMI`, so quote it (`cd "/c/Development/Agentic FMI"`). Inside the container it's `/workspace`, and the compose project name is fixed, so the space doesn't leak into container or volume names.
- **Line endings:** `.gitattributes` forces LF. Keep your editor on LF, because a CRLF shell script fails in Linux with `bash\r: No such file or directory`.
- **Hot reload:** file-change events from Windows bind mounts may not reach the container. After editing plugin files, run `/reload-plugins` in Claude Code. For faster I/O, run demos in `/lab`.
- **Git:** commit from the host's git. The container can read the repo (it's a `safe.directory`), but host commits avoid ownership and line-ending surprises.

## Running the CLI

```bash
uv run afmi --version
uv run afmi worker .agentic-fmi/runs/<id>   # run one queued run (the server spawns this; see below)
```

`afmi mcp --workspace DIR` runs the MCP server over stdio (Claude Code starts it through `scripts/afmi-mcp`). `afmi worker RUN_DIR` is internal: the supervisor starts it for each run. No other subcommands are planned for now.

To load the core plugin into the container's Claude Code (the [user guide](user-guide.md) describes what to try):

```bash
cd /lab && claude --plugin-dir /workspace
claude plugin validate /workspace          # check the manifests, and the skills with: claude plugin validate /workspace/skills
```

The plugin starts the MCP server through `scripts/afmi-mcp`. Run it by hand to see its stderr: `CLAUDE_PLUGIN_ROOT=/workspace sh /workspace/scripts/afmi-mcp /lab </dev/null`.

The live pane is a second, optional plugin (`agentic-fmi-pane`, early access) that depends on the first. Load both with `cd /lab && claude --plugin-dir /workspace --plugin-dir /workspace/pane`, then type `/fmi`. See [Working on the pane](#working-on-the-pane).

## Reference FMUs

```bash
uv run python scripts/fetch-reference-fmus.py       # inside the container; ask before downloading
```

- The script downloads Modelica Reference FMUs **v0.0.41** and verifies the pinned size and SHA-256.
- It extracts to `.cache/reference-fmus/0.0.41/`, with FMUs in `2.0/` and `3.0/` (for example `3.0/BouncingBall.fmu`).
- A `.complete` marker makes reruns a no-op. Use `--dest DIR` to extract elsewhere.
- The script uses only the standard library.
- The cache is gitignored, and tests never download (see [testing.md](testing.md)).

## Diagrams

The diagrams in [architecture.md](architecture.md) and [protocol.md](protocol.md) are committed SVGs rendered from Mermaid sources, because VS Code's Markdown preview and many other viewers don't render Mermaid code blocks.

1. Edit the source `docs/diagrams/<name>.mmd`.
2. Run `sh scripts/render-diagrams.sh` **on the host** (Git Bash, Linux or macOS). The dev container has no Docker CLI.
3. Commit the `.mmd` and its `.svg` together.

How it works:
- The script runs the pinned image `minlag/mermaid-cli:11.16.0` (about 600 MB compressed) with `docker run`. It stops if the image isn't present, so nothing large is downloaded by surprise; run it once with `--pull` to fetch the image.
- The shared look is `docs/diagrams/mermaid-config.json`: the neutral theme, `htmlLabels: false` (text is plain SVG, so the files render the same in an `<img>`), the Arial font, and a fixed `handDrawnSeed`, so rendering twice gives identical files. The background is white, so the diagrams stay readable in dark themes.
- Dashed outlines and arrows mark planned parts, and an orange box marks code that runs FMU native code.

Look at a diagram before committing it. `sh scripts/render-diagrams.sh --png .cache/diagrams-check` also writes PNGs (the folder is gitignored), which you can open. Check for overlapping labels, crossing edges and text that doesn't fit its box. A few layout rules from experience:
- Keep labels short and break lines with `<br/>`. In sequence diagrams every long message widens the whole diagram.
- Mermaid flips the inner direction of a subgraph that is laid out on its own, that is, one where no node inside has an edge to the outside. Give it an explicit `direction TB`.
- A node order that crosses edges can often be fixed by reordering the edge lines.

## Debugging tips

- **Python:** `uv run pytest -x --pdb`, `breakpoint()`, and `uv run python -X dev` for extra warnings.
- **MCP server**: stdout is the MCP channel, so never `print()` in the server. Log to stderr instead. In Claude Code, `/mcp` shows the server status.
- **Worker**: see [Debugging a worker](#debugging-a-worker) below, and [Debugging a live run](#debugging-a-live-run) for the command inbox.
- **Pane**: see [Working on the pane](#working-on-the-pane).

### Debugging a worker

Each run lives in `.agentic-fmi/runs/<id>/` (or under `$AFMI_STORE_DIR`):

| File | What to look for |
|---|---|
| `status.json` | `state`, `t` and `progress`, `heartbeat_at` (rewritten about every 0.5 s while the worker lives, even when `t` is frozen in a long native step), `pid` and `pid_start_time`, and `error` (`code`, a one-line `message`, the last 2 KB of `log.txt` in `log_tail`) |
| `log.txt` | The worker's stdout and stderr: the FMU's own log messages (`[ERROR] ...`), then the full Python traceback of a failure. Status never holds a traceback |
| `spec.json` | Everything the worker ran: FMU path and sha256, `fmi_type`, the resolved experiment, start values, inputs, outputs, `timeout_s` |
| `fmu/` | The extracted FMU, present only while the worker runs. One left behind means the worker was SIGKILLed |
| `../../events.jsonl` | `started`, then `finished`, `stopped` or `failed`, with `source: "worker"` |

To reproduce a run outside Claude, copy its directory, set the copy back to `queued`, and run the worker on it by hand. The worker only starts on a `queued` run and exits silently otherwise:

```bash
cp -r .agentic-fmi/runs/r7 .agentic-fmi/runs/r900
uv run python - <<'EOF'
from pathlib import Path
from agentic_fmi.store import runstore
from agentic_fmi.store.protocol import RunState, RunStatus, utc_now
run = Path(".agentic-fmi/runs/r900")
old = runstore.read_status(run)
runstore.write_status(run, RunStatus(run_id="r900", state=RunState.QUEUED, t_start=old.t_start,
                                     t_stop=old.t_stop, updated_at=utc_now()))
EOF
uv run afmi worker .agentic-fmi/runs/r900; echo "exit $?"   # FMU output appears in the terminal
```

Notes:
- The copy keeps the original `run_id` in `spec.json`, which stays immutable. The worker takes the `run_id` for status, summary and events from `status.json`, so set it to the new directory name as above.
- Exit code 0 means the outcome was recorded, including `failed`. Exit code 2 means `status.json` couldn't be read or written.
- Ctrl+C (SIGINT) stops the run like `run_stop` does: the worker writes `stopped` with the partial result.
- On timeout the worker writes `failed{timeout}`, removes `fmu/`, and ends itself. Spawned by the server, it leads its own process group and SIGKILLs it. Run by hand under `uv run` or a shell job, it isn't the group leader and calls `os._exit` instead, so the shell survives.
- The FMU is re-hashed at start. If you edited it since the run was created, you get `failed{fmu_changed}`.

### Debugging a live run

A live run ([ADR 0010](adr/0010-live-runs-and-the-pane.md)) is an ordinary run directory plus three more things. The commands and their outcomes are the run's replay log:

| File | What to look for |
|---|---|
| `status.json` | The worker's state (`paused` while the gate waits), `speed` (the pacing factor, 1 is real time), `last_cmd` (the ID of the last command the worker handled; a display field only), `reason` (why the run ended: the idle limit, or "ended early at t=..." when the FMU asked to terminate), and `heartbeat_mono` (the worker's monotonic clock, which the supervisor trusts over `heartbeat_at`) |
| `commands/<ms13>-<server\|pane>-<rand8>.json` | One `Command` per file (`pause`, `resume`, `step`, `set`, `stop`), at most 4 KiB. The worker applies each file once, in name order; other names are ignored. A file it can't parse is retried for 2 s and then rejected |
| `outcomes/<id>.json` | The worker's answer to each command: `status` (`applied` or `rejected`), the simulation time `t`, and `message` (why it was rejected, or a note). One file per command, never rewritten. A run killed by its timeout gets no final sweep, so its pending commands have no outcome |
| `live.json` | The current value and unit of every recorded scalar output at `t`, and the last 500 points of the charted outputs (the chart selection: the first 3 until a `chart` command from the pane changes it). Replaced at most 5 times a second and only on change; `seq` grows with every write |
| `../../events.jsonl` | A `command` event per handled command, next to `started` and the terminal event |

To see what a stuck live run is doing, read `status.json` and the newest files of `commands/` and `outcomes/`. A command without an outcome is either still waiting (the worker polls every 0.1 s while stepping and every 50 ms while paused) or the worker is gone: check `heartbeat_mono` and `ps -eo pid,cmd | grep "agentic_fmi worker"`. To send a command by hand, write it under a temporary name that doesn't match the pattern and rename it, so the worker never sees a half-written file:

```bash
cd .agentic-fmi/runs/r8
id=$(date +%s%3N)-server-00000000
echo "{\"protocol\":1,\"id\":\"$id\",\"source\":\"server\",\"issued_at\":\"$(date -u +%Y-%m-%dT%H:%M:%SZ)\",\"action\":\"step\",\"n\":1}" > commands/.tmp
mv commands/.tmp commands/$id.json; sleep 1; cat outcomes/$id.json
```

Notes:
- A live run waits at its start time: `run_start(live=true)` returns once `state` is `paused`. Time spent waiting does not count towards `timeout_s`, but a run paused longer than `idle_limit_s` (in `spec.json`, 30 min) without a command is stopped with its partial result.
- `live` runs count towards `max_workers` until they end. A forgotten paused run holds a slot: stop it with `run_stop`.
- The live tests (`tests/live/`) build a small FMU that discards steps with `gcc` (the "Discarder"). The dev container has `gcc` through `build-essential`; elsewhere those tests skip with a message.

### Working on the pane

The pane lives in `pane/` (plugin `agentic-fmi-pane`, TypeScript modules loaded by Claude Code; see [ADR 0010](adr/0010-live-runs-and-the-pane.md) and [ADR 0005](adr/0005-pane-as-separate-early-access-plugin.md)). It reads the run store and writes only command files.

```bash
claude plugin validate pane       # manifest, the $.state contract, the hooks module and its `$` rule
claude plugin test pane           # the pane suite (mounts the pane on both surfaces, no login needed)
```

- **Tests are the only check.** The container has no `tsc`, so types in `pane/types/index.d.ts` are not compiled: a type error shows up only when a test runs that line. Cover every branch you add. An editor that reads `pane/tsconfig.json` (VS Code) shows type errors once Claude Code has loaded the plugin, which writes the typings it extends to `pane/.claude-plugin/types/`.
- **Fixtures.** `pane/fixtures/store.ts` is generated from the pydantic models: change the protocol, then run `uv run python scripts/pane_fixtures.py` and commit the result (`--check` exits 1 if it is stale; `tests/store/test_pane_fixtures.py` is the drift test). Never edit it by hand.
- **Hot reload.** After editing pane files, run `/reload-plugins`, then type `/fmi` again: the pane registers its command and starts polling on `session.start`, which a reload does not fire again.
- **By eye.** The tests cannot show how the pane looks. Load both plugins (see [the user guide](user-guide.md#5-control-a-run-live-and-watch-it-in-the-pane)) and run a live run; check the terminal and the desktop surface if you can.
- **Rules to remember.** `$` is passed only to functions declared at the top of `register.tsx`; `Raster` draws on the terminal only and `Svg` elsewhere; `$.fs.write` is not atomic, so the pane writes each command file once and never rewrites it. If the pane shows nothing, check that the session was launched in the folder that holds `.agentic-fmi/` (or set `AFMI_STORE_DIR`).

### Supervision and stray workers

The server's `Supervisor` (`supervisor.py`) has no background thread. It checks a run whenever a tool asks (`run_status`, `run_list`, `run_start`'s wait) and once at server startup:
- A `running` or `paused` run whose pid is gone, a zombie, or now another process (start time differs), or whose heartbeat is older than 5 s (judged on the monotonic `heartbeat_mono` when the status has it, else on `heartbeat_at`), becomes `failed{worker_lost}`. The server first SIGKILLs the worker's process group, reaps it and confirms it is gone. It removes `fmu/` and attaches the tail of `log.txt`.
- A `queued` run becomes `failed{worker_lost}` ("worker never started") after 15 s, or at once if the worker this server spawned has already exited.
- A pid is only signalled while it is alive with the recorded `pid_start_time`. The server never signals itself or its own group, so a status that names it stays untouched.

To look at workers in the container, and to apply the rules by hand:

```bash
ps -eo pid,pgid,stat,lstart,cmd | grep "agentic_fmi worker"   # STAT T = SIGSTOPped, Z = zombie
uv run python - <<'PY'
from pathlib import Path
from agentic_fmi.config import load_config
from agentic_fmi.supervisor import Supervisor
print(Supervisor(load_config(Path("."))).reconcile("r7"))   # or .stop("r7")
PY
```

Notes:
- A worker you started by hand isn't a group leader, so the supervisor signals its pid alone.
- Two servers can share one store. Each can take over or stop a worker the other spawned, using only `status.json` and `/proc`. The terminal write happens once, under the store lock.
- The tests in `tests/supervisor/` kill every worker they start, including ones adopted by init. After a run, `ps -eo pid,cmd` should show no `agentic_fmi worker` process.
