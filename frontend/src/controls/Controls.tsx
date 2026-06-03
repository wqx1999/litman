// Pivot controls (D3): toggle aggregate <-> drilldown, filter by group, export
// PNG. Grouping/recolouring by project is the default encoding; the group
// filter is the GUI pivot for "show only these projects" and doubles as a
// recolour focus (dimming non-selected groups is done in the canvas).

import { groupColor } from '../graph/encoding'
import type { View } from '../graph/aggregate-drilldown'

interface Props {
  view: View
  drilldownProjects: string[]
  groups: string[]
  visibleGroups: Set<string> | null
  onHome: () => void
  onDrill: (project: string) => void
  onToggleGroup: (group: string) => void
  onResetGroups: () => void
  onExportPng: () => void
}

export function Controls({
  view,
  drilldownProjects,
  groups,
  visibleGroups,
  onHome,
  onDrill,
  onToggleGroup,
  onResetGroups,
  onExportPng,
}: Props) {
  return (
    <div className="flex flex-col gap-3 rounded-lg border border-stone-200 bg-white/90 p-3 text-sm shadow-sm backdrop-blur">
      <div className="flex items-center justify-between gap-2">
        <div className="flex items-center gap-2">
          <button
            type="button"
            onClick={onHome}
            disabled={view.kind === 'aggregate'}
            className="rounded-md bg-stone-800 px-3 py-1.5 text-xs font-medium text-white transition hover:bg-stone-700 disabled:cursor-default disabled:bg-stone-300"
          >
            ⌂ Overview
          </button>
          <span className="text-xs text-stone-500">
            {view.kind === 'aggregate'
              ? 'click a project to drill in'
              : `drilled into ${view.project}`}
          </span>
        </div>
        <button
          type="button"
          onClick={onExportPng}
          className="rounded-md border border-stone-300 px-3 py-1.5 text-xs font-medium text-stone-700 transition hover:bg-stone-100"
        >
          ↓ Export PNG
        </button>
      </div>

      <div>
        <div className="mb-1 flex items-center justify-between">
          <span className="text-xs font-medium text-stone-500">
            Drill into project
          </span>
        </div>
        <select
          value={view.kind === 'drilldown' ? view.project : ''}
          onChange={(e) => {
            if (e.target.value) onDrill(e.target.value)
            else onHome()
          }}
          className="w-full rounded-md border border-stone-300 px-2 py-1 text-xs"
        >
          <option value="">— Library overview —</option>
          {drilldownProjects.map((p) => (
            <option key={p} value={p}>
              {p}
            </option>
          ))}
        </select>
      </div>

      <div>
        <div className="mb-1 flex items-center justify-between">
          <span className="text-xs font-medium text-stone-500">
            Filter by group
          </span>
          <button
            type="button"
            onClick={onResetGroups}
            className="text-xs text-stone-400 hover:text-stone-600"
          >
            reset
          </button>
        </div>
        <div className="flex max-h-40 flex-wrap gap-1.5 overflow-y-auto">
          {groups.map((g) => {
            const active = visibleGroups === null || visibleGroups.has(g)
            return (
              <button
                key={g}
                type="button"
                onClick={() => onToggleGroup(g)}
                className={`flex items-center gap-1.5 rounded-full border px-2 py-0.5 text-xs transition ${
                  active
                    ? 'border-stone-300 bg-white text-stone-700'
                    : 'border-stone-200 bg-stone-100 text-stone-400'
                }`}
              >
                <span
                  className="inline-block h-2.5 w-2.5 rounded-full"
                  style={{ backgroundColor: groupColor(g), opacity: active ? 1 : 0.3 }}
                />
                {g}
              </button>
            )
          })}
        </div>
      </div>
    </div>
  )
}
