#!/usr/bin/env bash
# stdio entry point for the cross-agent MCP server.
# Register this script in Claude Code (.mcp.json) and in Codex (config.toml).
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PYTHON_BIN="${ROOT_DIR}/.venv/bin/python"

if [[ ! -x "${PYTHON_BIN}" ]]; then
    echo "cross-agent-mcp: venv missing, run: python3 -m venv .venv && .venv/bin/pip install -r requirements.txt" >&2
    exit 1
fi

export PYTHONPATH="${ROOT_DIR}/src${PYTHONPATH:+:${PYTHONPATH}}"
export PYTHONUNBUFFERED=1

# Started by an editor (no arguments), the server runs behind a front that can swap it for
# the current source when `run-server.sh reload` is run - no editor restart needed. Set
# CROSS_AGENT_RELOAD=0 in the registration to run the server directly instead.
if [[ $# -eq 0 ]]; then
    case "$(printf '%s' "${CROSS_AGENT_RELOAD:-1}" | tr '[:upper:]' '[:lower:]')" in
        0|false|no|off) ;;
        *) set -- --reload ;;
    esac
fi

exec "${PYTHON_BIN}" -m cross_agent_mcp "$@"
