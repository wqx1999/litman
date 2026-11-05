import { browse } from './browse'
import { cockpit } from './cockpit'
import { common } from './common'
import { reader } from './reader'
import { topbar } from './topbar'

/** The English UI text — the single source. Every other language is typed
 * against `Messages`, so the compiler lists whatever it is missing. One file
 * per area so the areas can be edited independently; inside an area, one
 * nested object per component. */
export const en = { common, topbar, cockpit, browse, reader }

export type Messages = typeof en
