import type { Messages } from '../en'

/** The Chinese UI text. Not written yet: every area falls back to English
 * (see DICTS in ../index.ts). Once it is complete, its type tightens from
 * Partial to the full `Messages`, so a missing entry fails `tsc -b`. */
export const zh: Partial<Messages> = {}
