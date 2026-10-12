// The pane's pure helpers (protocol.ts, charts.ts, view.ts) against the generated fixtures:
// the golden command files, store discovery, the tolerant readers, the downsampler, both line
// chart renderers (the terminal's exact glyphs, interpolated between the samples; the Svg within its
// limit), the values table (signal groups and rows of cells), progress bar, connection rows and run
// navigation, and the Set field. `claude plugin test pane` runs it.

import { expect, test } from 'claude-code/testing'

import { DISCOVERY, FILES, NOW_MS, PANE_COMMANDS, ROOT, STORE, VARIANTS, WORST_LIVE } from '../fixtures/store'
import { AXIS_ROWS, MAX_SVG_CHARS, PALETTE, SVG_POINTS, downsample, lineChartCells, lineChartSvg, tickLabel } from './charts'
import type { LineChart } from './charts'
import { asLive, asRuns, asSpec, asStatus, commandFile, isControllable, parseJson, storeDirFor } from './protocol'
import type { PaneAction } from './protocol'
import {
  CHART_MAX,
  SIGNAL_MAX,
  chartSeries,
  chartedNames,
  connectionRows,
  neighbourRun,
  outcomeToast,
  parseSet,
  progressBar,
  runListLine,
  signalDetail,
  signalGroups,
  signalRows,
  signalText,
  statusLine,
} from './view'
import type { AfmiPaneLive, AfmiPaneSpec } from '../types'

const doc = (path: string): unknown => parseJson(FILES[`${STORE}/${path}`]!)

test('commandFile writes exactly the golden command of each button', () => {
  const actions: Record<string, PaneAction> = {
    pause: { action: 'pause' },
    resume: { action: 'resume' },
    step: { action: 'step', n: 1 },
    set: { action: 'set', values: { e: 0.6 } },
    stop: { action: 'stop' },
    chart: { action: 'chart', names: ['h'] },
  }
  for (const [button, action] of Object.entries(actions)) {
    const { id, text } = commandFile(NOW_MS, '00000000', action)
    expect(id).toBe(`${NOW_MS}-pane-00000000`)
    expect(text).toBe(PANE_COMMANDS[button]!)
  }
})

test('storeDirFor resolves every DISCOVERY case as config.load_config does', () => {
  for (const row of DISCOVERY) expect(storeDirFor(row.env, ROOT, row.home), String(row.env)).toBe(row.store)
  expect(storeDirFor(null, 'C:\\work\\ws', null)).toBe('C:/work/ws/.agentic-fmi')
  expect(storeDirFor('~', ROOT, '/home/dev')).toBe('/home/dev')
})

test('the readers take the fixture documents and refuse torn ones', () => {
  expect(asRuns(doc('index.json'))?.map(run => run.run_id)).toEqual(['r5', 'r4', 'r3', 'r2', 'r1'])
  const spec = asSpec(doc('runs/r2/spec.json'))
  expect(spec).toMatchObject({ kind: 'system', live: true, name: 'loop', components: ['PI', 'plant'], settable_hints: ['PI.Ki', 'PI.Kp'] })
  expect(spec?.connections).toEqual([{ from: 'PI.u', to: 'plant.F' }, { from: 'plant.x', to: 'PI.y' }])
  expect(asSpec(doc('runs/r1/spec.json'))).toMatchObject({ kind: 'run', live: true, name: 'BouncingBall', t_stop: 3, settable_hints: ['e'] })
  const status = asStatus(doc('runs/r1/status.json'))
  expect(status).toMatchObject({ protocol: 1, state: 'paused', t: 1.25, speed: 1 })
  expect(isControllable(asSpec(doc('runs/r1/spec.json')), status)).toBe(true)
  expect(isControllable(asSpec(doc('runs/r3/spec.json')), asStatus(doc('runs/r3/status.json')))).toBe(false)
  expect(asStatus(doc('runs/r5/status.json'))?.error?.code).toBe('simulation_error')
  expect(asStatus(parseJson(VARIANTS.status_protocol_2!))?.protocol).toBe(2)
  for (const text of [VARIANTS.live_torn!, VARIANTS.live_empty!]) expect(asLive(parseJson(text))).toBeNull()
  const live = asLive(doc('runs/r1/live.json'))
  expect(live?.values).toEqual([{ name: 'h', unit: 'm', value: 0.290576 }, { name: 'v', unit: 'm/s', value: -1.4625 }])
  const short = JSON.parse(FILES[`${STORE}/runs/r1/live.json`]!)
  short.series.h.pop()
  expect(asLive(short)).toBeNull()
})

test('downsample keeps both ends and every bucket min and max, within the points', () => {
  expect(downsample([1, 2, 3], 10)).toEqual([0, 1, 2])
  const values = Array.from({ length: 1000 }, (_, i) => Math.sin(i / 7))
  values[500] = 9
  values[501] = -9
  const kept = downsample(values, 100)
  expect(kept.length).toBeLessThanOrEqual(100)
  expect(kept[0]).toBe(0)
  expect(kept.at(-1)).toBe(999)
  expect(kept).toContain(500)
  expect(kept).toContain(501)
  expect([...kept].sort((a, b) => a - b)).toEqual(kept)
  const gaps = downsample([1, ...Array<null>(50).fill(null), 2], 6)
  expect(gaps.some(index => index > 0 && index < 51)).toBe(true)
})

/** What a terminal shows for a line chart: its glyphs, row by row. */
function rasterText(chart: LineChart): string[] {
  const view = new DataView(Uint8Array.from(atob(chart.cells), char => char.charCodeAt(0)).buffer)
  return Array.from({ length: chart.rows }, (_, row) =>
    String.fromCharCode(...Array.from({ length: chart.columns }, (_, column) => view.getUint32((row * chart.columns + column) * 12, true))),
  )
}

test('the terminal chart is a smooth line with a value axis and a time axis', () => {
  const chart = lineChartCells([0, 0, 1, 1, 0.5], [0, 1, 2, 3, 4], 14, 3)
  expect(chart.rows).toBe(3 + AXIS_ROWS)
  expect(chart.cells.length).toBe((14 * 5 * 12 * 4) / 3)
  // The value labels (1, 0.5, 0) make the gutter 5 columns, so the plot is 9 columns for 5 samples: column x sits at
  // sample position x / 2 and takes the straight line between the two samples around it. The values are
  // 0, 0, 0, 0.5, 1, 1, 1, 0.75, 0.5, which round to plot rows (0 = top, 3 rows) 2, 2, 2, 1, 0, 0, 0, 1, 1.
  // The line is level on row 2 for three columns, climbs one row at a time (╭╯ twice, not a step of two rows), runs
  // along the top, then falls one row (╮╰) and stays on row 1. The old nearest-sample columns made │ steps.
  expect(rasterText(chart)).toEqual([
    '  1 ┤    ╭──╮ ',
    '0.5 ┤   ╭╯  ╰─',
    '  0 ┤───╯     ',
    '    └┬───┬───┬',
    '     0   2 4 s',
  ])
  // fewer columns than samples: column x sits at sample position 3x, so the plot shows samples 0, 3 and 6 (0, 1, 0)
  expect(rasterText(lineChartCells([0, 1, 0, 1, 0, 1, 0], [0, 1, 2, 3, 4, 5, 6], 6, 2))).toEqual(['1 ┤ ╭╮', '0 ┤─╯╰', '  └┬┬┬', '   0  '])
  expect(rasterText(lineChartCells([], [], 12, 2))).toEqual(['│ no values ', '│           ', '└┬────┬────┬', '            '])
  const gap = rasterText(lineChartCells([0, null, null, 1], [0, 1, 2, 3], 10, 2))
  expect(gap[0]!.includes('─') && gap[1]!.includes('─')).toBe(true)
  expect(gap.slice(0, 2).some(row => /[╭╮╰╯│]/.test(row.slice(4)))).toBe(false)
})

test('axis labels are short: 4 significant digits, exponents when tiny or huge', () => {
  expect([0, 1.23456, -0.5, 1e-5, 123456, 4].map(tickLabel)).toEqual(['0', '1.235', '-0.5', '1e-5', '1.23e+5', '4'])
})

test('the Svg chart has grid lines, value and time labels and a polyline broken by nulls', () => {
  const svg = lineChartSvg([0, 1, null, 1, 0], [0, 1, 2, 3, 4], 200, 100)
  expect(svg.match(/<polyline/g)).toHaveLength(2)
  expect(svg.match(/<line /g)).toHaveLength(3)
  expect(svg).toContain('>4 s</text>')
  expect(lineChartSvg([5, 5, 5], [0, 1, 2], 200, 100)).toContain('<polyline')
  const worst = asLive(parseJson(WORST_LIVE))!
  for (const chart of chartSeries(worst, SVG_POINTS)) {
    expect(chart.series.length).toBeLessThanOrEqual(SVG_POINTS)
    expect(lineChartSvg(chart.series, worst.time, 520, 170, chart.color).length).toBeLessThanOrEqual(MAX_SVG_CHARS)
  }
})

/** A live.json of columns a..f (values in that order) that charts `charted`, with two history points. */
function liveOf(charted: string[]): AfmiPaneLive {
  const names = ['a', 'b', 'c', 'd', 'e', 'f']
  return {
    protocol: 1,
    run_id: 'r1',
    seq: 1,
    t: 1,
    values: names.map(name => ({ name, unit: null, value: 1 })),
    time: [0, 1],
    series: Object.fromEntries(charted.map(name => [name, [0, 1]])),
  }
}

test('the selection is the keys of series, at most CHART_MAX', () => {
  expect(CHART_MAX).toBe(3)
  expect(chartedNames(liveOf(['c', 'a']))).toEqual(['c', 'a'])
  expect(chartedNames(liveOf([]))).toEqual([])
  expect(chartedNames(liveOf(['a', 'b', 'c', 'd']))).toEqual(['a', 'b', 'c'])
  expect(chartedNames(asLive(parseJson(WORST_LIVE))!)).toHaveLength(CHART_MAX)
  expect(chartedNames(asLive(doc('runs/r1/live.json'))!)).toEqual(['h', 'v'])
})

/** A system spec with these components (the other fields are not read by the values table). */
function systemOf(...components: string[]): AfmiPaneSpec {
  return { run_id: 'r1', kind: 'system', live: true, name: 'loop', t_stop: 1, outputs: [], settable_hints: [], components, connections: [] }
}

/** A live.json of these values (all 1, no unit) charting `charted`. */
function liveNamed(names: string[], charted: string[] = []): AfmiPaneLive {
  return { ...liveOf(charted), values: names.map(name => ({ name, unit: null, value: 1 })) }
}

test('a system run\'s values group by component in spec order; unknown components follow, labelled with their prefix', () => {
  const live = liveNamed(['plant.x', 'PI.u', 'plant.v.dot', 'ghost.a', 'PI.y', 'bare', 'ghost.b'], ['PI.u'])
  const groups = signalGroups(live, systemOf('PI', 'plant', 'unused'))
  expect(groups.map(group => group.label)).toEqual(['PI', 'plant', 'ghost', 'bare']) // 'unused' has no value: no group
  expect(groups.map(group => group.cells.map(cell => cell.short))).toEqual([['u', 'y'], ['x', 'v.dot'], ['a', 'b'], ['bare']]) // cut at the first dot only
  expect(groups[0]!.cells.map(cell => cell.name)).toEqual(['PI.u', 'PI.y']) // a click names the whole signal
  expect(groups.flatMap(group => group.cells).filter(cell => cell.charted).map(cell => cell.name)).toEqual(['PI.u'])
  expect(groups[0]!.cells[0]!.color).toBe(PALETTE[1]!) // PI.u is the second value: the colour of its chart
})

test('any other run is one group named after the run, its names never split on dots', () => {
  const live = liveNamed(['a.b', 'c'], ['c'])
  const named = (name: string): AfmiPaneSpec => ({ ...systemOf(), kind: 'run', name })
  expect(signalGroups(live, named('BouncingBall')).map(group => [group.label, group.cells.map(cell => cell.short)])).toEqual([['BouncingBall', ['a.b', 'c']]])
  expect(signalGroups(live, named('')).map(group => group.label)).toEqual(['r1']) // no name: the run id
  expect(signalGroups(live, null).map(group => group.label)).toEqual(['r1'])
  expect(signalGroups(liveNamed([]), named('x'))).toEqual([])
})

test('a signal reads as its mark, short name, value and unit', () => {
  const live: AfmiPaneLive = { ...liveOf(['h']), values: [{ name: 'h', unit: 'm', value: 0.290576 }, { name: 'v', unit: '', value: null }, { name: 'a_very_long_signal_name_of_a_big_model', unit: null, value: -2 }] }
  const cells = signalGroups(live, null)[0]!.cells
  expect(cells.map(signalText)).toEqual(['● h 0.290576 m', '○ v n/a', '○ a_very_long_signal_name_of_… -2'])
  expect(signalDetail(cells[0]!)).toBe('h 0.290576 m')
})

test('the values table: labels padded to the longest, cells as many to a row as fit, blank labels below, capped', () => {
  const live = liveNamed(['PI.u', 'PI.y', 'PI.e', 'PI.i', 'PI.d', 'plant.x'], ['PI.u'])
  const groups = signalGroups(live, systemOf('PI', 'plant'))
  // 'PI' holds five cells of '○ u 1' (5 columns) + 2 = 7; the 'plant' label (5) + a space leaves 22 of 28 columns: 3 to a row
  const { rows, more } = signalRows(groups, 28)
  expect(more).toBe(0)
  expect(rows.map(row => [row.label, row.cells.map(cell => cell.short), row.width])).toEqual([
    ['PI    ', ['u', 'y', 'e'], 7],
    ['      ', ['i', 'd'], 7],
    ['plant ', ['x'], 7],
  ])
  expect(signalRows(groups, 6).rows.map(row => row.cells.length)).toEqual([1, 1, 1, 1, 1, 1]) // at least one to a row, however narrow
  expect(signalRows(groups, 80).rows.map(row => row.cells.length)).toEqual([5, 1])
  expect(signalRows([{ label: 'a_component_name_of_great_length', cells: groups[0]!.cells }], 80).rows[0]!.label).toBe('a_component_nam… ')
  const many = Array.from({ length: SIGNAL_MAX + 20 }, (_, i) => `n${i}`)
  const capped = signalRows(signalGroups(liveNamed(many), null), 1000)
  expect(capped.more).toBe(20)
  expect(capped.rows.reduce((sum, row) => sum + row.cells.length, 0)).toBe(SIGNAL_MAX)
  expect(signalRows([], 80)).toEqual({ rows: [], more: 0 })
})

test('at most three charts are shown, and a chart keeps its colour when another is removed', () => {
  expect(chartSeries(liveOf(['a', 'b', 'c', 'd', 'e']), 10).map(chart => chart.name)).toEqual(['a', 'b', 'c'])
  const colours = (charted: string[]): [string, number][] => chartSeries(liveOf(charted), 10).map(chart => [chart.name, chart.color])
  expect(colours(['a', 'b', 'c'])).toEqual([['a', PALETTE[0]!], ['b', PALETTE[1]!], ['c', PALETTE[2]!]])
  expect(colours(['b', 'c'])).toEqual([['b', PALETTE[1]!], ['c', PALETTE[2]!]]) // a removed: the others keep theirs
  expect(colours(['a', 'c'])).toEqual([['a', PALETTE[0]!], ['c', PALETTE[2]!]]) // b removed
  expect(colours(['f'])).toEqual([['f', PALETTE[5 % PALETTE.length]!]]) // position 5 wraps around the palette
  expect(colours(['c', 'a']).map(([name]) => name)).toEqual(['c', 'a']) // in the order the worker published
})

test('an applied chart needs no toast; a rejected one and the other actions do', () => {
  const outcome = (action: string | null, status: string) => ({ id: 'x', action, status, t: 1.25, message: 'because' })
  expect(outcomeToast('r1', 'chart', outcome('chart', 'applied'))).toBeNull()
  expect(outcomeToast('r1', 'chart', outcome(null, 'applied'))).toBeNull() // the sent action stands in for an unknown one
  expect(outcomeToast('r1', 'chart', outcome('chart', 'rejected'))).toBe('r1 chart rejected at t=1.25: because')
  expect(outcomeToast('r1', 'pause', outcome('pause', 'applied'))).toBe('r1 pause applied at t=1.25: because')
})

test('the progress bar fills, connections line up, ◀ ▶ walk the index', () => {
  expect([progressBar(0.42, 10), progressBar(1.5, 4), progressBar(-1, 4)]).toEqual(['████░░░░░░ 42%', '████ 100%', '░░░░ 0%'])
  expect(connectionRows([{ from: 'PI.u', to: 'plant.F' }, { from: 'plant.x', to: 'PI.y' }])).toEqual(['PI.u    ──▶ plant.F', 'plant.x ──▶ PI.y'])
  const runs = asRuns(doc('index.json'))!
  expect([neighbourRun(runs, 'r3', true), neighbourRun(runs, 'r3', false), neighbourRun(runs, 'r1', true), neighbourRun(runs, null, true)]).toEqual(['r2', 'r4', null, 'r5'])
  expect(runListLine(runs[3]!, 'r2').startsWith('▸ r2 ')).toBe(true)
  expect(runListLine(runs[4]!, 'r2').startsWith('  r1 ')).toBe(true)
})

test('parseSet reads name=value pairs and says what is wrong', () => {
  expect(parseSet('e=0.6')).toEqual({ values: { e: 0.6 } })
  expect(parseSet(' PI.Kp = 12 ; on=true, off=false ')).toEqual({ values: { 'PI.Kp': 12, on: true, off: false } })
  expect(parseSet('')).toEqual({ error: 'Set expects name=value, for example e=0.6.' })
  expect(parseSet('=1')).toEqual({ error: 'Set expects name=value (for example e=0.6), not "=1".' })
  expect(parseSet('k=NaN')).toEqual({ error: 'k: "NaN" is not a number, true or false.' })
})

test('the status line follows the example: FMI, glyph, run, name, progress, time, speed', () => {
  const status = asStatus(doc('runs/r2/status.json'))!
  expect(statusLine({ ...status, t: 6.3, progress: 0.63, speed: 1 }, 'PI+MSD')).toBe('FMI ▶ r2 PI+MSD 63% t=6.3 s 1x')
  expect(statusLine({ ...status, state: 'finished', speed: 2 }, 'loop')).toBe('FMI ✓ r2 loop 42% t=4.2 s')
})

test('a build run reads "built", not like a simulation; other kinds are unchanged', () => {
  const status = asStatus(doc('runs/r4/status.json'))!
  expect(statusLine(status, 'pi_controller', 'build')).toBe('FMI ✓ r4 pi_controller built')
  expect(statusLine({ ...status, state: 'running' }, 'pi_controller', 'build')).toBe('FMI ▶ r4 pi_controller build running')
  expect(statusLine({ ...status, state: 'failed' }, 'pi_controller', 'build')).toBe('FMI ✗ r4 pi_controller build failed')
  expect(statusLine(status, 'pi_controller', 'run')).toBe('FMI ✓ r4 pi_controller 100% t=0 s')
  expect(statusLine(status, 'pi_controller')).toBe('FMI ✓ r4 pi_controller 100% t=0 s')
})
