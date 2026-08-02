import { useEffect, useRef, useState } from 'react'
import type { IdSegmentName, IdSegments } from '../api'

/** The Paper ID line on the add dialog, shared by both ways in.
 *
 * Always on screen, because the id is the one field on this card that is
 * permanent: it becomes the folder name, the `[[wiki-link]]` target and the
 * project symlink, and only `lit rename` moves it afterwards. Everything else
 * here is a pencil click away once the paper is in. So the settled case shows
 * as a quiet grey line you can click to take over, rather than as a form field
 * competing with the ones that actually need filling in.
 *
 * When the server cannot name the id by itself it opens into its three parts
 * — `<year>_<Family>_<Keyword>` — with the parts it derived shown settled and
 * only the missing one(s) editable. That is not a rare corner: `core.id`
 * refuses to build a keyword out of a title in a space-less script (a Chinese
 * title arrives at the tokenizer as one token and used to slug down to
 * whatever stray Latin character was in it — `2018_Zhang_A`), and a Chinese
 * author name leaves nothing to slug at all. Both used to land in one empty
 * box, which reads as "invent the whole thing" when in fact the year and often
 * the family name are already right there — and a first-time reader with no
 * reason to know an id is ASCII types the same Chinese back into it.
 *
 * `proposedId`/`error`/`suggestion`/`segments` all come from the server — the
 * frontend neither derives an id nor splits one, or the preview and the write
 * could disagree. */
export default function PaperIdField({
  proposedId,
  error,
  suggestion,
  segments,
  override,
  onOverride,
  disabled = false,
}: {
  /** The id the server derived, or null when it could not. */
  proposedId: string | null
  /** One-line reason, shown only alongside the input. */
  error: string | null
  /** Server-offered starting point; null means it had nothing worth offering. */
  suggestion: string | null
  /** The id in three parts, or null before the server has been asked. */
  segments: IdSegments | null
  /** What the user typed. Null = leave it to the server. */
  override: string | null
  onOverride: (value: string | null) => void
  disabled?: boolean
}) {
  // "Type the whole id myself": entered by clicking the settled id, and the
  // only mode before the server has segments to show. Sticky once entered —
  // segments arriving mid-word must not pull a half-typed id out from under
  // whoever is typing it.
  const [free, setFree] = useState(false)
  // Per-segment text, kept apart from the server's so that fixing the author
  // above cannot wipe a keyword typed below, and so a segment the server
  // learns to derive stops being the user's problem again.
  const [typed, setTyped] = useState<Partial<Record<IdSegmentName, string>>>({})

  const inputRef = useRef<HTMLInputElement>(null)
  const [takeFocus, setTakeFocus] = useState(false)

  const settled = proposedId !== null && !free
  const inParts = !settled && !free && segments !== null

  const partValue = (name: IdSegmentName): string => {
    if (segments === null) return ''
    if (!segments.needs.includes(name)) return segments[name] ?? ''
    return typed[name] ?? segments[name] ?? ''
  }

  // The three boxes own the override while they are on screen. A partly
  // filled id is reported as no id at all rather than as `1111_Wang_`, which
  // is a shape the validator accepts and nobody wants on disk — Add stays
  // disabled until all three parts say something.
  const y = partValue('year')
  const f = partValue('family')
  const k = partValue('keyword')
  useEffect(() => {
    if (free) return
    if (proposedId !== null) {
      // Derivation caught up (the author got fixed, say). Hand the id back to
      // the server rather than submitting a stale assembly of boxes.
      if (override !== null) onOverride(null)
      return
    }
    if (segments === null) return
    const joined = y && f && k ? `${y}_${f}_${k}` : null
    if (joined !== override) onOverride(joined)
  }, [free, proposedId, segments, y, f, k, override, onOverride])

  // Seed the free-text field the first time derivation fails with nothing to
  // show in parts, so the common case is "press Add" rather than "retype the
  // part of the title that was already usable". Guarded on `override === null`
  // so it can never overwrite something the user has since typed.
  useEffect(() => {
    if (segments !== null) return
    if (proposedId === null && override === null && suggestion) {
      onOverride(suggestion)
    }
  }, [segments, proposedId, override, suggestion, onOverride])

  // Move the caret into the field, but ONLY when the click below opened it.
  // Two reasons, and the second is the sharp one:
  //
  //  - clicking the id to change it and then having to click again to type is
  //    a gesture that does not finish;
  //  - the button the click landed on has just been unmounted, so focus falls
  //    back to <body> — outside the dialog. This modal handles Esc on the card
  //    itself, so a keystroke from <body> never reaches it, and the person is
  //    shut inside a dialog whose only other exit is the Cancel button.
  //
  // Not on the auto-expand path: that one fires while someone is typing a
  // title, and stealing the caret mid-word would be worse than either.
  useEffect(() => {
    if (!takeFocus) return
    inputRef.current?.focus()
    inputRef.current?.select()
    setTakeFocus(false)
  }, [takeFocus])

  if (settled) {
    return (
      <div className="mt-2 text-[11px] text-stone-400">
        Saved as{' '}
        <button
          type="button"
          onClick={() => {
            onOverride(proposedId)
            setFree(true)
            setTakeFocus(true)
          }}
          disabled={disabled}
          title="Click to choose the id yourself"
          className="rounded font-mono text-stone-500 underline decoration-stone-300 decoration-dotted underline-offset-2 transition duration-200 ease-fluid hover:text-accent-600 hover:decoration-accent-400 disabled:opacity-40"
        >
          {proposedId}
        </button>
      </div>
    )
  }

  const label = (
    <span className="mb-0.5 block text-[11px] font-semibold uppercase tracking-wider text-stone-500">
      Paper ID {proposedId === null && <span className="text-red-500">*</span>}
      {/* The charset sits on the label, not on the hint line below, because
          that line is spoken for whenever `error` is set — which is exactly
          when a part is missing and the person has never been told what it
          accepts. Written as a character range rather than "letters" so it
          cannot be read as including 汉字, the one case that lands here. */}
      <span className="ml-2 font-normal normal-case tracking-normal text-stone-400">
        A-Z, 0-9, - and _
      </span>
    </span>
  )

  if (inParts) {
    const box = (name: IdSegmentName, placeholder: string, width: string) => {
      const mine = segments!.needs.includes(name)
      return (
        <input
          type="text"
          value={partValue(name)}
          onChange={(e) => setTyped((t) => ({ ...t, [name]: e.target.value }))}
          readOnly={!mine}
          // A settled part is not a field: keeping it out of the tab order
          // means Tab walks the boxes that actually want an answer.
          tabIndex={mine ? undefined : -1}
          spellCheck={false}
          placeholder={mine ? placeholder : ''}
          disabled={disabled}
          aria-label={`Paper ID ${name}`}
          className={`${width} min-w-0 rounded-md border px-2 py-1 font-mono text-xs focus:outline-none disabled:opacity-50 ${
            mine
              ? 'border-stone-300 bg-white text-stone-800 placeholder:font-sans placeholder:text-stone-400 focus:border-accent-500'
              : // No border at all: a settled part reads as a chip the form
                // filled in, not as a field someone forgot to answer. With a
                // border it is one tint away from the empty box beside it.
                'cursor-default border-transparent bg-stone-100 text-stone-500'
          }`}
        />
      )
    }
    return (
      <div className="mt-2">
        {label}
        <div className="flex items-center gap-1">
          {box('year', '2018', 'w-14')}
          <span className="font-mono text-xs text-stone-400">_</span>
          {box('family', 'Zhang', 'w-24')}
          <span className="font-mono text-xs text-stone-400">_</span>
          {box('keyword', 'Keyword', 'flex-1')}
        </div>
        <p className="mt-1 text-[11px] leading-4 text-stone-400">
          {error ?? 'Permanent — `lit rename` changes it later.'}
        </p>
      </div>
    )
  }

  return (
    <div className="mt-2">
      {label}
      <input
        ref={inputRef}
        type="text"
        value={override ?? ''}
        onChange={(e) => {
          setFree(true)
          onOverride(e.target.value)
        }}
        spellCheck={false}
        placeholder="2018_Zhang_Keyword"
        disabled={disabled}
        aria-label="Paper ID"
        className="w-full rounded-md border border-stone-300 bg-white px-2 py-1 font-mono text-xs text-stone-800 placeholder:font-sans placeholder:text-stone-400 focus:border-accent-500 focus:outline-none disabled:opacity-50"
      />
      <p className="mt-1 text-[11px] leading-4 text-stone-400">
        {error ?? 'Permanent — `lit rename` changes it later.'}
      </p>
    </div>
  )
}
