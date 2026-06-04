// Controls: choose the colour/cluster dimension, see (and clear) the current
// focus, and export a PNG. Colouring is one way to look at the network;
// focusing into a value is the other (triggered from the legend swatches).

import { DIMENSIONS, DIMENSION_LABEL, type Dimension } from '../types'

interface Props {
  color: Dimension
  focus: { dim: Dimension; value: string } | null
  onSetColor: (dim: Dimension) => void
  onClearFocus: () => void
  onExportPng: () => void
}

export function Controls({
  color,
  focus,
  onSetColor,
  onClearFocus,
  onExportPng,
}: Props) {
  return (
    <div className="flex flex-col gap-4 rounded-xl border border-[#e3dccd] bg-[#faf8f3]/90 p-3 text-sm shadow-sm">
      {/* Focus state / back to overview */}
      <div>
        <div className="mb-1 text-xs font-medium uppercase tracking-wide text-stone-400">
          View
        </div>
        {focus ? (
          <div className="flex flex-col gap-1.5">
            <div className="text-xs text-stone-600">
              Focused on{' '}
              <span className="font-medium text-stone-800">
                {DIMENSION_LABEL[focus.dim]} = {focus.value}
              </span>
            </div>
            <button
              type="button"
              onClick={onClearFocus}
              className="self-start rounded-md bg-stone-800 px-3 py-1.5 text-xs font-medium text-white transition hover:bg-stone-700"
            >
              ← All papers
            </button>
          </div>
        ) : (
          <div className="text-xs text-stone-500">
            All papers. Click a colour in the legend to zoom into that slice.
          </div>
        )}
      </div>

      {/* Colour / cluster dimension */}
      <div>
        <div className="mb-1.5 text-xs font-medium uppercase tracking-wide text-stone-400">
          Colour &amp; cluster by
        </div>
        <div className="flex flex-wrap gap-1.5">
          {DIMENSIONS.map((d) => {
            const active = d === color
            return (
              <button
                key={d}
                type="button"
                onClick={() => onSetColor(d)}
                className={`rounded-full border px-2.5 py-1 text-xs transition ${
                  active
                    ? 'border-stone-700 bg-stone-800 text-white'
                    : 'border-[#ddd5c6] bg-white text-stone-600 hover:bg-stone-50'
                }`}
              >
                {DIMENSION_LABEL[d]}
              </button>
            )
          })}
        </div>
      </div>

      <button
        type="button"
        onClick={onExportPng}
        className="self-start rounded-md border border-[#ddd5c6] px-3 py-1.5 text-xs font-medium text-stone-700 transition hover:bg-stone-100"
      >
        ↓ Export PNG
      </button>
    </div>
  )
}
