/**
 * Links to other sites, clicked in the `lit gui --window` app window, open in
 * the user's default browser.
 *
 * That window runs on a browser profile of litman's own (commands/gui.py), so
 * a link it opens itself comes up with none of the user's sign-ins or
 * extensions — and a page has no way to hand a link to another browser. The
 * server has one: POST /api/open-url. gui.py marks the window's start address
 * with `?window=app` and nothing else ever carries it, so a tab (every tab
 * fallback included) never routes a click and keeps its links as they were.
 */
import { ApiError, openUrlOutside } from './api'

const QUERY_KEY = 'window'
const QUERY_VALUE = 'app'
const STORAGE_KEY = 'litman.appWindow'
// Windows' default double-click time.
const DOUBLE_CLICK_MS = 500

let appWindow = false

/** Read the app-window mark, once, before the app renders. The mark moves
 * from the address into sessionStorage, so a reload of the window keeps it
 * and an address copied out of the window never carries it. */
export function takeAppWindowMark(): void {
  const params = new URLSearchParams(location.search)
  if (params.get(QUERY_KEY) === QUERY_VALUE) {
    appWindow = true
    try {
      sessionStorage.setItem(STORAGE_KEY, '1')
    } catch {
      /* no storage: this load still knows, a reload falls back to the window */
    }
    params.delete(QUERY_KEY)
    const rest = params.toString()
    history.replaceState(
      history.state,
      '',
      location.pathname + (rest ? `?${rest}` : '') + location.hash,
    )
    return
  }
  try {
    appWindow = sessionStorage.getItem(STORAGE_KEY) === '1'
  } catch {
    appWindow = false
  }
}

export function inAppWindow(): boolean {
  return appWindow
}

/** Hosts the server refuses to hand to the browser: this computer, under the
 * names a browser writes it with (every IPv4 form comes out dotted, every IPv6
 * one compressed). Kept in the window without asking. */
function isThisComputer(hostname: string): boolean {
  const host = hostname.replace(/\.$/, '')
  return (
    host === 'localhost' ||
    host.endsWith('.localhost') ||
    /^127\.\d+\.\d+\.\d+$/.test(host) ||
    host === '0.0.0.0' ||
    host === '[::1]' ||
    host === '[::]' ||
    /^\[::ffff:(7f[0-9a-f]{2}:[0-9a-f]{1,4}|0:0)\]$/.test(host)
  )
}

/** Send every click on a link to another site to the default browser, until
 * the returned function is called.
 *
 * Capture phase on document, so it runs before any handler can stop the
 * event; `preventDefault` cancels only the navigation, so the link's own
 * onClick still runs (the SI window's Open website moves on to its files
 * step). A plain, modified or middle click all mean "open this link", and all
 * go the same way. When the server cannot open it, the reason is toasted and
 * the link opens here as before — still within the click's user activation,
 * since the server waits only a moment for the opener.
 */
export function routeExternalLinks(notify: (message: string, variant: 'error') => void): () => void {
  const opening = new Set<string>()

  function onClick(e: MouseEvent) {
    if (e.button !== (e.type === 'auxclick' ? 1 : 0)) return
    const a = e.target instanceof Element ? e.target.closest('a[href]') : null
    if (!a || a.hasAttribute('download')) return
    let url: URL
    try {
      url = new URL(a.getAttribute('href') ?? '', document.baseURI)
    } catch {
      return
    }
    if (url.protocol !== 'http:' && url.protocol !== 'https:') return
    if (url.origin === location.origin || isThisComputer(url.hostname)) return
    e.preventDefault()
    const href = url.href
    // A double click is one open, not two. The opener can answer before the
    // second click lands, so the link stays taken a moment longer.
    if (opening.has(href)) return
    opening.add(href)
    openUrlOutside(href)
      .catch((err: unknown) => {
        notify(
          err instanceof ApiError
            ? err.message
            : 'litman did not answer — opening the link in this window instead',
          'error',
        )
        // Opened with 'noopener' it would come back null, so the tie is cut by hand.
        const w = window.open(href, '_blank')
        if (w) w.opener = null
      })
      .finally(() => setTimeout(() => opening.delete(href), DOUBLE_CLICK_MS))
  }

  document.addEventListener('click', onClick, true)
  document.addEventListener('auxclick', onClick, true)
  return () => {
    document.removeEventListener('click', onClick, true)
    document.removeEventListener('auxclick', onClick, true)
  }
}
