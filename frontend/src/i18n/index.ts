import { useSyncExternalStore } from 'react'
import { en, type Messages } from './en'
import { zh } from './zh'

/** UI language — the GUI's own text only. Server error `detail`s, CLI
 * commands, agent output and the user's data stay as they are. */

export type { Messages }
export type Lang = 'en' | 'zh'
/** What the user picked: follow the system, or pin one language. */
export type LangPref = 'auto' | Lang

/** Each language's name, written in that language in every UI: someone who
 * cannot read the current one still finds their own. */
export const LANG_NAMES: Record<Lang, string> = { en: 'English', zh: '中文' }

const DICTS: Record<Lang, Messages> = { en, zh }

// localStorage only mirrors the preference so the first paint is already
// right. The record is the server's ui-state.json: the app window and a
// browser tab are two storage partitions (see fetchWhatsNew in api.ts).
const PREF_CACHE = 'litman.uiLang'

export function isLangPref(v: unknown): v is LangPref {
  return v === 'auto' || v === 'en' || v === 'zh'
}

/** The system language. The FIRST entry of the browser's preference list
 * decides — someone listing en-US before zh-CN reads English. Every Chinese
 * variant (zh-CN / zh-TW / zh-HK) gets the one Chinese table, Simplified. */
export function systemLang(): Lang {
  if (typeof navigator === 'undefined') return 'en'
  const first = navigator.languages?.[0] ?? navigator.language ?? ''
  return first.toLowerCase().startsWith('zh') ? 'zh' : 'en'
}

function resolve(p: LangPref): Lang {
  return p === 'auto' ? systemLang() : p
}

function readCachedPref(): LangPref {
  try {
    const v = localStorage.getItem(PREF_CACHE)
    return isLangPref(v) ? v : 'auto'
  } catch {
    return 'auto'
  }
}

let pref: LangPref = readCachedPref()
let lang: Lang = resolve(pref)
const listeners = new Set<() => void>()

function syncHtmlLang(): void {
  if (typeof document !== 'undefined') {
    document.documentElement.lang = lang === 'zh' ? 'zh-CN' : 'en'
  }
}
syncHtmlLang()

function emit(): void {
  listeners.forEach((l) => l())
}

/** Switch the resolved language; true when it changed. */
function apply(next: Lang): boolean {
  if (next === lang) return false
  lang = next
  syncHtmlLang()
  return true
}

/** Apply a preference and mirror it locally. Saving it to the server is the
 * caller's job (a preference loaded FROM the server must not echo back). */
export function setLangPref(next: LangPref): void {
  // 'auto' → 'zh' on a Chinese system changes the preference, not the
  // language: the switch still has to re-render.
  const prefChanged = next !== pref
  pref = next
  try {
    localStorage.setItem(PREF_CACHE, next)
  } catch {
    /* private mode / no storage — applies for this session only */
  }
  if (apply(resolve(next)) || prefChanged) emit()
}

export function getLangPref(): LangPref {
  return pref
}

export function getLang(): Lang {
  return lang
}

// 'auto' follows the system live: the browser fires this when its language
// list changes.
if (typeof window !== 'undefined') {
  window.addEventListener('languagechange', () => {
    if (pref === 'auto' && apply(systemLang())) emit()
  })
}

function subscribe(cb: () => void): () => void {
  listeners.add(cb)
  return () => {
    listeners.delete(cb)
  }
}

/** The current language; re-renders the caller when it changes. */
export function useLang(): Lang {
  return useSyncExternalStore(subscribe, getLang, getLang)
}

/** The stored preference (auto / en / zh); re-renders the caller when it
 * changes, the server's value arriving after mount included. */
export function useLangPref(): LangPref {
  return useSyncExternalStore(subscribe, getLangPref, getLangPref)
}

/** The UI text table for the current language. Call it in EVERY component
 * that renders text: it is what re-renders that component on a switch (a
 * memoized child never hears about a parent's re-render). */
export function useT(): Messages {
  return DICTS[useLang()]
}

/** The same table outside render — event handlers, toasts, helpers. Read at
 * call time, so the text comes out in the language of that moment. */
export function getT(): Messages {
  return DICTS[lang]
}

/** Locale for Date / number formatting. English keeps the browser's own
 * locale (a German system keeps its 24-hour clock, as before); Chinese pins
 * zh-CN. */
export function formatLocale(): string | undefined {
  return lang === 'zh' ? 'zh-CN' : undefined
}

/** Display label of a fixed-enum value (status / type / relation kind). The
 * VALUE is what the CLI writes to metadata.yaml and never changes; only its
 * label is localized. A value the table does not know (a newer server, a
 * hand-edited yaml) shows as-is. */
export function enumLabel(table: Record<string, string>, value: string): string {
  return Object.prototype.hasOwnProperty.call(table, value) ? table[value] : value
}
