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
import type { ToastVariant } from './Toast'

const PREFS: LangPref[] = ['auto', 'en', 'zh']

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
  const pref = useLangPref()
  const index = PREFS.indexOf(pref)

  const choose = (next: LangPref) => {
    if (next === pref) return
    setLangPref(next)
    saveUiLang(next).catch((e: unknown) => {
      const detail = e instanceof Error ? e.message : String(e)
      notify(getT().reader.cheatSheet.lang.saveFailed(detail), 'error')
    })
  }

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
