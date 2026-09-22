"""MCP server exposing the Claude Code ↔ Codex bridge.

Register the same server on both sides: Claude then reaches the live Codex thread with
`send_to_codex`, and Codex reaches the live Claude session with `send_to_claude`.
"""

import asyncio
import functools
import logging
import logging.handlers
import os
import shutil
import sys
from typing import Any, Dict, List, Optional

from mcp.server import MCPServer
from mcp.server.mcpserver import Context

from . import bridge, caller, config, discovery, outbox, panel, registry, uihook


logger = logging.getLogger('cross_agent_mcp')

SERVER_INSTRUCTIONS = (
    'Bridge between the two coding agents running in this editor. '
    '`send_to_codex` resumes the live Codex thread; `send_to_claude` resumes the live '
    'Claude Code session. Both keep the peer\'s existing conversation context. '
    'When no active peer session exists, a fresh one is created automatically. '
    'Sending is asynchronous: the tool returns as soon as the message is queued and never '
    'carries the peer\'s answer. If this session is open in an editor panel when the peer '
    'answers, the answer arrives later as a separate message here; if it is not, no message '
    'arrives and the answer is read with `bridge_status(delivery_id=...)` instead - the '
    'receipt says which is likely (return_panel_available_now). Either way: send it, say you '
    'sent it, and carry on - never wait for it or guess what it will say. `bridge_status` '
    'shows deliveries still in flight. '
    'Calls are capped by a hop budget so the two agents cannot ping-pong forever.'
)


def init_logging() -> None:
    """Send logs to a file and to stderr only: stdout carries the MCP protocol."""
    config.ensure_dirs()
    package_logger = logging.getLogger('cross_agent_mcp')
    if package_logger.handlers:
        return

    package_logger.setLevel(logging.DEBUG if os.environ.get('CROSS_AGENT_DEBUG') else logging.INFO)

    # the MCP SDK installs its own root handler; without this every record is emitted twice
    package_logger.propagate = False

    formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')

    file_handler = panel.SecureRotatingFileHandler(
        config.LOG_PATH, maxBytes=2_000_000, backupCount=3, encoding='utf-8')
    file_handler.setFormatter(formatter)
    package_logger.addHandler(file_handler)

    stream_handler = logging.StreamHandler(sys.stderr)
    stream_handler.setFormatter(formatter)
    package_logger.addHandler(stream_handler)


async def _run_blocking(func, *args, **kwargs) -> Any:
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, functools.partial(func, *args, **kwargs))


def _request_meta(ctx: Optional[Context]) -> Optional[Dict[str, Any]]:
    """Whatever the calling client attached to this request, as a plain dict."""
    try:
        if ctx is None:
            return None
        meta = ctx.request_context.meta
        if meta is None:
            return None
        if hasattr(meta, 'model_dump'):
            return meta.model_dump(by_alias=True)
        return dict(meta)
    except Exception:
        return None


def caller_session_from_meta(meta: Optional[Dict[str, Any]]) -> Optional[str]:
    """The session the caller says it is in.

    Codex attaches `x-codex-turn-metadata` to every MCP tool call: the thread id, the turn
    id, when the turn began. That is the calling thread, exactly - not the busiest thread of
    the window, which is all the process tree can tell when several threads share one
    app server. Claude Code attaches nothing of the kind, and does not need to: it runs one
    process per conversation, so the process tree already names the session.
    """
    if not isinstance(meta, dict):
        return None
    turn_meta = meta.get('x-codex-turn-metadata')
    if isinstance(turn_meta, dict):
        thread_id = turn_meta.get('thread_id') or turn_meta.get('session_id')
        if isinstance(thread_id, str) and thread_id:
            return thread_id
    return None


async def _send(target_agent: str, ctx: Optional[Context] = None, **kwargs) -> Dict[str, Any]:
    try:
        caller_session_id = caller_session_from_meta(_request_meta(ctx))
        return await _run_blocking(bridge.send_message, target_agent,
                                   caller_session_id=caller_session_id, **kwargs)
    except bridge.BridgeError as e:
        logger.error(f'_send [exception]: {target_agent} {e}')
        return {'ok': False, 'target_agent': target_agent, 'error': str(e)}
    except Exception as e:
        logger.error(f'_send [exception]: {target_agent} {e}')
        return {'ok': False, 'target_agent': target_agent, 'error': f'{type(e).__name__}: {e}'}


server: MCPServer = MCPServer(
    name='cross-agent',
    title='Cross-Agent Bridge',
    version='0.1.0',
    instructions=SERVER_INSTRUCTIONS,
)


@server.tool(
    name='send_to_codex',
    title='Send a message to the live Codex thread',
    description=(
        'Send a message to the Codex session the user is currently working in. The Codex '
        'thread keeps its full conversation context. If no active Codex thread exists for '
        'this working directory, a new one is created and reused for later calls. Use this '
        'to ask Codex for a review, a second opinion, a verification pass, or to hand it a '
        'long task. '
        'ASYNCHRONOUS: this returns as soon as the message is queued and NEVER contains '
        'Codex\'s answer. Codex answers on its own schedule - minutes is normal. If this '
        'session is open in an editor panel when it does, the answer is delivered here as a '
        'separate message; if it is not, the answer is not delivered at all and you read it '
        'with bridge_status(delivery_id=...) - the receipt says which is likely (return_panel_available_now). '
        'So: send, tell the user it was sent, and continue. Do not wait for the answer, do '
        'not poll for it, and never write what you think Codex will say.'
    ),
)
async def send_to_codex(
    message: str,
    session_id: Optional[str] = None,
    new_session: bool = False,
    scope: Optional[str] = None,
    cwd: Optional[str] = None,
    timeout: Optional[int] = None,
    conversation_id: Optional[str] = None,
    raw: bool = False,
    ctx: Optional[Context] = None,
) -> Dict[str, Any]:
    """Args:
    message: What to ask Codex. Be self-contained; Codex cannot see this conversation.
    session_id: Target a specific Codex thread - its id, or the conversation name shown in
        the panel. Fails loudly rather than creating a new thread when nothing matches.
    new_session: Force a brand new Codex thread even when an active one exists.
    scope: 'cwd' (default) = same directory or below, 'tree' = also parent directories,
        'any' = every recorded thread.
    cwd: Working directory used for discovery and for a newly created thread.
    timeout: Budget for the Codex turn itself, applied by the background worker. It does not
        make this call wait, so a small value only aborts work that would have finished -
        anything below the configured default is raised to it and the result says so.
    conversation_id: Continue an existing bridge conversation (shares the hop budget).
    raw: Send the message verbatim, without the bridge envelope.
    """
    return await _send(
        config.AGENT_CODEX, ctx, message=message, session_id=session_id,
        is_new_session=new_session, scope=scope, cwd=cwd, timeout=timeout,
        conversation_id=conversation_id, is_raw=raw,
    )


@server.tool(
    name='send_to_claude',
    title='Send a message to the live Claude Code session',
    description=(
        'Send a message to the Claude Code session the user is currently working in. The '
        'Claude session keeps its full conversation context. If no active Claude session '
        'exists for this working directory, a new one is created and reused for later calls. '
        'Use this to ask Claude to implement, refactor or explain something. '
        'ASYNCHRONOUS: this returns as soon as the message is queued and NEVER contains '
        'Claude\'s answer. Claude answers on its own schedule - minutes is normal. If this '
        'session is open in an editor panel when it does, the answer is delivered here as a '
        'separate message; if it is not, the answer is not delivered at all and you read it '
        'with bridge_status(delivery_id=...) - the receipt says which is likely (return_panel_available_now). '
        'So: send, tell the user it was sent, and continue. Do not wait for the answer, do '
        'not poll for it, and never write what you think Claude will say.'
    ),
)
async def send_to_claude(
    message: str,
    session_id: Optional[str] = None,
    new_session: bool = False,
    scope: Optional[str] = None,
    cwd: Optional[str] = None,
    timeout: Optional[int] = None,
    conversation_id: Optional[str] = None,
    raw: bool = False,
    allow_same_agent: bool = False,
    ctx: Optional[Context] = None,
) -> Dict[str, Any]:
    """Args:
    message: What to ask Claude. Be self-contained; Claude cannot see this conversation.
    session_id: Target a specific Claude session - its id, or the conversation name shown in
        the panel. Fails loudly rather than creating a new session when nothing matches.
    new_session: Force a brand new Claude session even when an active one exists.
    scope: 'cwd' (default) = same directory or below, 'tree' = also parent directories,
        'any' = every recorded session.
    cwd: Working directory used for discovery and for a newly created session.
    timeout: Budget for the Claude turn itself, applied by the background worker. It does not
        make this call wait, so a small value only aborts work that would have finished -
        anything below the configured default is raised to it and the result says so.
    conversation_id: Continue an existing bridge conversation (shares the hop budget).
    raw: Send the message verbatim, without the bridge envelope.
    allow_same_agent: Allow a Claude session to message another Claude session.
    """
    return await _send(
        config.AGENT_CLAUDE, ctx, message=message, session_id=session_id,
        is_new_session=new_session, scope=scope, cwd=cwd, timeout=timeout,
        conversation_id=conversation_id, is_raw=raw, allows_same_agent=allow_same_agent,
    )


@server.tool(
    name='list_agent_sessions',
    title='List discoverable Claude and Codex sessions',
    description=(
        'Show the Claude Code sessions and Codex threads the bridge can reach, newest first, '
        'together with how long ago each was touched and whether it counts as active.'
    ),
)
async def list_agent_sessions(
    agent: str = 'both',
    scope: Optional[str] = None,
    cwd: Optional[str] = None,
    limit: int = 10,
) -> Dict[str, Any]:
    """Args:
    agent: 'claude', 'codex' or 'both'.
    scope: 'cwd' (default), 'tree' or 'any'.
    cwd: Working directory to scope the lookup to.
    limit: Maximum number of sessions per agent.
    """
    scope = scope or config.DEFAULT_SCOPE
    target_cwd = os.path.realpath(os.path.expanduser(cwd)) if cwd else os.getcwd()
    agents: List[str] = ([config.AGENT_CLAUDE, config.AGENT_CODEX]
                         if agent == 'both' else [agent])

    result: Dict[str, Any] = {
        'ok': True,
        'scope': scope,
        'cwd': target_cwd,
        'active_window_minutes': config.ACTIVE_WINDOW_MINUTES,
    }
    for name in agents:
        try:
            result[name] = await _run_blocking(
                discovery.list_sessions, name, scope, target_cwd, limit)
        except Exception as e:
            result['ok'] = False
            result[name] = {'error': str(e)}
    return result


@server.tool(
    name='bridge_status',
    title='Inspect bridge state',
    description=(
        'Report which agent this MCP server is running under, the resolved peer sessions, '
        'the active hop budget, the messages this server still has in flight, and any '
        'session currently held by a delivery. Use it to diagnose why a relay was refused, '
        'or to see whether a message you sent has been delivered yet. '
        'With delivery_id it reports that one delivery instead, together with a fresh read '
        'of the peer\'s transcript: the answer of the last turn the peer finished after the '
        'request, or that it is still working. Use it when a reply was recovered as a '
        'fragment, or a DELIVERY FAILED notice said the peer may still be working. '
        'Any server answers for any delivery, even one whose server has since exited: '
        'deliveries are recorded from the moment they are queued, and one whose server is '
        'gone is reported with is_orphaned=true. Nothing is ever resent.'
    ),
)
async def bridge_status(cwd: Optional[str] = None, scope: Optional[str] = None,
                        delivery_id: Optional[str] = None,
                        ctx: Optional[Context] = None) -> Dict[str, Any]:
    """Args:
    cwd: Working directory to resolve sessions against.
    scope: 'cwd' (default), 'tree' or 'any'.
    delivery_id: Report one delivery (from send_to_* or a bridge notice) and what its target
        has written since, instead of the overall bridge state.
    """
    if delivery_id:
        return await _run_blocking(bridge.delivery_report, delivery_id)

    # what the calling client attached to this request. Codex names its thread here, which is
    # how a reply finds its way back to the thread that asked rather than the busiest one.
    caller_meta = _request_meta(ctx)
    caller_session_id = caller_session_from_meta(caller_meta)

    scope = scope or config.DEFAULT_SCOPE
    target_cwd = os.path.realpath(os.path.expanduser(cwd)) if cwd else os.getcwd()

    identity = await _run_blocking(caller.detect_caller)
    resolved: Dict[str, Any] = {}
    for name in (config.AGENT_CLAUDE, config.AGENT_CODEX):
        try:
            resolved[name] = await _run_blocking(
                discovery.find_active_session, name, scope, target_cwd, None)
        except Exception as e:
            resolved[name] = {'error': str(e)}

    def _describe_panel_session(session: Dict[str, Any]) -> Dict[str, Any]:
        return {
            'session_id': session['session_id'],
            'cwd': session.get('cwd'),
            'shim_pid': session.get('shim_pid'),
            'last_user_activity': session.get('last_user_activity'),
            'started_at': session.get('started_at'),
            'is_turn_active': session.get('is_turn_active'),
            # a turn paused on a prompt only the human can answer; not working, waiting
            'awaiting_approval': session.get('awaiting_approval'),
        }

    panels: Dict[str, Any] = {}
    for name in (config.AGENT_CLAUDE, config.AGENT_CODEX):
        sessions = (await _run_blocking(uihook.find_live_sessions, name)
                    if uihook.is_enabled() else [])
        foreign = (await _run_blocking(uihook.find_foreign_sessions, name)
                   if uihook.is_enabled() else [])
        panels[name] = {
            'selected_session_id': sessions[0]['session_id'] if sessions else None,
            'open_sessions': [_describe_panel_session(s) for s in sessions],
            'other_window_sessions': [_describe_panel_session(s) for s in foreign],
        }

    return {
        'ok': True,
        'running_under': identity['agent'],
        'process_chain': identity['chain'],
        # the thread this call came from, when the host says so (Codex does); replies to a
        # request made from here are addressed to it
        'caller_session_id': caller_session_id,
        'caller_meta': caller_meta,
        'cwd': target_cwd,
        'scope': scope,
        'resolved_sessions': resolved,
        'ide_panels': {
            'mode': config.UI_HOOK_MODE,
            'note': ('open_sessions lists every conversation tab of this editor window, ordered '
                     'by when the human last typed into it; selected_session_id is where a relay '
                     'with no session_id would land. Use pin_agent_session to force a different '
                     'one. other_window_sessions are panels open in OTHER editor windows: they '
                     'are never chosen automatically, but naming one in session_id delivers to '
                     'it through its panel. A session in neither list falls back to a headless '
                     'CLI resume the panel will not show.'),
            **panels,
        },
        'settings': {
            'active_window_minutes': config.ACTIVE_WINDOW_MINUTES,
            'max_hops': config.MAX_HOPS,
            'timeout_seconds': config.SEND_TIMEOUT_SECONDS,
            'panel_patience_seconds': config.PANEL_PATIENCE_SECONDS,
            'codex_sandbox_for_new_sessions': config.CODEX_SANDBOX,
            'claude_permission_mode': config.CLAUDE_PERMISSION_MODE,
            'claude_bin': config.CLAUDE_BIN,
            'codex_bin': config.CODEX_BIN,
            'home_dir': config.HOME_DIR,
        },
        'inherited_chain': {
            'conversation_id': os.environ.get(config.ENV_CONVERSATION_ID),
            'hop': os.environ.get(config.ENV_HOP),
            'sender': os.environ.get(config.ENV_SENDER),
            'self_session': os.environ.get(config.ENV_SELF_SESSION),
            'busy': os.environ.get(config.ENV_BUSY),
        },
        'deliveries': {
            'note': ('Messages this server is carrying. `pending` is still in flight: '
                     'state=delivering means the hand-over has not been acknowledged yet, '
                     'awaiting-peer means the peer has the message (accepted_at) and its turn '
                     'is running - or, with an error set, that the transport broke and the '
                     'peer transcript is being watched for the answer. kind=reply is a peer '
                     'answer on its way back into a session and counts as delivered the '
                     'moment it lands; kind=failure-notice tells a sender its request produced '
                     'nothing. is_undelivered=true means the peer never received the message. '
                     'Pass delivery_id to see one delivery with the peer\'s current progress. '
                     '`pending` and `recent` are this server process\'s own. `earlier` are '
                     'finished deliveries kept on disk. `in_flight_elsewhere` are deliveries '
                     'other server processes recorded as still in flight: is_orphaned=true when '
                     'that server is gone - nothing will move them on, and nothing resends them.'),
            **await _run_blocking(outbox.OUTBOX.snapshot),
        },
        'busy_locks': await _run_blocking(registry.list_busy_locks),
        'pins': registry.load_registry().get('pins', {}),
    }


@server.tool(
    name='pin_agent_session',
    title='Pin a peer session',
    description=(
        'Force every later relay for this working directory to target one specific session. '
        'Accepts the session id or the conversation name. A pin survives inactivity, unlike '
        'auto-discovery, and stops the bridge from ever starting a fresh conversation instead. '
        'Call with session_id empty to clear.'
    ),
)
async def pin_agent_session(
    agent: str,
    session_id: str = '',
    cwd: Optional[str] = None,
) -> Dict[str, Any]:
    """Args:
    agent: 'claude' or 'codex'.
    session_id: Session/thread id - or the conversation name - to pin. Empty removes the pin.
    cwd: Working directory the pin applies to.
    """
    if agent not in (config.AGENT_CLAUDE, config.AGENT_CODEX):
        return {'ok': False, 'error': f"agent must be 'claude' or 'codex', got: {agent}"}

    target_cwd = os.path.realpath(os.path.expanduser(cwd)) if cwd else os.getcwd()

    if not session_id:
        removed = await _run_blocking(registry.clear_pin, agent, target_cwd)
        return {'ok': True, 'agent': agent, 'cwd': target_cwd, 'was_pin_removed': removed}

    found = await _run_blocking(discovery.find_session, agent, session_id)
    if not found:
        try:
            found = await _run_blocking(discovery.find_session_by_name, agent, session_id)
        except (discovery.AmbiguousSessionName, discovery.UnprovenSessionName) as e:
            # A pin aims every later relay that names nothing, so an ambiguous name is refused
            # here for the reason it is refused when sending - and for longer.
            return {'ok': False, 'agent': agent, 'cwd': target_cwd,
                    'error': f'{e} Nothing was pinned.'}
    if not found:
        return {'ok': False,
                'error': f'no {agent} session matches {session_id!r}, by id or by name'}
    session_id = found['session_id']

    await _run_blocking(registry.set_pin, agent, target_cwd, session_id,
                        found.get('cwd') or target_cwd, True, False)
    return {'ok': True, 'agent': agent, 'cwd': target_cwd, 'pinned': found}


def run_check() -> int:
    """Print what the bridge can currently see, then exit. Not part of the MCP protocol."""
    identity = caller.detect_caller()
    scope = config.DEFAULT_SCOPE
    cwd = os.getcwd()

    print(f'cross-agent MCP {__import__("cross_agent_mcp").__version__}')
    print(f'  cwd            : {cwd}')
    print(f'  running under  : {identity["agent"]}')
    print(f'  claude bin     : {shutil.which(config.CLAUDE_BIN) or "NOT FOUND: " + config.CLAUDE_BIN}')
    print(f'  codex bin      : {shutil.which(config.CODEX_BIN) or "NOT FOUND: " + config.CODEX_BIN}')
    print(f'  scope          : {scope} (active window {config.ACTIVE_WINDOW_MINUTES} min)')
    print(f'  max hops       : {config.MAX_HOPS}, timeout {config.SEND_TIMEOUT_SECONDS}s')
    print(f'  new codex sandbox      : {config.CODEX_SANDBOX}')
    print(f'  claude permission mode : {config.CLAUDE_PERMISSION_MODE or "(agent default)"}')
    print(f'  state dir      : {config.HOME_DIR}')

    for agent in (config.AGENT_CLAUDE, config.AGENT_CODEX):
        resolved = discovery.find_active_session(agent, scope, cwd)
        if resolved:
            print(f'\n  active {agent}: {resolved["session_id"]}')
            print(f'      via {resolved.get("source")}, {resolved["age_minutes"]} min ago, '
                  f'cwd={resolved.get("cwd")}')
            if resolved.get('title'):
                print(f'      title: {resolved["title"]}')
        else:
            print(f'\n  active {agent}: none in scope -> a new session would be created')

    locks = registry.list_busy_locks()
    print(f'\n  busy locks     : {len(locks)}')
    return 0


def main() -> None:
    init_logging()

    if '--check' in sys.argv[1:]:
        sys.exit(run_check())

    if sys.stdin.isatty():
        print('cross-agent MCP is a stdio server: it expects MCP JSON-RPC on stdin and is meant '
              'to be launched by Claude Code or Codex, not run by hand.\n'
              'Run with --check to inspect the bridge state instead. Waiting on stdin, Ctrl-C to quit.',
              file=sys.stderr)

    bridge.install_shutdown_guard()

    logger.info(f'main [BEGIN]: cross-agent MCP server, cwd={os.getcwd()}')
    try:
        server.run('stdio')
    except KeyboardInterrupt:
        logger.info('main [END]: interrupted')
    finally:
        logging.shutdown()


if __name__ == '__main__':
    main()
