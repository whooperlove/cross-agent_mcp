"""Reach the peer session that is open in an editor panel.

Each panel shim registers a socket together with the pid chain it was launched from. The
Codex extension and the Claude Code extension are both children of the same VS Code extension
host, so the shim whose ancestry shares the nearest pid with this process belongs to the
window the user is looking at.

That ancestry test decides *which window*, not *what is reachable*. A shim's socket is an
ordinary AF_UNIX path, so any window's panel can be delivered to once its socket is known.
The distinction matters in exactly one place: picking a target on the caller's behalf must
stay inside this window - landing in another window's conversation would be a surprise - but
a session the caller named explicitly should be found wherever it actually lives, instead of
falling through to a headless CLI resume that the owning panel then rejects as a second
writer on the same transcript.
"""

import glob
import json
import logging
import os
import socket
from typing import Any, Dict, List, Optional

from . import config
from .panel import REGISTRY_DIR, process_ancestry


logger = logging.getLogger('cross_agent_mcp.uihook')

CONNECT_TIMEOUT_SECONDS = 3

UI_HOOK_AUTO = 'auto'
UI_HOOK_OFF = 'off'
UI_HOOK_REQUIRE = 'require'


def _is_pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except OSError as e:
        return getattr(e, 'errno', None) == 1  # EPERM means it exists but is not ours
    return True


def list_shims(agent: Optional[str] = None) -> List[Dict[str, Any]]:
    """Every registered panel shim, with dead registrations cleaned up."""
    shims: List[Dict[str, Any]] = []
    for path in glob.glob(REGISTRY_DIR + '*.json'):
        try:
            with open(path, 'r', encoding='utf-8') as f:
                record = json.load(f)
        except Exception:
            continue

        pid = int(record.get('pid', -1))
        if not _is_pid_alive(pid) or not os.path.exists(record.get('socket', '')):
            try:
                os.remove(path)
            except OSError:
                pass
            continue

        if agent and record.get('agent') != agent:
            continue

        record['registry_path'] = path
        shims.append(record)
    return shims


def find_local_shims(agent: str) -> List[Dict[str, Any]]:
    """Every shim for this agent belonging to this editor window.

    An extension keeps one process per open conversation, so a single window normally has
    several. They all share the same extension host, so the nearest shared ancestor selects
    the window and the list keeps every tab inside it.
    """
    own_chain = process_ancestry(os.getpid())
    scored: List[tuple] = []

    for shim in list_shims(agent):
        theirs = {int(p) for p in shim.get('ancestors') or []}
        for distance, pid in enumerate(own_chain):
            if pid > 1 and pid in theirs:
                scored.append((distance, shim))
                break

    if not scored:
        return []

    nearest = min(distance for distance, _ in scored)
    local = []
    for distance, shim in scored:
        if distance == nearest:
            shim['shared_ancestor_distance'] = distance
            local.append(shim)
    return local


def find_local_shim(agent: str) -> Optional[Dict[str, Any]]:
    """The single shim for this window, when there is only one worth talking about."""
    session = find_live_session(agent)
    if session:
        return session['shim']
    shims = find_local_shims(agent)
    return shims[0] if shims else None


class PanelUnreachable(Exception):
    """The shim's socket could not be connected to, so nothing was sent through it.

    Kept apart from every later failure because it is the one case where the message is
    known not to have left: a read that times out after the write may still have landed.
    """


def _request(socket_path: str, payload: Dict[str, Any], timeout: float) -> Dict[str, Any]:
    connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    connection.settimeout(CONNECT_TIMEOUT_SECONDS)
    try:
        try:
            connection.connect(socket_path)
        except (OSError, socket.timeout) as e:
            raise PanelUnreachable(f'{type(e).__name__}: {e} ({socket_path})') from e
        connection.sendall((json.dumps(payload, ensure_ascii=False) + '\n').encode('utf-8'))

        connection.settimeout(timeout)
        buffer = b''
        while b'\n' not in buffer:
            chunk = connection.recv(65536)
            if not chunk:
                break
            buffer += chunk
        if not buffer:
            return {'ok': False, 'error': 'shim closed the connection without answering'}
        return json.loads(buffer.split(b'\n', 1)[0].decode('utf-8'))
    finally:
        try:
            connection.close()
        except OSError:
            pass


def read_status(shim: Dict[str, Any]) -> Dict[str, Any]:
    try:
        return _request(shim['socket'], {'op': 'status'}, CONNECT_TIMEOUT_SECONDS)
    except Exception as e:
        return {'ok': False, 'error': f'{type(e).__name__}: {e}'}


def find_live_sessions(agent: str) -> List[Dict[str, Any]]:
    """Every panel session of this window, the one the user is working in first.

    A window keeps one process per conversation tab and nothing anywhere records which tab is
    focused, so the ordering is built from the strongest evidence available:

      1. when the human last typed into that tab, observed by the shim
      2. failing that (no tab has been typed into since the shims started, e.g. right after a
         window reload) the transcript's last write, which survives across restarts
      3. failing that, the most recently launched process - the most recently opened tab

    Observed input strictly outranks transcript time, because a bridged turn also touches the
    transcript and must never make the bridge keep picking its own last target.
    """
    return sessions_of(agent, find_local_shims(agent))


def foreign_shims(agent: str) -> List[Dict[str, Any]]:
    """Registered shims for this agent that belong to some other editor window."""
    local_pids = {s.get('pid') for s in find_local_shims(agent)}
    return [s for s in list_shims(agent) if s.get('pid') not in local_pids]


def find_foreign_sessions(agent: str) -> List[Dict[str, Any]]:
    """Panel sessions open in other editor windows.

    Reachable, but never auto-selected: the caller has to name one.
    """
    sessions = sessions_of(agent, foreign_shims(agent))
    for session in sessions:
        session['is_foreign_window'] = True
    return sessions


def sessions_of(agent: str, shims: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Read every conversation the given shims are hosting, best target first."""
    sessions: List[Dict[str, Any]] = []

    for shim in shims:
        status = read_status(shim)
        if not status.get('ok'):
            continue
        shim_activity = float(status.get('last_user_activity') or 0)

        for entry in (status.get('sessions') or status.get('threads') or []):
            session = dict(entry)
            session['session_id'] = session.get('session_id') or session.get('thread_id')
            if not session['session_id']:
                continue
            session['shim'] = shim
            session['shim_pid'] = shim.get('pid')
            session['last_user_activity'] = float(
                session.get('last_user_activity') or shim_activity)
            session['started_at'] = float(shim.get('started_at') or 0)
            session['transcript_mtime'] = _transcript_mtime(agent, session['session_id'])
            sessions.append(session)

    has_observed_input = any(s['last_user_activity'] for s in sessions)
    if has_observed_input:
        sessions.sort(key=lambda s: (s['last_user_activity'], s['started_at']), reverse=True)
    else:
        sessions.sort(key=lambda s: (s['transcript_mtime'], s['started_at']), reverse=True)
    return sessions


def _transcript_mtime(agent: str, session_id: str) -> float:
    from . import discovery
    try:
        found = discovery.find_session(agent, session_id)
    except Exception:
        return 0.0
    return float((found or {}).get('mtime') or 0)


def find_live_session(agent: str, session_id: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """The panel session to deliver to: a specific one when asked, else the active one.

    Without an id the answer is always this window's conversation. With an id, this window is
    still searched first, and only a session the caller named by hand is chased into another
    window - where the CLI fallback could not have reached it anyway.
    """
    sessions = find_live_sessions(agent)
    if not session_id:
        return sessions[0] if sessions else None

    match = next((s for s in sessions if s['session_id'] == session_id), None)
    if match:
        return match

    foreign = next((s for s in find_foreign_sessions(agent)
                    if s['session_id'] == session_id), None)
    if foreign:
        logger.info(f'find_live_session [other window]: {agent} {session_id} is hosted by '
                    f'shim pid={foreign.get("shim_pid")}')
    return foreign


def find_own_session(agent: str) -> Optional[Dict[str, Any]]:
    """The conversation this very process is running inside.

    Every other lookup here answers "which session should we talk to". This one answers "which
    session are we", and it is not a guess: the shim sits between the extension and the agent,
    so the agent - and this MCP server under it - are its descendants. The shim whose pid is
    in our own ancestry is therefore the one hosting us, not merely a plausible candidate.

    Worth having a separate answer for. Inferring our own identity from the registry lets a
    pin - which records where to *send* - stand in for who we *are*, and a pin left over from
    an old session then sends our return address somewhere that no longer exists.
    """
    own_chain = set(process_ancestry(os.getpid()))
    hosting = [s for s in find_local_shims(agent) if s.get('pid') in own_chain]
    if not hosting:
        return None

    sessions = sessions_of(agent, hosting)
    return sessions[0] if sessions else None


def find_panel_host(agent: str) -> Optional[Dict[str, Any]]:
    """A panel process that can host a brand new conversation, or None.

    This is a last resort. A panel showing only its conversation list still has a live
    process behind it, so a new conversation started there is at least visible - but starting
    one when an existing session could have been resumed would throw away the context the
    caller meant to reach, so callers must exhaust every lookup before asking for this.

    Only a shim that says it can open one is offered. "Has a process" is not the same as "can
    open a conversation": the Claude shim can only write into the stdin it was started with,
    so a panel already driving a session cannot host a second - and choosing it anyway is how
    a request for a fresh conversation ended up in the middle of an unrelated one. A shim too
    old to answer the question is not chosen either; there is a CLI path for that.
    """
    for shim in sorted(find_local_shims(agent),
                       key=lambda s: float(s.get('started_at') or 0), reverse=True):
        status = read_status(shim)
        if not status.get('ok'):
            continue
        if not status.get('can_create_session'):
            logger.info(f'find_panel_host [cannot create]: {agent} shim pid={shim.get("pid")} '
                        f'is driving {len(status.get("sessions") or [])} session(s)')
            continue
        return {'session_id': None, 'cwd': None, 'shim': shim, 'opens_new_session': True}
    return None


def send(text: str, shim: Dict[str, Any], session_id: Optional[str], timeout: int,
         cwd: Optional[str] = None, title: Optional[str] = None,
         accept_timeout: Optional[int] = None,
         create_new: bool = False) -> Dict[str, Any]:
    """Hand a message to the panel.

    With `accept_timeout` the shim answers as soon as the peer has taken the message, with
    `pending: true` and an `injectionId` for `await_turn`. Without it - or on a shim from before
    this existed, which ignores the field - the shim answers when the turn ends, so the socket
    is allowed the whole turn budget either way.
    """
    payload: Dict[str, Any] = {'op': 'send', 'text': text, 'timeout': timeout}
    if session_id:
        payload['sessionId'] = session_id
    if cwd:
        payload['cwd'] = cwd
    if title:
        payload['title'] = title
    if accept_timeout is not None:
        payload['acceptTimeout'] = accept_timeout
    if create_new:
        # said out loud to the shim as well, not only decided here: the shim is the only
        # party that knows whether it can honour it
        payload['createNew'] = True

    logger.info(f'send [BEGIN]: via {shim.get("agent")} panel shim '
                f'pid={shim.get("pid")} session={session_id}')
    try:
        return _request(shim['socket'], payload, timeout + CONNECT_TIMEOUT_SECONDS)
    except PanelUnreachable as e:
        logger.error(f'send [unreachable]: {e}')
        return {'ok': False, 'accepted': False, 'error': f'panel shim unreachable: {e}'}
    except Exception as e:
        logger.error(f'send [exception]: {e}')
        return {'ok': False, 'error': f'{type(e).__name__}: {e}'}
    finally:
        logger.info('send [END]')


def await_turn(shim: Dict[str, Any], injection_id: str, timeout: int) -> Dict[str, Any]:
    """Wait for a turn that `send` left running in the panel, up to `timeout` seconds.

    The answer has the same shape as `send`'s: `pending: true` while the turn goes on, `ok`
    with the reply once it ends. Asking again is always allowed; the shim keeps the turn until
    it has been collected.
    """
    payload = {'op': 'await', 'injectionId': injection_id, 'timeout': timeout}
    try:
        return _request(shim['socket'], payload, timeout + CONNECT_TIMEOUT_SECONDS)
    except Exception as e:
        logger.error(f'await_turn [exception]: {injection_id} {e}')
        return {'ok': False, 'error': f'{type(e).__name__}: {e}', 'is_transport_error': True}


def is_enabled() -> bool:
    return config.UI_HOOK_MODE != UI_HOOK_OFF
