import { useState } from 'react'

import type { FsAnchor } from '../api'
import DirectoryPicker, { ANCHOR_EMOJI } from './DirectoryPicker'
import type { PickerMode } from './DirectoryPicker'

/** The shared path-input class, lifted from the dialogs so all seven fields
 * match. No `dark:` variants — the app auto-adapts via the inverted `stone`
 * ramp + `.dark .bg-white`. */
const INPUT =
  'w-full min-w-0 flex-1 rounded-md border border-stone-300 bg-white px-2.5 py-1.5 ' +
  'text-sm text-stone-800 shadow-sm focus:outline-none focus:ring-1 focus:ring-accent-400 ' +
  'disabled:opacity-50'

interface PathFieldProps {
  value: string
  onChange: (path: string) => void
  mode: PickerMode
  placeholder?: string
  autoFocus?: boolean
  disabled?: boolean
  /** Keep "Enter submits" — the expert's paste-and-go must not get slower. */
  onEnter?: () => void
  /** Some spots select-all on focus (SetProjectPathDialog); preserved here. */
  selectOnFocus?: boolean
  /** Layout-only extra classes on the input+button row (e.g. `flex-1`). */
  className?: string
}

/** A text path input paired with a "Browse…" button (spec §3.1).
 *
 * The text input is byte-for-byte the bare `<input>` it replaces — same
 * value/onChange, Enter-to-submit, font-mono, spellCheck, disabled, and (per
 * spot) autoFocus/selectOnFocus — so pasting an absolute path and hitting Enter
 * is exactly as fast as before. Browse is purely additive: it opens the
 * DirectoryPicker, and picking a folder fills the field and closes the picker,
 * leaving the parent dialog open. */
export default function PathField({
  value,
  onChange,
  mode,
  placeholder,
  autoFocus,
  disabled,
  onEnter,
  selectOnFocus,
  className,
}: PathFieldProps) {
  const [picking, setPicking] = useState(false)

  return (
    <div className={`flex items-center gap-1.5 ${className ?? ''}`}>
      <input
        type="text"
        value={value}
        disabled={disabled}
        spellCheck={false}
        autoFocus={autoFocus}
        placeholder={placeholder}
        onChange={(e) => onChange(e.target.value)}
        onFocus={selectOnFocus ? (e) => e.target.select() : undefined}
        onKeyDown={(e) => {
          if (e.key === 'Enter') onEnter?.()
        }}
        className={`${INPUT} font-mono`}
      />
      <button
        type="button"
        disabled={disabled}
        onClick={() => setPicking(true)}
        className="shrink-0 rounded-md border border-stone-300 bg-white px-3 py-1.5 text-sm text-stone-600 shadow-sm transition-colors hover:bg-stone-50 disabled:opacity-50"
      >
        Browse…
      </button>
      {picking && (
        <DirectoryPicker
          initial={value}
          mode={mode}
          onCancel={() => setPicking(false)}
          onPick={(picked) => {
            onChange(picked)
            setPicking(false)
          }}
        />
      )}
    </div>
  )
}

/** A friendly name + emoji for a parent directory, for the create-vault cards
 * (#6/#7, spec §5): a known anchor reads as "🖥 Desktop", any other folder as
 * "📁 <basename>". `anchors` come from the mount-time `listDir()`. */
export interface LocationLabel {
  emoji: string
  label: string
}

export function describeLocation(parentDir: string, anchors: FsAnchor[]): LocationLabel {
  const clean = parentDir.trim().replace(/\/+$/, '')
  const hit = anchors.find((a) => a.path === clean)
  if (hit) return { emoji: ANCHOR_EMOJI[hit.label] ?? '📁', label: hit.label }
  const base = clean.split('/').pop() || clean || '~'
  return { emoji: '📁', label: base }
}
