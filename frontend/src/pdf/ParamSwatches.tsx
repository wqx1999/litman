// Colour / size / thickness swatch row for one annotation type. Shared by two
// surfaces so they never drift: the PDF toolbar (setting the creation default
// before you draw) and the floating editor popover (recolouring / resizing the
// selected annotation). Purely presentational — every piece of state and the
// pdf.js param dispatch live in PdfView; this component only renders + calls back.

export type ParamType = 'highlight' | 'freetext' | 'ink'

// Built-in colour bars (a few fixed swatches, no full colour picker — per the
// user's request). Highlight uses pdf.js's translucent palette; text / ink use
// opaque ink colours. The highlight hex values MUST stay in sync with
// HIGHLIGHT_COLORS in PdfView.tsx (the pdf.js highlight-palette config) so a
// swatch resolves to a known highlight name.
const HL_SWATCHES = [
  { name: 'Yellow', hex: '#FFFF98' },
  { name: 'Green', hex: '#53FFBC' },
  { name: 'Blue', hex: '#80EBFF' },
  { name: 'Pink', hex: '#FFCBE6' },
  { name: 'Red', hex: '#FF4F5F' },
]
const PEN_SWATCHES = [
  { name: 'Black', hex: '#1A1A1A' },
  { name: 'Red', hex: '#E03131' },
  { name: 'Blue', hex: '#1971C2' },
  { name: 'Green', hex: '#2F9E44' },
  { name: 'Orange', hex: '#E8590C' },
]
// FreeText font sizes (FREETEXT_SIZE param) — `ui` sizes the preview glyph.
const TEXT_SIZES = [
  { value: 12, ui: '11px' },
  { value: 18, ui: '14px' },
  { value: 28, ui: '18px' },
]
// Ink line widths (INK_THICKNESS param) — `dot` is the preview bar height.
const INK_WIDTHS = [
  { value: 2, dot: 2 },
  { value: 6, dot: 4 },
  { value: 12, dot: 7 },
]

interface Props {
  type: ParamType
  /** Currently-active colour for `type` (drives the selected-swatch ring). */
  color: string
  onColor: (hex: string) => void
  textSize: number
  onTextSize: (v: number) => void
  inkWidth: number
  onInkWidth: (v: number) => void
}

export default function ParamSwatches({
  type,
  color,
  onColor,
  textSize,
  onTextSize,
  inkWidth,
  onInkWidth,
}: Props) {
  const swatches = type === 'highlight' ? HL_SWATCHES : PEN_SWATCHES
  return (
    <div className="flex items-center gap-1.5">
      {swatches.map((c) => {
        const active = color.toLowerCase() === c.hex.toLowerCase()
        return (
          <button
            key={c.hex}
            onClick={() => onColor(c.hex)}
            title={c.name}
            aria-label={c.name}
            aria-pressed={active}
            className={
              'h-5 w-5 rounded-full transition ' +
              (active
                ? 'ring-2 ring-accent-500 ring-offset-1'
                : 'ring-1 ring-stone-300 hover:scale-110')
            }
            style={{ backgroundColor: c.hex }}
          />
        )
      })}

      {type === 'freetext' && (
        <div className="ml-1 flex items-center gap-0.5">
          {TEXT_SIZES.map((s) => (
            <button
              key={s.value}
              onClick={() => onTextSize(s.value)}
              aria-pressed={textSize === s.value}
              title={`Font size ${s.value}`}
              className={
                'flex h-6 w-6 items-center justify-center rounded-md font-semibold leading-none transition-colors ' +
                (textSize === s.value
                  ? 'bg-accent-50 text-accent-700 ring-1 ring-accent-500'
                  : 'text-stone-600 hover:bg-stone-200')
              }
              style={{ fontSize: s.ui }}
            >
              A
            </button>
          ))}
        </div>
      )}

      {type === 'ink' && (
        <div className="ml-1 flex items-center gap-0.5">
          {INK_WIDTHS.map((w) => (
            <button
              key={w.value}
              onClick={() => onInkWidth(w.value)}
              aria-pressed={inkWidth === w.value}
              title={`Thickness ${w.value}`}
              className={
                'flex h-6 w-7 items-center justify-center rounded-md transition-colors ' +
                (inkWidth === w.value
                  ? 'bg-accent-50 ring-1 ring-accent-500'
                  : 'hover:bg-stone-200')
              }
            >
              <span
                className="rounded-full bg-stone-600"
                style={{ width: '14px', height: `${w.dot}px` }}
              />
            </button>
          ))}
        </div>
      )}
    </div>
  )
}
