#!/usr/bin/env bash
# Wrapper for the Grok process the VS Code extension launches.
#
#   VS Code setting:  "grok.cliPath": "~/project/cross-agent_mcp/grok-shim.sh"
#
# The extension runs `<cliPath> agent [--reasoning-effort <level>] stdio` for each panel
# session. Every invocation is forwarded; only that ACP session is intercepted so the bridge
# can hand messages to the conversation the user has open. On any failure the real binary is
# exec'd directly and Grok behaves as usual.
set -uo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PYTHON_BIN="${ROOT_DIR}/.venv/bin/python"

if [[ ! -x "${PYTHON_BIN}" ]]; then
    echo "cross-agent shim: venv missing at ${PYTHON_BIN}, running Grok directly" >&2
    exec "${CROSS_AGENT_REAL_GROK:-${GROK_HOME:-$HOME/.grok}/bin/grok}" "$@"
fi

export PYTHONPATH="${ROOT_DIR}/src${PYTHONPATH:+:${PYTHONPATH}}"
export PYTHONUNBUFFERED=1

exec "${PYTHON_BIN}" -m cross_agent_mcp.grok_shim "$@"
