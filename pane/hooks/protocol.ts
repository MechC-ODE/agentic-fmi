// Tolerant readers of the run-store documents (docs/protocol.md, ADR 0010) and the pane's
// command writer. Pure functions: no `$` here (claude plugin validate allows `$` only in
// functions declared in register.tsx). Every reader takes parsed JSON (or anything) and returns
// a typed view or null; it never throws. Unknown fields are ignored, unknown enum values kept as
// text (shown generically), and a document whose protocol is not 1 is still returned with its
// protocol so the pane can say "unsupported protocol version" instead of guessing.
//
// Collaborators: register.tsx (the poller and the buttons), view.ts (the texts drawn from these
// views) and ../fixtures/store.ts (generated from the pydantic models: the reference inputs).

import type {
  AfmiPaneBuild,
  AfmiPaneLive,
  AfmiPaneOutcome,
  AfmiPaneRun,
  AfmiPaneSpec,
  AfmiPaneStatus,
} from '../types'

export const PROTOCOL = 1
export const COMMAND_ID = /^\d{13}-(server|pane)-[0-9a-f]{8}$/
export const COMMAND_MAX_BYTES = 4096
export const TERMINAL_STATES: readonly string[] = ['finished', 'failed', 'stopped']
export const ACTIVE_STATES: readonly string[] = ['queued', 'running', 'paused']

type Json = Record<string, unknown>

/** JSON.parse that answers undefined for empty, torn or non-JSON text. */
export function parseJson(text: string): unknown {
  try {
    return text === '' ? undefined : JSON.parse(text)
  } catch {
    return undefined
  }
}

function isObject(raw: unknown): raw is Json {
  return typeof raw === 'object' && raw !== null && !Array.isArray(raw)
}

function num(raw: unknown): number | null {
  return typeof raw === 'number' && Number.isFinite(raw) ? raw : null
}

function str(raw: unknown): string | null {
  return typeof raw === 'string' ? raw : null
}

function strings(raw: unknown): string[] {
  return Array.isArray(raw) ? raw.filter((item): item is string => typeof item === 'string') : []
}

/** The number of a run ID (`r12` -> 12), 0 for anything else. */
export function runNumber(runId: string): number {
  const match = /^r([1-9]\d*)$/.exec(runId)
  return match === null ? 0 : Number(match[1])
}

/** index.json -> picker rows, newest (highest run number) first; null if it isn't an index. */
export function asRuns(raw: unknown): AfmiPaneRun[] | null {
  if (!isObject(raw) || !Array.isArray(raw.runs)) return null
  const runs: AfmiPaneRun[] = []
  for (const entry of raw.runs) {
    if (!isObject(entry)) continue
    const runId = str(entry.run_id)
    if (runId === null || runNumber(runId) === 0) continue
    runs.push({
      run_id: runId,
      kind: str(entry.kind) ?? 'run',
      state: str(entry.state) ?? 'unknown',
      fmu_name: str(entry.fmu_name) ?? runId,
      created_at: str(entry.created_at) ?? '',
      updated_at: str(entry.updated_at) ?? '',
    })
  }
  return runs.sort((a, b) => runNumber(b.run_id) - runNumber(a.run_id))
}

/** status.json -> the shown fields; null without a run_id and a state (torn or foreign). */
export function asStatus(raw: unknown): AfmiPaneStatus | null {
  if (!isObject(raw)) return null
  const runId = str(raw.run_id)
  const state = str(raw.state)
  if (runId === null || state === null) return null
  const error = isObject(raw.error)
    ? { code: str(raw.error.code) ?? 'error', message: str(raw.error.message) ?? '' }
    : null
  return {
    protocol: num(raw.protocol) ?? 0,
    run_id: runId,
    state,
    t: num(raw.t),
    t_start: num(raw.t_start) ?? 0,
    t_stop: num(raw.t_stop) ?? 0,
    progress: num(raw.progress) ?? 0,
    speed: num(raw.speed),
    last_cmd: str(raw.last_cmd),
    reason: str(raw.reason),
    error,
    heartbeat_at: str(raw.heartbeat_at),
  }
}

function asColumn(raw: unknown): AfmiPaneLive['values'][number] | null {
  if (!isObject(raw)) return null
  const name = str(raw.name)
  return name === null ? null : { name, unit: str(raw.unit), value: num(raw.value) }
}

/** live.json -> values and history (null if torn, or if series and time lengths disagree). */
export function asLive(raw: unknown): AfmiPaneLive | null {
  if (!isObject(raw) || !Array.isArray(raw.values) || !Array.isArray(raw.time)) return null
  const runId = str(raw.run_id)
  const seq = num(raw.seq)
  const t = num(raw.t)
  if (runId === null || seq === null || t === null || !isObject(raw.series)) return null
  const time = raw.time.map(num)
  if (time.some(value => value === null)) return null
  const series: Record<string, (number | null)[]> = {}
  for (const [name, values] of Object.entries(raw.series)) {
    if (!Array.isArray(values) || values.length !== time.length) return null
    series[name] = values.map(num)
  }
  const values = raw.values.map(asColumn).filter(column => column !== null)
  return { protocol: num(raw.protocol) ?? 0, run_id: runId, seq, t, values, time: time as number[], series }
}

function connectionsOf(raw: unknown): { from: string; to: string }[] {
  if (!Array.isArray(raw)) return []
  return raw.flatMap(edge => {
    if (!isObject(edge)) return []
    const from = str(edge.from)
    const to = str(edge.to)
    return from === null || to === null ? [] : [{ from, to }]
  })
}

/** spec.json of a run or system run -> the spec facts (build specs: kind and name only). */
export function asSpec(raw: unknown): AfmiPaneSpec | null {
  if (!isObject(raw)) return null
  const runId = str(raw.run_id)
  if (runId === null) return null
  const kind = str(raw.kind) ?? 'run'
  const experiment = isObject(raw.experiment) ? raw.experiment : {}
  const fmu = isObject(raw.fmu) ? raw.fmu : {}
  const isSystem = kind === 'system'
  const components = isSystem && isObject(raw.components) ? Object.keys(raw.components) : []
  const hints = isSystem ? raw.parameters : raw.start_values
  return {
    run_id: runId,
    kind,
    live: raw.live === true,
    name: str(isSystem || kind === 'build' ? raw.name : fmu.model_name) ?? runId,
    t_stop: num(experiment.stop_time) ?? 0,
    outputs: strings(raw.outputs),
    settable_hints: isObject(hints) ? Object.keys(hints) : [],
    components,
    connections: isSystem ? connectionsOf(raw.connections) : [],
  }
}

/** outcomes/<id>.json -> what became of a command; null while missing or torn. */
export function asOutcome(raw: unknown): AfmiPaneOutcome | null {
  if (!isObject(raw)) return null
  const id = str(raw.id)
  const status = str(raw.status)
  if (id === null || status === null) return null
  return { id, action: str(raw.action), status, t: num(raw.t), message: str(raw.message) }
}

function counts(raw: unknown): Record<string, number> {
  const found: Record<string, number> = {}
  if (isObject(raw)) {
    for (const [key, value] of Object.entries(raw)) {
      const count = num(value)
      if (count !== null) found[key] = count
    }
  }
  return found
}

/**
 * runs/<id>/build.json (BuildRecord) -> a build card. Null when the document is torn, its protocol
 * is not 1 or one of the fields the card shows is missing; the source, options and versions are
 * not read. `warnings` is the number of diagnostics lines.
 */
export function asBuild(raw: unknown): AfmiPaneBuild | null {
  if (!isObject(raw) || num(raw.protocol) !== PROTOCOL) return null
  const runId = str(raw.run_id)
  const name = str(raw.name)
  const backend = str(raw.backend)
  const duration = num(raw.duration_s)
  if (runId === null || name === null || backend === null || duration === null) return null
  if (!isObject(raw.fmu) || !isObject(raw.summary)) return null
  const path = str(raw.fmu.path)
  const sha256 = str(raw.fmu.sha256)
  if (path === null || sha256 === null) return null
  return {
    run_id: runId,
    name,
    backend,
    fmu_path: path,
    sha256,
    interfaces: strings(raw.fmu.interfaces),
    counts: counts(raw.summary.counts),
    inputs: strings(raw.summary.inputs),
    outputs: strings(raw.summary.outputs),
    parameters: strings(raw.summary.parameters),
    duration_s: duration,
    warnings: Array.isArray(raw.warnings) ? raw.warnings.length : 0,
  }
}

/** Whether the pane may control this run: a live spec and a non-terminal, known state. */
export function isControllable(spec: AfmiPaneSpec | null, status: AfmiPaneStatus | null): boolean {
  return (
    spec !== null &&
    spec.live &&
    status !== null &&
    status.run_id === spec.run_id &&
    status.protocol === PROTOCOL &&
    ACTIVE_STATES.includes(status.state)
  )
}

export type PaneAction =
  | { action: 'pause' }
  | { action: 'resume'; speed?: number }
  | { action: 'step'; n: 1 }
  | { action: 'set'; values: Record<string, number | boolean> }
  | { action: 'stop' }
  | { action: 'chart'; names: string[] }

/**
 * A command file for runs/<run>/commands/<id>.json: `<nowMs 13 digits>-pane-<8 hex>` and the
 * JSON the worker parses (golden examples: PANE_COMMANDS in ../fixtures/store.ts). The pane
 * writes each name once and never rewrites it; the caller checks COMMAND_MAX_BYTES.
 */
export function commandFile(
  nowMs: number,
  randomHex8: string,
  command: PaneAction,
): { id: string; text: string } {
  const id = `${String(Math.trunc(nowMs)).padStart(13, '0')}-pane-${randomHex8}`
  const body = { protocol: PROTOCOL, id, source: 'pane', issued_at: new Date(nowMs).toISOString(), ...command }
  return { id, text: JSON.stringify(body) }
}

/** `a/./b/../c` -> `/a/c`: `.` and `..` folded lexically, no trailing slash, a drive kept. */
export function foldPath(path: string): string {
  const drive = /^[A-Za-z]:/.exec(path)?.[0] ?? ''
  const parts: string[] = []
  for (const part of path.slice(drive.length).split(/[\\/]/)) {
    if (part === '' || part === '.') continue
    if (part === '..') parts.pop()
    else parts.push(part)
  }
  return `${drive}/${parts.join('/')}`
}

/**
 * The run store for a session root, as config.load_config resolves it (cases: DISCOVERY in
 * ../fixtures/store.ts): AFMI_STORE_DIR trimmed; empty -> <root>/.agentic-fmi; `~` -> HOME;
 * relative -> under root; `.` and `..` segments folded lexically (symbolic links are not).
 */
export function storeDirFor(envValue: string | null, root: string, home: string | null): string {
  const value = (envValue ?? '').trim()
  if (value === '') return foldPath(`${root}/.agentic-fmi`)
  const expanded = home !== null && (value === '~' || value.startsWith('~/')) ? home + value.slice(1) : value
  const isAbsolute = /^([\\/]|[A-Za-z]:[\\/])/.test(expanded)
  return foldPath(isAbsolute ? expanded : `${root}/${expanded}`)
}
