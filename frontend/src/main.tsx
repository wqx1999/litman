import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import './index.css'
import App from './App'
import { SAMPLE_DATA } from './sample-data'
import type { GraphData } from './types'

// graph.py replaces the quoted injection token with a raw JSON object literal,
// so in production `window.__LIT_GRAPH_DATA__` is a GraphData object. We detect
// the injected value BY TYPE — never by string-equality against the literal
// token, so this module contributes no occurrence of the token to the compiled
// bundle (which would otherwise make graph.py's strict single-occurrence check
// trip). While unreplaced (`npm run dev`, or a stale/unbuilt asset) the value is
// still the placeholder string and we fall back to representative sample data.
declare global {
  interface Window {
    __LIT_GRAPH_DATA__?: GraphData | string
  }
}

function resolveData(): GraphData {
  const injected = window.__LIT_GRAPH_DATA__
  if (injected && typeof injected === 'object') return injected
  // A string (the unreplaced placeholder) or undefined: dev fallback. If it
  // happens to be a JSON string (defensive), parse it; otherwise sample data.
  if (typeof injected === 'string') {
    try {
      return JSON.parse(injected) as GraphData
    } catch {
      /* placeholder token / non-JSON: fall through to sample */
    }
  }
  return SAMPLE_DATA
}

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <App data={resolveData()} />
  </StrictMode>,
)
