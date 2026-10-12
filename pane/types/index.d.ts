// The pane's $.state contract (ADR 0005, ADR 0010): what the poller stores for the render hooks.
// Field names follow the run-store protocol (src/agentic_fmi/store/protocol.py, docs/protocol.md)
// so hooks/protocol.ts maps documents one to one. Every value is plain JSON data.

/** One row of the run picker: an index.json entry (state as written, unknown states kept). */
export type AfmiPaneRun = {
  run_id: string
  kind: string
  state: string
  fmu_name: string
  created_at: string
  updated_at: string
}

/** The status.json fields the pane shows; protocol is kept to show an unsupported version. */
export type AfmiPaneStatus = {
  protocol: number
  run_id: string
  state: string
  t: number | null
  t_start: number
  t_stop: number
  progress: number
  speed: number | null
  last_cmd: string | null
  reason: string | null
  error: { code: string; message: string } | null
  heartbeat_at: string | null
}

/** live.json: current values with units and the chart history; protocol kept as for status. */
export type AfmiPaneLive = {
  protocol: number
  run_id: string
  seq: number
  t: number
  values: { name: string; unit: string | null; value: number | null }[]
  time: number[]
  series: Record<string, (number | null)[]>
}

/** The spec facts the pane needs (spec.json is immutable, read once per selected run). */
export type AfmiPaneSpec = {
  run_id: string
  kind: string
  live: boolean
  name: string
  t_stop: number
  outputs: string[]
  /** Candidates for the Set field: start_values keys, or a system's parameter keys. */
  settable_hints: string[]
  /** Systems only: component names in spec order and the connections. */
  components: string[]
  connections: { from: string; to: string }[]
}

/** A built FMU's card, from runs/<id>/build.json (BuildRecord). */
export type AfmiPaneBuild = {
  run_id: string
  name: string
  backend: string
  fmu_path: string
  sha256: string
  interfaces: string[]
  counts: Record<string, number>
  inputs: string[]
  outputs: string[]
  parameters: string[]
  duration_s: number
  warnings: number
}

/** A command the pane wrote and whose outcome it still waits for (toast when it lands). */
export type AfmiPaneSent = { run_id: string; id: string; action: string; sent_at: number }

/** runs/<id>/outcomes/<id>.json (CommandOutcome): what the worker made of a command. */
export type AfmiPaneOutcome = {
  id: string
  action: string | null
  status: string
  t: number | null
  message: string | null
}

declare module 'claude-code' {
  interface PluginState {
    'agentic-fmi-pane': {
      store: string | null
      runs: AfmiPaneRun[]
      selected: string | null
      status: AfmiPaneStatus | null
      live: AfmiPaneLive | null
      spec: AfmiPaneSpec | null
      /** Build cards, newest first, at most 10: the finished build runs' build.json, each read once. */
      builds: AfmiPaneBuild[]
      sent: AfmiPaneSent[]
      problem: string | null
      /** Path -> `<mtimeMs>:<size>` of the last good read: the poller re-reads only on change. */
      stamps: Record<string, string>
      /** The speed the Resume button sends (the speed selector); null keeps the run's own. */
      speed: number | null
      /** The status line last pinned with $.ui.status (re-pinned only when it changes). */
      line: string | null
      /** The pane's open tab (hooks/view.ts TABS); Live when the pane first opens. */
      tab: 'live' | 'system' | 'fmus' | 'runs'
    }
  }
}
