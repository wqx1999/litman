// TS mirror of the JSON contract produced by `core/graph_model.py:build_graph`.
// Keep this in lock-step with that module's node/edge/summary shapes.

export type NodeType = 'project' | 'paper' | 'code' | 'corrupt'
export type NodeStatus = 'ok' | 'invalid' | 'corrupt'
export type EdgeStatus = 'ok' | 'invalid'

export interface GraphNode {
  id: string
  type: NodeType
  label: string
  size: number
  status: NodeStatus
  group: string
}

export interface GraphEdge {
  source: string
  target: string
  type: string
  directed: boolean
  weight: number
  status: EdgeStatus
}

export interface SubGraph {
  nodes: GraphNode[]
  edges: GraphEdge[]
}

export interface GraphSummary {
  papers: number
  projects: number
  codes: number
  corrupt: number
  invalid_edges: number
}

export interface GraphData {
  summary: GraphSummary
  aggregate: SubGraph
  drilldown: Record<string, SubGraph>
}

// react-force-graph mutates node/link objects in place (adds x/y/vx/vy and
// resolves link source/target to node refs). This is the runtime shape after
// the engine has touched the data.
export interface RenderNode extends GraphNode {
  x?: number
  y?: number
}

export interface RenderEdge extends Omit<GraphEdge, 'source' | 'target'> {
  source: string | RenderNode
  target: string | RenderNode
  __curvature?: number
}
