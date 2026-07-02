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

SIZE="$(du -sh "$ASSET_DIR" | cut -f1)"
echo "[build.sh] OK: $ASSET_DIR ($SIZE total)"
