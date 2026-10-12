// Chart and diagram drawing for the pane (ADR 0010). Pure functions, no `$`.
//
// Two line-chart renderers, picked by the surface, both with a value axis (top, middle and bottom
// labels) and a time axis (start, middle, end): on the terminal a Raster of box-drawing glyphs
// (asciichart style: one value per column, interpolated between the samples, joined by ╭╮╰╯│─; at
// most 512 columns by 256 rows, width-1 BMP glyphs, colours 0x00RRGGBB or 0x01000000 for the default), elsewhere an Svg with a
// polyline over light grid lines (source at most 131072 characters, about 600 points, min-max
// downsampled). Each surface draws nothing for the other's element, so register.tsx branches on
// e.surface. The system diagram is a fixed grid in component order with labelled edges: no layout
// algorithm.
//
// Collaborators: register.tsx (sizes the charts to the pane and picks the renderer) and the
// tests in units.test.ts, diagram.test.ts (the diagram's golden) and pane.test.ts (WORST_LIVE
// stays within the limits).

export const MAX_RASTER_COLUMNS = 512
export const MAX_RASTER_ROWS = 256
export const MAX_SVG_CHARS = 131_072
export const SVG_POINTS = 600
/** One colour per charted variable (live.json charts at most 4), as 0x00RRGGBB. */
export const PALETTE: readonly number[] = [0x2a9df4, 0xf4a62a, 0x3cb371, 0xd9534f]
/** Rows a terminal chart adds below its plot: the time axis and its labels. */
export const AXIS_ROWS = 2

const DEFAULT_COLOR = 0x01000000
/** The axes and their labels: a mid grey that reads on light and dark terminals. */
const AXIS_COLOR = 0x808080
const SPACE = 0x20

/**
 * Indices of at most `points` (at least 2) samples that keep both ends and each bucket's min and
 * max, in order. A bucket of nulls keeps one null, so a gap survives the downsampling.
 */
export function downsample(values: readonly (number | null)[], points: number): number[] {
  const count = values.length
  const room = Math.max(2, Math.floor(points))
  if (count <= room) return Array.from({ length: count }, (_, i) => i)
  const buckets = Math.max(1, Math.floor((room - 2) / 2))
  const inner = count - 2
  const picked = [0]
  for (let bucket = 0; bucket < buckets; bucket++) {
    const start = 1 + Math.floor((bucket * inner) / buckets)
    const end = 1 + Math.floor(((bucket + 1) * inner) / buckets)
    picked.push(...extremes(values, start, end))
  }
  picked.push(count - 1)
  return picked
}

/** The indices of the min and max of values[start, end) in order, or the first if all null. */
function extremes(values: readonly (number | null)[], start: number, end: number): number[] {
  let low = -1
  let high = -1
  let lowValue = Infinity
  let highValue = -Infinity
  for (let i = start; i < end; i++) {
    const value = values[i]
    if (value === null || value === undefined) continue
    if (value < lowValue) [low, lowValue] = [i, value]
    if (value > highValue) [high, highValue] = [i, value]
  }
  if (low < 0) return start < end ? [start] : []
  return low === high ? [low] : [Math.min(low, high), Math.max(low, high)]
}

/** The min and max of the non-null values, widened when flat; null when there are none. */
export function valueRange(series: readonly (number | null)[]): [number, number] | null {
  let low = Infinity
  let high = -Infinity
  for (const value of series) {
    if (value === null) continue
    low = Math.min(low, value)
    high = Math.max(high, value)
  }
  if (low > high) return null
  const pad = high - low > 1e-12 * Math.max(1, Math.abs(high)) ? 0 : Math.max(1e-9, Math.abs(high) * 0.01)
  return [low - pad, high + pad]
}

/** Each sample's x (0..columns-1 of `size` positions) and y (0 at the top), null kept. */
function scaled(series: readonly (number | null)[], width: number, height: number): ([number, number] | null)[] {
  const span = valueRange(series)
  if (span === null) return series.map(() => null)
  const [low, high] = span
  const last = Math.max(1, series.length - 1)
  return series.map((value, i) =>
    value === null ? null : [(i * (width - 1)) / last, (1 - (value - low) / (high - low)) * (height - 1)],
  )
}

/** An axis label: at most 4 significant digits, no trailing zeros; exponent form when tiny or huge. */
export function tickLabel(value: number): string {
  if (!Number.isFinite(value)) return String(value)
  if (Math.abs(value) < 1e-12) return '0'
  if (Math.abs(value) >= 1e5 || Math.abs(value) < 1e-3) return value.toExponential(2).replace(/\.?0+e/, 'e')
  return String(Number(value.toPrecision(4)))
}

/**
 * One value per plot column: the series interpolated linearly between the two samples around the
 * column's position, so a chart wider than its samples is a line, not a staircase. Where either
 * neighbour is null the nearest sample stands in, so a gap stays null. One column is the last sample.
 */
function columnSamples(series: readonly (number | null)[], width: number): (number | null)[] {
  if (series.length === 0) return []
  const last = series.length - 1
  return Array.from({ length: width }, (_, x) => {
    if (width === 1) return series[last] ?? null
    const position = (x * last) / (width - 1)
    const before = Math.floor(position)
    const a = series[before] ?? null
    const b = series[Math.min(last, before + 1)] ?? null
    if (a === null || b === null) return series[Math.round(position)] ?? null
    return a + (b - a) * (position - before)
  })
}

/** The value labels of a chart's top, middle and bottom rows (one row: the top's only). */
function valueTicks(span: [number, number], rows: number): { row: number; label: string }[] {
  const [low, high] = span
  const ticks = [{ row: 0, label: tickLabel(high) }]
  if (rows >= 3) ticks.push({ row: Math.floor((rows - 1) / 2), label: tickLabel((low + high) / 2) })
  if (rows >= 2) ticks.push({ row: rows - 1, label: tickLabel(low) })
  return ticks
}

/** A terminal chart: what a Raster takes, `rows` being the plot rows plus AXIS_ROWS. */
export type LineChart = { columns: number; rows: number; cells: string }

/**
 * A line chart of `series` (its samples taken at `time`) as Raster cells, `columns` wide with
 * `plotRows` rows of plot: value labels and a `┤│` axis on the left, the line in `color` drawn
 * with box-drawing glyphs (one interpolated value per column, asciichart style; a null breaks the line), and
 * below a `└┬─` time axis with the start, middle and end times in seconds.
 */
export function lineChartCells(
  series: readonly (number | null)[],
  time: readonly number[],
  columns: number,
  plotRows: number,
  color: number = PALETTE[0]!,
): LineChart {
  const rows = plotRows + AXIS_ROWS
  const glyphs = new Uint32Array(columns * rows).fill(SPACE)
  const colors = new Uint32Array(columns * rows).fill(DEFAULT_COLOR)
  const put = (row: number, column: number, text: string, fg: number): void => {
    for (let i = 0; i < text.length; i++) {
      const at = column + i
      if (row < 0 || row >= rows || at < 0 || at >= columns) continue
      glyphs[row * columns + at] = text.charCodeAt(i)
      colors[row * columns + at] = fg
    }
  }
  const span = valueRange(series)
  const ticks = span === null ? [] : valueTicks(span, plotRows)
  const gutter = Math.min(columns - 1, Math.max(1, ...ticks.map(tick => tick.label.length + 2)))
  const width = Math.max(1, columns - gutter)
  for (let row = 0; row < plotRows; row++) put(row, gutter - 1, '│', AXIS_COLOR)
  for (const tick of ticks) {
    put(tick.row, gutter - 2 - tick.label.length, tick.label, AXIS_COLOR)
    put(tick.row, gutter - 1, '┤', AXIS_COLOR)
  }
  drawTimeAxis(put, time, plotRows, gutter, width)
  if (span === null) put(Math.floor((plotRows - 1) / 2), gutter + 1, 'no values', AXIS_COLOR)
  else drawLine(put, columnSamples(series, width), span, plotRows, gutter, color)
  const view = new DataView(new ArrayBuffer(columns * rows * 12))
  for (let i = 0; i < columns * rows; i++) {
    view.setUint32(i * 12, glyphs[i]!, true)
    view.setUint32(i * 12 + 4, colors[i]!, true)
    view.setUint32(i * 12 + 8, DEFAULT_COLOR, true)
  }
  return { columns, rows, cells: base64(new Uint8Array(view.buffer)) }
}

type Put = (row: number, column: number, text: string, fg: number) => void

/** The `└─┬─` axis under the plot and, below it, the start, middle and end times (`4.2 s`). */
function drawTimeAxis(put: Put, time: readonly number[], plotRows: number, gutter: number, width: number): void {
  put(plotRows, gutter - 1, '└' + '─'.repeat(width), AXIS_COLOR)
  const middle = Math.floor((width - 1) / 2)
  for (const tick of [0, middle, width - 1]) put(plotRows, gutter + tick, '┬', AXIS_COLOR)
  if (time.length === 0) return
  const start = tickLabel(time[0]!)
  const end = `${tickLabel(time[time.length - 1]!)} s`
  const centre = tickLabel((time[0]! + time[time.length - 1]!) / 2)
  put(plotRows + 1, gutter, start, AXIS_COLOR)
  const centreAt = gutter + middle - Math.floor(centre.length / 2)
  const endAt = gutter + width - end.length
  if (centreAt > gutter + start.length && centreAt + centre.length < endAt) put(plotRows + 1, centreAt, centre, AXIS_COLOR)
  if (endAt > gutter + start.length) put(plotRows + 1, endAt, end, AXIS_COLOR)
}

/** The line, one value per column: `─` level, `╭╯`/`╮╰` where it climbs or falls, `│` between. */
function drawLine(put: Put, samples: readonly (number | null)[], span: [number, number], plotRows: number, gutter: number, color: number): void {
  const [low, high] = span
  const rowOf = (value: number): number => Math.min(plotRows - 1, Math.max(0, Math.round(((high - value) / (high - low)) * (plotRows - 1))))
  let previous: number | null = null
  samples.forEach((value, x) => {
    if (value === null || !Number.isFinite(value)) {
      previous = null
      return
    }
    const row = rowOf(value)
    const column = gutter + x
    if (previous === null || row === previous) put(row, column, '─', color)
    else if (row < previous) {
      put(previous, column, '╯', color)
      put(row, column, '╭', color)
      for (let between = row + 1; between < previous; between++) put(between, column, '│', color)
    } else {
      put(previous, column, '╮', color)
      put(row, column, '╰', color)
      for (let between = previous + 1; between < row; between++) put(between, column, '│', color)
    }
    previous = row
  })
}

function base64(bytes: Uint8Array): string {
  let text = ''
  for (let i = 0; i < bytes.length; i += 0x8000) text += String.fromCharCode(...bytes.subarray(i, i + 0x8000))
  return btoa(text)
}

/** Svg chart margins: room for the value labels on the left and the time labels below. */
const SVG_MARGIN = { left: 56, right: 10, top: 8, bottom: 20 } as const

/**
 * An Svg line chart of the (already downsampled) series sampled at `time`: light grid lines at the
 * top, middle and bottom value with their labels, the start, middle and end times below, and the
 * series as polylines (a null breaks the line).
 */
export function lineChartSvg(
  series: readonly (number | null)[],
  time: readonly number[],
  width: number,
  height: number,
  color: number = PALETTE[0]!,
): string {
  const plotWidth = width - SVG_MARGIN.left - SVG_MARGIN.right
  const plotHeight = height - SVG_MARGIN.top - SVG_MARGIN.bottom
  const span = valueRange(series)
  const parts: string[] = []
  const label = (x: number, y: number, text: string, anchor: string): string =>
    `<text x="${x.toFixed(1)}" y="${y.toFixed(1)}" text-anchor="${anchor}" font-family="sans-serif" font-size="10" fill="#888">${xmlText(text)}</text>`
  if (span !== null) {
    for (const [fraction, value] of [[0, span[1]], [0.5, (span[0] + span[1]) / 2], [1, span[0]]] as const) {
      const y = SVG_MARGIN.top + fraction * plotHeight
      parts.push(`<line x1="${SVG_MARGIN.left}" y1="${y.toFixed(1)}" x2="${width - SVG_MARGIN.right}" y2="${y.toFixed(1)}" stroke="#888" stroke-opacity="0.3" stroke-width="1"/>`)
      parts.push(label(SVG_MARGIN.left - 6, y + 3.5, tickLabel(value), 'end'))
    }
  }
  if (time.length > 0) {
    const first = time[0]!
    const last = time[time.length - 1]!
    const y = height - 6
    parts.push(label(SVG_MARGIN.left, y, tickLabel(first), 'start'))
    parts.push(label(SVG_MARGIN.left + plotWidth / 2, y, tickLabel((first + last) / 2), 'middle'))
    parts.push(label(width - SVG_MARGIN.right, y, `${tickLabel(last)} s`, 'end'))
  }
  const stroke = `#${color.toString(16).padStart(6, '0')}`
  let current: string[] = []
  const flush = (): void => {
    if (current.length > 0) {
      const points = current.length === 1 ? `${current[0]} ${current[0]}` : current.join(' ')
      parts.push(`<polyline fill="none" stroke="${stroke}" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round" points="${points}"/>`)
    }
    current = []
  }
  for (const point of scaled(series, plotWidth, plotHeight)) {
    if (point === null) flush()
    else current.push(`${(point[0] + SVG_MARGIN.left).toFixed(1)},${(point[1] + SVG_MARGIN.top).toFixed(1)}`)
  }
  flush()
  return `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 ${width} ${height}" width="${width}" height="${height}">${parts.join('')}</svg>`
}

/** Grid cell (row, column) of each component, in spec order, `perRow` to a row. */
export function gridCells(components: readonly string[], perRow: number): Map<string, [number, number]> {
  const cells = new Map<string, [number, number]>()
  components.forEach((name, i) => cells.set(name, [Math.floor(i / perRow), i % perRow]))
  return cells
}

type Edge = { from: string; to: string }
type Cell = readonly [number, number]
type Point = readonly [number, number]
/** One drawn connection: its line and arrowhead, its label chip, and the points it spans. */
type Piece = { line: string; label: string; points: Point[] }

const BOX_W = 120
const BOX_H = 40
const GAP_X = 180
const GAP_Y = 90
const PAD = 12
const MAX_COLUMNS = 4
/** Distance between the parallel lanes of connections between the same two components. */
const LANE = 16
/** How far a bowed edge clears the box it goes around. */
const BOW_MARGIN = 14
const ARROW = 9
const NAME_CHARS = 16
const PORT_CHARS = 10
const LABEL_STEP = 14
/** Room kept for the Svg's own tags, the background and the "not drawn" note. */
const SVG_RESERVE = 500
const INK = '#555555'

/** `text` without characters XML 1.0 forbids, `& < > " '` escaped: safe in text and attributes. */
export function xmlText(text: string): string {
  return text
    .replace(/[\u0000-\u0008\u000B\u000C\u000E-\u001F￾￿]/g, '')
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&apos;')
}

/** `text` cut to at most `max` characters, the cut marked with an ellipsis. */
export function clip(text: string, max: number): string {
  const chars = Array.from(text)
  return chars.length <= max ? text : `${chars.slice(0, max - 1).join('')}…`
}

/** A coordinate with at most one decimal and no trailing zero (stable text for the golden). */
function fixed(value: number): string {
  return String(Number(value.toFixed(1)))
}

function at(point: Point): string {
  return `${fixed(point[0])},${fixed(point[1])}`
}

/** `PI.u` -> [`PI`, `u`]: the component is everything before the first dot (component names have none). */
function split(ref: string): [string, string] {
  const cut = ref.indexOf('.')
  return cut < 0 ? [ref, ref] : [ref.slice(0, cut), ref.slice(cut + 1)]
}

function center(cell: Cell): Point {
  return [BOX_W / 2 + cell[1] * (BOX_W + GAP_X), BOX_H / 2 + cell[0] * (BOX_H + GAP_Y)]
}

function gcd(a: number, b: number): number {
  return b === 0 ? a : gcd(b, a % b)
}

/** The side of the box that extends in the direction of the unit vector `normal`. */
function reach(normal: Point): number {
  return Math.abs(normal[0]) * (BOX_W / 2) + Math.abs(normal[1]) * (BOX_H / 2)
}

/**
 * How far along `way` (from `offset` away from the box's centre) a line leaves the box; at most
 * half the way, so the start and end of an edge never cross.
 */
function exitFraction(offset: Point, way: Point): number {
  const half: Point = [BOX_W / 2, BOX_H / 2]
  let fraction = Infinity
  for (const axis of [0, 1] as const) {
    if (way[axis] !== 0) fraction = Math.min(fraction, ((way[axis] > 0 ? half[axis] : -half[axis]) - offset[axis]) / way[axis])
  }
  return Math.max(0, Math.min(fraction, 0.5))
}

/** The offset of lane `lane` of `lanes` along `normal`, kept within the box so the lanes stay distinct. */
function laneOffset(normal: Point, lane: number, lanes: number): Point {
  const step = lanes > 1 ? Math.min(LANE, (2 * (reach(normal) - 4)) / (lanes - 1)) : 0
  const shift = (lane - (lanes - 1) / 2) * step
  return [normal[0] * shift, normal[1] * shift]
}

/** The point at `u` (0..1) of the quadratic curve start-control-end (a line when control is the middle). */
function along(start: Point, control: Point, end: Point, u: number): Point {
  const [a, b, c] = [(1 - u) * (1 - u), 2 * u * (1 - u), u * u]
  return [a * start[0] + b * control[0] + c * end[0], a * start[1] + b * control[1] + c * end[1]]
}

function arrowhead(tip: Point, control: Point): string {
  const length = Math.hypot(tip[0] - control[0], tip[1] - control[1]) || 1
  const [ux, uy] = [(tip[0] - control[0]) / length, (tip[1] - control[1]) / length]
  const base: Point = [tip[0] - ARROW * ux, tip[1] - ARROW * uy]
  const wing: Point = [-uy * (ARROW / 2.2), ux * (ARROW / 2.2)]
  const left: Point = [base[0] + wing[0], base[1] + wing[1]]
  const right: Point = [base[0] - wing[0], base[1] - wing[1]]
  return `<polygon points="${at(tip)} ${at(left)} ${at(right)}" fill="${INK}"/>`
}

/** The label chip (a white plate hides the line behind the text) centred on `spot`. */
function chip(text: string, spot: Point): { svg: string; points: Point[] } {
  const width = Math.ceil(Array.from(text).length * 5.6) + 8
  const corner: Point = [spot[0] - width / 2, spot[1] - 7]
  const svg =
    `<rect x="${fixed(corner[0])}" y="${fixed(corner[1])}" width="${width}" height="14" rx="3" fill="#ffffff" stroke="#c4cbd6" stroke-width="0.5"/>` +
    `<text x="${fixed(spot[0])}" y="${fixed(spot[1] + 3.5)}" text-anchor="middle" font-size="10" fill="#222222">${xmlText(text)}</text>`
  return { svg, points: [corner, [corner[0] + width, corner[1] + 14]] }
}

/** One edge between two different components: a lane of the pair, bowed around boxes in its way. */
function linkPiece(src: Cell, dst: Cell, text: string, lane: number, lanes: number): Piece {
  const [a, b] = [center(src), center(dst)]
  const forward = isBefore(src, dst)
  const low = forward ? a : b
  const high = forward ? b : a
  const span = Math.hypot(high[0] - low[0], high[1] - low[1])
  const normal: Point = [-(high[1] - low[1]) / span, (high[0] - low[0]) / span]
  const offset = laneOffset(normal, lane, lanes)
  const way: Point = [b[0] - a[0], b[1] - a[1]]
  const [leave, enter] = [exitFraction(offset, way), exitFraction(offset, [-way[0], -way[1]])]
  const start: Point = [a[0] + offset[0] + way[0] * leave, a[1] + offset[1] + way[1] * leave]
  const end: Point = [b[0] + offset[0] - way[0] * enter, b[1] + offset[1] - way[1] * enter]
  const bows = gcd(Math.abs(dst[0] - src[0]), Math.abs(dst[1] - src[1])) > 1
  const peak = bows ? BOW_MARGIN + reach(normal) : 0
  const middle: Point = [(start[0] + end[0]) / 2, (start[1] + end[1]) / 2]
  const control: Point = [middle[0] + normal[0] * 2 * peak, middle[1] + normal[1] * 2 * peak]
  const visible = Math.max(1, Math.hypot(end[0] - start[0], end[1] - start[1]))
  const slide = (((lane - (lanes - 1) / 2) * LABEL_STEP) / visible) * (forward ? 1 : -1)
  const label = chip(text, along(start, control, end, 0.5 + Math.max(-0.2, Math.min(0.2, slide))))
  const path = bows ? `M${at(start)} Q${at(control)} ${at(end)}` : `M${at(start)} L${at(end)}`
  return {
    line: `<path d="${path}" fill="none" stroke="${INK}" stroke-width="1.5"/>${arrowhead(end, control)}`,
    label: label.svg,
    points: [start, end, along(start, control, end, 0.5), ...label.points],
  }
}

/** A connection from a component to itself: a loop above its box, one more loop per lane. */
function loopPiece(cell: Cell, text: string, lane: number): Piece {
  const [cx, cy] = center(cell)
  const top = cy - BOX_H / 2
  const height = 26 + lane * LANE
  const start: Point = [cx + 18, top]
  const end: Point = [cx - 18, top]
  const control: Point = [cx, top - 2 * height]
  const label = chip(text, [cx, top - height - 9])
  return {
    line: `<path d="M${at(start)} Q${at(control)} ${at(end)}" fill="none" stroke="${INK}" stroke-width="1.5"/>${arrowhead(end, control)}`,
    label: label.svg,
    points: [start, end, [cx, top - height], ...label.points],
  }
}

/** Whether `a` comes before `b` in grid reading order (the direction lanes are measured in). */
function isBefore(a: Cell, b: Cell): boolean {
  return a[0] < b[0] || (a[0] === b[0] && a[1] <= b[1])
}

function boxSvg(name: string, cell: Cell): string {
  const [cx, cy] = center(cell)
  return (
    `<rect x="${fixed(cx - BOX_W / 2)}" y="${fixed(cy - BOX_H / 2)}" width="${BOX_W}" height="${BOX_H}" rx="6" fill="#eaf1fb" stroke="#3b6ea8" stroke-width="1.5"/>` +
    `<text x="${fixed(cx)}" y="${fixed(cy + 4)}" text-anchor="middle" font-size="12" font-weight="bold" fill="#14304f">${xmlText(clip(name, NAME_CHARS))}</text>`
  )
}

/** The pieces that fit `room` characters: with labels if all do, else lines only, else the first few. */
function fitPieces(pieces: readonly Piece[], room: number): { drawn: Piece[]; hasLabels: boolean } {
  const size = (list: readonly Piece[], hasLabels: boolean): number =>
    list.reduce((sum, piece) => sum + piece.line.length + (hasLabels ? piece.label.length : 0), 0)
  if (size(pieces, true) <= room) return { drawn: [...pieces], hasLabels: true }
  const drawn: Piece[] = []
  let used = 0
  for (const piece of pieces) {
    if (used + piece.line.length > room) break
    used += piece.line.length
    drawn.push(piece)
  }
  return { drawn, hasLabels: false }
}

/**
 * The connection diagram as an Svg document: a box per component on a fixed grid (spec order,
 * `ceil(sqrt(n))` columns, at most 4), one arrow per connection labelled `out -> in` with its port
 * names. Connections between the same two components (either direction) take parallel lanes; one
 * that would run through the box between its ends bows around it. Pure and deterministic. A
 * diagram too large for the Svg limit drops its labels, then its last connections (a note says
 * how many); a connection naming a component that is not listed is counted as not drawn.
 */
export function diagramSvg(components: readonly string[], connections: readonly Edge[]): string {
  const columns = Math.max(1, Math.min(MAX_COLUMNS, Math.ceil(Math.sqrt(components.length))))
  const cells = gridCells(components, columns)
  const links = connections.flatMap(edge => {
    const [source, target] = [split(edge.from), split(edge.to)]
    const [src, dst] = [cells.get(source[0]), cells.get(target[0])]
    const text = `${clip(source[1], PORT_CHARS)} -> ${clip(target[1], PORT_CHARS)}`
    return src === undefined || dst === undefined ? [] : [{ src, dst, text }]
  })
  const key = (src: Cell, dst: Cell): string => (isBefore(src, dst) ? `${src}|${dst}` : `${dst}|${src}`)
  const sizes = new Map<string, number>()
  const lanes = links.map(({ src, dst }) => {
    sizes.set(key(src, dst), (sizes.get(key(src, dst)) ?? 0) + 1)
    return (sizes.get(key(src, dst)) ?? 1) - 1
  })
  const pieces = links.map(({ src, dst, text }, i) => {
    const count = sizes.get(key(src, dst)) ?? 1
    const isLoop = src[0] === dst[0] && src[1] === dst[1]
    return isLoop ? loopPiece(src, text, lanes[i]!) : linkPiece(src, dst, text, lanes[i]!, count)
  })
  if (components.length === 0) return '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1 1" width="1" height="1"/>'
  const boxes = components.map(name => boxSvg(name, cells.get(name)!)).join('')
  const { drawn, hasLabels } = fitPieces(pieces, MAX_SVG_CHARS - SVG_RESERVE - boxes.length)
  const points: Point[] = [...cells.values()].flatMap(cell => {
    const [cx, cy] = center(cell)
    return [[cx - BOX_W / 2, cy - BOX_H / 2], [cx + BOX_W / 2, cy + BOX_H / 2]] as Point[]
  })
  for (const piece of drawn) points.push(...piece.points)
  const notDrawn = connections.length - drawn.length
  const xs = points.map(point => point[0])
  const ys = points.map(point => point[1])
  const [x0, y0, x1, y1] = [Math.min(...xs), Math.min(...ys), Math.max(...xs), Math.max(...ys)]
  const note = notDrawn > 0 ? `${notDrawn} of ${connections.length} connections not drawn` : ''
  const [left, top] = [Math.floor(x0 - PAD), Math.floor(y0 - PAD)]
  const width = Math.ceil(x1 + PAD) - left
  const height = Math.ceil(y1 + PAD) - top + (note === '' ? 0 : 16)
  const label = note === '' ? '' : `<text x="${left + PAD}" y="${top + height - 6}" font-size="10" fill="#8a1f1f">${xmlText(note)}</text>`
  const lines = drawn.map(piece => piece.line).join('')
  const labels = hasLabels ? drawn.map(piece => piece.label).join('') : ''
  return (
    `<svg xmlns="http://www.w3.org/2000/svg" viewBox="${left} ${top} ${width} ${height}" width="${width}" height="${height}" font-family="sans-serif">` +
    `<rect x="${left}" y="${top}" width="${width}" height="${height}" rx="8" fill="#ffffff"/>${lines}${boxes}${labels}${label}</svg>`
  )
}

/** The connection diagram for the terminal: one text line per edge, `PI.u -> plant.F`. */
export function diagramLines(connections: readonly { from: string; to: string }[]): string[] {
  return connections.map(edge => `${edge.from} -> ${edge.to}`)
}
