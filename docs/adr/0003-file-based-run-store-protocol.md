# 0003. File-based, versioned run-store protocol

- Status: Accepted
- Date: 2026-10-09

## Context

Three parties share run state: the MCP server (Python), the workers (Python, separate processes) and the pane (TypeScript inside Claude Code). The pane's early-access mod API can read and write files (`$.fs`) and poll on a timer (`$.clock`). We don't want to rely on sockets there. The options were:

- Local sockets or HTTP from the workers. These need a server in every worker, port management and a reconnection story.
- SQLite. It's awkward to read from the pane, and locking over Windows bind mounts is unreliable.
- MCP notifications. These reach the client, not the pane, and not across restarts.
- Plain files.

## Decision

A versioned file protocol under `<workspace>/.agentic-fmi/` is the only contract between Python and the pane, and between the server and the workers. It consists of JSON, JSONL, CSV and PNG files:
- `index.json` and `events.jsonl`.
- Per run in `runs/<id>/`: `spec.json`, `status.json`, `summary.json`, `result.csv`, `log.txt`, `live.json`, `plots/` and a `commands/` inbox.

Every document carries `"protocol": 1`. Additive changes keep the version, breaking changes bump it, and readers ignore unknown fields. Writes are atomic (a temporary file plus `os.replace`). [docs/protocol.md](../protocol.md) has the details. Phase 1 generates JSON Schemas from the pydantic models.

## Consequences

- (+) The store is debuggable with `ls`, `cat` and `jq`, and tests can use fixture directories.
- (+) It survives restarts of Claude Code, the MCP server or the pane.
- (+) It works with `$.fs` polling, with no network code in the pane.
- (+) The files double as the reproducibility record (spec, results, logs).
- (-) Latency is roughly 100s of ms (polling, `live.json` at most 5 Hz). Tight control loops belong inside a co-simulation, not across this protocol.
- (-) Bind mounts from Windows are slower and may not deliver change events, so demos run in the `/lab` volume.
- (-) Concurrency rules are needed: a single writer per file, atomic replace, tolerant readers and a scheme for command file names.
- (-) The store grows on disk, so a cleanup tool is needed later.
