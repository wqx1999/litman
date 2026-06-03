// Self-explanatory legend (B4): someone reading a screenshot without litman
// installed must understand shapes, colours, and the drift signal. Shapes are
// tiny inline SVG matching the canvas glyphs, drawn in real palette colours so
// the "shape = type, colour = project" split reads at a glance. Floats top-
// right over the canvas so it sits next to what it explains.

import { CORRUPT_RED, DRIFT_RED, GREY } from '../graph/encoding'

function Swatch({ children }: { children: React.ReactNode }) {
  return (
    <svg width="16" height="16" viewBox="-9 -9 18 18" className="shrink-0">
      {children}
    </svg>
  )
}

function Row({ swatch, children }: { swatch: React.ReactNode; children: React.ReactNode }) {
  return (
    <li className="flex items-center gap-2">
      <Swatch>{swatch}</Swatch>
      <span>{children}</span>
    </li>
  )
}

export function Legend() {
  return (
    <div className="rounded-xl border border-[#e3dccd] bg-[#faf8f3]/95 p-3 text-[11px] leading-tight text-stone-600 shadow-md backdrop-blur">
      <div className="mb-2 text-xs font-semibold text-stone-700">Legend</div>

      <div className="mb-1 font-medium text-stone-400">Shape = type · colour = project</div>
      <ul className="mb-2.5 space-y-1.5">
        <Row swatch={<rect x="-6" y="-6" width="12" height="12" rx="2.5" fill="#c0613b" />}>
          Project
        </Row>
        <Row swatch={<circle r="6" fill="#4a6b7b" />}>Paper</Row>
        <Row swatch={<polygon points="0,-7 7,0 0,7 -7,0" fill="#6b7b4f" />}>Code repo</Row>
        <Row swatch={<circle r="6" fill={GREY} />}>Unassigned / weak</Row>
      </ul>

      <div className="mb-1 font-medium text-stone-400">Relations</div>
      <ul className="mb-2.5 space-y-1.5">
        <Row
          swatch={
            <>
              <line x1="-7" y1="0" x2="6" y2="0" stroke="#6b7b4f" strokeWidth="1.8" />
              <polygon points="7,0 2.5,-2.5 2.5,2.5" fill="#6b7b4f" />
            </>
          }
        >
          extends / contradicts (directed)
        </Row>
        <Row swatch={<line x1="-7" y1="0" x2="7" y2="0" stroke="#9a9082" strokeWidth="1.8" />}>
          related / shared (undirected)
        </Row>
      </ul>

      <div className="mb-1 font-medium text-stone-400">Health</div>
      <ul className="space-y-1.5">
        <Row
          swatch={
            <>
              <circle r="4.5" fill={CORRUPT_RED} />
              <circle r="7" fill="none" stroke={CORRUPT_RED} strokeWidth="1.8" />
            </>
          }
        >
          <span style={{ color: CORRUPT_RED }}>Corrupt</span> metadata
        </Row>
        <Row
          swatch={
            <line
              x1="-7"
              y1="0"
              x2="7"
              y2="0"
              stroke={DRIFT_RED}
              strokeWidth="1.6"
              strokeDasharray="3 2"
              opacity="0.6"
            />
          }
        >
          <span style={{ color: DRIFT_RED }}>Drift</span> (invalid, faded)
        </Row>
      </ul>
    </div>
  )
}
