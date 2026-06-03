# Vendored JavaScript — `lit graph` GUI

`index.html` in this directory is a single self-contained page produced by
`vite build` from the `litman/frontend/` subproject (run `bash
frontend/build.sh` to regenerate). All third-party JavaScript is inlined into
that file. This record satisfies OQ-B (vendored-JS license compliance): every
bundled dependency is permissively licensed (MIT), versions pinned at build
time below.

Rebuild whenever `frontend/` source changes and commit the regenerated
`index.html` alongside the source (M35 §2.8 red line).

## Runtime libraries inlined into `index.html`

| Library | Version | License |
|---|---|---|
| react | 19.2.7 | MIT |
| react-dom | 19.2.7 | MIT |
| react-force-graph-2d | 1.29.1 | MIT |
| force-graph (transitive engine of react-force-graph-2d) | 1.51.4 | MIT |
| d3-force / d3-* (transitive, via force-graph) | (bundled by force-graph) | ISC / BSD-3-Clause |

## Build-time only (NOT inlined; runs on the dev machine only)

| Tool | Version | License |
|---|---|---|
| vite | 7.3.x | MIT |
| vite-plugin-singlefile | 2.1.x | MIT |
| @vitejs/plugin-react | 5.1.x | MIT |
| tailwindcss + @tailwindcss/vite | 4.3.0 | MIT |
| typescript | 5.9.x | Apache-2.0 |

The d3 family used by `force-graph` (d3-force, d3-zoom, d3-drag, d3-selection,
etc.) is published by Mike Bostock under ISC / BSD-3-Clause. force-graph bundles
the parts it needs; none impose copyleft obligations.
