# 0003. File-based, versioned run-store protocol

**Status:** Accepted · **Date:** 2026-10-09

## Problem

The server, the workers and the pane (TypeScript inside Claude Code) all share each run's state. The pane's early-access API can only read and write files and poll a timer, and the state must survive a restart of any of the three.

## Decision

Versioned plain files under `<workspace>/.agentic-fmi/` are the only contract between them.

- JSON, JSONL, CSV and PNG: an index, an event log, and one folder per run (the diagram shows who writes what).
- Every document carries `"protocol": 1`. Additive changes keep the version; readers ignore unknown fields.
- One writer per file at a time, atomic writes (temporary file, then `os.replace`), unique command file names.
- Schemas are generated from the pydantic models; [protocol.md](../protocol.md) has every format.

![Who writes which file: the MCP server writes the index, events, spec and command files; the pane writes command files; the worker writes the status, live values, log, results and command outcomes. The server writes status only for a queued run and for the verdict after a lost worker.](../diagrams/adr-0003-store.svg)

## Options considered

| Option | Verdict | Why |
|---|---|---|
| **Plain files** | Chosen | Fits the pane's API, survives restarts, readable with `cat` |
| Sockets or HTTP | Rejected | A server in every worker, ports, reconnects |
| SQLite | Rejected | Hard to read from the pane; unreliable locks on Windows mounts |
| MCP notifications | Rejected | Don't reach the pane or survive restarts |

## Trade-offs

**Gains**
- Inspectable with `ls`, `cat` and `jq`; tests use fixture folders.
- Survives restarts; no network code in the pane.
- The files are the reproducibility record.

**Costs, and how we handle them**
- A few hundred milliseconds of latency, so tight control loops stay inside a co-simulation.
- Slow on Windows bind mounts: keep the store on a Linux filesystem, such as `/lab`.
- The store grows; a cleanup tool will be needed.
