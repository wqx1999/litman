import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { ForceGraph, type FGMethods } from './graph/ForceGraph'
import {
  distinctGroups,
  neighborIds,
  subgraphFor,
  type View,
} from './graph/aggregate-drilldown'
import { Controls } from './controls/Controls'
import { Legend } from './controls/Legend'
import { SummaryBanner } from './controls/SummaryBanner'
import type { GraphData, GraphNode } from './types'

export default function App({ data }: { data: GraphData }) {
  const [view, setView] = useState<View>({ kind: 'aggregate' })
  const [highlight, setHighlight] = useState<Set<string> | null>(null)
  const [visibleGroups, setVisibleGroups] = useState<Set<string> | null>(null)
  const [size, setSize] = useState({ w: 800, h: 600 })

  const graphRef = useRef<FGMethods | undefined>(undefined)
  const canvasWrapRef = useRef<HTMLDivElement>(null)

  const subgraph = useMemo(() => subgraphFor(data, view), [data, view])
  const groups = useMemo(() => distinctGroups(subgraph.nodes), [subgraph])
  const drilldownProjects = useMemo(
    () => Object.keys(data.drilldown).sort(),
    [data],
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

  // Reset highlight + group filter when switching views.
  useEffect(() => {
    setHighlight(null)
    setVisibleGroups(null)
  }, [view])

  const handleNodeClick = useCallback(
    (node: GraphNode) => {
      // In aggregate view, clicking a project drills in. Elsewhere, a click
      // toggles neighbour highlight.
      if (view.kind === 'aggregate' && node.type === 'project') {
        setView({ kind: 'drilldown', project: node.id })
        return
      }
      setHighlight((cur) => {
        if (cur && cur.has(node.id) && cur.size <= 1) return null
        const nb = neighborIds(subgraph.edges, node.id)
        return nb
      })
    },
    [view, subgraph],
  )

  const handleToggleGroup = useCallback(
    (group: string) => {
      setVisibleGroups((cur) => {
        const base = cur ?? new Set(groups)
        const next = new Set(base)
        if (next.has(group)) next.delete(group)
        else next.add(group)
        // All selected => null (show everything, no dimming distinction).
        return next.size === groups.length ? null : next
      })
    },
    [groups],
  )

  const exportPng = useCallback(() => {
    const wrap = canvasWrapRef.current
    const canvas = wrap?.querySelector('canvas')
    if (!canvas) return
    const url = canvas.toDataURL('image/png')
    const a = document.createElement('a')
    a.href = url
    const stamp =
      view.kind === 'aggregate' ? 'overview' : view.project.replace(/[^\w-]+/g, '_')
    a.download = `litman-graph-${stamp}.png`
    a.click()
  }, [view])

  return (
    <div className="flex h-screen w-screen flex-col bg-[#f3efe8] text-stone-800">
      <header className="flex items-center justify-between border-b border-[#e7e1d5] bg-[#faf8f3] px-5 py-3">
        <div>
          <h1 className="text-lg font-semibold tracking-tight">
            litman <span className="text-stone-400">·</span> knowledge graph
          </h1>
          <p className="text-xs text-stone-400">
            {view.kind === 'aggregate'
              ? 'Library overview — project clusters'
              : `Project: ${view.project}`}
          </p>
        </div>
        <SummaryBanner summary={data.summary} />
      </header>

      <div className="flex min-h-0 flex-1">
        <aside className="flex w-72 shrink-0 flex-col gap-3 overflow-y-auto border-r border-[#e7e1d5] bg-[#f3efe8] p-3">
          <Controls
            view={view}
            drilldownProjects={drilldownProjects}
            groups={groups}
            visibleGroups={visibleGroups}
            onHome={() => setView({ kind: 'aggregate' })}
            onDrill={(p) => setView({ kind: 'drilldown', project: p })}
            onToggleGroup={handleToggleGroup}
            onResetGroups={() => setVisibleGroups(null)}
            onExportPng={exportPng}
          />
        </aside>

        <main ref={canvasWrapRef} className="relative min-w-0 flex-1">
          <ForceGraph
            subgraph={subgraph}
            width={size.w}
            height={size.h}
            highlight={highlight}
            visibleGroups={visibleGroups}
            onNodeClick={handleNodeClick}
            graphRef={graphRef}
          />
          <div className="absolute right-3 top-3 z-10 w-52">
            <Legend />
          </div>
          {subgraph.nodes.length === 0 && (
            <div className="pointer-events-none absolute inset-0 flex items-center justify-center text-sm text-stone-400">
              No nodes in this view.
            </div>
          )}
        </main>
      </div>
    </div>
  )
}
