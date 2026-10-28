import { QuestionCircleIcon } from './icons'

// A dialog title's one-line explainer, read on hover. It replaces the paragraph
// these dialogs used to open with: what an action costs is said by its own
// confirm, so the title only needs to say what the dialog is for. Native
// `title` like every other hover hint in the app; muted like the count beside it.
export default function HelpHint({ text }: { text: string }) {
  return (
    <span
      role="img"
      aria-label={text}
      title={text}
      className="ml-1.5 inline-flex cursor-help align-[-2px] font-normal text-stone-400 transition-colors hover:text-stone-600"
    >
      <QuestionCircleIcon className="h-[14px] w-[14px]" />
    </span>
  )
}
