// The build card's reader and texts (protocol.ts asBuild, view.ts buildCardLines, buildListLine,
// headLine, capLines, diagramAlt) against the fixture's runs/r4/build.json. `claude plugin test
// pane` runs it.

import { expect, test } from 'claude-code/testing'

import { FILES, STORE } from '../fixtures/store'
import { asBuild, parseJson } from './protocol'
import { buildCardLines, buildListLine, capLines, diagramAlt, headLine } from './view'

const text = FILES[`${STORE}/runs/r4/build.json`]!
const raw = parseJson(text) as Record<string, unknown>
const record = (fields: Record<string, unknown>): unknown => ({ ...raw, ...fields })

test('asBuild reads the fixture build.json into a card', () => {
  expect(asBuild(raw)).toEqual({
    run_id: 'r4',
    name: 'pi_controller',
    backend: 'pythonfmu',
    fmu_path: 'generated/pi_controller/pi_controller.fmu',
    sha256: '0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef',
    interfaces: ['CoSimulation'],
    counts: { input: 1, output: 1, parameter: 3, total: 5 },
    inputs: ['y'],
    outputs: ['u'],
    parameters: ['Kp', 'Ki', 'r'],
    duration_s: 6.4,
    warnings: 0,
  })
  expect(asBuild(record({ warnings: ['a.py:3: warning: x', 'a.py:9: warning: y'] }))?.warnings).toBe(2)
})

test('asBuild is null for a torn, empty, foreign or incomplete document', () => {
  expect(asBuild(parseJson(text.slice(0, 80)))).toBeNull()
  expect(asBuild(parseJson(''))).toBeNull()
  expect(asBuild(null)).toBeNull()
  expect(asBuild([])).toBeNull()
  expect(asBuild(record({ protocol: 2 }))).toBeNull()
  expect(asBuild(record({ protocol: undefined }))).toBeNull()
  for (const field of ['run_id', 'name', 'backend', 'fmu', 'summary', 'duration_s']) {
    expect(asBuild(record({ [field]: undefined })), field).toBeNull()
  }
  expect(asBuild(record({ fmu: { path: 'x.fmu' } }))).toBeNull()
  expect(asBuild(record({ fmu: { sha256: 'ab' } }))).toBeNull()
})

test('asBuild ignores unknown fields and keeps what it can of odd lists', () => {
  const card = asBuild(record({ extra: 1, summary: { counts: { input: 'x', total: 2 }, inputs: ['a', 3], outputs: 'u' } }))
  expect(card?.counts).toEqual({ total: 2 })
  expect(card?.inputs).toEqual(['a'])
  expect(card?.outputs).toEqual([])
  expect(card?.parameters).toEqual([])
})

test('the card shows backend, interfaces, time, counts, names, file, a short hash and warnings', () => {
  const card = asBuild(raw)!
  expect(buildCardLines(card)).toEqual([
    'pythonfmu, CoSimulation, built in 6.4 s',
    '5 variables: input 1, output 1, parameter 3',
    'inputs (1): y',
    'outputs (1): u',
    'parameters (3): Kp, Ki, r',
    'generated/pi_controller/pi_controller.fmu  sha256 0123456789ab',
    'no warnings',
  ])
  expect(buildListLine(card)).toBe('r4 pi_controller: pythonfmu, CoSimulation, 5 variables, 6.4 s')
})

test('long name lists are cut with +N more, empty ones say none, warnings point to the log', () => {
  const names = Array.from({ length: 12 }, (_, i) => `p${i}`)
  const card = asBuild(record({
    warnings: ['w'],
    summary: { counts: { parameter: 30, total: 31, output: 0 }, inputs: [], outputs: [], parameters: names },
  }))!
  const lines = buildCardLines(card)
  expect(lines[1]).toBe('31 variables: parameter 30')
  expect(lines[2]).toBe('inputs: none')
  expect(lines[4]).toBe('parameters (30): p0, p1, p2, p3, p4, p5, p6, p7, +22 more')
  expect(lines[6]).toBe('1 warning; see runs/r4/build.log')
  expect(buildCardLines({ ...card, warnings: 3, interfaces: [] })[6]).toBe('3 warnings; see runs/r4/build.log')
  expect(buildCardLines({ ...card, interfaces: [] })[0]).toBe('pythonfmu, no interface listed, built in 6.4 s')
  expect(buildCardLines({ ...card, inputs: ['x'.repeat(60)] })[2]).toBe(`inputs (1): ${'x'.repeat(39)}…`)
})

test('a build run has a build head line, not a time range', () => {
  const status = { protocol: 1, run_id: 'r4', state: 'finished', t: 0, t_start: 0, t_stop: 0, progress: 1, speed: null, last_cmd: null, reason: null, error: null, heartbeat_at: null }
  const spec = { run_id: 'r4', kind: 'build', live: false, name: 'pi_controller', t_stop: 0, outputs: [], settable_hints: [], components: [], connections: [] }
  expect(headLine(status, spec, 'pi_controller')).toBe('✓ r4 pi_controller: build finished')
  expect(headLine({ ...status, state: 'failed' }, spec, 'pi_controller')).toBe('✗ r4 pi_controller: build failed')
  expect(headLine({ ...status, protocol: 2 }, spec, 'pi_controller')).toContain('unsupported protocol version 2')
})

test('capLines and diagramAlt keep long edge lists short', () => {
  expect(capLines(['a', 'b'], 2)).toEqual(['a', 'b'])
  expect(capLines(['a', 'b', 'c'], 2)).toEqual(['a', 'b', '... and 1 more'])
  expect(diagramAlt([])).toBe('The system has no connections.')
  expect(diagramAlt([{ from: 'PI.u', to: 'plant.F' }, { from: 'plant.x', to: 'PI.y' }])).toBe('Connections: PI.u -> plant.F; plant.x -> PI.y')
  const many = Array.from({ length: 15 }, (_, i) => ({ from: `a.o${i}`, to: `b.i${i}` }))
  expect(diagramAlt(many).endsWith('; ... and 3 more')).toBe(true)
})
