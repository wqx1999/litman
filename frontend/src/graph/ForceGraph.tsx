import { useEffect, useMemo, useState } from 'react'
import ForceGraph2D, {
  type ForceGraphMethods,
  type LinkObject,
  type NodeObject,
} from 'react-force-graph-2d'
import type {
  Dimension,
  GraphEdge,
  GraphNode,
  RenderEdge,
  RenderNode,
} from '../types'
import {
  CORRUPT_RED,
  DRIFT_RED,
  colorForKey,
  drawNode,
  edgeColor,
  nodeRadius,
  seedXY,
} from './encoding'
import {
  clusterTargets,
  colorKeyOf,
  isPivot,
  neighborIds,
  targetFor,
  withCurvature,
} from './dimensions'

type FGNode = NodeObject<RenderNode>
type FGLink = LinkObject<RenderNode, RenderEdge>
export type FGMethods = ForceGraphMethods<RenderNode, RenderEdge>

interface Props {
  nodes: GraphNode[]
  edges: GraphEdge[]
  // Dimension currently driving colour AND clustering.
  colorDim: Dimension
  // Distinct real values of `colorDim` present in `nodes` (cluster anchors).
  clusterValues: string[]
  // The paper whose detail card is open: gets a selection ring + pins the
  // neighbour highlight so its local structure stays lit while reading the card.
  selectedId: string | null
  width: number
  height: number
  // Clicking a node opens its detail card (delegated to the parent).
  onNodeClick: (node: GraphNode) => void
  graphRef: React.MutableRefObject<FGMethods | undefined>
}

// Cool-down ticks after which the engine freezes so the layout is stable for a
// screenshot. Large enough for the cluster force to settle a few-hundred-node
// view.
const COOLDOWN_TICKS = 160
// Cluster-anchor pull strength (per alpha tick). Strong enough to form visible
// clusters, weak enough that relation links still shape local structure.
const CLUSTER_STRENGTH = 0.13
// Show faint region labels only when few enough clusters that they don't
// clutter; above this, rely on the legend + hover.
const CLUSTER_LABEL_MAX = 14

function edgeEndpointId(end: string | number | FGNode | undefined): string {
  if (end == null) return ''
  if (typeof end === 'object') return String(end.id)
  return String(end)
}

export function ForceGraph({
  nodes,
  edges,
  colorDim,
  clusterValues,
  selectedId,
  width,
  height,
  onNodeClick,
  graphRef,
}: Props) {
  const [hoverId, setHoverId] = useState<string | null>(null)

  // Seed deterministic initial positions. Recomputed only when the visible
  // node/edge SET changes (focus toggle) — NOT on a recolour, so switching the
  // colour dimension recluster smoothly from current positions.
  const data = useMemo(() => {
    const rNodes: RenderNode[] = nodes.map((n) => {
      const { x, y } = seedXY(n.id)
      return { ...n, x, y }
    })
    const visibleIds = new Set(rNodes.map((n) => n.id))
    const links: RenderEdge[] = withCurvature(
      edges.filter(
        (e) => visibleIds.has(String(e.source)) && visibleIds.has(String(e.target)),
      ),
    ).map((e) => ({ ...e }))
    return { nodes: rNodes, links }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [nodes, edges])

  // Cluster anchors for the current colour dimension.
  const targets = useMemo(() => clusterTargets(clusterValues), [clusterValues])

  // (Re)install the cluster force whenever the dimension or its anchors change,
  // then reheat so the layout migrates to the new clustering.
  useEffect(() => {
    const fg = graphRef.current
    if (!fg) return
    let simNodes: RenderNode[] = []
    const force = (alpha: number) => {
      for (const n of simNodes) {
        const t = targetFor(n, colorDim, targets)
        if (!t) {
          // No value in this dimension -> gentle pull to centre.
          n.vx = (n.vx ?? 0) + (0 - (n.x ?? 0)) * 0.025 * alpha
          n.vy = (n.vy ?? 0) + (0 - (n.y ?? 0)) * 0.025 * alpha
          continue
        }
        n.vx = (n.vx ?? 0) + (t.x - (n.x ?? 0)) * CLUSTER_STRENGTH * alpha
        n.vy = (n.vy ?? 0) + (t.y - (n.y ?? 0)) * CLUSTER_STRENGTH * alpha
      }
    }
    ;(force as unknown as { initialize: (n: RenderNode[]) => void }).initialize = (
      n: RenderNode[],
    ) => {
      simNodes = n
    }
    fg.d3Force('cluster', force as never)
    fg.d3Force('charge')?.strength(-26)
    fg.d3ReheatSimulation()
  }, [colorDim, targets, data, graphRef])

  // Hover takes priority; a click pins the highlight to the selected node so
  // its neighbourhood stays lit while its detail card is open.
  const focusId = hoverId ?? selectedId
  const highlight = useMemo(
    () => (focusId ? neighborIds(data.links as unknown as GraphEdge[], focusId) : null),
    [focusId, data],
  )

  const paint = (node: FGNode, ctx: CanvasRenderingContext2D, globalScale: number) => {
    const x = node.x ?? 0
    const y = node.y ?? 0
    const corrupt = node.type === 'corrupt'
    const pivot = isPivot(node, colorDim)
    const r = nodeRadius(node.degree, pivot)
    const id = String(node.id)
    const dimmed = highlight !== null && !highlight.has(id)
    const fill = corrupt ? CORRUPT_RED : colorForKey(colorKeyOf(node, colorDim))

    ctx.globalAlpha = dimmed ? 0.12 : 1
    drawNode(ctx, x, y, r, fill, {
      highlighted: highlight?.has(id) ?? false,
      pivot,
      corrupt,
      drift: node.status === 'invalid',
    })

    // Selection ring (cool accent) on the node whose detail card is open.
    if (id === selectedId) {
      ctx.beginPath()
      ctx.strokeStyle = '#3f5a6b'
      ctx.lineWidth = 2
      ctx.arc(x, y, r + 5, 0, 2 * Math.PI)
      ctx.stroke()
    }

    // Labels (D9: never all-on, which is unreadable past a few dozen papers;
    // metadata title only, never notes/discussion). Two sources:
    //   * hovered node + its neighbours — always shown, darker (the focus read).
    //   * zoom-gated hub labels — when zoomed out only the most-connected papers
    //     (and pivots) are named; zooming in lowers the degree threshold so more
    //     appear, Obsidian-style. Suppressed on dimmed nodes during a hover
    //     focus so only the focused subgraph carries labels.
    const hovered = highlight !== null && highlight.has(id)
    const minDeg =
      globalScale >= 4 ? 0 : globalScale >= 2.5 ? 1 : globalScale >= 1.4 ? 2 : 4
    const hubLabel =
      !dimmed && !hovered && (node.degree >= minDeg || (pivot && globalScale >= 1.4))
    if (hovered || hubLabel) {
      const fontSize = Math.max(3, (hovered ? 12 : 11) / globalScale)
      ctx.font = `${fontSize}px Inter, Arial, sans-serif`
      ctx.fillStyle = hovered
        ? id === hoverId
          ? '#2f2922'
          : 'rgba(74,64,56,0.85)'
        : 'rgba(74,64,56,0.5)'
      ctx.textAlign = 'center'
      ctx.textBaseline = 'top'
      const label = node.label.length > 56 ? node.label.slice(0, 55) + '…' : node.label
      ctx.fillText(label, x, y + r + 2)
    }
    ctx.globalAlpha = 1
  }

  const paintPointerArea = (
    node: FGNode,
    color: string,
    ctx: CanvasRenderingContext2D,
  ) => {
    const x = node.x ?? 0
    const y = node.y ?? 0
    ctx.fillStyle = color
    ctx.beginPath()
    ctx.arc(x, y, nodeRadius(node.degree, isPivot(node, colorDim)) + 2, 0, 2 * Math.PI)
    ctx.fill()
  }

  // Faint cluster region labels at each anchor, drawn under the nodes so they
  // orient the reader ("this blob = topic X") without per-node clutter.
  const drawClusterLabels = (ctx: CanvasRenderingContext2D, globalScale: number) => {
    if (clusterValues.length === 0 || clusterValues.length > CLUSTER_LABEL_MAX) return
    const fontSize = Math.max(9, 13 / globalScale)
    ctx.font = `600 ${fontSize}px Inter, Arial, sans-serif`
    ctx.fillStyle = 'rgba(90,78,64,0.30)'
    ctx.textAlign = 'center'
    ctx.textBaseline = 'middle'
    for (const v of clusterValues) {
      const t = targets.get(v)
      if (t) ctx.fillText(v, t.x, t.y)
    }
  }

  const linkColor = (link: FGLink): string => {
    const s = edgeEndpointId(link.source)
    const t = edgeEndpointId(link.target)
    const dimmed = highlight !== null && !(highlight.has(s) && highlight.has(t))
    if (dimmed) return 'rgba(150,140,125,0.08)'
    const e = link as unknown as GraphEdge
    if (e.status === 'invalid') return 'rgba(168,73,58,0.45)'
    return edgeColor(e)
  }

  const linkWidth = (link: FGLink): number =>
    (link as unknown as GraphEdge).status === 'invalid' ? 1.2 : 2

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
      nodeVal={(n: FGNode) => nodeRadius(n.degree, isPivot(n, colorDim))}
      nodeLabel={() => ''}
      nodeCanvasObject={paint}
      nodePointerAreaPaint={paintPointerArea}
      onRenderFramePre={drawClusterLabels}
      linkColor={linkColor}
      linkWidth={linkWidth}
      linkLineDash={linkDash}
      linkCurvature={(l: FGLink) => l.__curvature ?? 0}
      linkDirectionalArrowColor={(l: FGLink) =>
        (l as unknown as GraphEdge).status === 'invalid' ? DRIFT_RED : edgeColor(l as unknown as GraphEdge)
      }
      linkDirectionalArrowLength={(l: FGLink) => (l.directed ? 5 : 0)}
      linkDirectionalArrowRelPos={0.5}
      cooldownTicks={COOLDOWN_TICKS}
      onEngineStop={() => graphRef.current?.zoomToFit(400, 50)}
      onNodeHover={(node: FGNode | null) => setHoverId(node ? String(node.id) : null)}
      onNodeClick={(node: FGNode) => onNodeClick(node)}
    />
  )
}
