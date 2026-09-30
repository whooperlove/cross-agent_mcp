"""Detect which agent launched this MCP server.

A stdio MCP server is spawned as a child process of its client, so walking the parent
chain tells us whether we are running under Claude Code, Codex or Grok Build. That is what
lets the bridge refuse a self-directed send (`send_to_claude` called from Claude).
"""

import logging
import os
import subprocess
from typing import Any, Dict, List, Optional

from . import config


logger = logging.getLogger('cross_agent_mcp.caller')

MAX_ANCESTRY_DEPTH = 12


def _read_process(pid: int) -> Optional[Dict[str, Any]]:
    """Return {'ppid': int, 'name': str} for a pid, or None when it cannot be read."""
    try:
        completed = subprocess.run(
            ['ps', '-o', 'ppid=,comm=', '-p', str(pid)],
            capture_output=True, text=True, timeout=5,
        )
    except Exception as e:
        logger.debug(f'_read_process [exception]: pid={pid} {e}')
        return None

    line = completed.stdout.strip()
    if not line:
        return None

    parts = line.split(None, 1)
    if len(parts) < 2:
        return None

    try:
        ppid = int(parts[0])
    except ValueError:
        return None

    return {'ppid': ppid, 'name': parts[1].strip()}


def classify_process_name(name: str) -> Optional[str]:
    """Map an executable path/name onto an agent id."""
    base = os.path.basename(name).lower()

    # the Codex check runs first: Codex may itself be launched from a Claude session
    if 'codex' in base:
        return config.AGENT_CODEX
    if 'claude' in base:
        return config.AGENT_CLAUDE
    if 'grok' in base:
        return config.AGENT_GROK
    return None


def detect_caller() -> Dict[str, Any]:
    """Walk the parent chain and report the nearest agent process."""
    chain: List[Dict[str, Any]] = []
    agent: Optional[str] = None
    agent_pid: Optional[int] = None

    pid = os.getppid()
    for _ in range(MAX_ANCESTRY_DEPTH):
        if pid <= 1:
            break

        info = _read_process(pid)
        if not info:
            break

        matched = classify_process_name(info['name'])
        chain.append({'pid': pid, 'name': info['name'], 'agent': matched})

        if matched and not agent:
            agent = matched
            agent_pid = pid

        pid = info['ppid']

    # an explicit override wins: useful when the bridge runs behind a wrapper process
    override = os.environ.get('CROSS_AGENT_SELF')
    if override:
        agent = override

    return {
        'agent': agent or 'unknown',
        'agent_pid': agent_pid,
        'self_pid': os.getpid(),
        'parent_pid': os.getppid(),
        'chain': chain,
    }
