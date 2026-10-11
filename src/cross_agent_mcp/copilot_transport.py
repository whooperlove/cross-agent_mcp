"""Attach to VS Code's published local Agent Host endpoint, without starting a host.

The authentication token is read only while connecting. Public descriptors contain a
metadata locator, never the token. A turn is accepted only when the host echoes its own
client sequence and turn id; a lost acknowledgement must never cause a second dispatch.
"""

import atexit
import base64
import contextlib
import datetime
import glob
import hashlib
import json
import logging
import os
import socket
import struct
import threading
import time
import urllib.parse
import uuid
from typing import Any, Dict, List, Optional

from . import config, panel


logger: logging.Logger = logging.getLogger('cross_agent_mcp.copilot_transport')


MAX_HEADER_BYTES = 16384
MAX_MESSAGE_BYTES = 16 * 1024 * 1024
MAX_SESSIONS = 500
TURN_RETENTION_SECONDS = 3600
ROOT_CHANNEL = 'ahp-root://'
SUPPORTED_PROTOCOL_VERSIONS = ('0.10.0',)
# VS Code publishes these editor-dependent tools through session/activeClientSet.
EDITOR_TOOL_NAMES = frozenset(('usages', 'problems', 'createJupyterNotebook', 'editNotebook',
                               'runNotebookCell', 'getNotebookSummary'))

_CLIENTS: Dict[str, Any] = {}
_TURNS: Dict[str, Any] = {}
_GUARD = threading.RLock()


def _session_id(value: Any) -> Optional[str]:
    if not isinstance(value, str):
        return None
    try:
        from . import copilot
        return copilot.normalize_session_id(value)
    except ImportError:
        pass
    for prefix in ('copilotcli:/', 'agent-host-copilotcli:/'):
        if value.startswith(prefix):
            value = value[len(prefix):]
            break
    try:
        canonical = str(uuid.UUID(value))
        return canonical if value.lower() == canonical else None
    except (ValueError, AttributeError):
        return None


def _epoch(value: Any) -> float:
    try:
        return datetime.datetime.fromisoformat(str(value).replace('Z', '+00:00')).timestamp()
    except (ValueError, TypeError):
        return 0.0


def _is_alive(pid: Any) -> bool:
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 1:
        return False
    try:
        os.kill(pid, 0)
        return True
    except PermissionError:
        return True
    except OSError:
        return False


def _entries(path: str) -> List[Dict[str, Any]]:
    try:
        with open(path, 'r', encoding='utf-8') as handle:
            value = json.load(handle)
        return [entry for entry in (value if isinstance(value, list) else [value])
                if isinstance(entry, dict)]
    except (OSError, ValueError):
        return []


def _valid_entry(entry: Dict[str, Any]) -> bool:
    endpoint = entry.get('endpoint')
    return (entry.get('schemaVersion') == 2 and entry.get('type') == 'editor'
            and _is_alive(entry.get('pid'))
            and isinstance(entry.get('instanceId'), str) and bool(entry['instanceId'])
            and isinstance(entry.get('connectionToken'), str) and bool(entry['connectionToken'])
            and isinstance(endpoint, dict) and endpoint.get('type') == 'socket'
            and isinstance(endpoint.get('path'), str) and os.path.isabs(endpoint['path'])
            and os.path.exists(endpoint['path']))


def _identity(entry: Dict[str, Any]) -> str:
    value = f'{entry["type"]}\0{entry["pid"]}\0{entry["instanceId"]}'
    return hashlib.sha256(value.encode('utf-8')).hexdigest()


def list_shims() -> List[Dict[str, Any]]:
    """Read live editor endpoints. Never clean, rewrite, or expose secret metadata."""
    found: Dict[str, Dict[str, Any]] = {}
    for directory in config.COPILOT_ENDPOINT_DIRS:
        directory = os.path.realpath(os.path.expanduser(directory)).rstrip('/')
        entry_dir = directory if os.path.basename(directory) == 'entries' else directory + '/entries'
        legacy = os.path.dirname(entry_dir) + '/metadata.json'
        paths = [legacy] + sorted(glob.glob(entry_dir + '/*.json'))
        for path in paths:
            for entry in _entries(path):
                if not _valid_entry(entry):
                    continue
                identity = _identity(entry)
                if path != legacy and os.path.basename(path) != f'{identity}.json':
                    continue
                try:
                    started_at = os.path.getmtime(path)
                except OSError:
                    continue
                found[identity] = {
                    'agent': config.AGENT_COPILOT,
                    'pid': entry['pid'],
                    'ancestors': panel.process_ancestry(entry['pid']),
                    'socket': entry['endpoint']['path'],
                    'registry_path': path,
                    'endpoint_identity': identity,
                    'started_at': started_at,
                    'transport': 'copilot-ahp',
                }
    return sorted(found.values(), key=lambda item: item['pid'])


def _load_entry(shim: Dict[str, Any]) -> Dict[str, Any]:
    for entry in _entries(str(shim.get('registry_path') or '')):
        if (_valid_entry(entry) and _identity(entry) == shim.get('endpoint_identity')
                and entry['endpoint']['path'] == shim.get('socket')
                and entry['pid'] == shim.get('pid')):
            return entry
    raise OSError('the published Copilot endpoint is no longer live')


def is_own_caller(shim: Dict[str, Any], session_id: str) -> bool:
    """Prove the caller's SDK session and endpoint owner without guessing a sender."""
    try:
        from . import caller, copilot
        pid = shim.get('pid')
        normalized = _session_id(session_id)
        sdk_session_id = os.environ.get(config.ENV_COPILOT_SESSION)
        if (shim.get('transport') != 'copilot-ahp'
                or not isinstance(pid, int) or isinstance(pid, bool) or pid <= 1
                or not normalized or not isinstance(sdk_session_id, str)
                or not copilot.SESSION_ID_PATTERN.fullmatch(sdk_session_id)
                or caller.detect_caller().get('agent') != config.AGENT_COPILOT):
            return False
        _load_entry(shim)
        session = copilot.find_copilot_session_by_sdk_id(sdk_session_id)
        return bool(session and not session.get('is_archived')
                    and session.get('session_id') == normalized
                    and pid in panel.process_ancestry(os.getpid()))
    except Exception as error:
        logger.debug(f'is_own_caller [exception]: {type(error).__name__}')
        return False


def _editor_presence(state: Dict[str, Any], own_client_id: str) -> Dict[str, Any]:
    evidence = set()
    count = 0
    for active in state.get('activeClients') or []:
        if not isinstance(active, dict):
            continue
        client_id = active.get('clientId')
        if (not isinstance(client_id, str) or client_id == own_client_id
                or client_id.startswith('cross-agent-')):
            continue
        tools = active.get('tools') or []
        names = {tool.get('name') for tool in tools if isinstance(tool, dict)
                 and isinstance(tool.get('name'), str)}
        editor_tools = names & EDITOR_TOOL_NAMES
        if len(editor_tools) < 2:
            continue
        count += 1
        evidence.update(editor_tools)
    return {'is_open_in_panel': count > 0, 'panel_presence_checked': True,
            'editor_client_count': count, 'panel_presence_evidence': sorted(evidence)}


class _WebSocket:
    """Bounded RFC 6455 text client over the published Unix domain socket."""

    def __init__(self, entry: Dict[str, Any], timeout: float) -> None:
        self._socket = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._buffer = bytearray()
        self._write_lock = threading.Lock()
        self._is_closed = False
        self._socket.settimeout(max(0.1, timeout))
        try:
            self._socket.connect(entry['endpoint']['path'])
            key = base64.b64encode(os.urandom(16)).decode('ascii')
            query = urllib.parse.urlencode({'tkn': entry['connectionToken']})
            request_text = (f'GET /?{query} HTTP/1.1\r\nHost: localhost\r\n'
                            f'Upgrade: websocket\r\nConnection: Upgrade\r\n'
                            f'Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n')
            self._socket.sendall(request_text.encode('ascii'))
            while b'\r\n\r\n' not in self._buffer:
                chunk = self._socket.recv(4096)
                if not chunk:
                    raise OSError('Copilot endpoint closed during authentication')
                self._buffer.extend(chunk)
                if len(self._buffer) > MAX_HEADER_BYTES:
                    raise OSError('Copilot endpoint sent oversized HTTP headers')
            raw, remainder = bytes(self._buffer).split(b'\r\n\r\n', 1)
            self._buffer = bytearray(remainder)
            lines = raw.decode('iso-8859-1').split('\r\n')
            headers: Dict[str, str] = {}
            for line in lines[1:]:
                name, separator, value = line.partition(':')
                if separator:
                    name = name.strip().lower()
                    if name in headers:
                        headers[name] += f', {value.strip()}'
                    else:
                        headers[name] = value.strip()
            expected = base64.b64encode(hashlib.sha1(
                f'{key}258EAFA5-E914-47DA-95CA-C5AB0DC85B11'.encode('ascii')).digest()).decode('ascii')
            connection = [value.strip().lower() for value in headers.get('connection', '').split(',')]
            if (len(lines[0].split()) < 2 or lines[0].split()[1] != '101'
                    or headers.get('upgrade', '').lower() != 'websocket'
                    or 'upgrade' not in connection
                    or headers.get('sec-websocket-accept') != expected
                    or headers.get('sec-websocket-extensions')):
                raise OSError('Copilot endpoint rejected the WebSocket handshake')
            self._socket.settimeout(0.5)
        except Exception:
            self.close()
            raise OSError('could not authenticate with the published Copilot endpoint') from None

    def close(self) -> None:
        self._is_closed = True
        with contextlib.suppress(OSError):
            self._socket.shutdown(socket.SHUT_RDWR)
        self._socket.close()

    def _read_exact(self, length: int) -> bytes:
        while len(self._buffer) < length:
            if self._is_closed:
                raise OSError('Copilot endpoint connection closed')
            try:
                chunk = self._socket.recv(min(65536, length - len(self._buffer)))
            except socket.timeout:
                continue
            if not chunk:
                raise OSError('Copilot endpoint connection closed')
            self._buffer.extend(chunk)
        value = bytes(self._buffer[:length])
        del self._buffer[:length]
        return value

    def _send_frame(self, opcode: int, payload: bytes) -> None:
        if len(payload) > MAX_MESSAGE_BYTES:
            raise ValueError('Copilot protocol message exceeds the size limit')
        length = len(payload)
        header = bytes([0x80 | opcode])
        if length < 126:
            header += bytes([0x80 | length])
        elif length < 65536:
            header += bytes([0x80 | 126]) + struct.pack('!H', length)
        else:
            header += bytes([0x80 | 127]) + struct.pack('!Q', length)
        mask = os.urandom(4)
        data = bytes(value ^ mask[index % 4] for index, value in enumerate(payload))
        with self._write_lock:
            self._socket.sendall(header + mask + data)

    def send_json(self, value: Dict[str, Any]) -> None:
        self._send_frame(1, json.dumps(value, ensure_ascii=False, separators=(',', ':')).encode('utf-8'))

    def read_json(self) -> Dict[str, Any]:
        fragments = bytearray()
        is_fragmented = False
        while True:
            first, second = self._read_exact(2)
            opcode = first & 0x0f
            is_final = bool(first & 0x80)
            length = second & 0x7f
            if first & 0x70 or second & 0x80:
                raise OSError('unsupported Copilot WebSocket frame flags')
            if length == 126:
                length = struct.unpack('!H', self._read_exact(2))[0]
                if length < 126:
                    raise OSError('non-canonical Copilot WebSocket frame length')
            elif length == 127:
                length = struct.unpack('!Q', self._read_exact(8))[0]
                if length < 65536:
                    raise OSError('non-canonical Copilot WebSocket frame length')
            if length > MAX_MESSAGE_BYTES or len(fragments) + length > MAX_MESSAGE_BYTES:
                raise OSError('Copilot protocol message exceeds the size limit')
            if opcode >= 8 and (not is_final or length > 125):
                raise OSError('invalid Copilot WebSocket control frame')
            payload = self._read_exact(length)
            if opcode == 8:
                if len(payload) == 1:
                    raise OSError('invalid Copilot WebSocket close frame')
                if payload:
                    code = struct.unpack('!H', payload[:2])[0]
                    if code < 1000 or code >= 5000 or code in (1004, 1005, 1006, 1015):
                        raise OSError('invalid Copilot WebSocket close status')
                    payload[2:].decode('utf-8')
                with contextlib.suppress(OSError):
                    self._send_frame(8, payload)
                raise OSError('Copilot endpoint closed the connection')
            if opcode == 9:
                self._send_frame(10, payload)
                continue
            if opcode == 10:
                continue
            if opcode == 1 and not is_fragmented:
                fragments.extend(payload)
                is_fragmented = not is_final
            elif opcode == 0 and is_fragmented:
                fragments.extend(payload)
                is_fragmented = not is_final
            else:
                raise OSError('unsupported Copilot WebSocket message type')
            if not is_fragmented:
                value = json.loads(fragments.decode('utf-8'))
                if not isinstance(value, dict) or value.get('jsonrpc') != '2.0':
                    raise OSError('invalid Copilot JSON-RPC message')
                return value


class _Turn:
    def __init__(self, session_id: str, channel: str, client_seq: int,
                 endpoint_identity: str) -> None:
        self.injection_id = f'xagent-{uuid.uuid4().hex}'
        self.session_id = session_id
        self.channel = channel
        self.client_seq = client_seq
        self.endpoint_identity = endpoint_identity
        self.is_dispatched = False
        self.is_accepted = False
        self.is_rejected = False
        self.error: Optional[str] = None
        self.parts: Dict[str, Dict[str, Any]] = {}
        self.after_tool_parts: Dict[str, str] = {}
        self.has_tool_boundary = False
        self.tool_ids = set()
        self.finished_at: Optional[float] = None
        self.progress = threading.Event()
        self.done = threading.Event()
        self.guard = threading.RLock()

    def finish(self, error: Optional[str] = None) -> None:
        self.error = error
        self.finished_at = time.time()
        self.done.set()
        self.progress.set()

    def describe(self) -> Dict[str, Any]:
        with self.guard:
            accepted: Optional[bool] = (True if self.is_accepted else False
                                        if self.is_rejected or not self.is_dispatched else None)
            base = {'injectionId': self.injection_id, 'sessionId': self.session_id,
                    'threadId': self.session_id, 'turnId': self.injection_id,
                    'wasCreated': False, 'accepted': accepted}
            reply = '\n'.join(self.after_tool_parts.values()).strip() if self.has_tool_boundary else ''
            if not reply:
                reply = '\n'.join(str(part.get('content') or '') for part in self.parts.values()
                                  if part.get('kind') == 'markdown').strip()
            if self.error:
                return {**base, 'ok': False, 'error': self.error}
            if self.done.is_set():
                return {**base, 'ok': True, 'reply': reply}
            return {**base, 'ok': False, 'pending': True,
                    'error': 'Copilot turn is still running', 'partial': reply}


class _Client:
    def __init__(self, shim: Dict[str, Any], timeout: float) -> None:
        entry = _load_entry(shim)
        self.shim = dict(shim)
        self.client_id = f'cross-agent-{uuid.uuid4().hex}'
        self._socket = _WebSocket(entry, min(timeout, 10))
        self._guard = threading.RLock()
        self._send_guard = threading.Lock()
        self._pending: Dict[str, Dict[str, Any]] = {}
        self._turns: Dict[str, _Turn] = {}
        self._active_channels: Dict[str, str] = {}
        self._activity: Dict[str, float] = {}
        self._channel_sessions: Dict[str, str] = {}
        self._next_sequence = 0
        self.is_closed = False
        threading.Thread(target=self._read, name='copilot-ahp', daemon=True).start()
        try:
            initialized = self.rpc('initialize', {
                'channel': ROOT_CHANNEL,
                'protocolVersions': list(SUPPORTED_PROTOCOL_VERSIONS),
                'clientId': self.client_id,
                'clientInfo': {'name': 'cross-agent-mcp', 'version': '1'},
            }, timeout)
            if initialized.get('protocolVersion') not in SUPPORTED_PROTOCOL_VERSIONS:
                raise OSError('Copilot host negotiated an unsupported protocol version')
        except Exception:
            self.close()
            raise

    def close(self) -> None:
        self.is_closed = True
        self._socket.close()

    def rpc(self, method: str, params: Dict[str, Any], timeout: float) -> Dict[str, Any]:
        request_id = uuid.uuid4().hex
        pending: Dict[str, Any] = {'event': threading.Event(), 'response': None}
        with self._guard:
            if self.is_closed:
                raise OSError('Copilot endpoint connection is no longer live')
            self._pending[request_id] = pending
        try:
            self._socket.send_json({'jsonrpc': '2.0', 'id': request_id,
                                    'method': method, 'params': params})
            if not pending['event'].wait(max(0, timeout)):
                raise TimeoutError('Copilot read-only protocol request timed out')
            response = pending['response']
            if not isinstance(response, dict) or 'error' in response:
                raise OSError('Copilot host refused the protocol request')
            result = response.get('result')
            if not isinstance(result, dict):
                raise OSError('Copilot host returned an invalid protocol result')
            return result
        finally:
            with self._guard:
                self._pending.pop(request_id, None)

    def _read(self) -> None:
        try:
            while not self.is_closed:
                message = self._socket.read_json()
                if 'id' in message and 'method' not in message:
                    with self._guard:
                        pending = self._pending.get(str(message['id']))
                    if pending:
                        pending['response'] = message
                        pending['event'].set()
                elif message.get('method') == 'action':
                    self._action(message.get('params'))
                elif 'id' in message:
                    # This client supplies no resources, tools, or approval decisions.
                    self._socket.send_json({'jsonrpc': '2.0', 'id': message['id'],
                                            'error': {'code': -32601,
                                                      'message': 'Client method not supported'}})
        except Exception:
            pass
        finally:
            self.close()
            with self._guard:
                for pending in self._pending.values():
                    pending['event'].set()
                for turn in self._turns.values():
                    with turn.guard:
                        if not turn.done.is_set():
                            turn.finish('Copilot transport disconnected; delivery may have landed. '
                                        'Do not resend this request.')

    def _action(self, envelope: Any) -> None:
        if not isinstance(envelope, dict) or not isinstance(envelope.get('action'), dict):
            return
        action = envelope['action']
        channel = envelope.get('channel')
        turn_id = action.get('turnId')
        with self._guard:
            turn = self._turns.get(turn_id)
            if action.get('type') == 'chat/turnStarted' and not envelope.get('rejectionReason'):
                self._active_channels[channel] = turn_id
                origin = envelope.get('origin') or {}
                text = (action.get('message') or {}).get('text')
                if origin.get('clientId') != self.client_id and not panel.request_token_in(text):
                    self._activity[self._channel_sessions.get(channel, '')] = _epoch(action.get('startedAt'))
            elif (action.get('type') in ('chat/turnComplete', 'chat/turnCancelled', 'chat/error')
                  and self._active_channels.get(channel) == turn_id):
                self._active_channels.pop(channel, None)
        if turn is None or channel != turn.channel:
            return
        with turn.guard:
            origin = envelope.get('origin') or {}
            if action.get('type') == 'chat/turnStarted':
                if origin.get('clientId') != self.client_id or origin.get('clientSeq') != turn.client_seq:
                    return
                if envelope.get('rejectionReason'):
                    turn.is_rejected = True
                    reason = str(envelope['rejectionReason']).lower()
                    turn.finish('the panel session is busy with another turn' if 'busy' in reason
                                else 'Copilot host refused this turn')
                    return
                turn.is_accepted = True
                turn.progress.set()
            elif not turn.is_accepted:
                return
            if action.get('type') == 'chat/toolCallStart':
                self._tool_boundary(turn, action.get('toolCallId'))
            elif action.get('type') == 'chat/responsePart':
                part = action.get('part')
                if isinstance(part, dict):
                    if part.get('kind') == 'toolCall':
                        self._tool_boundary(turn, (part.get('toolCall') or {}).get('toolCallId'))
                    elif isinstance(part.get('id'), str):
                        turn.parts[part['id']] = dict(part)
                        if part.get('kind') == 'markdown' and turn.has_tool_boundary:
                            turn.after_tool_parts[part['id']] = str(part.get('content') or '')
            elif action.get('type') == 'chat/delta' and isinstance(action.get('content'), str):
                part_id = str(action.get('partId') or '')
                part = turn.parts.setdefault(part_id, {'kind': 'markdown', 'content': ''})
                if part.get('kind') == 'markdown':
                    part['content'] = str(part.get('content') or '') + action['content']
                    if turn.has_tool_boundary:
                        turn.after_tool_parts[part_id] = turn.after_tool_parts.get(part_id, '') + action['content']
                    if sum(len(str(item.get('content') or '')) for item in turn.parts.values()) > MAX_MESSAGE_BYTES:
                        turn.finish('Copilot reply exceeds the collection size limit')
            elif action.get('type') == 'chat/turnComplete':
                turn.finish()
            elif action.get('type') == 'chat/turnCancelled':
                turn.finish('Copilot turn was cancelled')
            elif action.get('type') == 'chat/error':
                turn.finish('Copilot host reported a turn error')

    def _tool_boundary(self, turn: _Turn, tool_id: Any) -> None:
        if isinstance(tool_id, str) and tool_id in turn.tool_ids:
            return
        if isinstance(tool_id, str):
            turn.tool_ids.add(tool_id)
        turn.has_tool_boundary = True
        turn.after_tool_parts.clear()

    def _list_sessions(self, timeout: float) -> List[Dict[str, Any]]:
        sessions: List[Dict[str, Any]] = []
        cursor: Optional[str] = None
        seen = set()
        deadline = time.monotonic() + timeout
        while len(sessions) < MAX_SESSIONS:
            params: Dict[str, Any] = {'channel': ROOT_CHANNEL, 'limit': MAX_SESSIONS - len(sessions)}
            if cursor:
                params['cursor'] = cursor
            result = self.rpc('listSessions', params, max(0.1, deadline - time.monotonic()))
            items = result.get('items')
            if not isinstance(items, list):
                raise OSError('Copilot host returned an invalid session list')
            sessions.extend(item for item in items if isinstance(item, dict))
            cursor = result.get('nextCursor')
            if not cursor:
                return sessions
            if not isinstance(cursor, str) or cursor in seen:
                raise OSError('Copilot session pagination did not make progress')
            seen.add(cursor)
            if time.monotonic() >= deadline:
                raise TimeoutError('Copilot session listing timed out')
        if cursor:
            raise OSError('Copilot session listing exceeded the safe scan limit')
        return sessions

    def status(self, timeout: float) -> Dict[str, Any]:
        sessions = []
        for item in self._list_sessions(timeout):
            if item.get('provider') != 'copilotcli':
                continue
            session_id = _session_id(item.get('resource'))
            if not session_id:
                continue
            directories = item.get('workingDirectories') or []
            directory = urllib.parse.urlsplit(str(directories[0])) if directories else None
            status = int(item.get('status') or 0)
            sessions.append({'session_id': session_id, 'thread_id': session_id,
                             'title': item.get('title') or '',
                             'cwd': urllib.parse.unquote(directory.path) if directory and directory.scheme == 'file' else None,
                             'last_user_activity': self._activity.get(session_id, 0),
                             'last_seen': _epoch(item.get('modifiedAt')),
                             'is_turn_active': bool(status & 8),
                             # A persisted catalog entry is not evidence of an open panel.
                             'is_open_in_panel': False, 'panel_presence_checked': False,
                             'awaiting_approval': {'type': 'input-needed'} if status & 16 else None})
        return {'ok': True, 'agent': config.AGENT_COPILOT, 'pid': self.shim['pid'],
                'sessions': sessions, 'threads': sessions, 'can_create_session': False,
                'last_user_activity': 0}

    def _subscribe(self, channel: str, timeout: float) -> Dict[str, Any]:
        result = self.rpc('subscribe', {'channel': channel}, timeout)
        snapshot = result.get('snapshot')
        if (not isinstance(snapshot, dict) or snapshot.get('resource') != channel
                or not isinstance(snapshot.get('state'), dict)):
            raise OSError('Copilot host returned a mismatched channel snapshot')
        return snapshot['state']

    def presence(self, session_id: Any, timeout: float) -> Dict[str, Any]:
        """Probe the named sender's editor client or its verified calling SDK runtime.

        Subscription alone never publishes an active client. Unknown clients and editor
        clients with all identifying tools disabled do not establish editor presence.
        Without either proof, return traffic degrades to status-only delivery.
        """
        normalized = _session_id(session_id)
        if not normalized:
            return {'ok': False, 'is_open_in_panel': False,
                    'is_caller_runtime_available': False,
                    'error': 'an exact Copilot sessionId is required'}
        matches = [item for item in self._list_sessions(timeout)
                   if item.get('provider') == 'copilotcli' and _session_id(item.get('resource')) == normalized]
        if len(matches) != 1:
            return {'ok': False, 'is_open_in_panel': False,
                    'is_caller_runtime_available': False,
                    'error': 'the exact Copilot session is not hosted here'}
        state = self._subscribe(matches[0]['resource'], timeout)
        return {'ok': True, 'sessionId': normalized,
                'is_caller_runtime_available': is_own_caller(self.shim, normalized),
                **_editor_presence(state, self.client_id)}

    def send(self, payload: Dict[str, Any], timeout: float) -> Dict[str, Any]:
        if payload.get('createNew'):
            return {'ok': False, 'accepted': False, 'error': 'Copilot transport cannot create sessions'}
        session_id = _session_id(payload.get('sessionId'))
        if not session_id:
            return {'ok': False, 'accepted': False, 'error': 'an exact Copilot sessionId is required'}
        text = payload.get('text')
        if not isinstance(text, str) or not text.strip():
            return {'ok': False, 'accepted': False, 'error': 'message must not be empty'}
        if len(text.encode('utf-8')) > MAX_MESSAGE_BYTES // 2:
            return {'ok': False, 'accepted': False, 'error': 'Copilot message exceeds the size limit'}
        accept_timeout = max(0, min(float(payload.get('acceptTimeout') or timeout), timeout))
        with self._send_guard:
            matches = [item for item in self._list_sessions(timeout)
                       if item.get('provider') == 'copilotcli' and _session_id(item.get('resource')) == session_id]
            if len(matches) != 1:
                return {'ok': False, 'accepted': False, 'error': 'the exact Copilot session is not hosted here'}
            session_channel = matches[0]['resource']
            session = self._subscribe(session_channel, timeout)
            if session.get('lifecycle') != 'ready':
                return {'ok': False, 'accepted': False, 'error': 'Copilot session is not ready to accept input'}
            channel = session.get('defaultChat')
            catalog = session.get('chats') or []
            selected = next((item for item in catalog if item.get('resource') == channel), None)
            if not isinstance(channel, str) or selected is None:
                return {'ok': False, 'accepted': False, 'error': 'Copilot session has no verified default chat'}
            if selected.get('interactivity', 'full') != 'full':
                return {'ok': False, 'accepted': False, 'error': 'Copilot chat does not accept user input'}
            chat = self._subscribe(channel, timeout)
            if payload.get('requiresInteractiveClient'):
                # Return traffic may have waited in a queue since its first presence probe.
                # Check again immediately before dispatch; subscribing is never presence.
                current = self._subscribe(session_channel, timeout)
                if (not _editor_presence(current, self.client_id)['is_open_in_panel']
                        and not is_own_caller(self.shim, session_id)):
                    return {'ok': False, 'accepted': False,
                            'error': 'Copilot sender has no active editor client for return traffic'}
            # A queued turn may outlive the catalog check used to select this endpoint.
            # Missing metadata still permits an exact live-only host; ambiguity does not.
            from . import copilot
            try:
                copilot.find_copilot_session(session_id)
            except copilot.AmbiguousCopilotSession as error:
                logger.debug(f'send [ambiguous session]: {error}')
                return {'ok': False, 'accepted': False, 'error': str(error)}
            with self._guard:
                self._channel_sessions[channel] = session_id
                if (chat.get('activeTurn') or int(chat.get('status') or 0) & 8
                        or channel in self._active_channels
                        or any(turn.channel == channel and not turn.done.is_set() for turn in self._turns.values())):
                    return {'ok': False, 'accepted': False,
                            'error': 'the panel session is busy with another turn'}
                self._next_sequence += 1
                turn = _Turn(session_id, channel, self._next_sequence,
                             self.shim['endpoint_identity'])
                self._turns[turn.injection_id] = turn
                with _GUARD:
                    _TURNS[turn.injection_id] = turn
            message: Dict[str, Any] = {'text': text, 'origin': {'kind': 'user'}}
            # Retain the custom model/agent the human selected in this exact chat.
            draft = chat.get('draft') if isinstance(chat.get('draft'), dict) else {}
            previous = ((chat.get('turns') or [{}])[-1].get('message') or {})
            for key in ('model', 'agent'):
                selected_value = draft.get(key) if isinstance(draft.get(key), dict) else previous.get(key)
                if isinstance(selected_value, dict):
                    message[key] = dict(selected_value)
            action = {'type': 'chat/turnStarted', 'turnId': turn.injection_id,
                      'startedAt': datetime.datetime.now(datetime.timezone.utc).isoformat().replace('+00:00', 'Z'),
                      'message': message}
            turn.is_dispatched = True
            try:
                self._socket.send_json({'jsonrpc': '2.0', 'method': 'dispatchAction', 'params': {
                    'channel': channel, 'clientSeq': turn.client_seq, 'action': action}})
            except Exception:
                with turn.guard:
                    turn.finish('Copilot dispatch connection failed; delivery is uncertain. '
                                'Do not resend this request.')
            turn.progress.wait(accept_timeout)
            with turn.guard:
                if not turn.is_accepted and not turn.done.is_set():
                    turn.finish('Copilot hand-over acknowledgement timed out; delivery is uncertain. '
                                'Do not resend this request.')
            return turn.describe()


def _get_client(shim: Dict[str, Any], timeout: float) -> _Client:
    key = str(shim.get('endpoint_identity') or '')
    if not key:
        raise OSError('Copilot endpoint descriptor is missing its identity')
    with _GUARD:
        client = _CLIENTS.get(key)
        if client is None or client.is_closed:
            client = _Client(shim, timeout)
            _CLIENTS[key] = client
        return client


def request(shim: Dict[str, Any], payload: Dict[str, Any], timeout: float) -> Dict[str, Any]:
    """Implement the existing status/send/await contract without a new runtime or CLI."""
    operation = payload.get('op')
    with _GUARD:
        expired = [key for key, turn in _TURNS.items()
                   if turn.finished_at is not None
                   and time.time() - turn.finished_at > TURN_RETENTION_SECONDS]
        for key in expired:
            _TURNS.pop(key, None)
        clients = list(_CLIENTS.values())
        for key, client in list(_CLIENTS.items()):
            if client.is_closed:
                _CLIENTS.pop(key, None)
    for client in clients:
        with client._guard:
            for key in expired:
                client._turns.pop(key, None)
    if operation == 'await':
        with _GUARD:
            turn = _TURNS.get(str(payload.get('injectionId') or ''))
        if turn is None or turn.endpoint_identity != shim.get('endpoint_identity'):
            return {'ok': False, 'accepted': None, 'error': 'no matching Copilot turn is pending'}
        turn.done.wait(max(0, min(float(payload.get('timeout') or timeout), timeout)))
        return turn.describe()
    try:
        client = _get_client(shim, timeout)
        if operation == 'status':
            return client.status(timeout)
        if operation == 'presence':
            return client.presence(payload.get('sessionId'), timeout)
        if operation == 'send':
            return client.send(payload, timeout)
        return {'ok': False, 'accepted': False, 'error': 'unknown Copilot transport operation'}
    except Exception:
        result = {'ok': False, 'accepted': False,
                  'error': 'Copilot endpoint could not complete the pre-dispatch protocol request'}
        if operation == 'presence':
            result.update({'is_open_in_panel': False, 'is_caller_runtime_available': False})
        return result


def close_clients() -> None:
    """Release only connections this module created; never shut down the editor host."""
    with _GUARD:
        clients = list(_CLIENTS.values())
        _CLIENTS.clear()
    for client in clients:
        client.close()


atexit.register(close_clients)
