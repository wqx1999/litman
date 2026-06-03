// D9 visual encoding helpers, shared by the canvas renderer and the legend so
// the two never drift. Shape encodes entity type, colour encodes group, red
// overrides everything for corrupt/invalid (a drift-diagnostic signal that must
// win over group colour). Greys are the "weak / unassigned" reference tone
// (viz-conventions: grey = reference, not subject).

import type { GraphEdge, GraphNode, NodeType } from '../types'

export const RED = '#c0392b' // corrupt / invalid — overrides group colour
export const GREY = '#9aa0a6' // unassigned / weak relationship
export const EDGE_OK = '#b8bcc2'
export const EDGE_INVALID = RED

// Stable per-group palette (warm-leaning, viz-conventions). Assigned by hashing
// the group name so the same project keeps its colour across reopens and across
// aggregate/drilldown views.
const PALETTE = [
  '#c0392b', // crimson  (reserved tone; only hit by hash, never by red-status)
  '#d35400', // warm orange
  '#8e44ad', // purple
  '#16726b', // teal
  '#b9770e', // amber
  '#2c6fbb', // blue (secondary)
  '#7d3c98', // violet
  '#1e8449', // green (secondary)
  '#a04000', // burnt
  '#5d6d7e', // slate
]

function hashString(s: string): number {
  let h = 2166136261
  for (let i = 0; i < s.length; i++) {
    h ^= s.charCodeAt(i)
    h = Math.imul(h, 16777619)
  }
  return h >>> 0
}

export function groupColor(group: string): string {
  if (!group || group === '(unassigned)') return GREY
  return PALETTE[hashString(group) % PALETTE.length]
}

// Final fill colour for a node: red overrides group colour for corrupt/invalid.
export function nodeColor(node: GraphNode): string {
  if (node.status === 'corrupt' || node.status === 'invalid') return RED
  return groupColor(node.group)
}

export function edgeColor(edge: GraphEdge): string {
  if (edge.status === 'invalid') return EDGE_INVALID
  return RELATION_TYPE_COLORS[edge.type] ?? EDGE_OK
}

// Relation/edge type → colour so multi-edges between the same pair are
// distinguishable (D6). Membership edges (projects / code-clones / shared-
// papers) use a neutral tone; relation edges get their own hues.
export const RELATION_TYPE_COLORS: Record<string, string> = {
  related: '#5d6d7e',
  extends: '#1e8449',
  contradicts: '#c0392b',
  projects: '#c9ccd1',
  'code-clones': '#b9770e',
  'shared-papers': '#8e9096',
}

// Map a node's raw `size` (degree / count) to a canvas radius. sqrt keeps area
// roughly proportional to the value (bar-graph discipline carried to nodes).
export function nodeRadius(size: number): number {
  return 3 + Math.sqrt(Math.max(0, size)) * 1.6
}

// Deterministic seed offset for initial placement so reopening is roughly
// stable (B2/B4) — react-force-graph only reads it once before the sim runs.
export function seedXY(id: string): { x: number; y: number } {
  const h = hashString(id)
  const angle = (h % 360) * (Math.PI / 180)
  const r = 50 + (((h >>> 9) % 1000) / 1000) * 250
  return { x: Math.cos(angle) * r, y: Math.sin(angle) * r }
}

export const SHAPE_BY_TYPE: Record<NodeType, string> = {
  project: 'square',
  paper: 'circle',
  code: 'diamond',
  corrupt: 'circle (red ring)',
}

// Draw a node's shape on a 2D canvas at (x, y) with the given radius/fill.
export function drawNodeShape(
  ctx: CanvasRenderingContext2D,
  type: NodeType,
  x: number,
  y: number,
  r: number,
  fill: string,
  highlighted: boolean,
): void {
  ctx.beginPath()
  ctx.fillStyle = fill
  ctx.strokeStyle = highlighted ? '#111418' : 'rgba(0,0,0,0.25)'
  ctx.lineWidth = highlighted ? 2 : 0.8

  if (type === 'project') {
    const s = r * 1.7
    ctx.rect(x - s / 2, y - s / 2, s, s)
    ctx.fill()
    ctx.stroke()
  } else if (type === 'code') {
    const s = r * 1.3
    ctx.moveTo(x, y - s)
    ctx.lineTo(x + s, y)
    ctx.lineTo(x, y + s)
    ctx.lineTo(x - s, y)
    ctx.closePath()
    ctx.fill()
    ctx.stroke()
  } else {
    // paper + corrupt are circles; corrupt additionally gets a red ring.
    ctx.arc(x, y, r, 0, 2 * Math.PI)
    ctx.fill()
    ctx.stroke()
    if (type === 'corrupt') {
      ctx.beginPath()
      ctx.strokeStyle = RED
      ctx.lineWidth = 2.5
      ctx.arc(x, y, r + 2.5, 0, 2 * Math.PI)
      ctx.stroke()
    }
  }
}
