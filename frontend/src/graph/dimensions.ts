// Dimension-driven view logic for the paper-centric graph. Pure data shaping
// (no React, no canvas) so it stays unit-testable in isolation.
//
// Two orthogonal controls drive every view:
//   * COLOUR — recolour + recluster all papers by one dimension (projects /
//     topics / methods / data / codes). A paper with >1 value in that
//     dimension is a *pivot*: it bridges clusters.
//   * FOCUS  — zoom into one (dimension, value) slice: keep only papers that
//     carry that value, drop everyone else and all hub scaffolding. The slice
//     is then itself coloured by COLOUR (defaulting to a *different* dimension
//     so substructure is visible).

import type { Dimension, GraphData, GraphEdge, GraphNode } from '../types'
import { DIMENSIONS } from '../types'

// Synthetic colour keys for papers that are not a clean single value of the
// current colour dimension. Parenthesised so they cannot collide with a real
// TAXONOMY value (parentheses are not valid in controlled-vocabulary ids).
export const MULTI_KEY = '(multiple)' // pivot: belongs to >1 value
export const NONE_KEY = '(none)' // no value in this dimension

export interface View {
  color: Dimension
  focus: { dim: Dimension; value: string } | null
}

export function initialView(): View {
  return { color: 'projects', focus: null }
}

// When focusing into dimension `dim`, colouring by that same dimension is
// useless (every visible paper shares the value). Pick the first *other*
// dimension so the slice shows internal structure (e.g. focus a project ->
// colour its papers by topic).
export function colorAfterFocus(dim: Dimension): Dimension {
  return DIMENSIONS.find((d) => d !== dim) ?? dim
}

// The colour key for a node under the current colour dimension.
export function colorKeyOf(node: GraphNode, dim: Dimension): string {
  const vals = node.dims[dim]
  if (!vals || vals.length === 0) return NONE_KEY
  if (vals.length > 1) return MULTI_KEY
  return vals[0]
}

// A pivot under the current lens: carries more than one value of `dim`.
export function isPivot(node: GraphNode, dim: Dimension): boolean {
  return (node.dims[dim]?.length ?? 0) > 1
}

// Nodes visible in the current view. Overview shows every node (papers +
// corrupt). Focus keeps only papers carrying the focused value; corrupt papers
// (empty dims) fall out of any focus, which is correct — they still appear in
// the overview and in the summary count (invariant #14 is about the default
// view + count, not every user-requested slice).
export function visibleNodes(data: GraphData, view: View): GraphNode[] {
  if (!view.focus) return data.nodes
  const { dim, value } = view.focus
  return data.nodes.filter((n) => n.dims[dim]?.includes(value))
}

// Relation edges whose BOTH endpoints are in the visible set. Dangling-target
// edges (broken relation pairing) have no node to attach to and are dropped
// from the canvas; they are still counted in summary.invalid_edges and their
// source paper carries an `invalid` status, so the breakage surfaces.
export function visibleEdges(
  edges: GraphEdge[],
  visibleIds: Set<string>,
): GraphEdge[] {
  return edges.filter(
    (e) => visibleIds.has(String(e.source)) && visibleIds.has(String(e.target)),
  )
}

// Distinct colour keys present among the given nodes for `dim`, ordered:
// real values first (alpha), then the synthetic MULTI / NONE keys last so the
// legend reads "values..., multiple, none".
export function colorKeysPresent(nodes: GraphNode[], dim: Dimension): string[] {
  const real = new Set<string>()
  let hasMulti = false
  let hasNone = false
  for (const n of nodes) {
    const k = colorKeyOf(n, dim)
    if (k === MULTI_KEY) hasMulti = true
    else if (k === NONE_KEY) hasNone = true
    else real.add(k)
  }
  const out = Array.from(real).sort()
  if (hasMulti) out.push(MULTI_KEY)
  if (hasNone) out.push(NONE_KEY)
  return out
}

// Fixed target points for each real value of the colour dimension, placed
// evenly around a circle. Papers are pulled toward the average of their
// values' targets (see ForceGraph clusterForce), so single-value papers land
// in a cluster and multi-value pivots settle between clusters. Radius grows
// with the value count so dense dimensions (many topics) don't overlap.
export function clusterTargets(
  values: string[],
): Map<string, { x: number; y: number }> {
  const n = values.length
  const radius = Math.max(180, n * 26)
  const out = new Map<string, { x: number; y: number }>()
  values.forEach((v, i) => {
    const a = (i / Math.max(1, n)) * 2 * Math.PI - Math.PI / 2
    out.set(v, { x: Math.cos(a) * radius, y: Math.sin(a) * radius })
  })
  return out
}

// The cluster target for a node: the centroid of its values' targets. A
// single-value paper -> that cluster; a pivot -> midpoint of its clusters; a
// no-value paper -> the centre (null target means "pull gently to origin").
export function targetFor(
  node: GraphNode,
  dim: Dimension,
  targets: Map<string, { x: number; y: number }>,
): { x: number; y: number } | null {
  const vals = node.dims[dim]
  if (!vals || vals.length === 0) return null
  let sx = 0
  let sy = 0
  let n = 0
  for (const v of vals) {
    const t = targets.get(v)
    if (t) {
      sx += t.x
      sy += t.y
      n++
    }
  }
  if (n === 0) return null
  return { x: sx / n, y: sy / n }
}

// Assign a per-edge curvature so multiple edges between the same node pair fan
// out into distinct parallel arcs (D6). Single edges stay straight.
export function withCurvature(
  edges: GraphEdge[],
): (GraphEdge & { __curvature: number })[] {
  const groups = new Map<string, GraphEdge[]>()
  for (const e of edges) {
    const a = String(e.source)
    const b = String(e.target)
    const key = a < b ? `${a} ${b}` : `${b} ${a}`
    const bucket = groups.get(key)
    if (bucket) bucket.push(e)
    else groups.set(key, [e])
  }
  const out: (GraphEdge & { __curvature: number })[] = []
  for (const bucket of groups.values()) {
    const n = bucket.length
    bucket.forEach((e, i) => {
      const curvature = n === 1 ? 0 : (i - (n - 1) / 2) * 0.25
      out.push({ ...e, __curvature: curvature })
    })
  }
  return out
}

// Set of node ids one hop from `nodeId` in the given edge set, plus itself.
// Used for neighbour highlighting on hover.
export function neighborIds(edges: GraphEdge[], nodeId: string): Set<string> {
  const out = new Set<string>([nodeId])
  for (const e of edges) {
    const s = String(e.source)
    const t = String(e.target)
    if (s === nodeId) out.add(t)
    else if (t === nodeId) out.add(s)
  }
  return out
}

// A node's relations, split by type AND direction, reconstructed from the
// forward-only edge set the data layer emits (so the detail card can show both
// "this extends X" and "X extends this" without the data carrying reverse
// fields). `related` is symmetric so direction is irrelevant. Targets that are
// not real nodes (dangling / cross-vault ids) still appear — the card greys them.
export interface NodeRelations {
  extends: string[]
  extendedBy: string[]
  related: string[]
  contradicts: string[]
  contradictedBy: string[]
}

export function relationsOf(edges: GraphEdge[], nodeId: string): NodeRelations {
  const r: NodeRelations = {
    extends: [],
    extendedBy: [],
    related: [],
    contradicts: [],
    contradictedBy: [],
  }
  for (const e of edges) {
    const s = String(e.source)
    const t = String(e.target)
    if (e.type === 'related') {
      if (s === nodeId) r.related.push(t)
      else if (t === nodeId) r.related.push(s)
    } else if (e.type === 'extends') {
      if (s === nodeId) r.extends.push(t)
      else if (t === nodeId) r.extendedBy.push(s)
    } else if (e.type === 'contradicts') {
      if (s === nodeId) r.contradicts.push(t)
      else if (t === nodeId) r.contradictedBy.push(s)
    }
  }
  return r
}
