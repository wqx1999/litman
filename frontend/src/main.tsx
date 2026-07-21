import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import './index.css'
import App from './App'
import { startPresence } from './presence'

// Tell the server this page is alive for as long as it stays loaded — the
// --window shutdown gate follows the last live page, not the browser process.
startPresence()

// The webUI fetches all data over HTTP from the FastAPI server (no `file://`
// injection seam like the graph GUI), so main just mounts App.
createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <App />
  </StrictMode>,
)
