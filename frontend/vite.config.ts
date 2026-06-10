import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import tailwindcss from '@tailwindcss/vite'

// The webUI is a normal multi-file SPA (index.html + hashed JS/CSS/worker under
// assets/), served over HTTP by the FastAPI server's StaticFiles mount — unlike
// the knowledge-graph GUI, which is a single inlined file opened over file://.
//
// `base` stays the default "/" because the SPA is mounted at the server root, so
// the generated /assets/... URLs resolve correctly. The build lands directly at
// the vendored location the Python package serves (build.sh re-asserts the path,
// but configuring it here keeps a bare `vite build` correct too).
export default defineConfig({
  plugins: [react(), tailwindcss()],
  build: {
    outDir: '../src/litman/assets/webui',
    emptyOutDir: true,
    chunkSizeWarningLimit: 4096,
  },
})
