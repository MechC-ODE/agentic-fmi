# Architecture decision records

An ADR records one significant decision: its context, the decision itself, and its consequences. ADRs use a lightweight MADR format and stay short (under about 40 lines).

## Index

| ADR | Title | Status |
|---|---|---|
| [0001](0001-mcp-server-as-integration-layer.md) | MCP server as the integration layer | Accepted |
| [0002](0002-isolated-worker-process-per-run.md) | Isolated worker process per run or session | Accepted |
| [0003](0003-file-based-run-store-protocol.md) | File-based, versioned run-store protocol | Accepted |
| [0004](0004-omsimulator-as-multi-fmu-orchestrator.md) | OMSimulator as the multi-FMU orchestrator | Accepted |
| [0005](0005-pane-as-separate-early-access-plugin.md) | Pane as a separate early-access plugin | Accepted (amended by 0010) |
| [0006](0006-dedicated-dev-container-ubuntu-24-04.md) | Dedicated dev container on Ubuntu 24.04 | Accepted |
| [0007](0007-run-store-location-ids-and-supervision.md) | Run-store location, run IDs and worker supervision | Accepted |
| [0008](0008-fmu-builds-as-supervised-jobs.md) | FMU builds: one fmu_build tool, supervised build jobs, build.json provenance | Accepted |
| [0009](0009-systems-with-omsimulator.md) | Systems with OMSimulator: one run path, SSP and SSV as exchange formats, a pinned install | Accepted (amended by 0010) |
| [0010](0010-live-runs-and-the-pane.md) | Live runs and the pane: ordinary runs with a gate, a command inbox and live.json | Accepted |

## Rules

- Number new ADRs sequentially: `NNNN-short-title.md`. Add each one to the index above and, if it shapes the architecture, to [../architecture.md](../architecture.md#key-decisions).
- The statuses are `Proposed`, `Accepted`, `Superseded by NNNN` and `Deprecated`.
- Once an ADR is accepted, don't rewrite its decision. Write a new ADR that supersedes it, and update the old one's status.
- Write one ADR per significant decision: one that is hard to reverse, shapes interfaces, or picks between real alternatives.

## Template

```markdown
# NNNN. Title in the imperative or as a noun phrase

- Status: Proposed | Accepted | Superseded by NNNN | Deprecated
- Date: YYYY-MM-DD

## Context

What forces are at play? Which options were considered?

## Decision

What we do, stated plainly.

## Consequences

What gets easier (+) and what gets harder or riskier (-). Include follow-ups.
```
