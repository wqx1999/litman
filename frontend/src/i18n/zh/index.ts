import type { Messages } from '../en'
import { browse } from './browse'
import { cockpit } from './cockpit'
import { common } from './common'
import { reader } from './reader'
import { topbar } from './topbar'

/** The Chinese UI text. Typed as the full `Messages`: an entry missing here,
 * or one whose parameters differ from the English, fails `tsc -b`. Terms and
 * style follow the glossary in the i18n task spec (dev_docs). */
export const zh: Messages = { common, topbar, cockpit, browse, reader }
