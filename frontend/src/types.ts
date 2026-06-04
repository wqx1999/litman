// TS mirror of the JSON contract produced by `core/graph_model.py:build_graph`
// (rev 2 — paper-centric). Keep this in lock-step with that module's shapes.

// The closed set of colour / cluster / focus dimensions. Mirrors
// graph_model.DIMENSIONS exactly (order matters for the dropdown).
export const DIMENSIONS = [
  'projects',
  'topics',
  'methods',
  'data',
  'codes',
] as const
export type Dimension = (typeof DIMENSIONS)[number]

// Human-facing labels for each dimension (the raw key is also fine, but
// "code repos" reads better than "codes" in a dropdown).
export const DIMENSION_LABEL: Record<Dimension, string> = {
  projects: 'projects',
  topics: 'topics',
  methods: 'methods',
  data: 'data',
  codes: 'code repos',
}

export type NodeType = 'paper' | 'corrupt'
export type NodeStatus = 'ok' | 'invalid' | 'corrupt'
export type EdgeStatus = 'ok' | 'invalid'

// Cheap bibliographic projection shown in the click-to-open detail card. Mirrors
// graph_model._node_meta. NOT the full record — abstract / notes / PDF stay out
// (the GUI is offline); `read_status` is the paper's triage status (inbox /
// skim / deep-read / dropped), distinct from the node's graph `status`.
export interface NodeMeta {
  year: number | null
  authors: string[]
  n_authors: number
  journal: string
  doi: string
  type: string
  priority: string
  read_status: string
}

export interface GraphNode {
  id: string
  label: string
  type: NodeType
  status: NodeStatus
  degree: number
  // Membership across every dimension; the frontend colours / clusters /
  // focuses by reading these (no project or code *nodes* exist).
  dims: Record<Dimension, string[]>
  meta: NodeMeta
}

export interface GraphEdge {
  source: string
  target: string
  type: string
  directed: boolean
  weight: number
  status: EdgeStatus
}

export interface DimensionInfo {
  values: string[]
  invalid: string[]
}

export interface GraphSummary {
  papers: number
  corrupt: number
  invalid_edges: number
  dimensions: Record<Dimension, number>
}

export interface GraphData {
  summary: GraphSummary
  nodes: GraphNode[]
  edges: GraphEdge[]
  dimensions: Record<Dimension, DimensionInfo>
}

// react-force-graph mutates node/link objects in place (adds x/y/vx/vy and
// resolves link source/target to node refs). This is the runtime shape after
// the engine has touched the data.
export interface RenderNode extends GraphNode {
  x?: number
  y?: number
  vx?: number
  vy?: number
}

export interface RenderEdge extends Omit<GraphEdge, 'source' | 'target'> {
  source: string | RenderNode
  target: string | RenderNode
  __curvature?: number
}
