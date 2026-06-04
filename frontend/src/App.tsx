import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { ForceGraph, type FGMethods } from './graph/ForceGraph'
import {
  MULTI_KEY,
  NONE_KEY,
  type View,
  colorAfterFocus,
  colorKeysPresent,
  initialView,
  visibleEdges,
  visibleNodes,
} from './graph/dimensions'
import { Controls } from './controls/Controls'
import { DetailPanel } from './controls/DetailPanel'
import { Legend } from './controls/Legend'
import { SummaryBanner } from './controls/SummaryBanner'
import { DIMENSIONS, DIMENSION_LABEL, type Dimension, type GraphData } from './types'

export default function App({ data }: { data: GraphData }) {
  const [view, setView] = useState<View>(initialView())
  const [selectedId, setSelectedId] = useState<string | null>(null)
  const [size, setSize] = useState({ w: 800, h: 600 })

  const graphRef = useRef<FGMethods | undefined>(undefined)
  const canvasWrapRef = useRef<HTMLDivElement>(null)

  // id -> node for the detail card (relation-target resolution); built from ALL
  // nodes so a selected paper / relation target resolves even when the current
  // focus has filtered it off the canvas.
  const nodesById = useMemo(
    () => new Map(data.nodes.map((n) => [n.id, n])),
    [data.nodes],
  )
  const selectedNode = selectedId ? (nodesById.get(selectedId) ?? null) : null
  const dimInvalid = useMemo(() => {
    const out = {} as Record<Dimension, string[]>
    for (const d of DIMENSIONS) out[d] = data.dimensions[d].invalid
    return out
  }, [data.dimensions])

  const visNodes = useMemo(() => visibleNodes(data, view), [data, view])
  const visIds = useMemo(() => new Set(visNodes.map((n) => n.id)), [visNodes])
  const visEdges = useMemo(
    () => visibleEdges(data.edges, visIds),
    [data.edges, visIds],
  )
  // Real cluster anchors (synthetic multiple/none keys never get a cluster).
  const clusterValues = useMemo(
    () =>
      colorKeysPresent(visNodes, view.color).filter(
        (k) => k !== MULTI_KEY && k !== NONE_KEY,
      ),
    [visNodes, view.color],
  )
  // Colour keys present (incl. synthetic) for the legend.
  const legendKeys = useMemo(
    () => colorKeysPresent(visNodes, view.color),
    [visNodes, view.color],
  )

  // Track the canvas container size so the force graph fills the panel.
  useEffect(() => {
    const el = canvasWrapRef.current
    if (!el) return
    const ro = new ResizeObserver((entries) => {
      const rect = entries[0].contentRect
      setSize({ w: Math.max(200, rect.width), h: Math.max(200, rect.height) })
    })
    ro.observe(el)
    return () => ro.disconnect()
  }, [])

  const setColor = useCallback((dim: Dimension) => {
    setView((v) => ({ ...v, color: dim }))
  }, [])

  // Zoom into a (dimension, value) slice. Colour auto-switches to a different
  // dimension so the slice's internal structure is immediately visible.
  const focusValue = useCallback((dim: Dimension, value: string) => {
    setView({ focus: { dim, value }, color: colorAfterFocus(dim) })
  }, [])

  const clearFocus = useCallback(() => {
    setView((v) => ({ ...v, focus: null }))
  }, [])

  // A legend swatch click zooms into that value of the CURRENT colour
  // dimension (synthetic multiple/none keys are not focusable).
  const onLegendClick = useCallback(
    (key: string) => {
      if (key === MULTI_KEY || key === NONE_KEY) return
      focusValue(view.color, key)
    },
    [view.color, focusValue],
  )

  const exportPng = useCallback(() => {
    const canvas = canvasWrapRef.current?.querySelector('canvas')
    if (!canvas) return
    const a = document.createElement('a')
    a.href = canvas.toDataURL('image/png')
    const stamp = view.focus
      ? `${view.focus.dim}-${view.focus.value}`.replace(/[^\w-]+/g, '_')
      : `by-${view.color}`
    a.download = `litman-graph-${stamp}.png`
    a.click()
  }, [view])

  const subtitle = view.focus
    ? `Focus · ${DIMENSION_LABEL[view.focus.dim]} = ${view.focus.value}  ·  ${visNodes.length} papers, coloured by ${DIMENSION_LABEL[view.color]}`
    : `All papers, coloured by ${DIMENSION_LABEL[view.color]}`

  return (
    <div className="flex h-screen w-screen flex-col bg-[#f3efe8] text-stone-800">
      <header className="flex items-center justify-between border-b border-[#e7e1d5] bg-[#faf8f3] px-5 py-3">
        <div>
          <h1 className="text-lg font-semibold tracking-tight">
            litman <span className="text-stone-400">·</span> knowledge graph
          </h1>
          <p className="text-xs text-stone-400">{subtitle}</p>
        </div>
        <SummaryBanner summary={data.summary} />
      </header>

      <div className="flex min-h-0 flex-1">
        <aside className="flex w-72 shrink-0 flex-col gap-3 overflow-y-auto border-r border-[#e7e1d5] bg-[#f3efe8] p-3">
          <Controls
            color={view.color}
            focus={view.focus}
            onSetColor={setColor}
            onClearFocus={clearFocus}
            onExportPng={exportPng}
          />
        </aside>

        <main ref={canvasWrapRef} className="relative min-w-0 flex-1">
          <ForceGraph
            nodes={visNodes}
            edges={visEdges}
            colorDim={view.color}
            clusterValues={clusterValues}
            selectedId={selectedId}
            width={size.w}
            height={size.h}
            onNodeClick={(n) => setSelectedId(n.id)}
            graphRef={graphRef}
          />
          <div className="absolute right-3 top-3 z-10 w-56">
            <Legend
              colorDim={view.color}
              keys={legendKeys}
              focusedValue={view.focus?.value ?? null}
              onPick={onLegendClick}
            />
          </div>
          {selectedNode && (
            <div className="absolute left-3 top-3 z-20 w-80">
              <DetailPanel
                node={selectedNode}
                nodesById={nodesById}
                edges={data.edges}
                dimensionInvalid={dimInvalid}
                onClose={() => setSelectedId(null)}
                onFocusValue={focusValue}
                onSelectNode={(id) => setSelectedId(id)}
              />
            </div>
          )}
          {visNodes.length === 0 && (
            <div className="pointer-events-none absolute inset-0 flex items-center justify-center text-sm text-stone-400">
              No papers in this view.
            </div>
          )}
        </main>
      </div>
    </div>
  )
}
