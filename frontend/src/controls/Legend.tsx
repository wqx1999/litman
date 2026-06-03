// Self-explanatory legend (B4): someone reading a screenshot without litman
// installed must understand shapes, colours, and the red drift signal. Shapes
// are drawn as tiny inline SVG so they match the canvas glyphs exactly.

import { RED } from '../graph/encoding'

function Swatch({ children }: { children: React.ReactNode }) {
  return (
    <svg width="18" height="18" viewBox="-9 -9 18 18" className="shrink-0">
      {children}
    </svg>
  )
}

export function Legend() {
  return (
    <div className="rounded-lg border border-stone-200 bg-white/90 p-3 text-xs shadow-sm backdrop-blur">
      <div className="mb-2 font-semibold text-stone-700">Legend</div>

      <div className="mb-1 font-medium text-stone-500">Shape = entity</div>
      <ul className="mb-3 space-y-1">
        <li className="flex items-center gap-2">
          <Swatch>
            <rect x="-6" y="-6" width="12" height="12" fill="#5d6d7e" />
          </Swatch>
          <span>Project</span>
        </li>
        <li className="flex items-center gap-2">
          <Swatch>
            <circle r="6" fill="#5d6d7e" />
          </Swatch>
          <span>Paper</span>
        </li>
        <li className="flex items-center gap-2">
          <Swatch>
            <polygon points="0,-7 7,0 0,7 -7,0" fill="#5d6d7e" />
          </Swatch>
          <span>Code repo</span>
        </li>
      </ul>

      <div className="mb-1 font-medium text-stone-500">Colour</div>
      <ul className="mb-3 space-y-1">
        <li className="flex items-center gap-2">
          <Swatch>
            <circle r="6" fill="#d35400" />
          </Swatch>
          <span>Group (project) — stable per group</span>
        </li>
        <li className="flex items-center gap-2">
          <Swatch>
            <circle r="6" fill="#9aa0a6" />
          </Swatch>
          <span>Unassigned / weak link</span>
        </li>
      </ul>

      <div className="mb-1 font-medium text-stone-500">Health</div>
      <ul className="space-y-1">
        <li className="flex items-center gap-2">
          <Swatch>
            <circle r="5" fill="#9aa0a6" />
            <circle r="7.5" fill="none" stroke={RED} strokeWidth="2" />
          </Swatch>
          <span>
            <span style={{ color: RED }}>Corrupt</span> metadata (unreadable)
          </span>
        </li>
        <li className="flex items-center gap-2">
          <Swatch>
            <line x1="-7" y1="0" x2="7" y2="0" stroke={RED} strokeWidth="2" />
          </Swatch>
          <span>
            <span style={{ color: RED }}>Invalid</span> edge / node (drift)
          </span>
        </li>
        <li className="flex items-center gap-2">
          <Swatch>
            <line x1="-7" y1="0" x2="7" y2="0" stroke="#1e8449" strokeWidth="1.6" />
            <polygon points="7,0 3,-2 3,2" fill="#1e8449" />
          </Swatch>
          <span>Directed relation (extends / contradicts)</span>
        </li>
        <li className="flex items-center gap-2">
          <Swatch>
            <line x1="-7" y1="0" x2="7" y2="0" stroke="#5d6d7e" strokeWidth="1.6" />
          </Swatch>
          <span>Undirected relation (related / shared)</span>
        </li>
      </ul>
    </div>
  )
}
