#!/usr/bin/env bash
# Build the single-file knowledge-graph GUI and vendor it into the Python package.
#
# Idempotent + PATH-safe: prepends the litman conda env bin so node/npm/npx
# resolve even when the shell has not `conda activate`d. Run from anywhere.
#
# Output: src/litman/assets/graph/index.html — ONE self-contained file (JS+CSS
# inlined by vite-plugin-singlefile, see vite.config.ts). graph.py reads it,
# injects the graph JSON, and `file://`-opens a temp copy.
#
# RED LINE (M35 §2.8 / invariant #14): after editing anything under frontend/,
# re-run this and commit the regenerated assets/graph/ product alongside the
# source. A drift between source and product means the user runs a stale GUI.
set -euo pipefail

ENV_BIN="/work/wangq/software/miniconda3/envs/litman/bin"
if [ -d "$ENV_BIN" ]; then
  export PATH="$ENV_BIN:$PATH"
fi

FRONTEND_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ASSET="$FRONTEND_DIR/../src/litman/assets/graph/index.html"

cd "$FRONTEND_DIR"

if [ ! -d node_modules ]; then
  echo "[build.sh] node_modules missing — running npm install"
  npm install
fi

echo "[build.sh] vite build (single-file)"
npm run build

if [ ! -f "$ASSET" ]; then
  echo "[build.sh] ERROR: expected single-file asset not found at $ASSET" >&2
  exit 1
fi

# Verify it is truly self-contained: the singlefile build must leave no sibling
# .js/.css the HTML references. (assets/ subdir is the giveaway of a split build.)
ASSET_DIR="$(dirname "$ASSET")"
if [ -d "$ASSET_DIR/assets" ]; then
  echo "[build.sh] ERROR: split build detected ($ASSET_DIR/assets/ exists). " \
       "vite-plugin-singlefile did not inline everything." >&2
  exit 1
fi

SIZE="$(wc -c < "$ASSET")"
echo "[build.sh] OK: $ASSET ($SIZE bytes, single file)"
