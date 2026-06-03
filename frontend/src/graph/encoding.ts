// D9 visual encoding helpers, shared by the canvas renderer and the legend so
// the two never drift. Shape encodes entity type, colour encodes group. The
// drift signal (corrupt / invalid) is deliberately *de-emphasised* so it does
// not shout over the actual structure: corrupt papers (the rare, severe case)
// stay a bold red dot; invalid nodes keep their group colour but gain a thin
// red ring; invalid edges fade to a translucent dashed red (see ForceGraph).
// Greys are the "weak / unassigned" reference tone (viz-conventions).

import type { GraphEdge, GraphNode, NodeType } from '../types'

// Warm, editorial palette ("暖色高级灰"). Low-saturation earth tones chosen to
// sit together without clashing, so adjacent project clusters read as a family
// rather than a default-matplotlib rainbow. Assigned by hashing the group name
// so a project keeps its colour across reopens and aggregate/drilldown views.
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

export const GREY = '#a8a097' // unassigned / weak relationship (warm grey)
export const CORRUPT_RED = '#b3402e' // bold red — corrupt metadata only
export const DRIFT_RED = '#a8493a' // ring / edge accent for invalid (drift)
export const EDGE_OK = '#b6ae9f' // warm neutral membership/relation edge

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

// Final fill colour for a node. Corrupt = bold red (rare + most severe, must be
// seen). Invalid keeps its group colour (the red *ring* marks it instead, so a
// whole cluster of taxonomy-drift papers doesn't turn into a wall of red).
export function nodeColor(node: GraphNode): string {
  if (node.status === 'corrupt') return CORRUPT_RED
  return groupColor(node.group)
}

// Whether a node should get the thin red drift ring (invalid, e.g. unregistered
// taxonomy). Corrupt draws its own bolder ring in drawNodeShape.
export function nodeHasDriftRing(node: GraphNode): boolean {
  return node.status === 'invalid'
}

export function edgeColor(edge: GraphEdge): string {
  if (edge.status === 'invalid') return DRIFT_RED
  return RELATION_TYPE_COLORS[edge.type] ?? EDGE_OK
}

// Relation/edge type → colour so multi-edges between the same pair are
// distinguishable (D6). Membership edges (projects / code-clones / shared-
// papers) use neutral warm tones; relation edges get their own muted hues.
export const RELATION_TYPE_COLORS: Record<string, string> = {
  related: '#9a9082', // warm grey — undirected, weak
  extends: '#6b7b4f', // olive — directed
  contradicts: '#b5552f', // brick — directed
  projects: '#cdc5b6', // pale warm grey — membership
  'code-clones': '#b08a4a', // antique gold — membership
  'shared-papers': '#b0a89c', // warm grey — aggregate shared-paper edge
}

// Map a node's raw `size` (degree / count) to a canvas radius. sqrt keeps area
// roughly proportional to the value (bar-graph discipline carried to nodes).
export function nodeRadius(size: number): number {
  return 4 + Math.sqrt(Math.max(0, size)) * 1.7
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
  corrupt: 'circle (red)',
}

// Trace a node's outline path (no fill/stroke) so the caller can fill-with-
// shadow then stroke-without-shadow over the identical path.
function traceShape(
  ctx: CanvasRenderingContext2D,
  type: NodeType,
  x: number,
  y: number,
  r: number,
): void {
  ctx.beginPath()
  if (type === 'project') {
    const s = r * 1.7
    const rad = Math.min(3, s * 0.18) // soft rounded square
    ctx.roundRect(x - s / 2, y - s / 2, s, s, rad)
  } else if (type === 'code') {
    const s = r * 1.35
    ctx.moveTo(x, y - s)
    ctx.lineTo(x + s, y)
    ctx.lineTo(x, y + s)
    ctx.lineTo(x - s, y)
    ctx.closePath()
  } else {
    ctx.arc(x, y, r, 0, 2 * Math.PI)
  }
}

// Draw a node on a 2D canvas: soft drop shadow under the fill, a hairline warm
// stroke (or a dark stroke when highlighted), plus a red ring for corrupt /
// invalid drift markers.
export function drawNodeShape(
  ctx: CanvasRenderingContext2D,
  type: NodeType,
  x: number,
  y: number,
  r: number,
  fill: string,
  highlighted: boolean,
  driftRing: boolean,
): void {
  // 1) fill with a soft warm shadow for depth.
  ctx.save()
  ctx.fillStyle = fill
  ctx.shadowColor = 'rgba(70,55,40,0.22)'
  ctx.shadowBlur = highlighted ? 10 : 4
  ctx.shadowOffsetY = 1
  traceShape(ctx, type, x, y, r)
  ctx.fill()
  ctx.restore()

  // 2) hairline stroke over the same path (no shadow).
  traceShape(ctx, type, x, y, r)
  ctx.strokeStyle = highlighted ? '#3a322a' : 'rgba(80,62,44,0.20)'
  ctx.lineWidth = highlighted ? 2 : 0.7
  ctx.stroke()

  // 3) drift ring: bold for corrupt, thinner for invalid.
  if (type === 'corrupt' || driftRing) {
    ctx.beginPath()
    ctx.strokeStyle = type === 'corrupt' ? CORRUPT_RED : DRIFT_RED
    ctx.lineWidth = type === 'corrupt' ? 2.4 : 1.6
    ctx.arc(x, y, r + 3, 0, 2 * Math.PI)
    ctx.stroke()
  }
}
