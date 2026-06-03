import { useEffect, useMemo } from 'react'
import ForceGraph2D, {
  type ForceGraphMethods,
  type LinkObject,
  type NodeObject,
} from 'react-force-graph-2d'
import type { GraphEdge, GraphNode, RenderEdge, RenderNode, SubGraph } from '../types'
import {
  drawNodeShape,
  edgeColor,
  nodeColor,
  nodeHasDriftRing,
  nodeRadius,
  seedXY,
} from './encoding'
import { withCurvature } from './aggregate-drilldown'

// The concrete generic instantiations react-force-graph-2d uses internally.
type FGNode = NodeObject<RenderNode>
type FGLink = LinkObject<RenderNode, RenderEdge>
export type FGMethods = ForceGraphMethods<RenderNode, RenderEdge>

interface Props {
  subgraph: SubGraph
  width: number
  height: number
  // Set of node ids to keep at full opacity; everything else is dimmed. null =
  // no highlight (all full opacity).
  highlight: Set<string> | null
  // Only nodes whose group is in this set are drawn (null = show all).
  visibleGroups: Set<string> | null
  onNodeClick: (node: GraphNode) => void
  // Imperative handle so the parent (export-PNG button) can reach the canvas.
  graphRef: React.MutableRefObject<FGMethods | undefined>
}

// Cool-down ticks after which the engine stops so the layout freezes and the
// screenshot is stable (B2/B4). Small enough to settle fast on the few-hundred-
// node subgraphs the hierarchical view guarantees.
const COOLDOWN_TICKS = 120

function edgeEndpointId(end: string | number | FGNode | undefined): string {
  if (end == null) return ''
  if (typeof end === 'object') return String(end.id)
  return String(end)
}

export function ForceGraph({
  subgraph,
  width,
  height,
  highlight,
  visibleGroups,
  onNodeClick,
  graphRef,
}: Props) {
  // Seed deterministic initial positions so reopening is roughly stable. Done
  // on a fresh copy each time the subgraph identity changes (view switch).
  const data = useMemo(() => {
    const nodes: RenderNode[] = subgraph.nodes
      .filter((n) => visibleGroups === null || visibleGroups.has(n.group))
      .map((n) => {
        const { x, y } = seedXY(n.id)
        return { ...n, x, y }
      })
    const visibleIds = new Set(nodes.map((n) => n.id))
    const links: RenderEdge[] = withCurvature(
      subgraph.edges.filter(
        (e) => visibleIds.has(String(e.source)) && visibleIds.has(String(e.target)),
      ),
    ).map((e) => ({ ...e }))
    return { nodes, links }
  }, [subgraph, visibleGroups])

  // Re-run the simulation each time the data changes, then let cooldown freeze it.
  useEffect(() => {
    graphRef.current?.d3ReheatSimulation()
  }, [data, graphRef])

  const drawNode = (
    node: FGNode,
    ctx: CanvasRenderingContext2D,
    globalScale: number,
  ) => {
    const x = node.x ?? 0
    const y = node.y ?? 0
    const r = nodeRadius(node.size)
    const id = String(node.id)
    const dimmed = highlight !== null && !highlight.has(id)
    ctx.globalAlpha = dimmed ? 0.15 : 1
    drawNodeShape(
      ctx,
      node.type,
      x,
      y,
      r,
      nodeColor(node),
      highlight?.has(id) ?? false,
      nodeHasDriftRing(node),
    )

    // Label only when zoomed in enough or the node is highlighted, to avoid
    // clutter on dense views. Never renders notes/discussion — label is the
    // metadata title/name only (D9 red line).
    if (globalScale > 1.4 || (highlight !== null && highlight.has(id))) {
      const fontSize = Math.max(3, 11 / globalScale)
      ctx.font = `${fontSize}px Inter, Arial, sans-serif`
      ctx.fillStyle = dimmed ? 'rgba(74,64,56,0.3)' : '#4a4038'
      ctx.textAlign = 'center'
      ctx.textBaseline = 'top'
      const label = node.label.length > 48 ? node.label.slice(0, 47) + '…' : node.label
      ctx.fillText(label, x, y + r + 1)
    }
    ctx.globalAlpha = 1
  }

  const drawNodePointerArea = (
    node: FGNode,
    color: string,
    ctx: CanvasRenderingContext2D,
  ) => {
    const x = node.x ?? 0
    const y = node.y ?? 0
    ctx.fillStyle = color
    ctx.beginPath()
    ctx.arc(x, y, nodeRadius(node.size) + 2, 0, 2 * Math.PI)
    ctx.fill()
  }

  const linkColor = (link: FGLink): string => {
    const s = edgeEndpointId(link.source)
    const t = edgeEndpointId(link.target)
    const dimmed = highlight !== null && !(highlight.has(s) && highlight.has(t))
    if (dimmed) return 'rgba(150,140,125,0.10)'
    const e = link as unknown as GraphEdge
    // Invalid (drift) edges fade back so 67 of them don't drown the structure;
    // still visible (+ counted in the summary), just not shouting.
    if (e.status === 'invalid') return 'rgba(168,73,58,0.40)'
    return edgeColor(e)
  }

  const linkWidth = (link: FGLink): number => {
    if ((link as unknown as GraphEdge).status === 'invalid') return 1
    return 1.6 + Math.min(4, (link.weight ?? 1) - 1) * 0.7
  }

  const linkDash = (link: FGLink): number[] | null =>
    (link as unknown as GraphEdge).status === 'invalid' ? [3, 3] : null

  return (
    <ForceGraph2D
      ref={graphRef}
      graphData={data}
      width={width}
      height={height}
      backgroundColor="#f7f4ee"
      nodeRelSize={1}
      nodeVal={(n: FGNode) => nodeRadius(n.size)}
      nodeLabel={(n: FGNode) => `${n.type}: ${n.label}`}
      nodeCanvasObject={drawNode}
      nodePointerAreaPaint={drawNodePointerArea}
      linkColor={linkColor}
      linkWidth={linkWidth}
      linkLineDash={linkDash}
      linkCurvature={(l: FGLink) => l.__curvature ?? 0}
      linkDirectionalArrowLength={(l: FGLink) => (l.directed ? 5 : 0)}
      linkDirectionalArrowRelPos={0.5}
      cooldownTicks={COOLDOWN_TICKS}
      onEngineStop={() => {
        // Freeze: zoom-to-fit once, then the static cooldown keeps it still.
        graphRef.current?.zoomToFit(400, 40)
      }}
      onNodeClick={(node: FGNode) => onNodeClick(node)}
    />
  )
}
