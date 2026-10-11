"""Synthetic local AHP tests. No editor, paid model, or real session receives a turn.

    PYTHONPATH=src .venv/bin/python tests/copilot_transport.py
"""

import base64
import contextlib
import hashlib
import json
import os
import socket
import sqlite3
import struct
import sys
import tempfile
import threading
import time
import unittest
import uuid
from typing import Any, Dict, Optional, Tuple
from unittest.mock import patch

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT_DIR + '/src')

from cross_agent_mcp import caller, config, copilot, copilot_transport, panel  # noqa: E402


SESSION_ID = '11111111-1111-4111-8111-111111111111'
SDK_SESSION_ID = '33333333-3333-4333-8333-333333333333'
SESSION_CHANNEL = f'copilotcli:/{SESSION_ID}'
CHAT_CHANNEL = 'ahp-chat://default/synthetic-existing-chat'
MODEL = {'id': 'synthetic-custom-model', 'config': {'thinkingLevel': 'xhigh'}}


class SyntheticHost:
    def __init__(self, directory: str, mode: str = 'complete') -> None:
        self.mode = mode
        self.socket_path = directory + '/host.sock'
        self._listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._listener.bind(self.socket_path)
        self._listener.listen(2)
        self._listener.settimeout(0.1)
        self._stop = threading.Event()
        self._connections = []
        self._threads = []
        self.dispatches = []
        self.has_valid_masks = True
        self.has_authenticated = False
        self.has_pong = False
        self.has_closed = False
        self.failure: Optional[Exception] = None
        self.has_editor_client = True
        self.has_draft_model = True
        self.entry = {'schemaVersion': 2, 'type': 'editor', 'pid': os.getpid(),
                      'instanceId': uuid.uuid4().hex, 'protocolVersion': '0.10.0',
                      'connectionToken': uuid.uuid4().hex,
                      'endpoint': {'type': 'socket', 'path': self.socket_path}}
        self.identity = copilot_transport._identity(self.entry)
        self.entry_dir = directory + '/entries'
        os.mkdir(self.entry_dir)
        self.registry_path = self.entry_dir + f'/{self.identity}.json'
        with open(self.registry_path, 'w', encoding='utf-8') as handle:
            json.dump(self.entry, handle)
        self._thread = threading.Thread(target=self._accept, daemon=True)
        self._thread.start()

    def close(self) -> None:
        self._stop.set()
        self._listener.close()
        for connection in self._connections:
            with contextlib.suppress(OSError):
                connection.shutdown(socket.SHUT_RDWR)
            connection.close()
        self._thread.join(1)
        for thread in self._threads:
            thread.join(1)

    def _accept(self) -> None:
        while not self._stop.is_set():
            try:
                connection, _ = self._listener.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            self._connections.append(connection)
            thread = threading.Thread(target=self._serve, args=(connection,), daemon=True)
            self._threads.append(thread)
            thread.start()

    def _read_exact(self, connection: socket.socket, length: int) -> bytes:
        data = bytearray()
        while len(data) < length:
            piece = connection.recv(length - len(data))
            if not piece:
                raise EOFError
            data.extend(piece)
        return bytes(data)

    def _receive(self, connection: socket.socket) -> Any:
        first, second = self._read_exact(connection, 2)
        if not second & 0x80:
            self.has_valid_masks = False
            raise ValueError('client sent an unmasked WebSocket frame')
        length = second & 0x7f
        if length == 126:
            length = struct.unpack('!H', self._read_exact(connection, 2))[0]
        elif length == 127:
            length = struct.unpack('!Q', self._read_exact(connection, 8))[0]
        mask = self._read_exact(connection, 4)
        body = self._read_exact(connection, length)
        body = bytes(value ^ mask[index % 4] for index, value in enumerate(body))
        opcode = first & 0x0f
        if opcode == 10:
            self.has_pong = body == b'probe'
            return None
        if opcode == 8:
            self.has_closed = True
            return None
        if opcode != 1:
            raise ValueError('unexpected client frame')
        return json.loads(body.decode('utf-8'))

    def _frame(self, connection: socket.socket, opcode: int, data: bytes,
               is_final: bool = True) -> None:
        first = opcode | (0x80 if is_final else 0)
        if len(data) < 126:
            header = bytes([first, len(data)])
        elif len(data) < 65536:
            header = bytes([first, 126]) + struct.pack('!H', len(data))
        else:
            header = bytes([first, 127]) + struct.pack('!Q', len(data))
        connection.sendall(header + data)

    def _json(self, connection: socket.socket, value: Dict[str, Any],
              is_fragmented: bool = False) -> None:
        data = json.dumps(value, ensure_ascii=False).encode('utf-8')
        if is_fragmented:
            middle = len(data) // 2
            self._frame(connection, 1, data[:middle], False)
            self._frame(connection, 9, b'probe')
            self._frame(connection, 0, data[middle:])
        else:
            self._frame(connection, 1, data)

    def _action(self, connection: socket.socket, action: Dict[str, Any],
                origin: Optional[Dict[str, Any]] = None,
                rejection: Optional[str] = None) -> None:
        envelope = {'channel': CHAT_CHANNEL, 'action': action, 'origin': origin,
                    'serverSeq': 10}
        if rejection:
            envelope['rejectionReason'] = rejection
        self._json(connection, {'jsonrpc': '2.0', 'method': 'action', 'params': envelope}, True)

    def _serve(self, connection: socket.socket) -> None:
        client_id = ''
        try:
            request = bytearray()
            while b'\r\n\r\n' not in request:
                piece = connection.recv(1)
                if not piece:
                    return
                request.extend(piece)
            headers = request.decode('ascii')
            self.has_authenticated = f'?tkn={self.entry["connectionToken"]} ' in headers
            key = next(line.split(':', 1)[1].strip() for line in headers.split('\r\n')
                       if line.lower().startswith('sec-websocket-key:'))
            accept = base64.b64encode(hashlib.sha1(
                f'{key}258EAFA5-E914-47DA-95CA-C5AB0DC85B11'.encode('ascii')).digest()).decode('ascii')
            response = (f'HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\n'
                        f'Connection: Upgrade\r\nSec-WebSocket-Accept: {accept}\r\n\r\n')
            connection.sendall(response.encode('ascii'))
            while not self._stop.is_set():
                message = self._receive(connection)
                if not message:
                    continue
                params = message.get('params') or {}
                method = message.get('method')
                if method == 'initialize':
                    client_id = params['clientId']
                    result = {'protocolVersion': '0.10.0', 'serverSeq': 0, 'snapshots': []}
                elif method == 'listSessions':
                    result = {'items': [{'resource': SESSION_CHANNEL, 'provider': 'copilotcli',
                                        'title': 'copilot-test', 'status': 1,
                                        'workingDirectories': ['file:///tmp/project'],
                                        'modifiedAt': '2026-10-11T00:00:00Z'},
                                       {'resource': 'claude:/not-copilot', 'provider': 'claude',
                                        'title': 'another-agent'}]}
                elif method == 'subscribe':
                    channel = params['channel']
                    if channel == SESSION_CHANNEL:
                        state = {'lifecycle': 'loading' if self.mode == 'not-ready' else 'ready',
                                 'defaultChat': CHAT_CHANNEL,
                                 'chats': [] if self.mode == 'missing-chat' else [
                                     {'resource': CHAT_CHANNEL,
                                      'interactivity': 'readonly' if self.mode == 'readonly-chat' else 'full'}],
                                 'activeClients': [
                                     {'clientId': 'synthetic-editor', 'tools': [{'name': 'usages'}, {'name': 'problems'}]}
                                 ] if self.has_editor_client else []}
                    elif channel == CHAT_CHANNEL:
                        state = {'status': 8 if self.mode == 'busy' else 1,
                                 'turns': [{'id': 'previous', 'message': {'model': MODEL}}],
                                 'draft': {'text': 'unsent human draft', **({'model': MODEL} if self.has_draft_model else {})}}
                    else:
                        state = {}
                    result = {'snapshot': {'resource': channel, 'state': state, 'fromSeq': 0}}
                elif method == 'dispatchAction':
                    self.dispatches.append(params)
                    if self.mode == 'disconnect':
                        connection.shutdown(socket.SHUT_RDWR)
                        return
                    origin = {'clientId': client_id, 'clientSeq': params['clientSeq']}
                    action = params['action']
                    if self.mode == 'reject':
                        self._action(connection, action, origin, 'chat is busy')
                        continue
                    if self.mode == 'uncertain':
                        continue
                    # A different client's echo and completed turn are never our acceptance or reply.
                    self._action(connection, action, {'clientId': 'another-client', 'clientSeq': 0})
                    self._action(connection, {'type': 'chat/turnComplete', 'turnId': 'another-turn'})
                    self._action(connection, action, origin)
                    turn_id = action['turnId']
                    if self.mode in ('tools', 'tool-fallback'):
                        self._action(connection, {'type': 'chat/responsePart', 'turnId': turn_id,
                                                  'part': {'kind': 'markdown', 'id': 'narration', 'content': 'Before tool narration'}})
                        self._action(connection, {'type': 'chat/toolCallStart', 'turnId': turn_id,
                                                  'toolCallId': 'tool-one', 'toolName': 'synthetic'})
                        self._action(connection, {'type': 'chat/responsePart', 'turnId': turn_id,
                                                  'part': {'kind': 'toolCall', 'toolCall': {'toolCallId': 'tool-one'}}})
                    if self.mode == 'tool-fallback':
                        self._action(connection, {'type': 'chat/turnComplete', 'turnId': turn_id, 'duration': 1})
                        continue
                    self._action(connection, {'type': 'chat/responsePart', 'turnId': 'another-turn',
                                              'part': {'kind': 'markdown', 'id': 'wrong', 'content': 'WRONG'}})
                    self._action(connection, {'type': 'chat/responsePart', 'turnId': turn_id,
                                              'part': {'kind': 'markdown', 'id': 'answer', 'content': '안녕 '}})
                    self._action(connection, {'type': 'chat/delta', 'turnId': turn_id,
                                              'partId': 'answer', 'content': '세계'})
                    event = 'chat/turnCancelled' if self.mode == 'cancel' else 'chat/turnComplete'
                    self._action(connection, {'type': event, 'turnId': turn_id, 'duration': 1})
                    continue
                else:
                    continue
                self._json(connection, {'jsonrpc': '2.0', 'id': message['id'], 'result': result}, True)
        except (EOFError, OSError):
            pass
        except Exception as error:
            self.failure = error


class TransportTests(unittest.TestCase):
    def setUp(self) -> None:
        self._caller = patch.object(caller, 'detect_caller', return_value={'agent': 'unknown'})
        self._caller.start()
        self.addCleanup(self._caller.stop)
        self._temporary = tempfile.TemporaryDirectory(prefix='ca-ahp-', dir='/tmp')
        self._old_directories = config.COPILOT_ENDPOINT_DIRS
        self._old_stores = (config.COPILOT_HOST_DBS, config.COPILOT_SESSION_DIRS)
        config.COPILOT_HOST_DBS = (self._temporary.name + '/host.db',)
        config.COPILOT_SESSION_DIRS = (self._temporary.name + '/sessions',)
        self.host = SyntheticHost(self._temporary.name)
        config.COPILOT_ENDPOINT_DIRS = (self.host.entry_dir,)
        self.shim = copilot_transport.list_shims()[0]

    def tearDown(self) -> None:
        copilot_transport.close_clients()
        self.host.close()
        config.COPILOT_ENDPOINT_DIRS = self._old_directories
        config.COPILOT_HOST_DBS, config.COPILOT_SESSION_DIRS = self._old_stores
        self._temporary.cleanup()
        self.assertIsNone(self.host.failure)

    def _send(self, **changes: Any) -> Dict[str, Any]:
        return copilot_transport.request(self.shim, {'op': 'send', 'sessionId': SESSION_ID,
                                                    'text': 'synthetic request', 'acceptTimeout': 1,
                                                    **changes}, 2)

    def _finish(self, result: Dict[str, Any]) -> Dict[str, Any]:
        return copilot_transport.request(self.shim, {'op': 'await',
                                                    'injectionId': result['injectionId'],
                                                    'timeout': 2}, 3)

    def _create_store(self, root: str, sdk_session_id: str) -> Tuple[str, str]:
        sessions = root + '/sessions'
        session_directory = sessions + f'/{SESSION_ID}'
        os.makedirs(session_directory)
        database = root + '/host.db'
        with sqlite3.connect(database) as connection:
            connection.execute('CREATE TABLE sessions_v2 (session_uri TEXT, provider TEXT, '
                               'modified_time INTEGER, payload TEXT)')
            connection.execute('INSERT INTO sessions_v2 VALUES (?, ?, ?, ?)',
                               (f'copilotcli:/{SESSION_ID}', 'copilotcli', 0, '{}'))
        with sqlite3.connect(session_directory + '/session.db') as connection:
            connection.execute('CREATE TABLE session_metadata (key TEXT, value TEXT)')
            connection.execute('INSERT INTO session_metadata VALUES (?, ?)',
                               ('defaultChatProviderData', json.dumps({'sdkSessionId': sdk_session_id})))
        return database, sessions

    def test_a_queued_return_rechecks_duplicate_stores_before_dispatch(self) -> None:
        database, sessions = self._create_store(self._temporary.name + '/first', SDK_SESSION_ID)
        config.COPILOT_HOST_DBS = (database, self._temporary.name + '/second/host.db')
        config.COPILOT_SESSION_DIRS = (sessions, self._temporary.name + '/second/sessions')
        self.assertEqual(SESSION_ID, copilot.find_copilot_session(SESSION_ID)['session_id'])
        presence = copilot_transport.request(self.shim, {'op': 'presence', 'sessionId': SESSION_ID}, 2)
        self.assertTrue(presence['is_open_in_panel'])
        self._create_store(self._temporary.name + '/second', '44444444-4444-4444-8444-444444444444')
        result = self._send(requiresInteractiveClient=True)
        self.assertFalse(result['ok'])
        self.assertIs(result['accepted'], False)
        self.assertIn('multiple stores', result['error'])
        self.assertEqual([], self.host.dispatches)

    def test_read_only_status_and_secret_free_descriptor(self) -> None:
        status = copilot_transport.request(self.shim, {'op': 'status'}, 2)
        self.assertTrue(status['ok'])
        self.assertFalse(status['can_create_session'])
        self.assertEqual([SESSION_ID], [item['session_id'] for item in status['sessions']])
        self.assertEqual('/tmp/project', status['sessions'][0]['cwd'])
        self.assertNotIn(self.host.entry['connectionToken'], json.dumps([self.shim, status]))
        self.assertEqual([], self.host.dispatches)
        self.assertTrue(self.host.has_authenticated)

    def test_exact_turn_echo_completion_model_and_fragmented_ping(self) -> None:
        result = self._finish(self._send())
        self.assertTrue(result['ok'])
        self.assertTrue(result['accepted'])
        self.assertEqual('안녕 세계', result['reply'])
        self.assertEqual(SESSION_ID, result['sessionId'])
        self.assertEqual(1, len(self.host.dispatches))
        dispatch = self.host.dispatches[0]
        self.assertEqual(CHAT_CHANNEL, dispatch['channel'])
        self.assertEqual(MODEL, dispatch['action']['message']['model'])
        self.assertEqual({'kind': 'user'}, dispatch['action']['message']['origin'])
        self.assertTrue(self.host.has_valid_masks)
        self.assertTrue(self.host.has_pong)
        self.assertNotIn(self.host.entry['connectionToken'], json.dumps(result))

    def test_existing_editor_presence_and_detached_return_refusal(self) -> None:
        status = copilot_transport.request(self.shim, {'op': 'status'}, 2)
        self.assertFalse(status['sessions'][0]['is_open_in_panel'])
        presence = copilot_transport.request(self.shim, {'op': 'presence', 'sessionId': SESSION_ID}, 2)
        self.assertTrue(presence['is_open_in_panel'])
        self.assertEqual(1, presence['editor_client_count'])
        self.host.has_editor_client = False
        result = self._send(requiresInteractiveClient=True)
        self.assertFalse(result['accepted'])
        self.assertEqual([], self.host.dispatches)
        absent = copilot_transport.request(self.shim, {'op': 'presence', 'sessionId': SESSION_ID}, 2)
        self.assertFalse(absent['is_open_in_panel'])
        self.assertFalse(absent['is_caller_runtime_available'])
        # An explicit request may reach an existing dormant host session.
        self.assertTrue(self._finish(self._send())['ok'])

    def test_verified_sdk_caller_accepts_background_return_without_editor_presence(self) -> None:
        self.host.has_editor_client = False
        with (patch.dict(os.environ, {config.ENV_COPILOT_SESSION: SDK_SESSION_ID}),
              patch.object(caller, 'detect_caller', return_value={'agent': config.AGENT_COPILOT}),
              patch.object(copilot, 'find_copilot_session_by_sdk_id',
                           return_value={'session_id': SESSION_ID, 'is_archived': False}) as mapped,
              patch.object(panel, 'process_ancestry', return_value=[self.shim['pid']])):
            presence = copilot_transport.request(self.shim, {'op': 'presence', 'sessionId': SESSION_ID}, 2)
            self.assertTrue(presence['ok'])
            self.assertFalse(presence['is_open_in_panel'])
            self.assertEqual(0, presence['editor_client_count'])
            self.assertTrue(presence['is_caller_runtime_available'])
            self.assertTrue(self._finish(self._send(requiresInteractiveClient=True))['ok'])
            self.assertEqual(1, len(self.host.dispatches))
            self.assertEqual(CHAT_CHANNEL, self.host.dispatches[0]['channel'])
            mapped.assert_called_with(SDK_SESSION_ID)

    def test_unverified_sdk_callers_never_dispatch_background_returns(self) -> None:
        self.host.has_editor_client = False
        verified = {'session_id': SESSION_ID, 'is_archived': False}
        cases = (
            ('missing-env', '', verified, config.AGENT_COPILOT, True, None),
            ('invalid-env', 'not-a-session', verified, config.AGENT_COPILOT, True, None),
            ('uri-env', f'copilotcli:/{SDK_SESSION_ID}', verified, config.AGENT_COPILOT, True, None),
            ('unregistered', SDK_SESSION_ID, None, config.AGENT_COPILOT, True, None),
            ('archived', SDK_SESSION_ID, {**verified, 'is_archived': True}, config.AGENT_COPILOT, True, None),
            ('different-session', SDK_SESSION_ID, {**verified, 'session_id': str(uuid.uuid4())},
             config.AGENT_COPILOT, True, None),
            ('foreign-host', SDK_SESSION_ID, verified, config.AGENT_COPILOT, False, None),
            ('other-agent', SDK_SESSION_ID, verified, config.AGENT_CODEX, True, None),
            ('unknown-agent', SDK_SESSION_ID, verified, 'unknown', True, None),
            ('ambiguous', SDK_SESSION_ID, verified, config.AGENT_COPILOT, True,
             copilot.AmbiguousCopilotSdkSession(SDK_SESSION_ID, [
                 {**verified, 'host_db_path': '/synthetic/one'},
                 {**verified, 'host_db_path': '/synthetic/two'}])),
        )
        for name, sdk_id, session, agent, own_host, error in cases:
            with (self.subTest(case=name),
                  patch.dict(os.environ, {config.ENV_COPILOT_SESSION: sdk_id}),
                  patch.object(caller, 'detect_caller', return_value={'agent': agent}),
                  patch.object(copilot, 'find_copilot_session_by_sdk_id', return_value=session,
                               side_effect=error),
                  patch.object(panel, 'process_ancestry',
                               return_value=[self.shim['pid']] if own_host else [])):
                if not sdk_id:
                    os.environ.pop(config.ENV_COPILOT_SESSION, None)
                presence = copilot_transport.request(self.shim, {'op': 'presence', 'sessionId': SESSION_ID}, 2)
                self.assertFalse(presence['is_caller_runtime_available'])
                self.assertFalse(self._send(requiresInteractiveClient=True)['accepted'])
                self.assertEqual([], self.host.dispatches)

    def test_verified_caller_keeps_lifecycle_chat_and_busy_preflight(self) -> None:
        self.host.has_editor_client = False
        with (patch.dict(os.environ, {config.ENV_COPILOT_SESSION: SDK_SESSION_ID}),
              patch.object(caller, 'detect_caller', return_value={'agent': config.AGENT_COPILOT}),
              patch.object(copilot, 'find_copilot_session_by_sdk_id',
                           return_value={'session_id': SESSION_ID, 'is_archived': False}),
              patch.object(panel, 'process_ancestry', return_value=[self.shim['pid']])):
            for mode in ('not-ready', 'missing-chat', 'readonly-chat', 'busy'):
                with self.subTest(mode=mode):
                    self.host.mode = mode
                    self.assertFalse(self._send(requiresInteractiveClient=True)['accepted'])
                    self.assertEqual([], self.host.dispatches)

    def test_own_caller_invalid_descriptors_and_failures_are_unavailable(self) -> None:
        with (patch.dict(os.environ, {config.ENV_COPILOT_SESSION: SDK_SESSION_ID}),
              patch.object(caller, 'detect_caller', return_value={'agent': config.AGENT_COPILOT}),
              patch.object(copilot, 'find_copilot_session_by_sdk_id',
                           return_value={'session_id': SESSION_ID, 'is_archived': False}),
              patch.object(panel, 'process_ancestry', return_value=[self.shim['pid']])):
            for shim in (None, {}, {**self.shim, 'transport': 'another-transport'},
                         {**self.shim, 'pid': True}, {**self.shim, 'pid': 1}):
                with self.subTest(shim=shim):
                    self.assertFalse(copilot_transport.is_own_caller(shim, SESSION_ID))
            self.assertFalse(copilot_transport.is_own_caller(self.shim, '../invalid'))
            with patch.object(panel, 'process_ancestry', side_effect=OSError('synthetic failure')):
                self.assertFalse(copilot_transport.is_own_caller(self.shim, SESSION_ID))

    def test_our_subscription_and_unknown_tools_do_not_prove_presence(self) -> None:
        own = {'activeClients': [{'clientId': 'cross-agent-another-bridge',
                                  'tools': [{'name': 'usages'}, {'name': 'problems'}]}]}
        self.assertFalse(copilot_transport._editor_presence(own, 'my-client')['is_open_in_panel'])
        other = {'activeClients': [{'clientId': 'external', 'tools': [{'name': 'unknown'}]}]}
        self.assertFalse(copilot_transport._editor_presence(other, 'my-client')['is_open_in_panel'])

    def test_last_tool_boundary_final_reply_and_fallback(self) -> None:
        self.host.mode = 'tools'
        self.assertEqual('안녕 세계', self._finish(self._send())['reply'])
        self.host.mode = 'tool-fallback'
        self.assertEqual('Before tool narration', self._finish(self._send())['reply'])

    def test_draft_without_model_retains_previous_custom_model(self) -> None:
        self.host.has_draft_model = False
        self.assertTrue(self._finish(self._send())['ok'])
        self.assertEqual(MODEL, self.host.dispatches[0]['action']['message']['model'])

    def test_busy_and_invalid_targets_never_dispatch(self) -> None:
        self.host.mode = 'busy'
        self.assertFalse(self._send()['accepted'])
        self.host.mode = 'complete'
        for value in ('', '../escape', 'agent-host-copilotcli:/../escape', str(uuid.uuid4())):
            self.assertFalse(self._send(sessionId=value)['accepted'])
        self.assertFalse(self._send(createNew=True)['accepted'])
        self.assertEqual([], self.host.dispatches)

    def test_atomic_server_rejection_is_not_acceptance(self) -> None:
        self.host.mode = 'reject'
        result = self._send()
        self.assertFalse(result['ok'])
        self.assertIs(result['accepted'], False)
        self.assertIn('busy', result['error'])
        self.assertEqual(1, len(self.host.dispatches))

    def test_disconnect_before_echo_remains_uncertain_and_is_not_resent(self) -> None:
        self.host.mode = 'disconnect'
        result = self._send()
        self.assertFalse(result['ok'])
        self.assertIsNone(result['accepted'])
        self.assertIn('Do not resend', result['error'])
        self.assertIsNone(self._finish(result)['accepted'])
        self.assertEqual(1, len(self.host.dispatches))

    def test_missing_echo_timeout_remains_uncertain(self) -> None:
        self.host.mode = 'uncertain'
        result = self._send(acceptTimeout=0.1)
        self.assertIsNone(result['accepted'])
        self.assertFalse(result['ok'])
        self.assertEqual(1, len(self.host.dispatches))

    def test_cancellation_and_await_endpoint_binding(self) -> None:
        self.host.mode = 'cancel'
        sent = self._send()
        result = self._finish(sent)
        self.assertTrue(result['accepted'])
        self.assertFalse(result['ok'])
        self.assertIn('cancelled', result['error'])
        other = {**self.shim, 'endpoint_identity': 'another-host'}
        wrong = copilot_transport.request(other, {'op': 'await', 'injectionId': sent['injectionId']}, 1)
        self.assertIsNone(wrong['accepted'])
        self.assertEqual(1, len(self.host.dispatches))

    def test_prunes_only_finished_retained_turns(self) -> None:
        result = self._finish(self._send())
        turn = copilot_transport._TURNS[result['injectionId']]
        turn.finished_at = time.time() - copilot_transport.TURN_RETENTION_SECONDS - 1
        copilot_transport.request(self.shim, {'op': 'status'}, 2)
        self.assertNotIn(result['injectionId'], copilot_transport._TURNS)

    def test_misnamed_or_dead_registry_entries_are_ignored(self) -> None:
        os.rename(self.host.registry_path, self.host.entry_dir + '/misnamed.json')
        self.assertEqual([], copilot_transport.list_shims())


class WebSocketFrameTests(unittest.TestCase):
    def _read_invalid(self, data: bytes) -> None:
        left, right = socket.socketpair()
        client = copilot_transport._WebSocket.__new__(copilot_transport._WebSocket)
        client._socket = left
        client._buffer = bytearray()
        client._is_closed = False
        client._write_lock = threading.Lock()
        try:
            right.sendall(data)
            right.shutdown(socket.SHUT_WR)
            with self.assertRaises((OSError, UnicodeError, ValueError)):
                client.read_json()
        finally:
            client.close()
            right.close()

    def test_invalid_flags_control_continuation_utf8_and_close(self) -> None:
        for data in (b'\xc1\x00', b'\x81\x80', b'\x09\x00', b'\x80\x00',
                     b'\x81\x01\xff', b'\x88\x01x', b'\x88\x02\x03\xed'):
            with self.subTest(frame=data[:2]):
                self._read_invalid(data)

    def test_noncanonical_and_oversized_frames_fail_before_payload(self) -> None:
        self._read_invalid(b'\x81\x7e\x00\x01')
        self._read_invalid(b'\x81\x7f' + struct.pack('!Q', copilot_transport.MAX_MESSAGE_BYTES + 1))

    def test_outbound_masks_and_length_encodings(self) -> None:
        class CaptureSocket:
            def __init__(self) -> None:
                self.data = b''

            def sendall(self, data: bytes) -> None:
                self.data = data

        client = copilot_transport._WebSocket.__new__(copilot_transport._WebSocket)
        client._socket = CaptureSocket()
        client._write_lock = threading.Lock()
        for length in (0, 125, 126, 65535, 65536):
            body = b'a' * length
            client._send_frame(1, body)
            data = client._socket.data
            self.assertEqual(0x81, data[0])
            self.assertTrue(data[1] & 0x80)
            offset = 2 if length < 126 else 4 if length < 65536 else 10
            mask = data[offset:offset + 4]
            decoded = bytes(value ^ mask[index % 4] for index, value in enumerate(data[offset + 4:]))
            self.assertEqual(body, decoded)


if __name__ == '__main__':
    unittest.main(verbosity=2)
