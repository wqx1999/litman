import { useEffect, useRef, useState } from 'react'

/** The Paper ID line on the add dialog, shared by both ways in.
 *
 * Always on screen, because the id is the one field on this card that is
 * permanent: it becomes the folder name, the `[[wiki-link]]` target and the
 * project symlink, and only `lit rename` moves it afterwards. Everything else
 * here is a pencil click away once the paper is in. So it shows as a quiet
 * grey line you can click to take over, rather than as a form field competing
 * with the ones that actually need filling in.
 *
 * It turns into an input by itself when the server could not derive an id.
 * That is not a rare corner: `core.id` refuses to build a keyword out of a
 * title in a space-less script (a Chinese title arrives at the tokenizer as
 * one token and used to slug down to whatever stray Latin character was in
 * it — `2018_Zhang_A`), so without somewhere to type one, every Chinese-titled
 * paper and every patent dead-ends on this card. The field is the way past,
 * and the same one `lit add --id` has always been on the CLI.
 *
 * `proposedId`/`error`/`suggestion` all come from the server — the frontend
 * never derives an id itself, or the preview and the write could disagree. */
export default function PaperIdField({
  proposedId,
  error,
  suggestion,
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
  /** What the user typed. Null = leave it to the server. */
  override: string | null
  onOverride: (value: string | null) => void
  disabled?: boolean
}) {
  const editing = override !== null || proposedId === null
  const inputRef = useRef<HTMLInputElement>(null)
  const [takeFocus, setTakeFocus] = useState(false)

  // Seed the field from the server's suggestion the first time derivation
  // fails, so the common case is "press Add" rather than "retype the part of
  // the title that was already usable". Guarded on `override === null` so it
  // can never overwrite something the user has since typed.
  useEffect(() => {
    if (proposedId === null && override === null && suggestion) {
      onOverride(suggestion)
    }
  }, [proposedId, override, suggestion, onOverride])

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

  if (!editing) {
    return (
      <div className="mt-2 text-[11px] text-stone-400">
        Saved as{' '}
        <button
          type="button"
          onClick={() => {
            onOverride(proposedId)
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

  return (
    <div className="mt-2">
      <span className="mb-0.5 block text-[11px] font-semibold uppercase tracking-wider text-stone-500">
        Paper ID {proposedId === null && <span className="text-red-500">*</span>}
      </span>
      <input
        ref={inputRef}
        type="text"
        value={override ?? ''}
        onChange={(e) => onOverride(e.target.value)}
        spellCheck={false}
        placeholder="2018_Zhang_Keyword"
        disabled={disabled}
        aria-label="Paper ID"
        className="w-full rounded-md border border-stone-300 bg-white px-2 py-1 font-mono text-xs text-stone-800 placeholder:font-sans placeholder:text-stone-400 focus:border-accent-500 focus:outline-none disabled:opacity-50"
      />
      <p className="mt-1 text-[11px] leading-4 text-stone-400">
        {error ?? 'Letters, digits, - and _ only. Permanent — `lit rename` changes it later.'}
      </p>
    </div>
  )
}
