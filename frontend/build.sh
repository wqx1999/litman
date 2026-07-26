#!/usr/bin/env bash
# Build the litman webUI SPA and vendor it into the Python package.
#
# Idempotent + PATH-safe: prepends the litman conda env bin so node/npm/npx
# resolve even when the shell has not `conda activate`d. Run from anywhere.
#
# Output: src/litman/assets/webui/ — a MULTI-file vite build (index.html plus an
# assets/ subdir of hashed JS/CSS and the pdf.js worker). That split layout is
# EXPECTED here: the webUI is served over HTTP by the FastAPI StaticFiles mount,
# not inlined and file://-opened like the knowledge-graph GUI.
#
# RED LINE (invariant #14): after editing anything under frontend/, re-run this
# and commit the regenerated assets/webui/ product alongside the source. A drift
# between source and product means the user runs a stale GUI.
set -euo pipefail

ENV_BIN="/work/wangq/software/miniconda3/envs/litman/bin"
if [ -d "$ENV_BIN" ]; then
  export PATH="$ENV_BIN:$PATH"
fi

FRONTEND_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ASSET_DIR="$FRONTEND_DIR/../src/litman/assets/webui"
INDEX="$ASSET_DIR/index.html"

cd "$FRONTEND_DIR"

# ../assets/ is the single source for the brand marks. These two are copies —
# never hand-edit them. favicon.svg is the browser tab icon (public/ is copied
# verbatim into the build); logo.svg is bundled into the first-run page.
BRAND_DIR="$FRONTEND_DIR/../assets"
cp "$BRAND_DIR/icon.svg" "$FRONTEND_DIR/public/favicon.svg"
cp "$BRAND_DIR/logo.svg" "$FRONTEND_DIR/src/assets/logo.svg"

if [ ! -d node_modules ]; then
  echo "[build.sh] node_modules missing — running npm install"
  npm install
fi

echo "[build.sh] vite build (multi-file SPA)"
npm run build

if [ ! -f "$INDEX" ]; then
  echo "[build.sh] ERROR: expected built index.html not found at $INDEX" >&2
  exit 1
fi

# pdf.js decodes JBIG2 / JPEG2000 / CCITT-fax images (the encodings scanned PDFs
# use) in WebAssembly. The reader passes `wasmUrl` pointing at /wasm/ (see
# PdfView.tsx), so those .wasm modules and their JS fallbacks must sit at the
# served root. vite doesn't touch node_modules assets, so copy pdf.js's wasm/
# folder into the build product; the StaticFiles mount serves it (correct
# application/wasm MIME). Without this, scanned image-only PDFs render blank.
# Runs AFTER the build — vite's emptyOutDir wipes ASSET_DIR each time.
WASM_SRC="$FRONTEND_DIR/node_modules/pdfjs-dist/wasm"
if [ ! -d "$WASM_SRC" ]; then
  echo "[build.sh] ERROR: pdfjs-dist/wasm not found at $WASM_SRC" >&2
  exit 1
fi
rm -rf "$ASSET_DIR/wasm"
cp -r "$WASM_SRC" "$ASSET_DIR/wasm"
echo "[build.sh] vendored pdf.js wasm/ ($(ls "$ASSET_DIR/wasm" | wc -l | tr -d ' ') files)"

SIZE="$(du -sh "$ASSET_DIR" | cut -f1)"
echo "[build.sh] OK: $ASSET_DIR ($SIZE total)"
