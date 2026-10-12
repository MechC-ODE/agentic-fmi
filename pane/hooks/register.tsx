// The Agentic FMI pane (ADR 0005, ADR 0010): `/fmi` opens a pane over the run store. A header picks
// the run (◀ picker ▶) and the tab (hotkeys 1 to 4): Live (the run's status with a progress bar,
// Pause/Resume/Step/Set/Stop for live runs, the values with units grouped by component, line charts
// with axes, and for an active live run a chart picker: a click on a signal charts it, a click on
// a charted one or its ✕ removes the chart, at most 3 charts), System (a
// system's components and connections), FMUs (newly built FMUs and their cards) and Runs (every
// run, one press to open). A status line and toasts follow the store while the pane is closed too.
//
// Rules (binding, from the Phase 4 design):
// - Read only the run store (index.json, runs/<id>/{spec,status,live,build}.json and
//   runs/<id>/outcomes/<id>.json); never XML, SSP, result.csv or events.jsonl. Toasts come from
//   state transitions seen in index.json and status.json, and from the outcomes of the commands
//   this pane wrote.
// - Write only runs/<id>/commands/<ms13>-pane-<8 hex>.json, each name once (hooks/protocol.ts
//   commandFile); never status.json or anything else. The chart picker is a `chart` command with
//   the full new selection; the pane draws whatever live.json's `series` holds, so a rejected
//   chart changes nothing on screen.
// - Store discovery: AFMI_STORE_DIR, else <session root>/.agentic-fmi, resolved as
//   config.load_config does (storeDirFor; fixtures DISCOVERY).
// - Parse tolerantly: a torn or invalid file keeps the previous value; an unknown protocol
//   version or state is shown generically. Poll with $.fs.stat and read only on a change of
//   mtimeMs or size (the stamps kept in $.state).
// - Pass `$` only to functions declared at the top of this file (claude plugin validate).
// - Draw per surface: Raster on the terminal, Svg elsewhere (hooks/charts.ts).
// - Keep what a drawing needs in $.state (a hot reload drops module variables); the module
//   variables below only hold the timer and whether the pane is open.
//
// Collaborators: protocol.ts (readers, commandFile, storeDirFor), view.ts (texts, picker,
// toasts, chart series, build cards), charts.ts (renderers, the diagram), ../types (the $.state
// contract).

import { atom, read, update } from 'claude-code'
import type { Elements, EngineInterface, Register } from 'claude-code'

import type { AfmiPaneBuild, AfmiPaneLive, AfmiPaneRun, AfmiPaneSpec, AfmiPaneStatus } from '../types'
import { MAX_RASTER_COLUMNS, diagramSvg, lineChartCells, lineChartSvg, SVG_POINTS } from './charts'
import {
  ACTIVE_STATES,
  COMMAND_MAX_BYTES,
  PROTOCOL,
  asBuild,
  asLive,
  asOutcome,
  asRuns,
  asSpec,
  asStatus,
  commandFile,
  isControllable,
  parseJson,
  runNumber,
  storeDirFor,
} from './protocol'
import type { PaneAction } from './protocol'
import {
  CHARTED_MARK,
  CHART_MAX,
  SPEEDS,
  TABS,
  UNCHARTED_MARK,
  buildCardLines,
  buildListLine,
  chartSeries,
  chartedNames,
  connectionRows,
  defaultRun,
  detailLine,
  diagramAlt,
  headLine,
  neighbourRun,
  newActiveRun,
  outcomeToast,
  parseSet,
  pickerOptions,
  progressBar,
  runListLine,
  runName,
  runToasts,
  signalDetail,
  signalGroups,
  signalRows,
  signalText,
  statusLine,
  statusToast,
} from './view'
import type { ChartSeries, PaneTab, SignalCell } from './view'

const PANE = 'fmi'
const POLL_MS = 250
/** Poll period while the pane is closed and no run is active (status line and toasts only). */
const IDLE_POLL_MS = 2000
/** A poll still running after this long (real time) is taken as lost and no longer blocks. */
const POLL_STALE_MS = 5000
/** How long the pane waits for a command's outcome before it stops looking. */
const SENT_TTL_MS = 60_000
/** Plot rows of a terminal chart (the time axis adds charts.AXIS_ROWS). */
const CHART_ROWS = 8
const SVG_WIDTH = 520
const SVG_HEIGHT = 170
/** Build cards kept in $.state (the newest finished build runs), all listed on the FMUs tab. */
const BUILDS_MAX = 10
/** Rows of the Runs tab (newest first), then "... and N older". */
const RUN_ROWS = 20

const storeAtom = atom({ plugin: 'agentic-fmi-pane', key: 'store' } as const, null)
const runsAtom = atom({ plugin: 'agentic-fmi-pane', key: 'runs' } as const, [])
const selectedAtom = atom({ plugin: 'agentic-fmi-pane', key: 'selected' } as const, null)
const statusAtom = atom({ plugin: 'agentic-fmi-pane', key: 'status' } as const, null)
const liveAtom = atom({ plugin: 'agentic-fmi-pane', key: 'live' } as const, null)
const specAtom = atom({ plugin: 'agentic-fmi-pane', key: 'spec' } as const, null)
const buildsAtom = atom({ plugin: 'agentic-fmi-pane', key: 'builds' } as const, [])
const problemAtom = atom({ plugin: 'agentic-fmi-pane', key: 'problem' } as const, null)
const sentAtom = atom({ plugin: 'agentic-fmi-pane', key: 'sent' } as const, [])
const stampsAtom = atom({ plugin: 'agentic-fmi-pane', key: 'stamps' } as const, {})
const speedAtom = atom({ plugin: 'agentic-fmi-pane', key: 'speed' } as const, null)
const lineAtom = atom({ plugin: 'agentic-fmi-pane', key: 'line' } as const, null)
const tabAtom = atom({ plugin: 'agentic-fmi-pane', key: 'tab' } as const, 'live')

type Dollar = EngineInterface
type Stamps = Record<string, string>
/** The elements both branches draw with (mobile has no Input or Select). */
type Common = Pick<Elements['terminal'], 'Box' | 'Text' | 'Button'> &
  Partial<Pick<Elements['terminal'], 'Input' | 'Select'>>
/** What the pane draws: the selected run's documents only (another run's are dropped). */
type Shown = {
  store: string | null
  runs: AfmiPaneRun[]
  selected: string | null
  problem: string | null
  status: AfmiPaneStatus | null
  spec: AfmiPaneSpec | null
  live: AfmiPaneLive | null
  speed: number | null
  builds: AfmiPaneBuild[]
  tab: PaneTab
}
/** What a surface draws for the pane's elements that differ by surface (charts, diagram), and the cells it has across. */
type Drawn = { charts: JSX.Element[]; diagram: JSX.Element | null; columns: number }
type Actions = {
  pick: (runId: string) => unknown
  open: (runId: string) => unknown
  older: () => unknown
  newer: () => unknown
  tab: (tab: PaneTab) => unknown
  pause: () => unknown
  resume: () => unknown
  step: () => unknown
  stop: () => unknown
  set: (text: string) => unknown
  speed: (value: string) => unknown
  toggleChart: (name: string) => unknown
  removeChart: (name: string) => unknown
}

let poller: { cancel: () => void; ms: number } | undefined
let pollingSince = 0
let isPaneOpen = false

/** The run store directory (storeDirFor), resolved once per session and kept in $.state. */
async function findStore($: Dollar): Promise<string> {
  const held = await read($, storeAtom)
  if (held !== null) return held
  const value = (await $.env.get('AFMI_STORE_DIR')) ?? null
  const home = value?.trim().startsWith('~') ? ((await $.env.get('HOME')) ?? null) : null
  const store = storeDirFor(value, await $.session.root(), home)
  await update($, storeAtom, () => store)
  return store
}

/** The parsed file if its mtime or size differs from `stamp`; null when unchanged or missing. */
async function readChanged($: Dollar, path: string, stamp: string | undefined): Promise<{ raw: unknown; stamp: string } | null> {
  const stat = await $.fs.stat(path).catch(() => undefined)
  if (stat === undefined || stat.kind !== 'file') return null
  const now = `${stat.mtimeMs}:${stat.size}`
  if (now === stamp) return null
  const text = await $.fs.read(path).catch(() => '')
  return { raw: parseJson(typeof text === 'string' ? text : ''), stamp: now }
}

/** Show another run: its documents are read afresh on the next poll. */
async function selectRun($: Dollar, runId: string | null): Promise<void> {
  await update($, selectedAtom, () => runId)
  await update($, specAtom, () => null)
  await update($, statusAtom, () => null)
  await update($, liveAtom, () => null)
}

/** index.json: the picker rows, toasts for transitions, and a newly started run selected. */
async function pollIndex($: Dollar, store: string, stamps: Stamps): Promise<readonly AfmiPaneRun[]> {
  const path = `${store}/index.json`
  const previous = await read($, runsAtom)
  const got = await readChanged($, path, stamps[path])
  const runs = got === null ? null : asRuns(got.raw)
  if (got === null || runs === null) return previous
  const isFirst = stamps[path] === undefined
  stamps[path] = got.stamp
  await update($, runsAtom, () => runs)
  if (!isFirst) for (const text of runToasts(previous, runs, await read($, statusAtom))) $.ui.toast(text)
  const selected = await read($, selectedAtom)
  const next = (isFirst ? null : newActiveRun(previous, runs)) ?? selected ?? defaultRun(runs)
  if (next !== selected) await selectRun($, next)
  return runs
}

/** spec.json once per selected run (it is immutable). */
async function pollSpec($: Dollar, dir: string, runId: string): Promise<void> {
  if ((await read($, specAtom))?.run_id === runId) return
  const got = await readChanged($, `${dir}/spec.json`, undefined)
  const spec = got === null ? null : asSpec(got.raw)
  if (spec?.run_id === runId) await update($, specAtom, () => spec)
}

/**
 * build.json of each finished build run among the newest BUILDS_MAX, read once (a finished build's
 * record is final). A missing or torn file leaves the cards as they are and is read again only
 * when its mtime or size changes; the cards of runs that left the window are dropped.
 */
async function pollBuilds($: Dollar, store: string, runs: readonly AfmiPaneRun[], stamps: Stamps): Promise<void> {
  const wanted = runs.filter(run => run.kind === 'build' && run.state === 'finished').slice(0, BUILDS_MAX)
  const ids = new Set(wanted.map(run => run.run_id))
  const all = await read($, buildsAtom)
  const held = all.filter(build => ids.has(build.run_id))
  const known = new Set(held.map(build => build.run_id))
  const fresh: AfmiPaneBuild[] = []
  for (const run of wanted) {
    if (known.has(run.run_id)) continue
    const path = `${store}/runs/${run.run_id}/build.json`
    const got = await readChanged($, path, stamps[path])
    if (got === null) continue
    stamps[path] = got.stamp
    const build = asBuild(got.raw)
    if (build?.run_id === run.run_id) fresh.push(build)
  }
  if (fresh.length === 0 && held.length === all.length) return
  await update($, buildsAtom, () => [...fresh, ...held].sort((a, b) => runNumber(b.run_id) - runNumber(a.run_id)))
}

/** status.json of the selected run; a toast when its state changes. */
async function pollStatus($: Dollar, dir: string, runId: string, stamps: Stamps): Promise<void> {
  const path = `${dir}/status.json`
  const held = await read($, statusAtom)
  const current = held?.run_id === runId ? held : null
  const got = await readChanged($, path, current === null ? undefined : stamps[path])
  const status = got === null ? null : asStatus(got.raw)
  if (got === null || status === null || status.run_id !== runId) return
  stamps[path] = got.stamp
  if (current !== null && current.state !== status.state) {
    $.ui.toast(statusToast(current, status, runName(await read($, specAtom), null)))
  }
  await update($, statusAtom, () => status)
}

/** live.json of the selected run (kept after the run ends; absent for batch runs). */
async function pollLive($: Dollar, dir: string, runId: string, stamps: Stamps): Promise<void> {
  const path = `${dir}/live.json`
  const held = await read($, liveAtom)
  const current = held?.run_id === runId ? held : null
  const got = await readChanged($, path, current === null ? undefined : stamps[path])
  const live = got === null ? null : asLive(got.raw)
  if (got === null || live === null || live.run_id !== runId) return
  stamps[path] = got.stamp
  if (current?.seq !== live.seq) await update($, liveAtom, () => live)
}

/** Toast the outcome of each command this pane wrote, once it lands (or give up after a while). */
async function pollOutcomes($: Dollar, store: string): Promise<void> {
  const sent = await read($, sentAtom)
  if (sent.length === 0) return
  const now = await $.clock.now()
  const done = new Set<string>()
  for (const command of sent) {
    const path = `${store}/runs/${command.run_id}/outcomes/${command.id}.json`
    const text = await $.fs.read(path).catch(() => '')
    const outcome = asOutcome(parseJson(typeof text === 'string' ? text : ''))
    const toast = outcome === null ? null : outcomeToast(command.run_id, command.action, outcome)
    if (toast !== null) $.ui.toast(toast)
    if (outcome !== null || now - command.sent_at > SENT_TTL_MS) done.add(command.id)
  }
  if (done.size > 0) await update($, sentAtom, list => list.filter(command => !done.has(command.id)))
}

/** Pin the status line for the selected run, only when its text changes. */
async function pinStatus($: Dollar): Promise<void> {
  const status = await read($, statusAtom)
  const spec = await read($, specAtom)
  const line = status === null ? null : statusLine(status, runName(spec, null), spec?.kind ?? null)
  if (line === (await read($, lineAtom))) return
  await update($, lineAtom, () => line)
  $.ui.status(line ?? undefined)
}

/** One poll: index.json, then the finished builds' records, the selected run's spec, status and
 * live documents, then the outcomes of sent commands; the status line; the poll period follows
 * activity. */
async function poll($: Dollar): Promise<void> {
  const store = await findStore($)
  const held = await read($, stampsAtom)
  const stamps: Stamps = { ...held }
  const runs = await pollIndex($, store, stamps)
  await pollBuilds($, store, runs, stamps)
  const selected = await read($, selectedAtom)
  if (selected !== null) {
    const dir = `${store}/runs/${selected}`
    await pollSpec($, dir, selected)
    await pollStatus($, dir, selected, stamps)
    await pollLive($, dir, selected, stamps)
  }
  await pollOutcomes($, store)
  if (JSON.stringify(stamps) !== JSON.stringify(held)) await update($, stampsAtom, () => stamps)
  await pinStatus($)
  if ((await read($, problemAtom)) !== null) await update($, problemAtom, () => null)
  const wanted = isPaneOpen || runs.some(run => ACTIVE_STATES.includes(run.state)) ? POLL_MS : IDLE_POLL_MS
  if (poller !== undefined && poller.ms !== wanted) startPolling($, wanted)
}

/** A poll that never throws and never overlaps a running one; a failure is shown in the pane. */
async function pollSafely($: Dollar): Promise<void> {
  if (Date.now() - pollingSince < POLL_STALE_MS) return
  pollingSince = Date.now()
  try {
    await poll($)
  } catch (error) {
    await update($, problemAtom, () => `The pane could not read the run store: ${String(error)}`)
  } finally {
    pollingSince = 0
  }
}

/** (Re)start the poller with `periodMs`; poll() switches between POLL_MS and IDLE_POLL_MS. */
function startPolling($: Dollar, periodMs: number): void {
  poller?.cancel()
  const timer = $.clock.every(periodMs, () => void pollSafely($))
  poller = { cancel: () => timer.cancel(), ms: periodMs }
}

function randomHex8(): string {
  const bytes = crypto.getRandomValues(new Uint8Array(4))
  return Array.from(bytes, byte => byte.toString(16).padStart(2, '0')).join('')
}

/** Write one command file for the selected run (commands/<id>.json, written once). */
async function sendCommand($: Dollar, command: PaneAction): Promise<void> {
  const runId = await read($, selectedAtom)
  const spec = await read($, specAtom)
  const status = await read($, statusAtom)
  if (runId === null || spec?.run_id !== runId || !isControllable(spec, status)) {
    $.ui.toast(`${runId ?? 'No run'} takes no ${command.action} now: only an active live run takes commands.`)
    return
  }
  const now = await $.clock.now()
  const { id, text } = commandFile(now, randomHex8(), command)
  if (new TextEncoder().encode(text).length > COMMAND_MAX_BYTES) {
    $.ui.toast(`The ${command.action} command is over ${COMMAND_MAX_BYTES} bytes; send fewer values at once.`)
    return
  }
  await $.fs.write(`${await findStore($)}/runs/${runId}/commands/${id}.json`, text)
  await update($, sentAtom, list => [...list, { run_id: runId, id, action: command.action, sent_at: now }].slice(-50))
}

async function resumeRun($: Dollar): Promise<void> {
  const speed = await read($, speedAtom)
  await sendCommand($, speed === null ? { action: 'resume' } : { action: 'resume', speed })
}

async function submitSet($: Dollar, text: string): Promise<void> {
  const parsed = parseSet(text)
  if ('error' in parsed) $.ui.toast(parsed.error)
  else await sendCommand($, { action: 'set', values: parsed.values })
}

async function pickSpeed($: Dollar, value: string): Promise<void> {
  const speed = Number(value)
  await update($, speedAtom, () => (value !== 'keep' && Number.isFinite(speed) && speed > 0 ? speed : null))
}

/** The selected run's charted names, from the live.json on screen; null without one for the selected run. */
async function chartedNow($: Dollar): Promise<string[] | null> {
  const live = await read($, liveAtom)
  return live === null || live.run_id !== (await read($, selectedAtom)) ? null : chartedNames(live)
}

/** Chart one more recorded output: the shown selection plus `name` (nothing for a name already charted or a full chart). */
async function addChart($: Dollar, name: string): Promise<void> {
  const names = await chartedNow($)
  if (names === null || names.includes(name) || names.length >= CHART_MAX) return
  await sendCommand($, { action: 'chart', names: [...names, name] })
}

/** Remove one chart: the shown selection without `name`. */
async function removeChart($: Dollar, name: string): Promise<void> {
  const names = await chartedNow($)
  if (names === null || !names.includes(name)) return
  await sendCommand($, { action: 'chart', names: names.filter(charted => charted !== name) })
}

/** A click on a signal: chart it, or remove its chart; with CHART_MAX charts shown, say so and write nothing. */
async function toggleChart($: Dollar, name: string): Promise<void> {
  const names = await chartedNow($)
  if (names === null) return
  if (names.includes(name)) await removeChart($, name)
  else if (names.length >= CHART_MAX) $.ui.toast(`At most ${CHART_MAX} charts: remove one first (✕, or click a charted signal).`)
  else await addChart($, name)
}

async function pickRun($: Dollar, runId: string): Promise<void> {
  if (runId === (await read($, selectedAtom))) return
  await selectRun($, runId)
  await pollSafely($)
}

/** A row of the Runs tab: show that run on the Live tab. */
async function openRun($: Dollar, runId: string): Promise<void> {
  await pickRun($, runId)
  await update($, tabAtom, () => 'live' as const)
}

/** ◀ and ▶: the next older or newer run in the index, if there is one. */
async function pickNeighbour($: Dollar, older: boolean): Promise<void> {
  const next = neighbourRun(await read($, runsAtom), await read($, selectedAtom), older)
  if (next !== null) await pickRun($, next)
}

async function pickTab($: Dollar, tab: PaneTab): Promise<void> {
  await update($, tabAtom, () => tab)
}

/** The handlers the drawing's elements run (each reads $.state when pressed, not when drawn). */
function actionsFor($: Dollar): Actions {
  return {
    pick: runId => pickRun($, runId),
    open: runId => openRun($, runId),
    older: () => pickNeighbour($, true),
    newer: () => pickNeighbour($, false),
    tab: tab => pickTab($, tab),
    pause: () => sendCommand($, { action: 'pause' }),
    resume: () => resumeRun($),
    step: () => sendCommand($, { action: 'step', n: 1 }),
    stop: () => sendCommand($, { action: 'stop' }),
    set: text => submitSet($, text),
    speed: value => pickSpeed($, value),
    toggleChart: name => toggleChart($, name),
    removeChart: name => removeChart($, name),
  }
}

/** What the drawing reads (and so redraws on): the selected run's documents only. */
async function shown($: Dollar): Promise<Shown> {
  const selected = await read($, selectedAtom)
  const mine = <T extends { run_id: string }>(doc: T | null): T | null => (doc?.run_id === selected ? doc : null)
  return {
    store: await read($, storeAtom),
    runs: await read($, runsAtom),
    selected,
    problem: await read($, problemAtom),
    status: mine(await read($, statusAtom)),
    spec: mine(await read($, specAtom)),
    live: mine(await read($, liveAtom)),
    speed: await read($, speedAtom),
    builds: await read($, buildsAtom),
    tab: await read($, tabAtom),
  }
}

function picker(t: Common, view: Shown, act: Actions): JSX.Element {
  const { Select, Text } = t
  if (view.runs.length === 0) return <Text dimColor>no runs yet</Text>
  if (Select === undefined) return <Text>{`Run ${view.selected ?? '-'}`}</Text>
  const value = view.selected === null ? {} : { value: view.selected }
  return <Select key="run" label="Run" options={pickerOptions(view.runs, view.selected)} {...value} onSelect={act.pick} />
}

/** ◀ run picker ▶, then the tab buttons (the open one drawn as the primary button). */
function header(t: Common, view: Shown, act: Actions): JSX.Element {
  const { Box, Button } = t
  return (
    <Box flexDirection="column">
      <Box flexDirection="row" gap={1}>
        <Button key="older" label="◀" plain onPress={act.older} />
        {picker(t, view, act)}
        <Button key="newer" label="▶" plain onPress={act.newer} />
      </Box>
      <Box flexDirection="row" gap={1}>
        {TABS.map(tab => (
          <Button
            key={`tab:${tab.id}`}
            label={`${tab.hotkey} ${tab.label}`}
            hotkey={tab.hotkey}
            {...(view.tab === tab.id ? { variant: 'primary' as const } : { dimColor: true })}
            onPress={() => act.tab(tab.id)}
          />
        ))}
      </Box>
    </Box>
  )
}

/** Pause/Resume/Step/Stop, the resume speed and the Set field: active live runs only. */
function controls(t: Common, view: Shown, act: Actions): JSX.Element | null {
  const { Box, Button, Input, Select } = t
  if (view.status === null || view.spec === null || !isControllable(view.spec, view.status)) return null
  const state = view.status.state
  const isPaused = state === 'paused'
  const hints = view.spec.settable_hints.slice(0, 4)
  return (
    <Box flexDirection="column">
      <Box flexDirection="row" gap={1}>
        {state === 'running' && <Button key="pause" label="Pause" onPress={act.pause} />}
        {isPaused && <Button key="resume" label="Resume" variant="primary" onPress={act.resume} />}
        {isPaused && <Button key="step" label="Step" onPress={act.step} />}
        <Button key="stop" label="Stop" onPress={act.stop} />
        {isPaused && Select !== undefined && (
          <Select key="speed" label="at" options={SPEEDS} value={view.speed === null ? 'keep' : String(view.speed)} onSelect={act.speed} />
        )}
      </Box>
      {state !== 'queued' && Input !== undefined && (
        <Input key="set" label="Set" placeholder={hints.length === 0 ? 'name=value' : `name=value (${hints.join(', ')})`} submitLabel="set" onSubmit={act.set} />
      )}
    </Box>
  )
}

/**
 * One signal of the values table, padded to `width`: for an active live run a button that toggles
 * the signal's chart (a charted signal's dot is in its chart's colour, so the dot is the legend),
 * else plain text with the same marks.
 */
function signalCell(t: Common, cell: SignalCell, width: number, canEdit: boolean, act: Actions): JSX.Element {
  const { Button, Text } = t
  const text = signalText(cell)
  if (!canEdit) return <Text key={`cell:${cell.name}`}>{text.padEnd(width)}</Text>
  const mark = cell.charted ? <Text color={hexColor(cell.color)}>{CHARTED_MARK}</Text> : UNCHARTED_MARK
  return (
    <Button key={`signal:${cell.name}`} label={text} plain onPress={() => act.toggleChart(cell.name)}>
      {mark}
      {` ${signalDetail(cell)}`.padEnd(width - 1)}
    </Button>
  )
}

/** The values by component: a label column, then the signals flowing left to right (cut at SIGNAL_MAX, then "... and N more"). */
function signalTable(t: Common, live: AfmiPaneLive, spec: AfmiPaneSpec | null, canEdit: boolean, columns: number, act: Actions): JSX.Element | null {
  const { Box, Text } = t
  const { rows, more } = signalRows(signalGroups(live, spec), columns)
  if (rows.length === 0) return null
  return (
    <Box flexDirection="column">
      {rows.map((row, i) => (
        <Box key={`signalrow:${i}`} flexDirection="row">
          <Text bold>{row.label}</Text>
          {row.cells.map(cell => signalCell(t, cell, row.width, canEdit, act))}
        </Box>
      ))}
      {more > 0 && <Text dimColor>{`... and ${more} more`}</Text>}
    </Box>
  )
}

/** How to chart: one dim line under the values (active live runs only). */
function chartHint(t: Common, live: AfmiPaneLive): JSX.Element {
  const { Text } = t
  const count = chartedNames(live).length
  const hint = `Charts (${count} of ${CHART_MAX}): click a signal to chart it; ✕ or a second click removes it.`
  return <Text dimColor>{count === 0 ? 'No charts: click a signal above to chart one.' : hint}</Text>
}

/** One chart: its coloured title with a ✕ that removes it (only where `canEdit`), then the drawing. */
function chartBox(t: Common, chart: ChartSeries, canEdit: boolean, act: Actions, plot: JSX.Element): JSX.Element {
  const { Box, Button, Text } = t
  return (
    <Box key={`chartbox:${chart.name}`} flexDirection="column">
      <Box key={`title:${chart.name}`} flexDirection="row">
        <Box flexGrow={1}>
          <Text key={`label:${chart.name}`} bold color={hexColor(chart.color)}>{chart.title}</Text>
        </Box>
        {canEdit && <Button key={`unchart:${chart.name}`} label="✕" plain onPress={() => act.removeChart(chart.name)} />}
      </Box>
      {plot}
    </Box>
  )
}

function headText(view: Shown): string {
  if (view.status !== null) {
    const run = view.runs.find(row => row.run_id === view.selected) ?? null
    return headLine(view.status, view.spec, runName(view.spec, run))
  }
  if (view.selected !== null) return `${view.selected}: reading its status...`
  return `No runs in ${view.store ?? 'the run store'} yet. Start one with run_start.`
}

/** A built FMU's card in a rounded frame: name, backend, interfaces, counts, names, file, hash, warnings. */
function buildCard(t: Common, build: AfmiPaneBuild): JSX.Element {
  const { Box, Text } = t
  return (
    <Box flexDirection="column" borderStyle="round" paddingX={1}>
      <Text bold>{`Built FMU ${build.name}`}</Text>
      {buildCardLines(build).map((line, i) => <Text key={`card:${i}`}>{line}</Text>)}
    </Box>
  )
}

/** The Live tab: the status frame (head, progress bar, why it ended), controls, values, charts. */
function liveTab(t: Common, view: Shown, act: Actions, drawn: Drawn): JSX.Element {
  const { Box, Text } = t
  const status = view.status
  const live = view.live
  const isLiveKnown = live !== null && live.protocol === PROTOCOL
  const canEdit = isControllable(view.spec, status)
  const detail = status === null ? null : detailLine(status)
  const isBuild = view.spec?.kind === 'build'
  const build = isBuild ? (view.builds.find(card => card.run_id === view.selected) ?? null) : null
  return (
    <Box flexDirection="column" gap={1}>
      <Box flexDirection="column" borderStyle="round" paddingX={1}>
        <Text bold>{headText(view)}</Text>
        {status !== null && status.protocol === PROTOCOL && !isBuild && <Text>{progressBar(status.progress)}</Text>}
        {detail !== null && <Text dimColor>{detail}</Text>}
      </Box>
      {controls(t, view, act)}
      {live !== null && !isLiveKnown && <Text>{`live.json: unsupported protocol version ${live.protocol}`}</Text>}
      {isLiveKnown && signalTable(t, live, view.spec, canEdit, drawn.columns, act)}
      {isLiveKnown && canEdit && chartHint(t, live)}
      {isLiveKnown && drawn.charts}
      {build !== null && buildCard(t, build)}
    </Box>
  )
}

/** The System tab: a system run's components and its connections (diagram or list), else a hint. */
function systemTab(t: Common, view: Shown, drawn: Drawn): JSX.Element {
  const { Box, Text } = t
  const spec = view.spec
  if (spec === null) return <Text dimColor>{view.selected === null ? 'No run selected.' : `${view.selected}: reading the run...`}</Text>
  if (spec.kind !== 'system') {
    return <Text dimColor>{`${spec.run_id} is not a system run (${spec.name}). Pick a system run to see its components and connections.`}</Text>
  }
  return (
    <Box flexDirection="column" gap={1}>
      <Text bold>{`Components (${spec.components.length}): ${spec.components.join(', ')}`}</Text>
      {drawn.diagram}
    </Box>
  )
}

/** The FMUs tab: the selected build's card (else the newest) and every new FMU, one press to show its card. */
function fmusTab(t: Common, view: Shown, act: Actions): JSX.Element {
  const { Box, Button, Text } = t
  if (view.builds.length === 0) return <Text dimColor>No FMUs built yet. Ask Claude to build one with fmu_build.</Text>
  const card = view.builds.find(build => build.run_id === view.selected) ?? view.builds[0]!
  return (
    <Box flexDirection="column" gap={1}>
      {buildCard(t, card)}
      <Box flexDirection="column">
        <Text bold>New FMUs</Text>
        {view.builds.map(build => (
          <Button
            key={`fmu:${build.run_id}`}
            label={`${build.run_id === card.run_id ? '▸' : ' '} ${buildListLine(build)}`}
            plain
            {...(build.run_id === card.run_id ? {} : { dimColor: true })}
            onPress={() => act.pick(build.run_id)}
          />
        ))}
      </Box>
    </Box>
  )
}

/** The Runs tab: the newest RUN_ROWS runs; a press opens the run on the Live tab. */
function runsTab(t: Common, view: Shown, act: Actions): JSX.Element {
  const { Box, Button, Text } = t
  if (view.runs.length === 0) return <Text dimColor>{headText(view)}</Text>
  return (
    <Box flexDirection="column">
      {view.runs.slice(0, RUN_ROWS).map(run => (
        <Button
          key={`runrow:${run.run_id}`}
          label={runListLine(run, view.selected)}
          plain
          {...(run.run_id === view.selected ? {} : { dimColor: true })}
          onPress={() => act.open(run.run_id)}
        />
      ))}
      {view.runs.length > RUN_ROWS && <Text dimColor>{`... and ${view.runs.length - RUN_ROWS} older`}</Text>}
    </Box>
  )
}

/** The pane: the header, the open tab, and a problem reading the store if there is one. */
function paneBody(t: Common, view: Shown, act: Actions, drawn: Drawn): JSX.Element {
  const { Box, Text } = t
  const tabs: Record<PaneTab, () => JSX.Element> = {
    live: () => liveTab(t, view, act, drawn),
    system: () => systemTab(t, view, drawn),
    fmus: () => fmusTab(t, view, act),
    runs: () => runsTab(t, view, act),
  }
  return (
    <Box flexDirection="column" gap={1}>
      {header(t, view, act)}
      {(tabs[view.tab] ?? tabs.live)()}
      {view.problem !== null && <Text dimColor>{view.problem}</Text>}
    </Box>
  )
}

export const register: Register = on => {
  on('session.start', async ($, e, next) => {
    await $.command.register({ name: 'fmi', description: 'Open the Agentic FMI live pane' })
    isPaneOpen = (await $.ui.panes()).some(pane => pane.id === PANE)
    startPolling($, isPaneOpen ? POLL_MS : IDLE_POLL_MS)
    return next(e)
  })

  on('command.run', { command: 'fmi' }, async $ => {
    const opened = await $.ui.open({ id: PANE, title: 'Agentic FMI' })
    isPaneOpen = true
    startPolling($, POLL_MS)
    await pollSafely($)
    return { text: opened.isPlaced ? 'Agentic FMI pane opened.' : `Agentic FMI pane waits: ${opened.reason}` }
  })

  on('ui.close', { id: PANE }, async ($, e, next) => {
    isPaneOpen = false
    startPolling($, IDLE_POLL_MS)
    return next(e)
  })

  on('ui.render', { component: 'Pane', requestId: PANE }, async ($, e) => {
    const view = await shown($)
    const act = actionsFor($)
    const live = view.tab === 'live' ? view.live : null
    const canEdit = isControllable(view.spec, view.status)
    const system = view.tab === 'system' && view.spec?.kind === 'system' ? view.spec : null
    const bodyColumns = Math.max(1, Math.floor(e.props.bodyColumns))
    if (e.surface === 'terminal') {
      const t = $.ui.resolve(e)
      const { Box, Raster, Text } = t
      const columns = Math.min(MAX_RASTER_COLUMNS, bodyColumns)
      const charts = live === null ? [] : chartSeries(live, Number.MAX_SAFE_INTEGER).map(chart => {
        const drawn = lineChartCells(chart.series, live.time, columns, CHART_ROWS, chart.color)
        const plot = <Raster key={`chart:${chart.name}`} columns={drawn.columns} rows={drawn.rows} cells={drawn.cells} />
        return chartBox(t, chart, canEdit, act, plot)
      })
      const diagram = system === null ? null : (
        <Box flexDirection="column">
          <Text bold>Connections</Text>
          {system.connections.length === 0 && <Text dimColor>none</Text>}
          {connectionRows(system.connections).map((line, i) => <Text key={`edge:${i}`}>{line}</Text>)}
        </Box>
      )
      return paneBody(t, view, act, { charts, diagram, columns: bodyColumns })
    }
    const t = $.ui.resolve(e)
    const { Box, Svg, Text } = t
    const charts = live === null ? [] : chartSeries(live, SVG_POINTS).map(chart => {
      const source = lineChartSvg(chart.series, live.time, SVG_WIDTH, SVG_HEIGHT, chart.color)
      return chartBox(t, chart, canEdit, act, <Svg key={`chart:${chart.name}`} source={source} alt={chart.label} />)
    })
    const diagram = system === null ? null : (
      <Box flexDirection="column">
        <Text bold>Connections</Text>
        <Svg key="diagram" source={diagramSvg(system.components, system.connections)} alt={diagramAlt(system.connections)} />
      </Box>
    )
    return paneBody(t, view, act, { charts, diagram, columns: bodyColumns })
  })
}

/** A palette colour (0x00RRGGBB) as the `#rrggbb` a Text colour takes. */
function hexColor(color: number): string {
  return `#${color.toString(16).padStart(6, '0')}`
}
