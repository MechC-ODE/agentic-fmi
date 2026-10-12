// The pane against a fake run store (../fixtures/store.ts, generated from the pydantic models),
// on the terminal and desktop surfaces: the header (run picker, ◀ ▶) and the tabs, the Live tab
// (status with progress bar, the values with units grouped by component, line charts, the chart
// picker: a click on a signal and a ✕ per chart), the buttons' command files (against the
// PANE_COMMANDS goldens), transitions and toasts, torn files, unknown versions and
// states, the worst-case live.json, the FMUs tab and build cards (a torn or foreign build.json,
// read once), the System tab (connection list on the terminal, Svg on the desktop), the Runs tab
// and store discovery. `claude plugin test pane` runs it; nothing touches a real filesystem.

import { describe, expect, mock, test } from 'claude-code/testing'
import type { On } from 'claude-code'

import { DISCOVERY, FILES, NOW_MS, PANE_COMMANDS, ROOT, STORE, VARIANTS, WORST_LIVE } from '../fixtures/store'
import { MAX_RASTER_COLUMNS, MAX_RASTER_ROWS, MAX_SVG_CHARS, diagramSvg } from './charts'

const SURFACES = ['terminal', 'desktop'] as const
const PLUGIN = 'agentic-fmi-pane'
const PANE_PROPS = {
  title: 'Agentic FMI',
  isFocused: false,
  bodyColumns: 60,
  placement: 'dock',
  scroll: { offset: 0, bodyRows: 40 },
  view: {},
} as const
const R1 = `${STORE}/runs/r1`
const R2 = `${STORE}/runs/r2`
const R4_BUILD = `${STORE}/runs/r4/build.json`
const R2_EDGES = [{ from: 'PI.u', to: 'plant.F' }, { from: 'plant.x', to: 'PI.y' }]
const CARD = [
  'Built FMU pi_controller',
  'pythonfmu, CoSimulation, built in 6.4 s',
  '5 variables: input 1, output 1, parameter 3',
  'inputs (1): y',
  'outputs (1): u',
  'parameters (3): Kp, Ki, r',
  'generated/pi_controller/pi_controller.fmu  sha256 0123456789ab',
  'no warnings',
]
const NEW_FMU_ROW = 'r4 pi_controller: pythonfmu, CoSimulation, 5 variables, 6.4 s'
/** The store documents the pane may read: never XML, SSP, events.jsonl, result.csv or an FMU. */
const READABLE = /\/(?:index|spec|status|live|build)\.json$|\/outcomes\/[^/]+\.json$/
const CONTROLS = ['pause', 'resume', 'step', 'stop', 'set', 'speed']
/** The chart picker's controls on r1 (charts h and v): none for a run that takes no commands. */
const CHART_CONTROLS = ['unchart:h', 'unchart:v']
const HINT = 'click a signal to chart it; ✕ or a second click removes it.'
const NO_CHARTS = 'No charts: click a signal above to chart one.'
const TOO_MANY = 'At most 3 charts: remove one first (✕, or click a charted signal).'

type World = {
  writes: { path: string; text: string }[]
  reads: string[]
  toasts: string[]
  lines: (string | undefined)[]
  stats: string[]
  put: (path: string, text: string) => void
  text: (path: string) => string
}

/** A run store in memory under `store` (FILES moved there), answering $.fs, the env and the UI. */
function fakeStore(on: On, env: Record<string, string> = {}, store: string = STORE): World {
  const files = new Map<string, { text: string; mtimeMs: number }>()
  let tick = 0
  const world: World = {
    writes: [],
    reads: [],
    toasts: [],
    lines: [],
    stats: [],
    put: (path, text) => void files.set(path, { text, mtimeMs: NOW_MS + ++tick }),
    text: path => files.get(path)?.text ?? '',
  }
  for (const [path, text] of Object.entries(FILES)) world.put(store + path.slice(STORE.length), text)
  mock.env(on, env)
  on('session.root', () => ({ value: ROOT }))
  on('ui.open', () => ({ value: { isPlaced: true } }))
  on('ui.panes', () => ({ value: [] }))
  on('ui.toast', ($, e) => {
    world.toasts.push(e.text)
    return { value: undefined }
  })
  on('ui.status', ($, e) => {
    world.lines.push(e.text)
    return { value: undefined }
  })
  on('fs.stat', ($, e) => {
    world.stats.push(e.path)
    const file = files.get(e.path)
    if (file === undefined) return { deny: `ENOENT: no such file, stat '${e.path}'` }
    return { value: { kind: 'file' as const, size: file.text.length, mtimeMs: file.mtimeMs, isLink: false } }
  })
  on('fs.read', ($, e) => {
    world.reads.push(e.path)
    const file = files.get(e.path)
    return file === undefined ? { deny: `ENOENT: no such file, open '${e.path}'` } : { value: file.text }
  })
  on('fs.write', ($, e) => {
    world.writes.push({ path: e.path, text: e.text })
    world.put(e.path, e.text)
    return { value: undefined }
  })
  return world
}

type Ui = Awaited<ReturnType<typeof openPane>>

/** A cell of the values table drawn as text (a run that takes no commands): the text, padded with spaces. */
function cellText(text: string): RegExp {
  return new RegExp(`^${text.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')} *$`)
}

/** The keys of the values table's buttons, in the order drawn. */
async function signalKeys(ui: Ui): Promise<string[]> {
  return (await ui.findAll({ type: 'Button' })).map(button => String(button.key)).filter(key => key.startsWith('signal:'))
}

/** A signal button's label (its cell text), or undefined without such a button. */
async function signalLabel(ui: Ui, name: string): Promise<unknown> {
  return (await ui.find({ type: 'Button', key: `signal:${name}` }))?.props.label
}

/** The colour of a charted signal's dot (the Text in its button), undefined for an uncharted one. */
async function dotColour(ui: Ui, name: string): Promise<unknown> {
  const dot = (await ui.find({ type: 'Button', key: `signal:${name}` }))?.children[0] as { props?: { color?: unknown } } | string | undefined
  return typeof dot === 'object' ? dot.props?.color : undefined
}

/** The fixture document at `path` with some fields replaced. */
function edited(world: World, path: string, fields: Record<string, unknown>): string {
  return JSON.stringify({ ...JSON.parse(world.text(path)), ...fields })
}

/** A live.json of `path` with `seq` raised and the named series left out (their values stay listed). */
function withoutSeries(world: World, path: string, ...names: string[]): string {
  const live = JSON.parse(world.text(path))
  live.seq += 1
  for (const name of names) delete live.series[name]
  return JSON.stringify(live)
}

/** A command file's text with the random part and the time taken out. */
function normal(text: string): string {
  return text.replace(/\d{13}-pane-[0-9a-f]{8}/, '<id>').replace(/"issued_at":"[^"]*"/, '"issued_at":"<t>"')
}

async function openPane($: Parameters<Parameters<typeof test>[1]>[0], surface: (typeof SURFACES)[number], bodyColumns = 60) {
  return $.ui.mount({ plugin: PLUGIN, surface, component: 'Pane', requestId: 'fmi', props: { ...PANE_PROPS, bodyColumns } })
}

describe('the pane', () => {
  test('/fmi shows the newest active run: picker, status, values with units, a chart per variable', async ($, on) => {
    mock.clock(on, { now: NOW_MS })
    const world = fakeStore(on)
    expect((await $.command.run({ command: 'fmi' })).text).toBe('Agentic FMI pane opened.')
    expect(world.lines.at(-1)).toBe('FMI ▶ r2 PI+plant 42% t=4.2 s 2x')
    for (const surface of SURFACES) {
      const ui = await openPane($, surface)
      const picker = await ui.find({ type: 'Select', key: 'run' })
      expect(picker?.props.value).toBe('r2')
      expect((picker?.props.options as { value: string }[]).map(option => option.value)).toEqual(['r5', 'r4', 'r3', 'r2', 'r1'])
      expect(await ui.find({ type: 'Text', text: '▶ r2 PI+plant: running, t = 4.2 of 10 s (42%), live, 2x' })).toBeDefined()
      // the values are grouped by component in spec order (PI, plant), with the short names
      expect((await ui.findAll({ type: 'Text', text: /^(PI|plant) +$/ })).map(label => label.text)).toEqual(['PI    ', 'plant '])
      expect(await signalKeys(ui)).toEqual(['signal:PI.u', 'signal:plant.x'])
      expect(await signalLabel(ui, 'PI.u')).toBe('● u 0.00504197 N')
      expect(await signalLabel(ui, 'plant.x')).toBe('● x 0.985013 m')
      expect(await ui.find({ type: 'Text', text: '████████░░░░░░░░░░░░ 42%' })).toBeDefined()
      expect(await ui.find({ type: 'Text', text: 'plant.x [m]' })).toBeDefined()
      expect(await ui.find({ type: 'Text', text: `Charts (2 of 3): ${HINT}` })).toBeDefined()
      expect(await ui.find({ type: 'Select', key: 'addchart' })).toBeUndefined()
      expect(await ui.find({ type: 'Button', key: 'unchart:plant.x' })).toBeDefined()
      expect((await ui.find({ type: 'Button', key: 'unchart:PI.u' }))?.props.label).toBe('✕')
      expect(await ui.find({ type: 'Button', key: 'pause' })).toBeDefined()
      expect(await ui.find({ type: 'Button', key: 'resume' })).toBeUndefined()
      expect((await ui.find({ type: 'Button', key: 'tab:live' }))?.props.variant).toBe('primary')
      expect(await ui.find({ type: 'Text', text: 'Connections' })).toBeUndefined()
      const rasters = await ui.findAll({ type: 'Raster' })
      const svgs = await ui.findAll({ type: 'Svg' })
      if (surface === 'terminal') {
        expect(rasters.map(raster => raster.key)).toEqual(['chart:plant.x', 'chart:PI.u'])
        expect(rasters[0]?.props.columns).toBe(60)
        expect(rasters[0]?.props.rows).toBe(10)
        expect(String(rasters[0]?.props.cells).length).toBe((60 * 10 * 12 * 4) / 3)
        expect(svgs).toHaveLength(0)
      } else {
        expect(svgs).toHaveLength(2)
        expect(String(svgs[0]?.props.source)).toContain('<polyline')
        expect(String(svgs[0]?.props.source)).toContain('4.2 s</text>')
        expect(String(svgs[0]?.props.alt)).toMatch(/^plant\.x \[m\]  .* over t 0 \.\. 4\.2 s$/)
        expect(rasters).toHaveLength(0)
      }
      await ui.unmount()
    }
  })

  test('the picker shows another run; a paused live run offers Resume, Step, Stop, speed and Set', async ($, on) => {
    mock.clock(on, { now: NOW_MS })
    fakeStore(on)
    await $.command.run({ command: 'fmi' })
    for (const surface of SURFACES) {
      const ui = await openPane($, surface)
      await ui.select({ key: 'run', value: 'r1' })
      expect(await ui.find({ type: 'Text', text: '‖ r1 BouncingBall: paused, t = 1.25 of 3 s (42%), live, 1x' })).toBeDefined()
      // a single FMU's values are one group named after the run, the names whole
      expect(await ui.find({ type: 'Text', text: /^BouncingBall +$/ })).toBeDefined()
      expect(await signalKeys(ui)).toEqual(['signal:h', 'signal:v'])
      expect(await signalLabel(ui, 'h')).toBe('● h 0.290576 m')
      expect(await signalLabel(ui, 'v')).toBe('● v -1.4625 m/s')
      for (const key of ['resume', 'step', 'stop']) expect(await ui.find({ type: 'Button', key })).toBeDefined()
      expect(await ui.find({ type: 'Button', key: 'pause' })).toBeUndefined()
      expect((await ui.find({ type: 'Input', key: 'set' }))?.props.placeholder).toBe('name=value (e)')
      expect((await ui.find({ type: 'Select', key: 'speed' }))?.props.value).toBe('keep')
      await ui.select({ key: 'run', value: 'r2' })
      await ui.unmount()
    }
  })

  test('a system\'s values are grouped by component in spec order, unknown components last; cells flow as the pane is wide', async ($, on) => {
    mock.clock(on, { now: NOW_MS })
    const world = fakeStore(on)
    const live = JSON.parse(world.text(`${R2}/live.json`))
    live.values.push({ name: 'PI.y', unit: null, value: 1.5 }, { name: 'ghost.z', unit: 'K', value: null }, { name: 'plain', unit: null, value: 7 })
    world.put(`${R2}/live.json`, JSON.stringify(live))
    await $.command.run({ command: 'fmi' })
    for (const surface of SURFACES) {
      for (const [columns, rows] of [[60, 4], [30, 5]] as const) {
        const ui = await openPane($, surface, columns)
        // PI holds u and y (cells of 18 columns): both on one row at 60 columns, one to a row at 30 (a blank label under PI)
        expect((await ui.findAll({ type: 'Text', text: /^(PI|plant|ghost|plain) +$/ })).map(label => label.text)).toEqual(['PI    ', 'plant ', 'ghost ', 'plain '])
        expect((await ui.findAll({ type: 'Text', text: /^ +$/ })).length).toBe(rows - 4)
        expect(await signalKeys(ui)).toEqual(['signal:PI.u', 'signal:PI.y', 'signal:plant.x', 'signal:ghost.z', 'signal:plain'])
        expect(await signalLabel(ui, 'PI.y')).toBe('○ y 1.5')
        expect(await signalLabel(ui, 'ghost.z')).toBe('○ z n/a')
        expect(await signalLabel(ui, 'plain')).toBe('○ plain 7')
        expect((await ui.findAll({ type: 'Box' })).filter(box => String(box.key).startsWith('signalrow:'))).toHaveLength(rows)
        await ui.unmount()
      }
    }
  })

  test('each button writes one command file that matches its golden', async ($, on) => {
    mock.clock(on, { now: NOW_MS })
    const world = fakeStore(on)
    await $.command.run({ command: 'fmi' })
    for (const surface of SURFACES) {
      const ui = await openPane($, surface)
      const presses: [string, string, () => Promise<unknown>][] = [
        ['r2', 'pause', () => ui.press({ key: 'pause' })],
        ['r1', 'resume', () => ui.press({ key: 'resume' })],
        ['r1', 'step', () => ui.press({ key: 'step' })],
        ['r1', 'set', () => ui.input({ key: 'set', text: 'e=0.6' })],
        ['r1', 'stop', () => ui.press({ key: 'stop' })],
        ['r1', 'chart', () => ui.press({ key: 'unchart:v' })], // r1 charts h and v: v removed leaves h
        ['r1', 'chart', () => ui.press({ key: 'signal:v' })], // a click on the charted v removes it: the same file
      ]
      for (const [run, button, press] of presses) {
        await ui.select({ key: 'run', value: run })
        const before = world.writes.length
        await press()
        expect(world.writes.length, `${surface} ${button}`).toBe(before + 1)
        const written = world.writes.at(-1)!
        const id = JSON.parse(written.text).id as string
        expect(id).toMatch(/^\d{13}-pane-[0-9a-f]{8}$/)
        expect(written.path).toBe(`${STORE}/runs/${run}/commands/${id}.json`)
        expect(normal(written.text)).toBe(normal(PANE_COMMANDS[button]!))
      }
      await ui.select({ key: 'run', value: 'r2' })
      await ui.unmount()
    }
    expect(world.writes.filter(write => write.text.includes('"issued_at":"2025-10-09T08:53:20.000Z"')).length).toBe(14)
  })

  test('Resume sends the picked speed; a bad Set is refused with a toast and writes nothing', async ($, on) => {
    mock.clock(on, { now: NOW_MS })
    const world = fakeStore(on)
    await $.command.run({ command: 'fmi' })
    for (const surface of SURFACES) {
      const ui = await openPane($, surface)
      await ui.select({ key: 'run', value: 'r1' })
      await ui.select({ key: 'speed', value: '10' })
      await ui.press({ key: 'resume' })
      expect(JSON.parse(world.writes.at(-1)!.text)).toMatchObject({ action: 'resume', speed: 10 })
      await ui.select({ key: 'speed', value: 'keep' })
      const before = world.writes.length
      await ui.input({ key: 'set', text: 'e' })
      await ui.input({ key: 'set', text: 'e=fast' })
      expect(world.writes.length).toBe(before)
      expect(world.toasts.slice(-2)).toEqual([
        'Set expects name=value (for example e=0.6), not "e".',
        'e: "fast" is not a number, true or false.',
      ])
      await ui.select({ key: 'run', value: 'r2' })
      await ui.unmount()
    }
  })

  test('no buttons for a batch run, even a running one, or for a finished live run', async ($, on) => {
    const clock = mock.clock(on, { now: NOW_MS })
    const world = fakeStore(on)
    world.put(`${STORE}/runs/r3/status.json`, edited(world, `${STORE}/runs/r3/status.json`, { state: 'running', t: 1, progress: 0.33 }))
    // a batch run has no live.json; if one is there anyway, its charts are drawn without a picker
    world.put(`${STORE}/runs/r3/live.json`, withoutSeries(world, `${R1}/live.json`, 'v').replace('"r1"', '"r3"'))
    await $.command.run({ command: 'fmi' })
    for (const surface of SURFACES) {
      const ui = await openPane($, surface)
      await ui.select({ key: 'run', value: 'r3' })
      expect(await ui.find({ type: 'Text', text: /^▶ r3 BouncingBall: running, .*, batch$/ })).toBeDefined()
      for (const key of [...CONTROLS, ...CHART_CONTROLS]) expect(await ui.find({ key })).toBeUndefined()
      expect(await ui.find({ type: 'Text', text: /^(Charts \(|No charts)/ })).toBeUndefined()
      expect(await ui.find({ type: 'Input' })).toBeUndefined()
      // the values are still listed, with the marks of the charts drawn (no buttons to change them)
      expect(await signalKeys(ui)).toEqual([])
      expect(await ui.find({ type: 'Text', text: cellText('● h 0.290576 m') })).toBeDefined()
      expect(await ui.find({ type: 'Text', text: cellText('○ v -1.4625 m/s') })).toBeDefined()
      expect(await ui.findAll({ type: surface === 'terminal' ? 'Raster' : 'Svg' })).toHaveLength(1)
      await ui.select({ key: 'run', value: 'r2' })
      await ui.unmount()
    }
    world.put(`${R1}/status.json`, edited(world, `${R1}/status.json`, { state: 'finished', t: 3, progress: 1, speed: null }))
    world.put(`${R1}/live.json`, withoutSeries(world, `${R1}/live.json`, 'v')) // v could be added, were it running
    for (const surface of SURFACES) {
      const ui = await openPane($, surface)
      await ui.select({ key: 'run', value: 'r1' })
      await clock.advance(250)
      expect(await ui.find({ type: 'Text', text: /^✓ r1 BouncingBall: finished/ })).toBeDefined()
      for (const key of [...CONTROLS, ...CHART_CONTROLS]) expect(await ui.find({ key })).toBeUndefined()
      expect(await ui.find({ type: 'Text', text: /^(Charts \(|No charts)/ })).toBeUndefined()
      expect(await signalKeys(ui)).toEqual([])
      expect(await ui.find({ type: 'Text', text: cellText('● h 0.290576 m') })).toBeDefined()
      expect(await ui.find({ type: 'Text', text: cellText('○ v -1.4625 m/s') })).toBeDefined()
      expect(await ui.findAll({ type: surface === 'terminal' ? 'Raster' : 'Svg' })).toHaveLength(1)
      await ui.select({ key: 'run', value: 'r2' })
      await ui.unmount()
    }
  })

  test('a new live.json redraws; a torn or empty one keeps the previous data', async ($, on) => {
    const clock = mock.clock(on, { now: NOW_MS })
    const world = fakeStore(on)
    await $.command.run({ command: 'fmi' })
    for (const surface of SURFACES) {
      const ui = await openPane($, surface)
      await ui.select({ key: 'run', value: 'r1' })
      const fresh = JSON.parse(FILES[`${R1}/live.json`]!)
      fresh.seq = 43
      fresh.values[0].value = surface === 'terminal' ? 0.5 : 0.25
      world.put(`${R1}/live.json`, JSON.stringify(fresh))
      await clock.advance(250)
      const shown = `● h ${fresh.values[0].value} m`
      expect(await signalLabel(ui, 'h')).toBe(shown)
      for (const broken of [VARIANTS.live_torn!, VARIANTS.live_empty!]) {
        world.put(`${R1}/live.json`, broken)
        world.put(`${R1}/status.json`, broken === '' ? '' : world.text(`${R1}/status.json`).slice(0, 40))
        await clock.advance(250)
        expect(await signalLabel(ui, 'h')).toBe(shown)
        expect(await ui.find({ type: 'Text', text: /^‖ r1 BouncingBall: paused/ })).toBeDefined()
        expect(await ui.find({ type: 'Button', key: 'resume' })).toBeDefined()
      }
      world.put(`${R1}/status.json`, FILES[`${R1}/status.json`]!)
      await ui.select({ key: 'run', value: 'r2' })
      await ui.unmount()
    }
  })

  test('protocol 2 and unknown states are shown generically, without buttons', async ($, on) => {
    const clock = mock.clock(on, { now: NOW_MS })
    const world = fakeStore(on)
    await $.command.run({ command: 'fmi' })
    for (const surface of SURFACES) {
      const ui = await openPane($, surface)
      await ui.select({ key: 'run', value: 'r1' })
      world.put(`${R1}/status.json`, VARIANTS.status_protocol_2!)
      await clock.advance(250)
      expect(await ui.find({ type: 'Text', text: /^r1: unsupported protocol version 2;/ })).toBeDefined()
      expect(world.lines.at(-1)).toBe('FMI r1: unsupported protocol version 2')
      for (const key of CONTROLS) expect(await ui.find({ key })).toBeUndefined()
      world.put(`${R1}/status.json`, VARIANTS.status_unknown_state!)
      await clock.advance(250)
      expect(await ui.find({ type: 'Text', text: '? r1 BouncingBall: hibernating, t = 1.25 of 3 s (42%), live' })).toBeDefined()
      expect(world.lines.at(-1)).toBe('FMI ? r1 BouncingBall hibernating 42% t=1.25 s')
      for (const key of CONTROLS) expect(await ui.find({ key })).toBeUndefined()
      world.put(`${R1}/live.json`, edited(world, `${R1}/live.json`, { protocol: 2, seq: 99 }))
      await clock.advance(250)
      expect(await ui.find({ type: 'Text', text: 'live.json: unsupported protocol version 2' })).toBeDefined()
      expect(await ui.find({ type: 'Text', text: /0\.290576/ })).toBeUndefined()
      expect(await signalKeys(ui)).toEqual([])
      world.put(`${R1}/status.json`, FILES[`${R1}/status.json`]!)
      world.put(`${R1}/live.json`, FILES[`${R1}/live.json`]!)
      await ui.select({ key: 'run', value: 'r2' })
      await ui.unmount()
    }
  })

  test('toasts on state transitions and on the outcome of a command the pane sent', async ($, on) => {
    const clock = mock.clock(on, { now: NOW_MS })
    const world = fakeStore(on)
    await $.command.run({ command: 'fmi' })
    expect(world.toasts).toEqual([])
    const ui = await openPane($, 'terminal')
    await ui.press({ key: 'pause' })
    const id = JSON.parse(world.writes.at(-1)!.text).id as string
    world.put(`${R2}/outcomes/${id}.json`, JSON.stringify({ protocol: 1, run_id: 'r2', id, source: 'pane', action: 'pause', status: 'applied', t: 4.2, at: 'x', message: null }))
    world.put(`${R2}/status.json`, edited(world, `${R2}/status.json`, { state: 'paused' }))
    await clock.advance(250)
    expect(world.toasts).toEqual(['r2 PI+plant: running → paused', 'r2 pause applied at t=4.2'])
    expect(world.lines.at(-1)).toBe('FMI ‖ r2 PI+plant 42% t=4.2 s 2x')
    const index = JSON.parse(world.text(`${STORE}/index.json`))
    index.runs[1].state = 'paused'
    index.runs[4].state = 'stopped'
    index.runs.push({ ...index.runs[0], run_id: 'r6', state: 'queued' })
    world.put(`${STORE}/index.json`, JSON.stringify(index))
    await clock.advance(250)
    expect(world.toasts.slice(2)).toEqual(['r5 BouncingBall: failed → stopped', 'r6 BouncingBall: queued'])
    expect((await ui.find({ type: 'Select', key: 'run' }))?.props.value).toBe('r6')
    await ui.unmount()
  })

  test('a click on a signal writes a chart command with the whole new selection; ✕ removes a chart', async ($, on) => {
    mock.clock(on, { now: NOW_MS })
    const world = fakeStore(on)
    world.put(`${R1}/live.json`, withoutSeries(world, `${R1}/live.json`, 'v')) // charts h; v is recorded, not charted
    await $.command.run({ command: 'fmi' })
    for (const surface of SURFACES) {
      const ui = await openPane($, surface)
      await ui.select({ key: 'run', value: 'r1' })
      expect(await ui.find({ type: 'Text', text: `Charts (1 of 3): ${HINT}` })).toBeDefined()
      expect(await ui.find({ type: 'Select', key: 'addchart' })).toBeUndefined()
      expect(await ui.findAll({ type: 'Button' }).then(buttons => buttons.map(b => b.key).filter(key => String(key).startsWith('unchart:')))).toEqual(['unchart:h'])
      expect(await signalLabel(ui, 'h')).toBe('● h 0.290576 m')
      expect(await signalLabel(ui, 'v')).toBe('○ v -1.4625 m/s')
      const before = world.writes.length
      await ui.press({ key: 'signal:v' })
      expect(world.writes.length).toBe(before + 1)
      const written = world.writes.at(-1)!
      expect(written.path).toMatch(new RegExp(`^${STORE}/runs/r1/commands/\\d{13}-pane-[0-9a-f]{8}\\.json$`))
      expect(JSON.parse(written.text)).toMatchObject({ source: 'pane', action: 'chart', names: ['h', 'v'] })
      expect(normal(written.text)).toBe(normal(PANE_COMMANDS.chart!).replace('"names":["h"]', '"names":["h","v"]'))
      await ui.press({ key: 'signal:h' }) // h is charted: a second click removes it
      expect(world.writes.length).toBe(before + 2)
      expect(JSON.parse(world.writes.at(-1)!.text)).toMatchObject({ action: 'chart', names: [] }) // no chart left is allowed
      await ui.press({ key: 'unchart:h' })
      expect(JSON.parse(world.writes.at(-1)!.text)).toMatchObject({ action: 'chart', names: [] })
      expect(world.toasts).toEqual([])
      await ui.select({ key: 'run', value: 'r2' })
      await ui.unmount()
    }
  })

  test('with no chart the pane says how to add one; at three charts a click on another signal only toasts', async ($, on) => {
    const clock = mock.clock(on, { now: NOW_MS })
    const world = fakeStore(on)
    await $.command.run({ command: 'fmi' })
    for (const surface of SURFACES) {
      const ui = await openPane($, surface)
      await ui.select({ key: 'run', value: 'r1' })
      world.put(`${R1}/live.json`, withoutSeries(world, `${R1}/live.json`, 'h', 'v'))
      await clock.advance(250)
      expect(await ui.find({ type: 'Text', text: NO_CHARTS })).toBeDefined()
      expect(await ui.find({ type: 'Text', text: /^Charts \(/ })).toBeUndefined()
      expect(await signalLabel(ui, 'h')).toBe('○ h 0.290576 m')
      expect(await signalLabel(ui, 'v')).toBe('○ v -1.4625 m/s')
      expect(await ui.findAll({ type: surface === 'terminal' ? 'Raster' : 'Svg' })).toHaveLength(0)
      world.put(`${R1}/live.json`, FILES[`${R1}/live.json`]!.replace('"seq":42', '"seq":44'))
      await clock.advance(250)
      expect(await ui.find({ type: 'Text', text: NO_CHARTS })).toBeUndefined()
      await ui.select({ key: 'run', value: 'r2' })
      await ui.unmount()
    }
    world.put(`${R1}/live.json`, WORST_LIVE.replace('"run_id":"r999999"', '"run_id":"r1"'))
    for (const surface of SURFACES) {
      const ui = await openPane($, surface)
      await ui.select({ key: 'run', value: 'r1' })
      await clock.advance(250)
      expect(await ui.find({ type: 'Text', text: `Charts (3 of 3): ${HINT}` })).toBeDefined()
      expect((await ui.findAll({ type: 'Button' })).filter(button => String(button.key).startsWith('unchart:'))).toHaveLength(3)
      const names = (await signalKeys(ui)).map(key => key.slice('signal:'.length))
      const [charted, other] = [names[0]!, names[3]!] // the first three values are charted
      expect(String(await signalLabel(ui, charted)).startsWith('● ')).toBe(true)
      expect(String(await signalLabel(ui, other)).startsWith('○ ')).toBe(true)
      const before = world.writes.length
      await ui.press({ key: `signal:${other}` })
      expect(world.writes.length).toBe(before) // full: nothing is written, the pane says why
      expect(world.toasts.at(-1)).toBe(TOO_MANY)
      await ui.press({ key: `signal:${charted}` }) // a charted signal still toggles off
      expect(world.writes.length).toBe(before + 1)
      expect(JSON.parse(world.writes.at(-1)!.text).names).toHaveLength(2)
      await ui.select({ key: 'run', value: 'r2' })
      await ui.unmount()
    }
  })

  test('a chart keeps its colour when another is removed, and its signal\'s dot wears it', async ($, on) => {
    const clock = mock.clock(on, { now: NOW_MS })
    const world = fakeStore(on)
    await $.command.run({ command: 'fmi' })
    const ui = await openPane($, 'terminal')
    await ui.select({ key: 'run', value: 'r1' })
    const colourOf = async (name: string): Promise<unknown> => (await ui.find({ type: 'Text', text: name }))?.props.color
    const [h, v] = [await colourOf('h [m]'), await colourOf('v [m/s]')]
    expect(h).not.toBe(v)
    expect([await dotColour(ui, 'h'), await dotColour(ui, 'v')]).toEqual([h, v]) // the dot is the legend
    world.put(`${R1}/live.json`, withoutSeries(world, `${R1}/live.json`, 'h'))
    await clock.advance(250)
    expect(await ui.find({ type: 'Text', text: 'h [m]' })).toBeUndefined()
    expect(await colourOf('v [m/s]')).toBe(v) // v was second and is now first, and keeps its colour
    expect(await dotColour(ui, 'v')).toBe(v)
    expect(await dotColour(ui, 'h')).toBeUndefined() // h is no longer charted: a plain ○
    expect(await signalLabel(ui, 'h')).toBe('○ h 0.290576 m')
    await ui.select({ key: 'run', value: 'r2' })
    await ui.unmount()
  })

  test('an applied chart outcome makes no toast; a rejected one does', async ($, on) => {
    const clock = mock.clock(on, { now: NOW_MS })
    const world = fakeStore(on)
    await $.command.run({ command: 'fmi' })
    const ui = await openPane($, 'terminal')
    await ui.select({ key: 'run', value: 'r1' })
    const outcome = (id: string, status: string, message: string | null): string =>
      JSON.stringify({ protocol: 1, run_id: 'r1', id, source: 'pane', action: 'chart', status, t: 1.25, at: 'x', message })
    await ui.press({ key: 'unchart:v' })
    const applied = JSON.parse(world.writes.at(-1)!.text).id as string
    world.put(`${R1}/outcomes/${applied}.json`, outcome(applied, 'applied', null))
    await clock.advance(250)
    expect(world.toasts).toEqual([])
    await ui.press({ key: 'unchart:v' })
    const rejected = JSON.parse(world.writes.at(-1)!.text).id as string
    expect(rejected).not.toBe(applied)
    world.put(`${R1}/outcomes/${rejected}.json`, outcome(rejected, 'rejected', "'v' is not a recorded output with a live value."))
    await clock.advance(250)
    expect(world.toasts).toEqual(["r1 chart rejected at t=1.25: 'v' is not a recorded output with a live value."])
    await ui.select({ key: 'run', value: 'r2' })
    await ui.unmount()
  })

  test('the largest live.json stays within the Raster and Svg limits and lists 90 signals', async ($, on) => {
    mock.clock(on, { now: NOW_MS })
    const world = fakeStore(on)
    world.put(`${R1}/live.json`, WORST_LIVE.replace('"run_id":"r999999"', '"run_id":"r1"'))
    await $.command.run({ command: 'fmi' })
    for (const surface of SURFACES) {
      const ui = await openPane($, surface, 1000)
      await ui.select({ key: 'run', value: 'r1' })
      // 200 values are recorded; the pane lists the first SIGNAL_MAX (90) as buttons, 15 to a row of a 1000-column
      // body (cells of 63 columns after the 13-column label), and says how many it left out
      const keys = await signalKeys(ui)
      expect(keys).toHaveLength(90)
      expect(await signalLabel(ui, keys[0]!.slice('signal:'.length))).toBe('● component_00.a_long_variabl… -0.0000123457 kg.m2/(s3.A)xxxx')
      expect((await ui.findAll({ type: 'Box' })).filter(box => String(box.key).startsWith('signalrow:'))).toHaveLength(6)
      expect(await ui.find({ type: 'Text', text: '... and 110 more' })).toBeDefined()
      if (surface === 'terminal') {
        const rasters = await ui.findAll({ type: 'Raster' })
        expect(rasters).toHaveLength(3)
        for (const raster of rasters) {
          expect(raster.props.columns).toBe(MAX_RASTER_COLUMNS)
          expect(Number(raster.props.rows)).toBeLessThanOrEqual(MAX_RASTER_ROWS)
          expect(String(raster.props.cells).length).toBe((MAX_RASTER_COLUMNS * Number(raster.props.rows) * 12 * 4) / 3)
        }
      } else {
        const svgs = await ui.findAll({ type: 'Svg' })
        expect(svgs).toHaveLength(3)
        for (const svg of svgs) expect(String(svg.props.source).length).toBeLessThanOrEqual(MAX_SVG_CHARS)
      }
      await ui.select({ key: 'run', value: 'r2' })
      await ui.unmount()
    }
  })
})

describe('build cards and the connection diagram', () => {
  test('the FMUs tab lists the new FMU with its card; a build run shows its card on the Live tab', async ($, on) => {
    mock.clock(on, { now: NOW_MS })
    fakeStore(on)
    await $.command.run({ command: 'fmi' })
    for (const surface of SURFACES) {
      const ui = await openPane($, surface)
      expect(await ui.find({ type: 'Text', text: 'New FMUs' })).toBeUndefined()
      expect(await ui.find({ type: 'Text', text: CARD[0] })).toBeUndefined()
      await ui.press({ key: 'tab:fmus' })
      expect((await ui.find({ type: 'Button', key: 'tab:fmus' }))?.props.variant).toBe('primary')
      expect(await ui.find({ type: 'Text', text: 'New FMUs' })).toBeDefined()
      expect((await ui.find({ type: 'Button', key: 'fmu:r4' }))?.props.label).toBe(`▸ ${NEW_FMU_ROW}`)
      for (const line of CARD) expect(await ui.find({ type: 'Text', text: line }), line).toBeDefined()
      await ui.press({ key: 'tab:live' })
      await ui.select({ key: 'run', value: 'r4' })
      expect(await ui.find({ type: 'Text', text: '✓ r4 pi_controller: build finished' })).toBeDefined()
      for (const line of CARD) expect(await ui.find({ type: 'Text', text: line }), line).toBeDefined()
      expect(await ui.find({ type: 'Text', text: 'New FMUs' })).toBeUndefined()
      expect(await ui.find({ type: 'Text', text: /░ \d+%$/ })).toBeUndefined()
      for (const key of CONTROLS) expect(await ui.find({ key })).toBeUndefined()
      expect(await ui.findAll({ type: 'Raster' })).toHaveLength(0)
      expect(await ui.findAll({ type: 'Svg' })).toHaveLength(0)
      await ui.select({ key: 'run', value: 'r2' })
      await ui.unmount()
    }
  })

  test('the header walks the runs with ◀ ▶; the Runs tab opens a run on the Live tab', async ($, on) => {
    mock.clock(on, { now: NOW_MS })
    fakeStore(on)
    await $.command.run({ command: 'fmi' })
    for (const surface of SURFACES) {
      const ui = await openPane($, surface)
      await ui.press({ key: 'older' })
      expect((await ui.find({ type: 'Select', key: 'run' }))?.props.value).toBe('r1')
      await ui.press({ key: 'older' })
      expect((await ui.find({ type: 'Select', key: 'run' }))?.props.value).toBe('r1')
      await ui.press({ key: 'newer' })
      await ui.press({ key: 'newer' })
      expect((await ui.find({ type: 'Select', key: 'run' }))?.props.value).toBe('r3')
      await ui.press({ key: 'tab:runs' })
      const rows = (await ui.findAll({ type: 'Button' })).filter(button => String(button.key).startsWith('runrow:'))
      expect(rows.map(row => row.key)).toEqual(['runrow:r5', 'runrow:r4', 'runrow:r3', 'runrow:r2', 'runrow:r1'])
      expect(String(rows[2]?.props.label).startsWith('▸ r3 ')).toBe(true)
      await ui.press({ key: 'runrow:r1' })
      expect((await ui.find({ type: 'Button', key: 'tab:live' }))?.props.variant).toBe('primary')
      expect(await ui.find({ type: 'Text', text: /^‖ r1 BouncingBall: paused/ })).toBeDefined()
      await ui.select({ key: 'run', value: 'r2' })
      await ui.unmount()
    }
  })

  test('a torn build.json draws no card until it is whole; a finished build is read once', async ($, on) => {
    const clock = mock.clock(on, { now: NOW_MS })
    const world = fakeStore(on)
    world.put(R4_BUILD, FILES[R4_BUILD]!.slice(0, 90))
    await $.command.run({ command: 'fmi' })
    const ui = await openPane($, 'terminal')
    expect(await ui.find({ type: 'Text', text: 'New FMUs' })).toBeUndefined()
    await ui.select({ key: 'run', value: 'r4' })
    expect(await ui.find({ type: 'Text', text: '✓ r4 pi_controller: build finished' })).toBeDefined()
    expect(await ui.find({ type: 'Text', text: CARD[0] })).toBeUndefined()
    world.put(R4_BUILD, FILES[R4_BUILD]!)
    await clock.advance(250)
    for (const line of CARD) expect(await ui.find({ type: 'Text', text: line }), line).toBeDefined()
    const settled = world.stats.filter(path => path === R4_BUILD).length
    world.put(R4_BUILD, FILES[R4_BUILD]!.slice(0, 90))
    await clock.advance(1000)
    for (const line of CARD) expect(await ui.find({ type: 'Text', text: line }), line).toBeDefined()
    expect(world.stats.filter(path => path === R4_BUILD)).toHaveLength(settled)
    expect(world.reads.filter(path => path === R4_BUILD)).toHaveLength(2)
    await ui.select({ key: 'run', value: 'r2' })
    await ui.unmount()
  })

  test('a build.json of another protocol version draws no card', async ($, on) => {
    mock.clock(on, { now: NOW_MS })
    const world = fakeStore(on)
    world.put(R4_BUILD, edited(world, R4_BUILD, { protocol: 2 }))
    await $.command.run({ command: 'fmi' })
    for (const surface of SURFACES) {
      const ui = await openPane($, surface)
      expect(await ui.find({ type: 'Text', text: 'New FMUs' })).toBeUndefined()
      await ui.select({ key: 'run', value: 'r4' })
      expect(await ui.find({ type: 'Text', text: '✓ r4 pi_controller: build finished' })).toBeDefined()
      expect(await ui.find({ type: 'Text', text: CARD[0] })).toBeUndefined()
      await ui.select({ key: 'run', value: 'r2' })
      await ui.unmount()
    }
  })

  test('the pane reads only run-store JSON documents, build.json once, and writes nothing', async ($, on) => {
    mock.clock(on, { now: NOW_MS })
    const world = fakeStore(on)
    await $.command.run({ command: 'fmi' })
    const ui = await openPane($, 'terminal')
    for (const run of ['r1', 'r2', 'r3', 'r4', 'r5', 'r2']) await ui.select({ key: 'run', value: run })
    expect(world.reads.length).toBeGreaterThan(0)
    expect(world.reads.filter(path => !READABLE.test(path))).toEqual([])
    expect(world.reads.filter(path => path === R4_BUILD)).toHaveLength(1)
    expect(world.writes).toEqual([])
    await ui.unmount()
  })

  test('the System tab shows a system run\'s components and connections: a list on the terminal, the Svg on the desktop', async ($, on) => {
    mock.clock(on, { now: NOW_MS })
    fakeStore(on)
    await $.command.run({ command: 'fmi' })
    for (const surface of SURFACES) {
      const ui = await openPane($, surface)
      await ui.press({ key: 'tab:system' })
      expect(await ui.find({ type: 'Text', text: 'Components (2): PI, plant' })).toBeDefined()
      expect(await ui.find({ type: 'Text', text: 'Connections' })).toBeDefined()
      expect(await ui.findAll({ type: 'Raster' })).toHaveLength(0)
      const edge = await ui.find({ type: 'Text', text: 'PI.u    ──▶ plant.F' })
      const svgs = await ui.findAll({ type: 'Svg' })
      if (surface === 'terminal') {
        expect(edge).toBeDefined()
        expect(await ui.find({ type: 'Text', text: 'plant.x ──▶ PI.y' })).toBeDefined()
        expect(svgs).toHaveLength(0)
      } else {
        expect(edge).toBeUndefined()
        expect(svgs).toHaveLength(1)
        expect(String(svgs[0]?.props.source)).toBe(diagramSvg(['PI', 'plant'], R2_EDGES))
        expect(String(svgs[0]?.props.alt)).toBe('Connections: PI.u -> plant.F; plant.x -> PI.y')
      }
      await ui.select({ key: 'run', value: 'r1' })
      expect(await ui.find({ type: 'Text', text: 'Connections' })).toBeUndefined()
      expect(await ui.find({ type: 'Text', text: /^r1 is not a system run \(BouncingBall\)\./ })).toBeDefined()
      await ui.select({ key: 'run', value: 'r2' })
      await ui.press({ key: 'tab:live' })
      await ui.unmount()
    }
  })

  test('a larger system stays within the Svg limit; the terminal list is cut', async ($, on) => {
    mock.clock(on, { now: NOW_MS })
    const world = fakeStore(on)
    const spec = JSON.parse(world.text(`${R2}/spec.json`))
    const names = Array.from({ length: 12 }, (_, i) => `C${i}`)
    spec.components = Object.fromEntries(names.map(name => [name, spec.components.PI]))
    spec.connections = Array.from({ length: 60 }, (_, i) => ({ from: `C${i % 12}.out${i}`, to: `C${(i * 5 + 1) % 12}.in${i}` }))
    world.put(`${R2}/spec.json`, JSON.stringify(spec))
    await $.command.run({ command: 'fmi' })
    for (const surface of SURFACES) {
      const ui = await openPane($, surface)
      await ui.press({ key: 'tab:system' })
      if (surface === 'terminal') {
        expect(await ui.findAll({ type: 'Text', text: /^C\d+\.out\d+ +──▶ C\d+\.in\d+$/ })).toHaveLength(40)
        expect(await ui.find({ type: 'Text', text: '... and 20 more' })).toBeDefined()
      } else {
        const diagram = (await ui.findAll({ type: 'Svg' })).at(-1)
        expect(String(diagram?.props.source).length).toBeLessThanOrEqual(MAX_SVG_CHARS)
        expect(String(diagram?.props.source)).toContain('>C11</text>')
        expect(String(diagram?.props.alt).endsWith('; ... and 48 more')).toBe(true)
      }
      await ui.press({ key: 'tab:live' })
      await ui.unmount()
    }
  })
})

describe('store discovery', () => {
  for (const row of DISCOVERY) {
    test(`AFMI_STORE_DIR ${JSON.stringify(row.env)} -> ${row.store}`, async ($, on) => {
      mock.clock(on, { now: NOW_MS })
      const env = row.env === null ? { HOME: row.home } : { AFMI_STORE_DIR: row.env, HOME: row.home }
      const world = fakeStore(on, env, row.store)
      await $.command.run({ command: 'fmi' })
      expect(world.stats[0]).toBe(`${row.store}/index.json`)
      const ui = await openPane($, 'terminal')
      expect((await ui.find({ type: 'Select', key: 'run' }))?.props.value).toBe('r2')
      await ui.unmount()
    })
  }
})
