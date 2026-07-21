// Page-presence signal for the server's --window shutdown gate: hold a
// WebSocket open to /api/presence for as long as this page is loaded. The
// server only counts connections — no messages flow in either direction.
// A socket rather than a timer heartbeat on purpose: Chrome throttles JS
// timers in hidden/minimized windows down to about once a minute, so a POST
// heartbeat from a minimized window goes quiet and the server would kill a
// live page. Socket disconnect is a network event, and protocol ping/pong
// lives in the browser's network stack — neither runs on the throttled
// timer queue.

let attempt = 0

function connect(): void {
  const scheme = location.protocol === 'https:' ? 'wss' : 'ws'
  const ws = new WebSocket(`${scheme}://${location.host}/api/presence`)
  ws.onopen = () => {
    attempt = 0
  }
  // Reconnect is scheduled from onclose ONLY: a failed attempt fires error
  // and then close, so scheduling from both doubles the retry rate every
  // round. Exponential backoff capped at 8s, retrying forever — if the
  // server is really gone this page is dead anyway, and the idle retry
  // loop is harmless.
  ws.onclose = () => {
    const delay = Math.min(1000 * 2 ** attempt, 8000)
    attempt += 1
    setTimeout(connect, delay)
  }
}

/** Open (and keep reopening) the presence socket. Called once at page load. */
export function startPresence(): void {
  connect()
}
