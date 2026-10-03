#!/usr/bin/env bash
# Drop-in replacement for the Codex binary the VS Code extension launches.
#
#   VS Code setting:  "chatgpt.cliExecutable": "~/project/cross-agent_mcp/codex-shim.sh"
#
# Every invocation is forwarded to the real Codex binary. Only the plain `app-server` stdio
# session is intercepted, so the bridge can hand messages to the thread open in the panel.
# If anything here fails, the real binary is exec'd directly and Codex behaves as usual.
set -uo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PYTHON_BIN="${ROOT_DIR}/.venv/bin/python"
LATEST_CODEX="/Users/dexter/.nvm/versions/node/v22.22.1/bin/codex"

# The VS Code extension bundles an older Codex binary that lacks newer models.
if [[ -z "${CROSS_AGENT_REAL_CODEX:-}" && -x "${LATEST_CODEX}" ]]; then
    export CROSS_AGENT_REAL_CODEX="${LATEST_CODEX}"
fi

if [[ ! -x "${PYTHON_BIN}" ]]; then
    echo "cross-agent shim: venv missing at ${PYTHON_BIN}, running Codex directly" >&2
    exec "${CROSS_AGENT_REAL_CODEX:-codex}" "$@"
fi

export PYTHONPATH="${ROOT_DIR}/src${PYTHONPATH:+:${PYTHONPATH}}"
export PYTHONUNBUFFERED=1

exec "${PYTHON_BIN}" -m cross_agent_mcp.appserver_shim "$@"
