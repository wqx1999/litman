import { useEffect, useRef, useState } from 'react'

import { saveUiLang } from '../api'
import {
  getT,
  LANG_NAMES,
  setLangPref,
  systemLang,
  useLangPref,
  useT,
  type LangPref,
} from '../i18n'
import { useEscapeLayer } from './escapeStack'
import { CheckIcon, LanguageIcon } from './icons'
import type { ToastVariant } from './Toast'

const PREFS: LangPref[] = ['auto', 'en', 'zh']

/** Apply a language choice at once and store it on the server (ui-state.json,
 * machine-wide, so the app window, a browser tab and the welcome page agree).
 * A failed save leaves the choice in force for this window and hands the
 * message to `onSaveError`. */
function useChooseLang(onSaveError: (msg: string) => void) {
  const pref = useLangPref()
  const choose = (next: LangPref) => {
    if (next === pref) return
    setLangPref(next)
    saveUiLang(next).catch(() => {
      onSaveError(getT().reader.cheatSheet.lang.saveFailed)
    })
  }
  return { pref, choose }
}

/** The UI-language switch: follow the system, or pin English / Chinese.
 *
 * It lives in the `?` panel header rather than the top bar, which already has
 * enough buttons. A click applies at once and is stored on the server
 * (ui-state.json), so the app window and a browser tab agree. If the save
 * fails, the choice still holds in this window until a reload. Same sliding
 * segmented control as the left panel's Reading / Read / All. */
export default function LangSwitch({
  notify,
}: {
  notify: (msg: string, variant?: ToastVariant) => void
}) {
  const t = useT().reader.cheatSheet.lang
  const { pref, choose } = useChooseLang((msg) => notify(msg, 'error'))
  const index = PREFS.indexOf(pref)

  return (
    <div
      role="radiogroup"
      aria-label={t.label}
      className="relative inline-grid shrink-0 grid-cols-3 rounded-lg bg-stone-200 p-0.5"
    >
      <div
        className="pointer-events-none absolute inset-y-0.5 left-0.5 rounded-md bg-white shadow-sm transition-transform duration-300 ease-fluid"
        style={{
          width: 'calc((100% - 4px) / 3)',
          transform: `translateX(calc(${index} * 100%))`,
        }}
      />
      {PREFS.map((p) => (
        <button
          key={p}
          type="button"
          role="radio"
          aria-checked={pref === p}
          title={p === 'auto' ? t.autoTitle(systemLang()) : undefined}
          onClick={() => choose(p)}
          className={`relative z-10 whitespace-nowrap rounded-md px-2.5 py-0.5 text-[11px] font-medium transition-colors ${
            pref === p ? 'text-stone-900' : 'text-stone-600 hover:text-stone-800'
          }`}
        >
          {p === 'auto' ? t.auto : LANG_NAMES[p]}
        </button>
      ))}
    </div>
  )
}

/** The same choice as a corner icon with a drop-down, for the welcome page:
 * the first-run card has no `?` panel, and picking the language there is part
 * of setting litman up. The choice is stored machine-wide like the switch
 * above, so the library view opens in it too. */
export function LangMenu({ onSaveError }: { onSaveError: (msg: string) => void }) {
  const t = useT().reader.cheatSheet.lang
  const { pref, choose } = useChooseLang(onSaveError)
  const [open, setOpen] = useState(false)
  const root = useRef<HTMLDivElement>(null)

  useEscapeLayer(open, () => setOpen(false))
  useEffect(() => {
    if (!open) return
    const onDown = (e: MouseEvent) => {
      if (!root.current?.contains(e.target as Node)) setOpen(false)
    }
    document.addEventListener('mousedown', onDown)
    return () => document.removeEventListener('mousedown', onDown)
  }, [open])

  return (
    <div ref={root} className="relative">
      <button
        type="button"
        title={t.label}
        aria-label={t.label}
        aria-haspopup="menu"
        aria-expanded={open}
        onClick={() => setOpen((o) => !o)}
        className={`flex h-8 w-8 items-center justify-center rounded-lg transition-colors ${
          open ? 'bg-stone-100 text-stone-700' : 'text-stone-400 hover:bg-stone-100 hover:text-stone-700'
        }`}
      >
        <LanguageIcon className="h-[18px] w-[18px]" />
      </button>
      {open && (
        <div
          role="menu"
          aria-label={t.label}
          className="absolute right-0 top-full z-20 mt-1 min-w-36 animate-grow-in rounded-xl border border-stone-200 bg-white p-1 text-left shadow-lg shadow-stone-900/5"
        >
          {PREFS.map((p) => (
            <button
              key={p}
              type="button"
              role="menuitemradio"
              aria-checked={pref === p}
              title={p === 'auto' ? t.autoTitle(systemLang()) : undefined}
              onClick={() => {
                choose(p)
                setOpen(false)
              }}
              className="flex w-full items-center justify-between gap-4 whitespace-nowrap rounded-lg px-2.5 py-1.5 text-sm text-stone-700 transition-colors hover:bg-stone-100"
            >
              {p === 'auto' ? t.auto : LANG_NAMES[p]}
              <CheckIcon
                className={`h-4 w-4 text-accent-600 ${pref === p ? '' : 'invisible'}`}
              />
            </button>
          ))}
        </div>
      )}
    </div>
  )
}
