#!/usr/bin/env bash
#
# install.sh — install litman into an isolated conda env (source-tree path).
#
# This is the source-tree counterpart of `pipx install litman`: both put litman
# into its OWN environment, so it never pollutes base or another project's env.
#
# It does ONE thing — install. It deliberately does NOT deploy the Claude Code
# skills, create a vault, install shell completion, or configure cloud sync.
# Run `lit setup` after install for the interactive onboarding wizard, or call
# the individual commands directly (`lit install-completion`, `lit install-skill`,
# `lit init`, `lit sync setup`) for scripted onboarding.
# The follow-up wizard is identical whether you installed from source (here) or
# from PyPI (pipx), which keeps the README symmetric across the two paths.
#
set -euo pipefail

usage() {
  cat <<'EOF'
install.sh — install litman into an isolated conda env (source-tree path).

Counterpart of `pipx install litman`. Installs only; run `lit setup` after
install for the interactive onboarding wizard (completion, skill, vault,
optional sync), or call those four commands directly for scripted onboarding.

Usage:
  ./install.sh                 # editable dev install into conda env 'litman'
  ./install.sh --prod          # regular (non-editable) install
  ./install.sh --env NAME      # use a different env name (default: litman)
  ./install.sh --help

Requires conda (used for environment isolation). No conda? Once litman is on
PyPI, `pipx install litman` is the no-conda path.
EOF
}

# --- args ---
MODE="dev"
ENV_NAME="litman"
PY_VERSION="3.12"
while [ $# -gt 0 ]; do
  case "$1" in
    --prod) MODE="prod" ;;
    --env) shift; ENV_NAME="${1:?--env needs a name}" ;;
    -h|--help) usage; exit 0 ;;
    *) echo "error: unknown argument '$1' (see --help)" >&2; exit 2 ;;
  esac
  shift
done

# --- locate the repo (this script sits at the package root, beside pyproject.toml) ---
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

# --- pretty output (only when stdout is a terminal) ---
if [ -t 1 ]; then
  BOLD=$'\033[1m'; GREEN=$'\033[32m'; YELLOW=$'\033[33m'; RED=$'\033[31m'; RESET=$'\033[0m'
else
  BOLD=""; GREEN=""; YELLOW=""; RED=""; RESET=""
fi
info() { echo "${GREEN}==>${RESET} $*"; }
warn() { echo "${YELLOW}warning:${RESET} $*" >&2; }
die()  { echo "${RED}error:${RESET} $*" >&2; exit 1; }

# --- 1. require conda (this installer isolates via conda, never touches base) ---
command -v conda >/dev/null 2>&1 \
  || die "conda not found. This installer uses conda for env isolation. Install Miniconda first, or (once litman ships on PyPI) use 'pipx install litman'."

# --- 2. create the env if it does not exist, else reuse it ---
# `conda run` needs only conda on PATH — no `conda activate` shell hook, which
# is exactly the fragile part we want to avoid inside a script.
if conda run -n "$ENV_NAME" python --version >/dev/null 2>&1; then
  info "Reusing existing conda env: ${BOLD}${ENV_NAME}${RESET}"
else
  info "Creating conda env ${BOLD}${ENV_NAME}${RESET} (python ${PY_VERSION})"
  conda create -n "$ENV_NAME" "python=${PY_VERSION}" -y
fi

# --- 3. install litman into that env (no activate needed) ---
if [ "$MODE" = "dev" ]; then
  info "Installing litman (editable + dev deps) into ${ENV_NAME}"
  conda run -n "$ENV_NAME" python -m pip install -e ".[dev]"
else
  info "Installing litman into ${ENV_NAME}"
  conda run -n "$ENV_NAME" python -m pip install .
fi

# --- 4. self-check ---
info "Installed: ${BOLD}$(conda run -n "$ENV_NAME" lit --version 2>&1 || true)${RESET}"

# --- 5. next steps: deploy + vault, both manual, symmetric with the PyPI path ---
cat <<EOF

${GREEN}${BOLD}litman is installed in conda env '${ENV_NAME}'.${RESET}

Next step (interactive onboarding wizard — same on PyPI / pipx path):

  conda activate ${ENV_NAME}
  lit setup                             # completion + Claude Code skill + first vault + (optional) cloud sync

For scripted onboarding, call the four steps directly instead of \`lit setup\`:
\`lit install-completion\`, \`lit install-skill\`, \`lit init /path/to/parent\`,
\`lit sync setup\`. \`lit init\` auto-registers the new vault as active, so no
\`\$LIT_LIBRARY\` export is needed.

Headless/HPC? set 'default_pdf_viewer' in <vault>/lit-config.yaml.
EOF
