// Visual encoding helpers, shared by the canvas renderer and the legend so the
// two never drift. The model is paper-centric: every node is a paper (a
// circle), COLOUR encodes the chosen dimension's value, a *pivot* (paper with
// >1 value in that dimension) gets a distinct dark fill + ring, and unassigned
// papers fade to a warm grey. The drift signal (corrupt / invalid) is
// deliberately de-emphasised so it does not shout over the structure: corrupt
// papers (rare, severe) are a bold red dot; invalid papers keep their colour
// but gain a thin red ring; invalid edges fade to translucent dashed red.

import type { GraphEdge } from '../types'
import { MULTI_KEY, NONE_KEY } from './dimensions'

// Warm, editorial palette ("暖色高级灰"). Low-saturation earth tones chosen to
// sit together without clashing, so adjacent clusters read as a family rather
// than a default-matplotlib rainbow. Assigned by hashing the value so a given
// value keeps its colour across recolours / focus.
const PALETTE = [
  '#c0613b', // terracotta
  '#6b7b4f', // olive
  '#c9a14a', // ochre
  '#4a6b7b', // slate blue
  '#9b5d6b', // rosewood
  '#5e8d80', // sage teal
  '#a8743a', // caramel
  '#7d6b82', // muted mauve
  '#4f6b52', // forest
  '#8a7d6b', // taupe
]

export const GREY = '#bcb4a8' // unassigned (no value in this dimension)
export const PIVOT = '#403a33' // dark warm — a paper bridging >1 value (pivot)
export const CORRUPT_RED = '#b3402e' // bold red — corrupt metadata only
export const DRIFT_RED = '#a8493a' // ring / edge accent for invalid (drift)
export const EDGE_OK = '#b6ae9f' // warm neutral relation edge

function hashString(s: string): number {
  let h = 2166136261
  for (let i = 0; i < s.length; i++) {
    h ^= s.charCodeAt(i)
    h = Math.imul(h, 16777619)
  }
  return h >>> 0
}

// `#rrggbb` -> `rgba(r,g,b,a)`. All palette / marker colours are 6-digit hex,
// so the glow halo can be drawn in a node's own hue at an arbitrary alpha.
function withAlpha(hex: string, a: number): string {
  const h = hex.replace('#', '')
  const r = parseInt(h.slice(0, 2), 16)
  const g = parseInt(h.slice(2, 4), 16)
  const b = parseInt(h.slice(4, 6), 16)
  return `rgba(${r}, ${g}, ${b}, ${a})`
}

// Colour for a colour-key (a dimension value, or the synthetic MULTI / NONE).
export function colorForKey(key: string): string {
  if (key === NONE_KEY) return GREY
  if (key === MULTI_KEY) return PIVOT
  return PALETTE[hashString(key) % PALETTE.length]
}

// Relation edge type -> colour so multi-edges between the same pair are
// distinguishable (D6). Undirected `related` is a quiet warm grey; the
// directed types get their own muted hues.
export const RELATION_TYPE_COLORS: Record<string, string> = {
  related: '#a79d8d',
  extends: '#6b7b4f',
  contradicts: '#b5552f',
}

export function edgeColor(edge: GraphEdge): string {
  if (edge.status === 'invalid') return DRIFT_RED
  return RELATION_TYPE_COLORS[edge.type] ?? EDGE_OK
}

// Map a paper's relation degree to a canvas radius. sqrt keeps area roughly
// proportional to connectivity (bar-graph discipline carried to nodes). Pivots
// get a small bump so they read as hubs even at low degree.
export function nodeRadius(degree: number, pivot: boolean): number {
  return (pivot ? 5 : 4) + Math.sqrt(Math.max(0, degree)) * 1.6
}

// Deterministic seed offset for initial placement so reopening is roughly
// stable — react-force-graph only reads it once before the sim runs.
export function seedXY(id: string): { x: number; y: number } {
  const h = hashString(id)
  const angle = (h % 360) * (Math.PI / 180)
  const r = 40 + (((h >>> 9) % 1000) / 1000) * 220
  return { x: Math.cos(angle) * r, y: Math.sin(angle) * r }
}

// Draw a paper node: a same-hue glow halo, a circle filled `fill`, a hairline
// warm stroke (dark + thicker when highlighted), a dark ring for pivots, and a
// red ring for corrupt / invalid drift.
export function drawNode(
  ctx: CanvasRenderingContext2D,
  x: number,
  y: number,
  r: number,
  fill: string,
  opts: { highlighted: boolean; pivot: boolean; corrupt: boolean; drift: boolean },
): void {
  // 0) soft glow halo in the node's OWN hue (a radial gradient fading to
  //    transparent). On the light canvas this reads as a gentle bloom; where
  //    halos overlap inside a cluster they add up, so a dense cluster gets a
  //    luminous coloured core without any per-node clutter. Wider + brighter
  //    when highlighted so a hover focus glows.
  const haloR = r * (opts.highlighted ? 3.2 : 2.2)
  const innerA = opts.highlighted ? 0.55 : 0.18
  const grad = ctx.createRadialGradient(x, y, r * 0.6, x, y, haloR)
  grad.addColorStop(0, withAlpha(fill, innerA))
  grad.addColorStop(1, withAlpha(fill, 0))
  ctx.fillStyle = grad
  ctx.beginPath()
  ctx.arc(x, y, haloR, 0, 2 * Math.PI)
  ctx.fill()

  // 1) filled core circle (opaque, drawn over the halo).
  ctx.fillStyle = fill
  ctx.beginPath()
  ctx.arc(x, y, r, 0, 2 * Math.PI)
  ctx.fill()

  // 2) hairline stroke. Pivots get a dark ring so they pop as hubs.
  ctx.beginPath()
  ctx.arc(x, y, r, 0, 2 * Math.PI)
  if (opts.highlighted) {
    ctx.strokeStyle = '#2f2922'
    ctx.lineWidth = 2
  } else if (opts.pivot) {
    ctx.strokeStyle = PIVOT
    ctx.lineWidth = 1.6
  } else {
    ctx.strokeStyle = 'rgba(80,62,44,0.22)'
    ctx.lineWidth = 0.7
  }
  ctx.stroke()

  // 3) red ring: bold for corrupt, thinner for invalid drift.
  if (opts.corrupt || opts.drift) {
    ctx.beginPath()
    ctx.strokeStyle = opts.corrupt ? CORRUPT_RED : DRIFT_RED
    ctx.lineWidth = opts.corrupt ? 2.4 : 1.5
    ctx.arc(x, y, r + 3, 0, 2 * Math.PI)
    ctx.stroke()
  }
}
