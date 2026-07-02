import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import './index.css'
import App from './App'

// The webUI fetches all data over HTTP from the FastAPI server (no `file://`
// injection seam like the graph GUI), so main just mounts App.
createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <App />
  </StrictMode>,
)
