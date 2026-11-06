import { Fragment } from 'react'
import type { ReactNode } from 'react'

/** Messages mark commands with backticks, as in "run `lit sync`". These set
 * the marked spans as code, on the page's light surfaces or on the toast's
 * dark pill. */

const TONE = {
  light: 'bg-stone-200/70 text-stone-700',
  dark: 'bg-white/15 text-white',
} as const

/** Text with its `code` spans set as code. Text with an unpaired backtick is
 * left as it is. */
export function withCode(text: string, tone: keyof typeof TONE = 'light'): ReactNode {
  const parts = text.split('`')
  if (parts.length < 3 || parts.length % 2 === 0) return text
  return parts.map((part, i) =>
    i % 2 === 1 ? (
      <code key={i} className={`rounded px-1 font-mono text-[12px] ${TONE[tone]}`}>
        {part}
      </code>
    ) : (
      <Fragment key={i}>{part}</Fragment>
    ),
  )
}

/** The same text without the backticks, for a hover title or a label. */
export function withoutCode(text: string): string {
  return text.split('`').length % 2 === 1 ? text.replaceAll('`', '') : text
}
