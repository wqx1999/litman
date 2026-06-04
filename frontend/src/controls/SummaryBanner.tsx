// Summary banner: paper count, a few dimension cardinalities, and the drift
// counters. Corrupt + invalid-edge counts get red emphasis when non-zero — the
// banner doubles as a drift readout (invariant #14: nothing vanishes silently).

import { CORRUPT_RED, GREY } from '../graph/encoding'
import type { GraphSummary } from '../types'

function Stat({
  label,
  value,
  alert,
}: {
  label: string
  value: number
  alert?: boolean
}) {
  const danger = alert && value > 0
  return (
    <div className="flex flex-col items-center px-2.5">
      <span
        className="text-xl font-semibold tabular-nums"
        style={{ color: danger ? CORRUPT_RED : '#4a4038' }}
      >
        {value}
      </span>
      <span
        className="text-[10px] uppercase tracking-wide"
        style={{ color: danger ? CORRUPT_RED : GREY }}
      >
        {label}
      </span>
    </div>
  )
}

function Divider() {
  return <div className="h-8 w-px bg-stone-200" />
}

export function SummaryBanner({ summary }: { summary: GraphSummary }) {
  const d = summary.dimensions
  return (
    <div className="flex items-center gap-0.5 rounded-lg border border-stone-200 bg-white/90 px-2 py-2 shadow-sm backdrop-blur">
      <Stat label="papers" value={summary.papers} />
      <Divider />
      <Stat label="projects" value={d.projects} />
      <Divider />
      <Stat label="topics" value={d.topics} />
      <Divider />
      <Stat label="code repos" value={d.codes} />
      <Divider />
      <Stat label="corrupt" value={summary.corrupt} alert />
      <Divider />
      <Stat label="invalid edges" value={summary.invalid_edges} alert />
    </div>
  )
}
