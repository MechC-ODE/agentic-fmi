# 0005. Pane as a separate early-access plugin

- Status: Accepted. Amended by [0010](0010-live-runs-and-the-pane.md): the pane reads no `events.jsonl` (toasts come from `index.json` and `status.json` transitions and from command outcomes), and it reads `spec.json`, `build.json` and `outcomes/` besides the files named below.
- Date: 2026-10-09

## Context

Users want to watch and steer simulations live, with progress, values, charts and buttons, without asking Claude for every update. Claude Code's mod API provides what's needed:
- A pane, a status line, toasts and commands.
- `Raster` and `Svg` drawing.
- `$.fs` and `$.clock` polling.

But the API is early access and may change. Other MCP clients, such as Claude Desktop, don't have it.

## Decision

The pane is a separate plugin, `agentic-fmi-pane`, in `pane/`, listed in the same marketplace as the core plugin `agentic-fmi`.
- It's a thin layer over the run-store protocol ([ADR 0003](0003-file-based-run-store-protocol.md)). It reads `index.json`, `status.json`, `live.json` and `events.jsonl`, and writes command files.
- It never calls Python or the MCP server, and holds no business logic. Metrics and summaries are computed in Python.
- It's opened with `/fmi`. Every pane action has an equivalent MCP tool, so the core plugin works fully without it.
- The pane never wakes Claude by itself; it only reads the store and writes commands.

## Consequences

- (+) Mod API churn is contained in `pane/`. The core stays portable and stable.
- (+) It's testable in isolation with `claude plugin test pane` and fixture run stores, on the `terminal` and `desktop` surfaces.
- (-) A small TypeScript protocol reader (`pane/hooks/protocol.ts`) duplicates the pydantic models. It must track the protocol version, and the Phase 1 generated schemas help here.
- (-) Users install two plugins to get the live experience.
- (-) Editing the pane over a Windows bind mount may not hot-reload, so run `/reload-plugins`.
