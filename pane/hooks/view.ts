// The texts the pane shows, from the views protocol.ts reads (ADR 0010): the status line, the
// pane's head, the values table (signals grouped by component, as rows of cells), the run picker's
// rows, toasts on state transitions and command outcomes, the build card and the "New FMUs" rows,
// the diagram's edge list and alt text, and the parsing of the Set field. Pure functions, no `$`.
//
// Collaborators: register.tsx (draws and toasts these), protocol.ts (the views), charts.ts (the
// chart series and the edge list), the tests in units.test.ts and pane.test.ts.

import { PALETTE, clip, diagramLines, downsample, valueRange } from './charts'
import { ACTIVE_STATES, PROTOCOL, runNumber } from './protocol'
import type {
  AfmiPaneBuild,
  AfmiPaneLive,
  AfmiPaneOutcome,
  AfmiPaneRun,
  AfmiPaneSpec,
  AfmiPaneStatus,
} from '../types'

/** At most this many runs in the picker (newest first; the selected one is always listed). */
export const PICKER_RUNS = 50
/** The terminal's edge list and the diagram's alt text show at most this many connections. */
export const EDGE_LINES = 40
const ALT_EDGES = 12
/** The values table shows at most this many signals (a system can record 200), then "... and N more". */
export const SIGNAL_MAX = 90
/** A signal's name in the values table is cut at this many characters. */
const NAME_CHARS = 28
/** A group's label (a component or the run's name) is cut at this many characters. */
const LABEL_CHARS = 16
/** The marks in front of a signal in the values table: charted (the dot is drawn in its chart's colour) or not. */
export const CHARTED_MARK = '●'
export const UNCHARTED_MARK = '○'
/** The most charts the Live tab shows and the most a `chart` command may name (Limits.live_variables). */
export const CHART_MAX = 3
/** Cells of the progress bar on the Live tab. */
const PROGRESS_CELLS = 20
/** A build card names at most this many inputs, outputs or parameters, each cut to CARD_NAME_CHARS. */
const CARD_NAMES = 8
const CARD_NAME_CHARS = 40
const SHA_CHARS = 12
/** The speed selector's options: `keep` sends no speed (the run keeps its own). */
export const SPEEDS: readonly { value: string; label: string }[] = [
  { value: 'keep', label: 'keep speed' },
  ...[0.1, 0.5, 1, 2, 5, 10, 100, 1000].map(speed => ({ value: String(speed), label: `${speed}x` })),
]

const GLYPHS: Readonly<Record<string, string>> = {
  queued: '…',
  running: '▶',
  paused: '‖',
  finished: '✓',
  failed: '✗',
  stopped: '■',
}

/** A number for people: at most 4 significant digits, no exponent below 1e21. */
export function formatNumber(value: number): string {
  return String(Number(value.toPrecision(4)))
}

/** The run's short name: a system's components joined with `+` (at most 3), else its name. */
export function runName(spec: AfmiPaneSpec | null, run: AfmiPaneRun | null): string {
  if (spec?.kind === 'system' && spec.components.length > 0 && spec.components.length <= 3) {
    return spec.components.join('+')
  }
  return spec?.name ?? run?.fmu_name ?? ''
}

/** A known state's glyph, `?` for a state this pane doesn't know (shown generically). */
function stateMark(state: string): string {
  return GLYPHS[state] ?? '?'
}

/** The pinned status line: `FMI ▶ r12 PI+MSD 63% t=6.3 s 1x` (an unknown state spelled out). A build run
 * (`kind` "build", the spec's kind; null when unknown) reads `FMI ✓ r4 pi_controller built`. */
export function statusLine(status: AfmiPaneStatus, name: string, kind: string | null = null): string {
  if (status.protocol !== PROTOCOL) {
    return `FMI ${status.run_id}: unsupported protocol version ${status.protocol}`
  }
  const parts = ['FMI', stateMark(status.state), status.run_id]
  if (name !== '') parts.push(name)
  if (kind === 'build') return [...parts, status.state === 'finished' ? 'built' : `build ${status.state}`].join(' ')
  if (GLYPHS[status.state] === undefined) parts.push(status.state)
  parts.push(`${Math.round(status.progress * 100)}%`)
  if (status.t !== null) parts.push(`t=${formatNumber(status.t)} s`)
  if (status.speed !== null && ACTIVE_STATES.includes(status.state)) parts.push(`${formatNumber(status.speed)}x`)
  return parts.join(' ')
}

/** The pane's head line: run, name, state, time and progress, live or batch, speed. */
export function headLine(status: AfmiPaneStatus, spec: AfmiPaneSpec | null, name: string): string {
  if (status.protocol !== PROTOCOL) {
    return `${status.run_id}: unsupported protocol version ${status.protocol}; update the agentic-fmi-pane plugin.`
  }
  if (spec?.kind === 'build') return `${stateMark(status.state)} ${status.run_id} ${name}: build ${status.state}`
  const time = status.t === null ? '-' : formatNumber(status.t)
  const mode = spec === null ? '' : spec.live ? ', live' : ', batch'
  const speed = status.speed !== null && ACTIVE_STATES.includes(status.state) ? `, ${formatNumber(status.speed)}x` : ''
  return (
    `${stateMark(status.state)} ${status.run_id} ${name}: ${status.state}, t = ${time} of ` +
    `${formatNumber(status.t_stop)} s (${Math.round(status.progress * 100)}%)${mode}${speed}`
  )
}

/** Why the run ended or failed, when the status says; null otherwise. */
export function detailLine(status: AfmiPaneStatus): string | null {
  if (status.error !== null) return `${status.error.code}: ${status.error.message}`
  return status.reason
}

/** One signal of the values table: its full name (what a click charts), short name, value text and chart state. */
export type SignalCell = { name: string; short: string; value: string; charted: boolean; color: number }
/** The signals of one component (a system run) or of the run's one FMU, in `live.json` order. */
export type SignalGroup = { label: string; cells: SignalCell[] }
/** One row of the values table: the group's label (blank on continuation rows) and the cells, each `width` wide. */
export type SignalRow = { label: string; cells: SignalCell[]; width: number }

/** The palette colour of the `index`-th value of a live.json: a chart keeps it however many others are charted. */
function paletteColor(index: number): number {
  return PALETTE[index % PALETTE.length]!
}

/** `0.985013 m` (the unit when there is one), or `n/a` for a null value. */
function valueText(column: AfmiPaneLive['values'][number]): string {
  if (column.value === null) return 'n/a'
  return column.unit === null || column.unit === '' ? String(column.value) : `${column.value} ${column.unit}`
}

/**
 * The values of a live.json grouped for the table. A system run groups by component (the part of a
 * name before its first dot, in the order of `spec.components`; names of an unknown component follow
 * in groups labelled with their prefix) and shows the part after the dot. Any other run is one group
 * labelled with the run's name, the names whole (a single FMU's names may hold dots).
 */
export function signalGroups(live: AfmiPaneLive, spec: AfmiPaneSpec | null): SignalGroup[] {
  const charted = new Set(chartedNames(live))
  const isSystem = spec?.kind === 'system'
  const parts = live.values.map((column, i): [string, SignalCell] => {
    const dot = isSystem ? column.name.indexOf('.') : -1
    const cell = {
      name: column.name,
      short: dot < 0 ? column.name : column.name.slice(dot + 1),
      value: valueText(column),
      charted: charted.has(column.name),
      color: paletteColor(i),
    }
    return [dot < 0 ? column.name : column.name.slice(0, dot), cell]
  })
  if (!isSystem) {
    const label = spec === null || spec.name === '' ? live.run_id : spec.name
    return parts.length === 0 ? [] : [{ label, cells: parts.map(([, cell]) => cell) }]
  }
  const groups = new Map<string, SignalCell[]>(spec.components.map(name => [name, []]))
  for (const [label, cell] of parts) groups.set(label, [...(groups.get(label) ?? []), cell])
  return [...groups].filter(([, cells]) => cells.length > 0).map(([label, cells]) => ({ label, cells }))
}

/** `y 1.18552 m`: a signal's (cut) short name and value. */
export function signalDetail(cell: SignalCell): string {
  return `${clip(cell.short, NAME_CHARS)} ${cell.value}`
}

/** `● y 1.18552 m` if charted, else `○ y 1.18552 m`. */
export function signalText(cell: SignalCell): string {
  return `${cell.charted ? CHARTED_MARK : UNCHARTED_MARK} ${signalDetail(cell)}`
}

/** A group's rows: the label (cut at LABEL_CHARS, in the first row only) and the cells as many to a row as fit. */
function groupRows(group: SignalGroup, labelWidth: number, columns: number): SignalRow[] {
  const width = Math.max(...group.cells.map(cell => signalText(cell).length)) + 2
  const perRow = Math.max(1, Math.floor((columns - labelWidth - 1) / width))
  const rows: SignalRow[] = []
  for (let from = 0; from < group.cells.length; from += perRow) {
    const label = from === 0 ? clip(group.label, LABEL_CHARS) : ''
    rows.push({ label: label.padEnd(labelWidth + 1), cells: group.cells.slice(from, from + perRow), width })
  }
  return rows
}

/**
 * The values table as rows fitting `columns` cells, for at most SIGNAL_MAX signals; `more` counts
 * those left out. Labels are padded to the longest, then a space, then the cells flow left to
 * right: each is as wide as its group's longest plus two, and a row holds at least one.
 */
export function signalRows(groups: readonly SignalGroup[], columns: number): { rows: SignalRow[]; more: number } {
  let room = SIGNAL_MAX
  const shown: SignalGroup[] = []
  for (const group of groups) {
    const cells = group.cells.slice(0, room)
    room -= cells.length
    if (cells.length > 0) shown.push({ label: group.label, cells })
  }
  const labelWidth = Math.max(0, ...shown.map(group => Array.from(clip(group.label, LABEL_CHARS)).length))
  const total = groups.reduce((sum, group) => sum + group.cells.length, 0)
  return { rows: shown.flatMap(group => groupRows(group, labelWidth, columns)), more: total - (SIGNAL_MAX - room) }
}

/** `███████░░░░░░░░░░░░░ 35%`: the run's progress as a bar of `width` cells. */
export function progressBar(progress: number, width: number = PROGRESS_CELLS): string {
  const share = Math.min(1, Math.max(0, progress))
  const filled = Math.round(share * width)
  return `${'█'.repeat(filled)}${'░'.repeat(width - filled)} ${Math.round(share * 100)}%`
}

/** A chart's title: `h [m]`, or the name alone without a unit. */
export function chartTitle(name: string, unit: string | null): string {
  return unit === null || unit === '' ? name : `${name} [${unit}]`
}

/** The chart's caption for screen readers (the Svg's alt): `h [m]  0 .. 1.2 over t 0 .. 1.25 s`. */
export function chartLabel(name: string, unit: string | null, range: [number, number] | null, time: readonly number[]): string {
  const span = range === null ? 'no values' : `${formatNumber(range[0])} .. ${formatNumber(range[1])}`
  const window = time.length === 0 ? '' : ` over t ${formatNumber(time[0]!)} .. ${formatNumber(time[time.length - 1]!)} s`
  return `${chartTitle(name, unit)}  ${span}${window}`
}

/** One chart per charted variable (at most CHART_MAX): its title, caption, colour and samples. */
export type ChartSeries = { name: string; title: string; label: string; color: number; series: (number | null)[] }

/** The charted names, in order: the worker's selection is the keys of `live.series` (at most CHART_MAX shown). */
export function chartedNames(live: AfmiPaneLive): string[] {
  return Object.keys(live.series).slice(0, CHART_MAX)
}

/**
 * The charts of a live.json: each variable's history through the one downsampler, to at most
 * `points` samples (SVG_POINTS on the desktop; the terminal takes every sample and picks one per
 * column itself). A chart's colour follows its name's position in `live.values`, so it keeps
 * its colour when another chart is removed.
 */
export function chartSeries(live: AfmiPaneLive, points: number): ChartSeries[] {
  const units = new Map(live.values.map(column => [column.name, column.unit]))
  const positions = new Map(live.values.map((column, i) => [column.name, i]))
  return chartedNames(live).map((name, i) => {
    const values = live.series[name]!
    return {
      name,
      title: chartTitle(name, units.get(name) ?? null),
      label: chartLabel(name, units.get(name) ?? null, valueRange(values), live.time),
      color: paletteColor(positions.get(name) ?? i),
      series: downsample(values, points).map(index => values[index] ?? null),
    }
  })
}

/** The pane's tabs, in order, with the digit that switches to each while the pane has the focus. */
export const TABS = [
  { id: 'live', label: 'Live', hotkey: '1' },
  { id: 'system', label: 'System', hotkey: '2' },
  { id: 'fmus', label: 'FMUs', hotkey: '3' },
  { id: 'runs', label: 'Runs', hotkey: '4' },
] as const
export type PaneTab = (typeof TABS)[number]['id']

/** The run next to `selected` in the index (newest first): `older` goes down the list, else up. */
export function neighbourRun(runs: readonly AfmiPaneRun[], selected: string | null, older: boolean): string | null {
  const at = runs.findIndex(run => run.run_id === selected)
  if (at < 0) return runs[0]?.run_id ?? null
  return runs[at + (older ? 1 : -1)]?.run_id ?? null
}

/** A row of the Runs tab: `▸ r8  PI+plant  system  paused` (the marker on the selected run). */
export function runListLine(run: AfmiPaneRun, selected: string | null): string {
  const marker = run.run_id === selected ? '▸' : ' '
  const kind = run.kind === 'run' ? '' : `  ${run.kind}`
  return `${marker} ${run.run_id.padEnd(4)} ${run.fmu_name}${kind}  ${stateMark(run.state)} ${run.state}`
}

/** The run shown when nothing is picked: the newest active (live or running) run, else the newest. */
export function defaultRun(runs: readonly AfmiPaneRun[]): string | null {
  return (runs.find(run => ACTIVE_STATES.includes(run.state)) ?? runs[0])?.run_id ?? null
}

/** The newest run that is new in `next` and active: a run started since the last poll. */
export function newActiveRun(previous: readonly AfmiPaneRun[], next: readonly AfmiPaneRun[]): string | null {
  const known = new Set(previous.map(run => run.run_id))
  return next.find(run => !known.has(run.run_id) && ACTIVE_STATES.includes(run.state))?.run_id ?? null
}

/** The picker's options: the newest PICKER_RUNS runs, plus the selected one if older. */
export function pickerOptions(runs: readonly AfmiPaneRun[], selected: string | null): { value: string; label: string }[] {
  const shown = runs.slice(0, PICKER_RUNS)
  const extra = runs.find(run => run.run_id === selected && !shown.includes(run))
  return [...shown, ...(extra === undefined ? [] : [extra])].map(run => ({
    value: run.run_id,
    label: `${run.run_id} ${run.fmu_name} ${run.kind === 'run' ? '' : `(${run.kind}) `}${run.state}`,
  }))
}

/**
 * Toasts for the index's state transitions: a new run, a changed state. The selected run's
 * transition already toasted from its status.json (`shown`) is skipped.
 */
export function runToasts(
  previous: readonly AfmiPaneRun[],
  next: readonly AfmiPaneRun[],
  shown: AfmiPaneStatus | null,
): string[] {
  const before = new Map(previous.map(run => [run.run_id, run.state]))
  const toasts: string[] = []
  for (const run of [...next].sort((a, b) => runNumber(a.run_id) - runNumber(b.run_id))) {
    const old = before.get(run.run_id)
    if (old === run.state || (shown?.run_id === run.run_id && shown.state === run.state)) continue
    toasts.push(`${run.run_id} ${run.fmu_name}: ${old === undefined ? run.state : `${old} → ${run.state}`}`)
  }
  return toasts
}

/** The toast for the selected run's status transition. */
export function statusToast(previous: AfmiPaneStatus, next: AfmiPaneStatus, name: string): string {
  const detail = detailLine(next)
  return `${next.run_id} ${name}: ${previous.state} → ${next.state}${detail === null ? '' : ` (${detail})`}`
}

/**
 * The toast for a command's outcome: `r1 set rejected at t=1.25: g is a fixed parameter...`; null
 * for an applied `chart`, which shows on the screen as the chart itself.
 */
export function outcomeToast(runId: string, action: string, outcome: AfmiPaneOutcome): string | null {
  if (outcome.status === 'applied' && (outcome.action ?? action) === 'chart') return null
  const at = outcome.t === null ? '' : ` at t=${formatNumber(outcome.t)}`
  const message = outcome.message === null ? '' : `: ${outcome.message}`
  return `${runId} ${outcome.action ?? action} ${outcome.status}${at}${message}`
}

/**
 * The Set field: `name=value` pairs separated by commas or semicolons; a value is a number,
 * `true` or `false`. Answers the values or what is wrong with the text.
 */
export function parseSet(text: string): { values: Record<string, number | boolean> } | { error: string } {
  const values: Record<string, number | boolean> = {}
  const pairs = text.split(/[,;]/).map(pair => pair.trim()).filter(pair => pair !== '')
  for (const pair of pairs) {
    const cut = pair.indexOf('=')
    const name = pair.slice(0, cut).trim()
    const raw = pair.slice(cut + 1).trim()
    if (cut <= 0 || name === '' || /\s/.test(name) || raw === '') {
      return { error: `Set expects name=value (for example e=0.6), not "${pair}".` }
    }
    const value = raw === 'true' ? true : raw === 'false' ? false : Number(raw)
    if (typeof value === 'number' && !Number.isFinite(value)) {
      return { error: `${name}: "${raw}" is not a number, true or false.` }
    }
    values[name] = value
  }
  return pairs.length === 0 ? { error: 'Set expects name=value, for example e=0.6.' } : { values }
}

/** At most `max` lines, then `... and N more` (the value lines, the terminal's edge list and the Svg's alt text). */
export function capLines(lines: readonly string[], max: number): string[] {
  return lines.length <= max ? [...lines] : [...lines.slice(0, max), `... and ${lines.length - max} more`]
}

/** The terminal's connection list: `PI.u    ──▶ plant.F`, sources padded to line up, at most EDGE_LINES. */
export function connectionRows(connections: readonly { from: string; to: string }[]): string[] {
  const width = Math.max(0, ...connections.slice(0, EDGE_LINES).map(edge => edge.from.length))
  return capLines(connections.map(edge => `${edge.from.padEnd(width)} ──▶ ${edge.to}`), EDGE_LINES)
}

/** The Svg's alt text: the connections as a sentence, `Connections: PI.u -> plant.F; ...`. */
export function diagramAlt(connections: readonly { from: string; to: string }[]): string {
  if (connections.length === 0) return 'The system has no connections.'
  return `Connections: ${capLines(diagramLines(connections), ALT_EDGES).join('; ')}`
}

/** `5 variables: input 1, output 1, parameter 3` from the summary counts (zeros left out). */
function countsLine(counts: Readonly<Record<string, number>>): string {
  const total = counts.total
  const head = total === undefined ? 'variables' : `${total} ${total === 1 ? 'variable' : 'variables'}`
  const parts = Object.entries(counts)
    .filter(([key, count]) => key !== 'total' && count > 0)
    .map(([key, count]) => `${key} ${count}`)
  return parts.length === 0 ? head : `${head}: ${parts.join(', ')}`
}

/** `inputs (3): a, b, c`, long lists cut to CARD_NAMES with `+N more`, `inputs: none` when empty. */
function namesLine(label: string, names: readonly string[], count: number | undefined): string {
  const total = Math.max(count ?? 0, names.length)
  if (total === 0) return `${label}: none`
  const shown = names.slice(0, CARD_NAMES).map(name => clip(name, CARD_NAME_CHARS))
  const more = total - shown.length
  return `${label} (${total}): ${shown.join(', ')}${more > 0 ? `, +${more} more` : ''}`
}

/** The build card's lines under its title: backend, interfaces, time, counts, names, file, hash, warnings. */
export function buildCardLines(build: AfmiPaneBuild): string[] {
  const interfaces = build.interfaces.length === 0 ? 'no interface listed' : build.interfaces.join(' + ')
  const warnings = build.warnings === 1 ? '1 warning' : `${build.warnings} warnings`
  return [
    `${build.backend}, ${interfaces}, built in ${formatNumber(build.duration_s)} s`,
    countsLine(build.counts),
    namesLine('inputs', build.inputs, build.counts.input),
    namesLine('outputs', build.outputs, build.counts.output),
    namesLine('parameters', build.parameters, build.counts.parameter),
    `${build.fmu_path}  sha256 ${build.sha256.slice(0, SHA_CHARS)}`,
    build.warnings === 0 ? 'no warnings' : `${warnings}; see runs/${build.run_id}/build.log`,
  ]
}

/** One row of the "New FMUs" list: `r4 pi_controller: pythonfmu, CoSimulation, 5 variables, 6.4 s`. */
export function buildListLine(build: AfmiPaneBuild): string {
  const total = build.counts.total
  const parts = [build.backend, ...build.interfaces]
  if (total !== undefined) parts.push(`${total} ${total === 1 ? 'variable' : 'variables'}`)
  parts.push(`${formatNumber(build.duration_s)} s`)
  return `${build.run_id} ${build.name}: ${parts.join(', ')}`
}
