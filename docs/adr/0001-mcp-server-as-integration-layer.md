# 0001. MCP server as the integration layer

**Status:** Accepted · **Date:** 2026-10-09

## Problem

Claude has to inspect, run, build and connect FMUs, and the mature FMI libraries are Python (FMPy, OMSimulator, pythonfmu). Claude needs typed tools with small answers that aren't tied to one app.

## Decision

A Python MCP server, `afmi mcp`, started by the core plugin over stdio, gives Claude its FMI tools.

- Each tool is a thin, typed adapter over a core function.
- Results are compact JSON or a PNG plot. Errors are `ToolError`s that say what to do next.
- Long jobs are started, then polled.

![Any MCP client calls the Agentic FMI MCP server over stdio; the server starts runs and builds that use FMPy, OMSimulator and pythonfmu inside workers.](../diagrams/adr-0001-integration.svg)

## Options considered

| Option | Verdict | Why |
|---|---|---|
| **MCP server in Python** | Chosen | Standard protocol, next to the FMI libraries |
| Tools in a Claude Code mod | Rejected | Early-access TypeScript API that would still call Python |
| A custom app on the Claude API | Rejected | Rebuilds what Claude Code already has |
| Skills plus Bash scripts | Rejected | Untyped output; FMU code would run in the shell |

## Trade-offs

**Gains**
- Works in Claude Desktop and other MCP clients; only the pane is Claude Code specific.
- Schemas come from pydantic, and tools are testable in-process.

**Costs, and how we handle them**
- Tool output is Claude's only view, so tools return summaries and plots, never raw data.
- Stdout belongs to the protocol, so FMU code runs in workers ([0002](0002-isolated-worker-process-per-run.md)).
- The server can't push updates; the pane shows live progress ([0005](0005-pane-as-separate-early-access-plugin.md)).

<details>
<summary><b>Details:</b> launch, and the MCP SDK 2.3 behaviour the server relies on</summary>

- **Launch.** The `mcpServers` entry in `.claude-plugin/plugin.json` starts the server, not a root `.mcp.json`, which Claude Code would also read as project configuration when the repository is opened.
- **Imports** (checked against mcp 2.3.0): `from mcp.server import MCPServer`, `from mcp.server.mcpserver import Context, Image`, `from mcp.server.mcpserver.exceptions import ToolError`, `from mcp import Client, StdioServerParameters`, `from mcp.types import CallToolResult, TextContent, ImageContent, ToolAnnotations`.
- **Errors.** A `ToolError("msg")` reaches Claude as `is_error=True` with `Error executing tool <name>: msg`. Any other exception shows only `Error executing tool <name>`; its traceback goes to stderr.
- **Return types.** A pydantic return type gives indented JSON plus `structured_content` and an `outputSchema`. `Annotated[CallToolResult, Model]` keeps the schema and sends minified JSON, which the output budget uses. `Image(data=png, format="png")` gives `image/png` content without a schema.
- **Long jobs.** `run_start` waits up to `wait_s`; `run_status` takes over after that.
- **Lifespan.** `MCPServer(name, lifespan=...)` takes an async context manager that yields the app state; tools read it through `ctx.request_context.lifespan_context`. The in-memory `Client` runs the lifespan too.
- **Running.** Sync tools run on worker threads. While serving stdio, fd 1 points at stderr and fd 0 at `/dev/null`, so prints from tools and child processes miss the wire; anything printed before `run()` corrupts it.

</details>
