# 0001. MCP server as the integration layer

- Status: Accepted
- Date: 2026-10-09

## Context

Claude needs to operate FMUs: inspect, run, control, compose and generate them. The mature FMI tooling is Python (FMPy, the OMSimulator Python API, pythonfmu). The options were:

- **Tools registered by a Claude Code mod.** This is TypeScript, tied to Claude Code and on an early-access API, and it would still need to call Python.
- **A custom app** wrapping the Claude API. It would rebuild the chat, permissions and UI that Claude Code already provides.
- **Skills plus Bash scripts only.** This produces untyped, verbose output, and native code would run in the shell session.
- **An MCP server.** MCP is the standard tool protocol supported by Claude Code, Claude Desktop and other clients. Prior art (`mcp-fmi`) shows the fit, though it covers only single-FMU basics.

## Decision

The integration layer is a Python MCP server, `afmi mcp`, built on the MCP Python SDK v2:
- It runs over stdio and is launched by the `mcpServers` entry in the core plugin's `.claude-plugin/plugin.json` (not a root `.mcp.json`, which Claude Code would also read as project config when the repo is opened).
- Tool functions are thin adapters over core functions, with typed inputs and outputs from pydantic models.
- Results are compact JSON plus optional PNG images. Errors meant for the model are raised as `ToolError` with actionable text. Phase 1 must confirm the `ToolError` import path in MCP SDK 2.x (mcp 2.3.0 is current).
- Long jobs use start and poll tools (`run_start` with `wait_s`, then `run_status`).

## Consequences

- (+) The server is portable to Claude Desktop and other MCP clients. Only the pane is Claude Code specific.
- (+) It stays in the Python ecosystem, next to FMPy and OMSimulator. Schemas come from pydantic, and tools can be tested in-process with the SDK client.
- (-) Tool output is the only channel to the model, so it needs an output size budget (principle 2).
- (-) Stdout belongs to the MCP protocol. Anything that might print, including FMU native code, must run elsewhere ([ADR 0002](0002-isolated-worker-process-per-run.md)).
- (-) The server can't push to the model, so live updates reach the user through the pane ([ADR 0005](0005-pane-as-separate-early-access-plugin.md)).

## Phase 1 verification (mcp 2.3.0)

The open item above was checked with an in-memory `Client` and a real stdio subprocess. The decision is unchanged.
- **Imports:**
  - `from mcp.server import MCPServer`
  - `from mcp.server.mcpserver import Context, Image`
  - `from mcp.server.mcpserver.exceptions import ToolError`
  - `from mcp import Client, StdioServerParameters`
  - `from mcp.types import CallToolResult, TextContent, ImageContent, ToolAnnotations`
- **Errors:** A `ToolError("msg")` reaches the model as `is_error=True` with the text `Error executing tool <name>: msg`. Any other exception shows only `Error executing tool <name>`, and the traceback is logged to stderr.
- **Return types:**
  - A pydantic return type gives indented JSON text plus `structured_content` and an `outputSchema`. Returning `Annotated[CallToolResult, Model]` keeps the schema and lets us send minified JSON. We use this for the output budget.
  - Returning `Image(data=png, format="png")` gives `image/png` content, no structured content and no output schema.
- **Lifespan:** `MCPServer(name, lifespan=...)`, where the lifespan is an async context manager that yields the app state. Tools read the state through `ctx.request_context.lifespan_context`, and the in-memory `Client` runs the lifespan too.
- **Running:**
  - Sync tools run on worker threads.
  - `server.run("stdio")` blocks until the client disconnects.
  - While serving, stdio points fd 1 at stderr and fd 0 at `/dev/null`, so prints from tools and child processes miss the wire. Output printed before `run()` corrupts it.
