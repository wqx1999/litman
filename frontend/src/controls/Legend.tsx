// Legend: the colour key for the current dimension, doubling as the focus
// picker — clicking a value zooms into that slice. A short marker note explains
// the pivot / corrupt / drift cues. Floats top-right over the canvas so it sits
// next to what it explains, and stays readable in an exported screenshot (B4).

import { CORRUPT_RED, DRIFT_RED, PIVOT, colorForKey } from '../graph/encoding'
import { MULTI_KEY, NONE_KEY } from '../graph/dimensions'
import { DIMENSION_LABEL, type Dimension } from '../types'

function keyLabel(key: string): string {
  if (key === MULTI_KEY) return 'multiple (pivot)'
  if (key === NONE_KEY) return 'none'
  return key
}

interface Props {
  colorDim: Dimension
  keys: string[]
  focusedValue: string | null
  onPick: (key: string) => void
}

export function Legend({ colorDim, keys, focusedValue, onPick }: Props) {
  return (
    <div className="rounded-xl border border-[#e3dccd] bg-[#faf8f3]/95 p-3 text-[11px] leading-tight text-stone-600 shadow-md backdrop-blur">
      <div className="mb-2 flex items-baseline justify-between">
        <span className="text-xs font-semibold text-stone-700">
          Colour = {DIMENSION_LABEL[colorDim]}
        </span>
        <span className="text-[10px] text-stone-400">click to zoom</span>
      </div>

      <ul className="mb-2.5 max-h-64 space-y-1 overflow-y-auto pr-1">
        {keys.map((k) => {
          const focusable = k !== MULTI_KEY && k !== NONE_KEY
          const isFocused = focusable && k === focusedValue
          return (
            <li key={k}>
              <button
                type="button"
                disabled={!focusable}
                onClick={() => onPick(k)}
                className={`flex w-full items-center gap-2 rounded px-1 py-0.5 text-left transition ${
                  focusable ? 'hover:bg-stone-200/50' : 'cursor-default'
                } ${isFocused ? 'bg-stone-200/70 font-medium text-stone-800' : ''}`}
              >
                <span
                  className="inline-block h-3 w-3 shrink-0 rounded-full"
                  style={{
                    backgroundColor: colorForKey(k),
                    border: k === MULTI_KEY ? `1.5px solid ${PIVOT}` : 'none',
                  }}
                />
                <span className="truncate">{keyLabel(k)}</span>
              </button>
            </li>
          )
        })}
        {keys.length === 0 && <li className="px-1 text-stone-400">no values</li>}
      </ul>

      <div className="border-t border-[#e7e1d5] pt-2 text-[10px] text-stone-500">
        <div className="mb-0.5">
          <span style={{ color: PIVOT }}>●</span> dark ring = pivot (bridges
          values)
        </div>
        <div className="mb-0.5">
          <span style={{ color: CORRUPT_RED }}>●</span> corrupt metadata
        </div>
        <div>
          <span style={{ color: DRIFT_RED }}>○</span> red ring / dashed edge =
          drift
        </div>
      </div>
    </div>
  )
}
