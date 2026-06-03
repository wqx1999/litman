import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import tailwindcss from '@tailwindcss/vite'
import { viteSingleFile } from 'vite-plugin-singlefile'

// `vite build` must emit ONE self-contained HTML: JS + CSS inlined, no sibling
// asset files. graph.py reads that single file, replaces the injection token,
// writes it to a temp path, and `file://`-opens it (no server, offline-ok).
//
// The output lands directly at the vendored location consumed by the Python
// package (build.sh re-asserts the path, but configuring it here keeps a bare
// `vite build` correct too). assetsInlineLimit is maxed so the singlefile
// plugin has nothing left to externalize.
export default defineConfig({
  plugins: [react(), tailwindcss(), viteSingleFile()],
  build: {
    outDir: '../src/litman/assets/graph',
    emptyOutDir: false,
    assetsInlineLimit: 100000000,
    cssCodeSplit: false,
    chunkSizeWarningLimit: 4096,
    rollupOptions: {
      output: {
        inlineDynamicImports: true,
      },
    },
  },
})
