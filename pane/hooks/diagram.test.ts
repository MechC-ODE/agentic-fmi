// The connection diagram (charts.ts diagramSvg, diagramLines) on its own: a golden Svg for the
// fixture's loop (r2: PI.u -> plant.F, plant.x -> PI.y), determinism, parallel and back edges,
// the larger systems within the Svg limit, and XML escaping. `claude plugin test pane` runs it.

import { expect, test } from 'claude-code/testing'

import { FILES, STORE } from '../fixtures/store'
import { MAX_SVG_CHARS, clip, diagramLines, diagramSvg, xmlText } from './charts'
import { asSpec, parseJson } from './protocol'

const r2 = asSpec(parseJson(FILES[`${STORE}/runs/r2/spec.json`]!))!

/** The r2 diagram, element by element (joined with nothing: the Svg has no whitespace). */
const R2_DIAGRAM = [
  '<svg xmlns="http://www.w3.org/2000/svg" viewBox="-12 -12 444 64" width="444" height="64" font-family="sans-serif">',
  '<rect x="-12" y="-12" width="444" height="64" rx="8" fill="#ffffff"/>',
  '<path d="M120,12 L300,12" fill="none" stroke="#555555" stroke-width="1.5"/>',
  '<polygon points="300,12 291,16.1 291,7.9" fill="#555555"/>',
  '<path d="M300,28 L120,28" fill="none" stroke="#555555" stroke-width="1.5"/>',
  '<polygon points="120,28 129,23.9 129,32.1" fill="#555555"/>',
  '<rect x="0" y="0" width="120" height="40" rx="6" fill="#eaf1fb" stroke="#3b6ea8" stroke-width="1.5"/>',
  '<text x="60" y="24" text-anchor="middle" font-size="12" font-weight="bold" fill="#14304f">PI</text>',
  '<rect x="300" y="0" width="120" height="40" rx="6" fill="#eaf1fb" stroke="#3b6ea8" stroke-width="1.5"/>',
  '<text x="360" y="24" text-anchor="middle" font-size="12" font-weight="bold" fill="#14304f">plant</text>',
  '<rect x="182" y="5" width="42" height="14" rx="3" fill="#ffffff" stroke="#c4cbd6" stroke-width="0.5"/>',
  '<text x="203" y="15.5" text-anchor="middle" font-size="10" fill="#222222">u -&gt; F</text>',
  '<rect x="196" y="21" width="42" height="14" rx="3" fill="#ffffff" stroke="#c4cbd6" stroke-width="0.5"/>',
  '<text x="217" y="31.5" text-anchor="middle" font-size="10" fill="#222222">x -&gt; y</text>',
  '</svg>',
].join('')

/** `count` components C0.. and `edges` connections between them, in a fixed pattern. */
function system(count: number, edges: number): { components: string[]; connections: { from: string; to: string }[] } {
  const components = Array.from({ length: count }, (_, i) => `C${i}`)
  const connections = Array.from({ length: edges }, (_, i) => ({
    from: `${components[i % count]}.out_${i}`,
    to: `${components[(i * 7 + 3) % count]}.in_${i}`,
  }))
  return { components, connections }
}

/** Every tag is plain and every attribute value holds no markup; text holds no raw `< > "` or bare `&`. */
function expectWellFormed(svg: string): void {
  const tag = /<\/?[a-z]+(?: [A-Za-z-]+="[^"<>&]*")*\/?>/g
  expect(svg.replace(tag, '')).not.toMatch(/[<>"]/)
  expect(svg).not.toMatch(/&(?!(?:amp|lt|gt|quot|apos);)/)
  expect(svg.startsWith('<svg xmlns="http://www.w3.org/2000/svg" ')).toBe(true)
  expect(svg.endsWith('</svg>')).toBe(true)
}

const pathsOf = (svg: string): string[] => [...svg.matchAll(/<path d="([^"]*)"/g)].map(match => match[1]!)

test('the r2 loop draws the golden Svg: two boxes, two lanes, an arrow each, out -> in labels', () => {
  expect(r2.components).toEqual(['PI', 'plant'])
  const svg = diagramSvg(r2.components, r2.connections)
  expect(svg).toBe(R2_DIAGRAM)
  expectWellFormed(svg)
})

test('the diagram is a pure function: the same input gives the same bytes', () => {
  const { components, connections } = system(12, 20)
  const first = diagramSvg(components, connections)
  expect(diagramSvg([...components], connections.map(edge => ({ ...edge })))).toBe(first)
  expect(diagramSvg(components, connections)).toBe(first)
  expect(diagramSvg(components, connections.slice().reverse())).not.toBe(first)
})

test('the terminal edge list is one `out -> in` line per connection, in spec order', () => {
  expect(diagramLines(r2.connections)).toEqual(['PI.u -> plant.F', 'plant.x -> PI.y'])
  expect(diagramLines([])).toEqual([])
})

test('parallel, back and self connections stay apart', () => {
  const lanes = diagramSvg(['a', 'b'], [
    { from: 'a.x', to: 'b.x' },
    { from: 'a.y', to: 'b.y' },
    { from: 'b.z', to: 'a.z' },
  ])
  const drawn = pathsOf(lanes)
  expect(new Set(drawn).size).toBe(3)
  const ys = drawn.map(d => Number(/^M[\d.-]+,([\d.-]+)/.exec(d)![1]))
  expect(new Set(ys).size).toBe(3)
  expect(Math.min(...ys.slice(1).map((y, i) => Math.abs(y - ys[i]!)))).toBeGreaterThanOrEqual(8)
  expect(drawn.every(d => d.includes(' L'))).toBe(true)
  const loop = pathsOf(diagramSvg(['a'], [{ from: 'a.o', to: 'a.i' }]))
  expect(loop).toHaveLength(1)
  expect(loop[0]).toContain(' Q')
})

test('an edge that would run through a box between its ends bows around it', () => {
  const bowed = pathsOf(diagramSvg(['a', 'b', 'c', 'd', 'e'], [{ from: 'a.o', to: 'c.i' }, { from: 'a.o', to: 'b.i' }]))
  expect(bowed[0]).toContain(' Q')
  expect(bowed[1]).toContain(' L')
  const nine = ['a', 'b', 'c', 'd', 'e', 'f', 'g', 'h', 'i']
  const diagonal = pathsOf(diagramSvg(nine, [{ from: 'a.o', to: 'i.i' }, { from: 'a.o', to: 'e.i' }]))
  expect(diagonal[0]).toContain(' Q')
  expect(diagonal[1]).toContain(' L')
})

test('12 components with 20 connections fit the Svg limit, an arrow and a label each', () => {
  const { components, connections } = system(12, 20)
  const svg = diagramSvg(components, connections)
  expect(svg.length).toBeLessThanOrEqual(MAX_SVG_CHARS)
  expectWellFormed(svg)
  expect(svg.match(/<polygon /g)).toHaveLength(20)
  expect(svg.match(/font-size="10"/g)).toHaveLength(20)
  for (const name of components) expect(svg).toContain(`>${name}</text>`)
  expect(svg).not.toContain('not drawn')
})

test('the largest system (64 components, 1024 connections) stays within the Svg limit and says what is not drawn', () => {
  const { components, connections } = system(64, 1024)
  const svg = diagramSvg(components, connections)
  expect(svg.length).toBeLessThanOrEqual(MAX_SVG_CHARS)
  expectWellFormed(svg)
  const drawn = svg.match(/<polygon /g)!.length
  expect(drawn).toBeLessThan(1024)
  expect(svg).toContain(`>${1024 - drawn} of 1024 connections not drawn</text>`)
})

test('a connection to an unlisted component is counted, not drawn; no components give an empty drawing', () => {
  const svg = diagramSvg(['a'], [{ from: 'x.o', to: 'a.i' }])
  expect(svg).toContain('>1 of 1 connections not drawn</text>')
  expect(svg).not.toContain('<polygon')
  expect(diagramSvg([], [])).toBe('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1 1" width="1" height="1"/>')
})

test('names are escaped for XML, control characters dropped, long names and ports cut', () => {
  const svg = diagramSvg(['a<&"', "b>'"], [{ from: 'a<&".out<\u0001x', to: "b>'.in&" }])
  expectWellFormed(svg)
  expect(svg).toContain('>a&lt;&amp;&quot;</text>')
  expect(svg).toContain('>b&gt;&apos;</text>')
  expect(svg).toContain('>out&lt;x -&gt; in&amp;</text>')
  expect(svg).not.toContain('\u0001')
  const long = diagramSvg(['a'.repeat(40), 'b'], [{ from: `${'a'.repeat(40)}.${'p'.repeat(30)}`, to: 'b.q' }])
  expect(long).toContain(`>${'a'.repeat(15)}…</text>`)
  expect(long).toContain(`>${'p'.repeat(9)}… -&gt; q</text>`)
  expect(xmlText('<&>"\'')).toBe('&lt;&amp;&gt;&quot;&apos;')
  expect(clip('abcdef', 4)).toBe('abc…')
  expect(clip('abcd', 4)).toBe('abcd')
})
