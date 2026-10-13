# Architecture decision records

An ADR records one significant decision: the problem, what we decided, what else we considered, and what the decision costs. Each one is short enough to read in a few minutes; the implementation details sit in a collapsed **Details** section at the end.

## Index

| ADR | Decision in one line | Status |
|---|---|---|
| [0001](0001-mcp-server-as-integration-layer.md) | Claude gets its FMI tools from a Python MCP server, so they work in any MCP client | Accepted |
| [0002](0002-isolated-worker-process-per-run.md) | Every run's FMU code runs in its own worker process, never in the server | Accepted |
| [0003](0003-file-based-run-store-protocol.md) | Server, workers and pane share state only through versioned plain files | Accepted |
| [0004](0004-omsimulator-as-multi-fmu-orchestrator.md) | OMSimulator 3 runs connected FMUs; FMPy runs single FMUs | Accepted, partly superseded by 0009 |
| [0005](0005-pane-as-separate-early-access-plugin.md) | The live pane is a separate, optional plugin that only reads the store and writes commands | Accepted, amended by 0010 |
| [0006](0006-dedicated-dev-container-ubuntu-24-04.md) | Development happens in a dedicated Ubuntu 24.04 container | Accepted |
| [0007](0007-run-store-location-ids-and-supervision.md) | Where the store lives, how run IDs are claimed, and how dead workers are detected and recorded | Accepted |
| [0008](0008-fmu-builds-as-supervised-jobs.md) | One `fmu_build` tool; every build is a supervised job with provenance | Accepted |
| [0009](0009-systems-with-omsimulator.md) | Systems run one way, through OMSimulator's API; SSP and SSV are only for exchange; the install is pinned | Accepted, amended by 0010 |
| [0010](0010-live-runs-and-the-pane.md) | Live runs are ordinary runs steered through command files, drawn from `live.json` | Accepted |

## Rules

- Number new ADRs sequentially: `NNNN-short-title.md`. Add each one to the index above and, if it shapes the architecture, to [../architecture.md](../architecture.md#key-decisions).
- The statuses are `Proposed`, `Accepted`, `Superseded by NNNN` and `Deprecated`.
- Once an ADR is accepted, don't change its decision. Write a new ADR that supersedes or amends it, and update the old one's status. Editing for clarity is fine.
- Write one ADR per significant decision: one that is hard to reverse, shapes interfaces, or picks between real alternatives.
- Prefer a diagram or a table to a paragraph. Diagram sources live in [../diagrams/](../diagrams/) as `adr-NNNN-*.mmd`; render them with `scripts/render-diagrams.sh` and commit the `.mmd` and `.svg` together.

## Template

```markdown
# NNNN. Title in the imperative or as a noun phrase

**Status:** Proposed | Accepted | Superseded by NNNN | Deprecated · **Date:** YYYY-MM-DD

## Problem

What goes wrong if we do nothing, and the facts that constrain the answer. A few sentences.

## Decision

What we do, stated plainly in a short list, with a diagram where it helps.

## Options considered

| Option | Verdict | Why |
|---|---|---|
| **The chosen option** | Chosen | ... |
| An alternative | Rejected | ... |

## Trade-offs

**Gains**
- ...

**Costs, and how we handle them**
- ...

<details>
<summary><b>Details:</b> what an implementer needs</summary>

Exact rules, limits and edge cases.

</details>
```
