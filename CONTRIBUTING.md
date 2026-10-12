# Contributing

Thanks for helping. The project is young, so expect interfaces to move. Read [docs/architecture.md](docs/architecture.md) and [docs/codebase-map.md](docs/codebase-map.md) first. They take about 15 minutes.

## License of contributions

Agentic FMI is licensed under the [PolyForm Noncommercial License 1.0.0](LICENSE), and the copyright holder also offers commercial licenses. By submitting a contribution you agree that it is licensed under the project's license and that the copyright holder may include it in commercially licensed versions. If that doesn't suit you, say so in your pull request before it is merged.

## Dev setup

Everything runs in the dev container, and you need only Docker Desktop and Git on the host.

```bash
docker compose up -d --build
docker exec -it agentic-fmi-dev bash          # a shell in /workspace as user dev
uv sync                                       # inside the container
uv run pytest -q -m "not omc"
```

In Git Bash on Windows, prefix one-off `docker exec -w ...` commands with `MSYS_NO_PATHCONV=1`. [docs/development.md](docs/development.md) covers volumes, Claude Code login, the Reference FMUs and Windows quirks.

## Workflow

1. Open or pick an issue, and agree on the approach for anything non-trivial. Record a significant decision as an ADR in [docs/adr/](docs/adr/README.md).
2. Branch from `main`, for example `feat/store-atomic-writes`.
3. Make one focused change, with its tests and docs. Keep MCP tools thin and put logic in core functions.
4. Run the checks in the container (see the checklist).
5. Open a pull request that describes what changed, why, and how you tested it.

## Code conventions

- Python 3.11+ syntax (ruff targets py311; the dev container uses 3.12). Type hints on every function; mypy passes on `src/`.
- Every module starts with a docstring stating its purpose and its collaborators.
- Keep it simple: SOLID where it helps, KISS and YAGNI always.
  - The one real seam is the engine signature `simulate(spec, directory, gate) -> EngineResult` in `engines/base.py`, implemented by FMPy and OMSimulator ([ADR 0009](docs/adr/0009-systems-with-omsimulator.md)).
  - A new interface needs two real implementations.
  - Plain functions for stateless logic; pydantic models or dataclasses for data.
  - No dependency-injection framework, registry or class hierarchy for its own sake.
  - Configuration comes only from `config.py`. No global state.
- MCP tool functions are thin adapters over core functions. Errors meant for the model raise `ToolError` with actionable text: what failed, why, and what to do next.
- The MCP server never loads FMU binaries: it parses `modelDescription.xml` only, and workers execute.
- Input FMUs are never modified; derived artifacts go to new files.
- Tools return summaries, metrics and PNG plots, never raw time series, within the output size budget.
- Every run records its spec, the FMU's sha256 and the tool versions.
- Run-store changes follow the versioning rule in [docs/protocol.md](docs/protocol.md).
- Tools never download anything. Never commit secrets or binary FMUs.

## Commit style

Use [Conventional Commits](https://www.conventionalcommits.org/): `type(scope): summary`, in the imperative and under about 72 characters.

- Types: `feat`, `fix`, `docs`, `test`, `refactor`, `build`, `ci`, `chore`.
- Scopes: the area changed, such as `cli`, `fmu`, `store`, `engines`, `worker`, `mcp`, `pane`, `system`, `devcontainer`, `scripts`, `adr`.

Example: `fix(worker): mark run failed when heartbeat goes stale`.

Commit from the host's git, where line endings are normalized to LF by `.gitattributes`.

## Pull request checklist

- [ ] Tests added or updated, and `uv run pytest -q -m "not omc"` passes in the container.
- [ ] `uv run ruff check .`, `uv run ruff format --check .` and `uv run mypy` are clean.
- [ ] New modules have a docstring stating their purpose and collaborators.
- [ ] Docs updated: [docs/codebase-map.md](docs/codebase-map.md), plus architecture, protocol and testing docs if affected.
- [ ] Diagram changes: edit the `.mmd` in `docs/diagrams/`, run `scripts/render-diagrams.sh` and commit the `.mmd` and the `.svg` together ([docs/development.md](docs/development.md#diagrams)).
- [ ] `CHANGELOG.md` has an entry under `Unreleased`.
- [ ] No FMU binaries, secrets or generated files committed, and no input FMU modified.
- [ ] Run-store changes follow the versioning rule in [docs/protocol.md](docs/protocol.md).
