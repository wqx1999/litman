// Summary banner: papers / projects / codes / corrupt / invalid-edge counts.
// Corrupt and invalid get red emphasis when non-zero — the banner doubles as a
// drift-diagnostic readout (invariant #14: nothing vanishes, counts surface).

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
    <div className="flex flex-col items-center px-3">
      <span
        className="text-xl font-semibold tabular-nums"
        style={{ color: danger ? CORRUPT_RED : '#4a4038' }}
      >
        {value}
      </span>
      <span
        className="text-xs uppercase tracking-wide"
        style={{ color: danger ? CORRUPT_RED : GREY }}
      >
        {label}
      </span>
    </div>
  )
}

export function SummaryBanner({ summary }: { summary: GraphSummary }) {
  return (
    <div className="flex items-center gap-1 rounded-lg border border-stone-200 bg-white/90 px-2 py-2 shadow-sm backdrop-blur">
      <Stat label="papers" value={summary.papers} />
      <div className="h-8 w-px bg-stone-200" />
      <Stat label="projects" value={summary.projects} />
      <div className="h-8 w-px bg-stone-200" />
      <Stat label="codes" value={summary.codes} />
      <div className="h-8 w-px bg-stone-200" />
      <Stat label="corrupt" value={summary.corrupt} alert />
      <div className="h-8 w-px bg-stone-200" />
      <Stat label="invalid edges" value={summary.invalid_edges} alert />
    </div>
  )
}
