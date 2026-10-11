"""Isolated Copilot store and response-recovery checks; never runs an agent turn.

    PYTHONPATH=src .venv/bin/python -B tests/copilot_discovery.py
"""

import hashlib
import json
import os
import pathlib
import sqlite3
import sys
import tempfile
import time
import unittest
from typing import Any, Dict, List, Optional, Tuple
from unittest.mock import patch


STATE_ROOT = tempfile.TemporaryDirectory(prefix='cross-agent-copilot-tests-')
os.environ['CROSS_AGENT_HOME'] = STATE_ROOT.name + '/bridge'
os.environ['CROSS_AGENT_COPILOT_HOST_DB'] = STATE_ROOT.name + '/missing-host.db'
os.environ['CROSS_AGENT_COPILOT_SESSION_DIR'] = STATE_ROOT.name + '/missing-sessions'
os.environ['CROSS_AGENT_COPILOT_SDK_SESSION_DIR'] = STATE_ROOT.name + '/missing-sdk'
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))) + '/src')

from cross_agent_mcp import config, copilot  # noqa: E402


SESSION_ID = '11111111-1111-4111-8111-111111111111'
OTHER_SESSION_ID = '22222222-2222-4222-8222-222222222222'
SDK_SESSION_ID = '33333333-3333-4333-8333-333333333333'
OTHER_SDK_SESSION_ID = '44444444-4444-4444-8444-444444444444'
TOKEN = 'req_1791700000_ab12cd'


class CopilotDiscoveryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(dir=STATE_ROOT.name)
        self.addCleanup(self.temporary.cleanup)
        self.root = self.temporary.name
        self.project = self.root + '/project'
        os.makedirs(self.project)
        self.host, self.sessions = self._create_store('Code')
        self.sdk = self.root + '/sdk'
        os.makedirs(self.sdk)
        self.original = (config.COPILOT_HOST_DBS, config.COPILOT_SESSION_DIRS,
                         config.COPILOT_SDK_SESSION_DIRS)
        config.COPILOT_HOST_DBS = (self.host,)
        config.COPILOT_SESSION_DIRS = (self.sessions,)
        config.COPILOT_SDK_SESSION_DIRS = (self.sdk,)
        self.addCleanup(self._restore_config)

    def _restore_config(self) -> None:
        (config.COPILOT_HOST_DBS, config.COPILOT_SESSION_DIRS,
         config.COPILOT_SDK_SESSION_DIRS) = self.original

    def _create_store(self, name: str) -> Tuple[str, str]:
        profile = self.root + '/' + name
        host = profile + '/User/globalStorage/agent-host.db'
        sessions = profile + '/agentSessionData'
        os.makedirs(os.path.dirname(host))
        os.makedirs(sessions)
        with sqlite3.connect(host) as connection:
            connection.execute('CREATE TABLE sessions_v2 (session_uri TEXT PRIMARY KEY, '
                               'provider TEXT, modified_time INTEGER, payload TEXT)')
        return host, sessions

    def _add_session(self, session_id: str = SESSION_ID, title: Optional[str] = 'copilot-test',
                     cwd: Optional[str] = None, metadata: Optional[Dict[str, Any]] = None,
                     host: Optional[str] = None, sessions: Optional[str] = None) -> str:
        host = host or self.host
        sessions = sessions or self.sessions
        project = self.project if cwd is None else cwd
        data = {'data': {'summary': 'Generated title',
                         'project': {'uri': pathlib.Path(project).as_uri()}}}
        with sqlite3.connect(host) as connection:
            connection.execute('INSERT INTO sessions_v2 VALUES (?, ?, ?, ?)',
                               (f'copilotcli:/{session_id}', 'copilotcli',
                                int(time.time() * 1000), json.dumps(data)))
        path = sessions + f'/{session_id}/session.db'
        os.makedirs(os.path.dirname(path))
        values = {'copilot.project.uri': pathlib.Path(project).as_uri(),
                  'copilot.model': {'id': 'vendor/unchanged-custom-model'},
                  'defaultChatProviderData': {'sdkSessionId': SDK_SESSION_ID}}
        if title is not None:
            values['customTitle'] = title
        values.update(metadata or {})
        with sqlite3.connect(path) as connection:
            connection.execute('CREATE TABLE session_metadata (key TEXT PRIMARY KEY, value TEXT)')
            connection.execute('CREATE TABLE turns (id TEXT, event_id TEXT, checkpoint_ref TEXT)')
            connection.execute('CREATE TABLE local_turns (turn_id TEXT, payload TEXT)')
            connection.executemany('INSERT INTO session_metadata VALUES (?, ?)',
                                   [(key, json.dumps(value) if not isinstance(value, str)
                                     else value) for key, value in values.items()])
        return path

    def _write_events(self, events: List[Dict[str, Any]], sdk_id: str = SDK_SESSION_ID,
                      declared_id: Optional[str] = None) -> str:
        path = self.sdk + f'/{sdk_id}/events.jsonl'
        os.makedirs(os.path.dirname(path), exist_ok=True)
        start = {'type': 'session.start', 'data': {'sessionId': declared_id or sdk_id}}
        with open(path, 'w', encoding='utf-8') as stream:
            for event in [start] + events:
                stream.write(json.dumps(event) + '\n')
        return path

    def _turn(self, text: str, token: Optional[str] = TOKEN, turn_id: str = 'turn-1',
              finished: bool = True, stamp: Optional[float] = None) -> List[Dict[str, Any]]:
        stamp = time.time() if stamp is None else stamp
        iso = time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(stamp))
        result = [
            {'type': 'user.message', 'data': {'turnId': turn_id, 'content': f'Question {token or ""}'}},
            {'type': 'assistant.turn_start', 'data': {'turnId': turn_id}},
            {'type': 'assistant.message', 'data': {'turnId': turn_id, 'content': text,
                                                  'toolRequests': []}},
        ]
        if finished:
            result.append({'type': 'assistant.turn_end', 'timestamp': iso,
                           'data': {'turnId': turn_id}})
        return result

    def test_exact_uuid_and_uri_normalization_rejects_uri_tricks(self) -> None:
        for value in (SESSION_ID, SESSION_ID.upper(), f'copilotcli:/{SESSION_ID}',
                      f'agent-host-copilotcli:/{SESSION_ID}'):
            self.assertEqual(copilot.normalize_session_id(value), SESSION_ID)
        for value in ('*', '../' + SESSION_ID, f'copilotcli:///{SESSION_ID}',
                      f'copilotcli:/{SESSION_ID}?x=1', f'copilotcli:/{SESSION_ID}#x',
                      ' ' + SESSION_ID, None, 1):
            self.assertIsNone(copilot.normalize_session_id(value))

    def test_custom_title_model_and_editor_identity(self) -> None:
        self._add_session()
        session = copilot.find_copilot_session(f'agent-host-copilotcli:/{SESSION_ID}')
        self.assertEqual(session['title'], 'copilot-test')
        self.assertTrue(session['is_named'])
        self.assertEqual(session['cwd'], self.project)
        self.assertEqual(session['model_id'], 'vendor/unchanged-custom-model')
        self.assertEqual(session['sdk_session_id'], SDK_SESSION_ID)
        self.assertEqual(session['session_uri'], f'copilotcli:/{SESSION_ID}')
        self.assertEqual(session['ui_session_uri'], f'agent-host-copilotcli:/{SESSION_ID}')

    def test_json_encoded_strings_and_generated_title(self) -> None:
        self._add_session(title=json.dumps('Named title'), metadata={
            'copilot.project.uri': json.dumps(pathlib.Path(self.project).as_uri())})
        self._add_session(OTHER_SESSION_ID, title=None)
        self.assertEqual(copilot.find_copilot_session(SESSION_ID)['title'], 'Named title')
        generated = copilot.find_copilot_session(OTHER_SESSION_ID)
        self.assertEqual(generated['title'], 'Generated title')
        self.assertFalse(generated['is_named'])

    def test_scopes_limit_and_mtime_filter(self) -> None:
        self._add_session()
        self._add_session(OTHER_SESSION_ID, cwd=self.project + '/child')
        self.assertEqual(len(copilot.list_copilot_sessions('cwd', self.project)), 2)
        self.assertEqual(len(copilot.list_copilot_sessions('cwd', self.project + '/child')), 1)
        self.assertEqual(len(copilot.list_copilot_sessions('tree', self.project + '/child')), 2)
        self.assertEqual(len(copilot.list_copilot_sessions('any', '/unrelated', limit=1)), 1)
        self.assertEqual(copilot.list_copilot_sessions('cwd', self.project, limit=0), [])
        self.assertEqual(copilot.list_copilot_sessions('any', self.project,
                                                     since_mtime=time.time() + 60), [])
        with self.assertRaises(ValueError):
            copilot.list_copilot_sessions('invalid', self.project)

    def test_duplicate_uuid_in_distinct_profiles_is_ambiguous(self) -> None:
        self._add_session()
        other_host, other_sessions = self._create_store('Code-Insiders')
        self._add_session(host=other_host, sessions=other_sessions)
        config.COPILOT_HOST_DBS = (self.host, other_host)
        config.COPILOT_SESSION_DIRS = (self.sessions, other_sessions)
        self.assertEqual(len(copilot.list_copilot_sessions('any', self.project)), 2)
        with self.assertRaises(copilot.AmbiguousCopilotSession):
            copilot.find_copilot_session(SESSION_ID)
        config.COPILOT_HOST_DBS = (self.host, self.host)
        config.COPILOT_SESSION_DIRS = (self.sessions, self.sessions)
        self.assertIsNotNone(copilot.find_copilot_session(SESSION_ID))

    def test_exact_sdk_uuid_identifies_each_default_chat_without_activity_or_pins(self) -> None:
        older_path = self._add_session()
        self._add_session(OTHER_SESSION_ID, metadata={
            'defaultChatProviderData': {'sdkSessionId': OTHER_SDK_SESSION_ID}})
        old_time = time.time() - 3600
        os.utime(older_path, (old_time, old_time))
        with sqlite3.connect(self.host) as connection:
            connection.execute('UPDATE sessions_v2 SET modified_time = ? WHERE session_uri = ?',
                               (int(old_time * 1000), f'copilotcli:/{SESSION_ID}'))
        self.assertEqual(copilot.list_copilot_sessions('any', self.project)[0]['session_id'],
                         OTHER_SESSION_ID)
        with patch('cross_agent_mcp.registry.get_pin', return_value={
                'session_id': OTHER_SESSION_ID, 'is_sticky': True}) as pin:
            self.assertEqual(copilot.find_copilot_session_by_sdk_id(SDK_SESSION_ID)['session_id'],
                             SESSION_ID)
            self.assertEqual(copilot.find_copilot_session_by_sdk_id(
                OTHER_SDK_SESSION_ID.upper())['session_id'], OTHER_SESSION_ID)
            pin.assert_not_called()

    def test_sdk_identity_input_is_uuid_only(self) -> None:
        self._add_session()
        for value in (f'copilotcli:/{SDK_SESSION_ID}', f'agent-host-copilotcli:/{SDK_SESSION_ID}',
                      ' ' + SDK_SESSION_ID, SDK_SESSION_ID + ' ', '../' + SDK_SESSION_ID,
                      SDK_SESSION_ID + '?query', '*', None, 123):
            self.assertIsNone(copilot.find_copilot_session_by_sdk_id(value))
        self.assertIsNone(copilot.find_copilot_session_by_sdk_id(OTHER_SDK_SESSION_ID))

    def test_sdk_uuid_shared_by_different_host_sessions_is_ambiguous(self) -> None:
        self._add_session()
        self._add_session(OTHER_SESSION_ID)
        with self.assertRaises(copilot.AmbiguousCopilotSdkSession) as raised:
            copilot.find_copilot_session_by_sdk_id(SDK_SESSION_ID)
        self.assertIsInstance(raised.exception, copilot.AmbiguousCopilotSession)
        self.assertEqual(raised.exception.sdk_session_id, SDK_SESSION_ID)
        self.assertEqual(len(raised.exception.sessions), 2)

    def test_same_host_and_sdk_uuid_in_distinct_profiles_is_ambiguous(self) -> None:
        self._add_session()
        other_host, other_sessions = self._create_store('Code-Insiders')
        self._add_session(host=other_host, sessions=other_sessions)
        config.COPILOT_HOST_DBS = (self.host, other_host)
        config.COPILOT_SESSION_DIRS = (self.sessions, other_sessions)
        with self.assertRaises(copilot.AmbiguousCopilotSdkSession):
            copilot.find_copilot_session_by_sdk_id(SDK_SESSION_ID)

    def test_archived_sdk_identity_is_unavailable_and_cannot_hide_ambiguity(self) -> None:
        self._add_session(metadata={'isArchived': 'true'})
        self.assertIsNone(copilot.find_copilot_session_by_sdk_id(SDK_SESSION_ID))
        self._add_session(OTHER_SESSION_ID)
        with self.assertRaises(copilot.AmbiguousCopilotSdkSession):
            copilot.find_copilot_session_by_sdk_id(SDK_SESSION_ID)

    def test_sdk_reverse_mapping_ignores_missing_malformed_and_other_chat_metadata(self) -> None:
        path = self._add_session()
        with sqlite3.connect(path) as connection:
            connection.execute('DELETE FROM session_metadata WHERE key = ?',
                               ('defaultChatProviderData',))
            connection.execute('INSERT INTO session_metadata VALUES (?, ?)', (
                'chatProviderData:subagent-chat', json.dumps({'sdkSessionId': SDK_SESSION_ID})))
        self.assertIsNone(copilot.find_copilot_session_by_sdk_id(SDK_SESSION_ID))
        for value in ('{broken', 'null', json.dumps({'sdkSessionId': f'copilotcli:/{SDK_SESSION_ID}'}),
                      json.dumps({'sdkSessionId': ' ' + SDK_SESSION_ID})):
            with sqlite3.connect(path) as connection:
                connection.execute('INSERT OR REPLACE INTO session_metadata VALUES (?, ?)',
                                   ('defaultChatProviderData', value))
            self.assertIsNone(copilot.find_copilot_session_by_sdk_id(SDK_SESSION_ID))

    def test_sdk_reverse_mapping_rejects_metadata_outside_configured_store(self) -> None:
        path = self._add_session()
        outside = self.root + '/outside-session.db'
        os.rename(path, outside)
        os.symlink(outside, path)
        self.assertIsNone(copilot.find_copilot_session_by_sdk_id(SDK_SESSION_ID))

    def test_missing_sdk_data_has_no_recovered_answer(self) -> None:
        self._add_session()
        progress = copilot.peer_progress(SESSION_ID, token=TOKEN)
        self.assertIsNone(progress['answer'])
        self.assertFalse(progress['recovery_available'])
        self.assertIsNone(copilot.find_copilot_session('*'))
        self.assertIsNone(copilot.peer_progress(OTHER_SESSION_ID, token=TOKEN))

    def test_completed_turn_correlates_prompt_and_final_token(self) -> None:
        self._add_session()
        self._write_events(self._turn(f'Complete\n{TOKEN}'))
        progress = copilot.peer_progress(SESSION_ID, after=time.time() - 60, token=TOKEN)
        self.assertEqual(progress['answer'], f'Complete\n{TOKEN}')
        self.assertFalse(progress['is_working'])
        self.assertTrue(progress['recovery_available'])
        self.assertIsNotNone(progress['answered_at'])

    def test_incomplete_turn_and_mismatched_end_are_not_answers(self) -> None:
        self._add_session()
        events = self._turn(f'Interim {TOKEN}', finished=False)
        events.append({'type': 'assistant.turn_end', 'data': {'turnId': 'wrong-turn'}})
        self._write_events(events)
        progress = copilot.peer_progress(SESSION_ID, token=TOKEN)
        self.assertIsNone(progress['answer'])
        self.assertTrue(progress['is_working'])

    def test_tool_narration_is_discarded(self) -> None:
        self._add_session()
        events = self._turn(f'Complete {TOKEN}')
        events[2:2] = [
            {'type': 'assistant.message', 'data': {'turnId': 'turn-1', 'content': 'Working',
                                                  'toolRequests': []}},
            {'type': 'assistant.message', 'data': {'turnId': 'turn-1', 'content': 'Tool call',
                                                  'toolRequests': [{'name': 'read'}]}},
        ]
        self._write_events(events)
        self.assertEqual(copilot.peer_progress(SESSION_ID, token=TOKEN)['answer'],
                         f'Complete {TOKEN}')

    def _interaction_rounds(self) -> List[Dict[str, Any]]:
        events = self._turn(f'Calling tool {TOKEN}', turn_id='0')
        for event in events:
            if event['type'] != 'assistant.turn_end':
                event['data']['interactionId'] = 'request-interaction'
        events[2]['data']['toolRequests'] = [{'name': 'cross-agent-bridge_status'}]
        final_round = self._turn(f'Final response\n{TOKEN}', turn_id='1')[1:]
        for event in final_round:
            if event['type'] != 'assistant.turn_end':
                event['data']['interactionId'] = 'request-interaction'
        return events + final_round

    def test_sdk_tool_rounds_correlate_by_exact_interaction(self) -> None:
        self._add_session()
        events = self._interaction_rounds()
        events[3:3] = [{'type': 'tool.execution_complete', 'data': {
            'turnId': '0', 'interactionId': 'request-interaction', 'success': False,
            'error': {'message': 'Tool does not exist'}}}]
        self._write_events(events)
        progress = copilot.peer_progress(SESSION_ID, after=time.time() - 60, token=TOKEN)
        self.assertEqual(progress['answer'], f'Final response\n{TOKEN}')
        self.assertFalse(progress['is_working'])

    def test_sdk_intermediate_tool_round_and_open_final_round_are_not_answers(self) -> None:
        self._add_session()
        events = self._interaction_rounds()
        for partial in (events[:4], events[:-1]):
            self._write_events(partial)
            progress = copilot.peer_progress(SESSION_ID, token=TOKEN)
            self.assertIsNone(progress['answer'])
            self.assertTrue(progress['is_working'])

    def test_later_round_invalidates_earlier_echo_in_the_same_interaction(self) -> None:
        self._add_session()
        events = self._interaction_rounds()
        events[2]['data']['toolRequests'] = []
        self._write_events(events[:-1])
        self.assertIsNone(copilot.peer_progress(SESSION_ID, token=TOKEN)['answer'])
        events[-2]['data']['content'] = 'Final response without an echo'
        self._write_events(events)
        progress = copilot.peer_progress(SESSION_ID, token=TOKEN)
        self.assertIsNone(progress['answer'])
        self.assertEqual(progress['unmatched_turn'], 'Final response without an echo')

    def test_another_interaction_cannot_reuse_an_earlier_prompt_or_turn_id(self) -> None:
        self._add_session()
        events = self._interaction_rounds()[:4]
        unrelated = self._turn(f'Quoted earlier {TOKEN}', token=None, turn_id='0')
        for event in unrelated:
            if event['type'] != 'assistant.turn_end':
                event['data']['interactionId'] = 'unrelated-interaction'
        self._write_events(events + unrelated)
        progress = copilot.peer_progress(SESSION_ID, token=TOKEN)
        self.assertIsNone(progress['answer'])
        self.assertEqual(progress['unmatched_turn'], f'Quoted earlier {TOKEN}')

    def test_round_with_different_or_missing_interaction_does_not_match_prompt(self) -> None:
        self._add_session()
        events = self._interaction_rounds()
        for interaction_id in ('different-interaction', None):
            modified = json.loads(json.dumps(events))
            for event in modified[4:]:
                event['data']['interactionId'] = interaction_id
            self._write_events(modified)
            self.assertIsNone(copilot.peer_progress(SESSION_ID, token=TOKEN)['answer'])

    def test_message_cannot_cross_an_interaction_boundary(self) -> None:
        self._add_session()
        events = self._interaction_rounds()
        events[-2]['data']['interactionId'] = 'different-interaction'
        self._write_events(events)
        self.assertIsNone(copilot.peer_progress(SESSION_ID, token=TOKEN)['answer'])

    def test_unrelated_quoted_token_and_inexact_echo_are_not_answers(self) -> None:
        self._add_session()
        for prompt_token, text in ((None, f'Quoted old {TOKEN}'), (TOKEN, f'{TOKEN}abcdef')):
            self._write_events(self._turn(text, token=prompt_token))
            progress = copilot.peer_progress(SESSION_ID, after=time.time() - 60, token=TOKEN)
            self.assertIsNone(progress['answer'])
            self.assertEqual(progress['unmatched_turn'], text)

    def test_stale_and_uncorrelated_latest_answers_are_not_recovered(self) -> None:
        self._add_session()
        self._write_events(self._turn(f'Old {TOKEN}', stamp=time.time() - 3600))
        self.assertIsNone(copilot.peer_progress(SESSION_ID, after=time.time() - 60,
                                               token=TOKEN)['answer'])
        self.assertEqual(copilot.peer_progress(SESSION_ID)['answer'], f'Old {TOKEN}')
        self.assertEqual(copilot.peer_progress(SESSION_ID, after=time.time() - 7200)['answer'],
                         f'Old {TOKEN}')

    def test_stream_identity_and_symlink_containment(self) -> None:
        self._add_session()
        event_path = self._write_events(self._turn(f'Complete {TOKEN}'),
                                       declared_id=OTHER_SESSION_ID)
        self.assertFalse(copilot.peer_progress(SESSION_ID, token=TOKEN)['recovery_available'])
        outside = self.root + '/outside-events.jsonl'
        os.rename(event_path, outside)
        os.symlink(outside, event_path)
        self.assertFalse(copilot.peer_progress(SESSION_ID, token=TOKEN)['recovery_available'])

    def test_session_and_host_symlinks_cannot_escape_stores(self) -> None:
        path = self._add_session()
        outside = self.root + '/outside-session.db'
        os.rename(path, outside)
        os.symlink(outside, path)
        self.assertIsNone(copilot.find_copilot_session(SESSION_ID))
        outside_host = self.root + '/outside-host.db'
        os.rename(self.host, outside_host)
        os.symlink(outside_host, self.host)
        self.assertEqual(copilot.list_copilot_sessions('any', self.project), [])

    def test_sdk_duplicate_does_not_choose_newest_store(self) -> None:
        self._add_session()
        event_path = self._write_events(self._turn(f'Complete {TOKEN}'))
        second_root = self.root + '/sdk-other'
        second_path = second_root + f'/{SDK_SESSION_ID}/events.jsonl'
        os.makedirs(os.path.dirname(second_path))
        pathlib.Path(second_path).write_bytes(pathlib.Path(event_path).read_bytes())
        config.COPILOT_SDK_SESSION_DIRS = (self.sdk, second_root)
        self.assertFalse(copilot.peer_progress(SESSION_ID, token=TOKEN)['recovery_available'])

    def test_discovery_and_progress_do_not_change_store_files(self) -> None:
        session_path = self._add_session()
        event_path = self._write_events(self._turn(f'Complete {TOKEN}'))
        paths = (self.host, session_path, event_path)
        before = [hashlib.sha256(pathlib.Path(path).read_bytes()).hexdigest() for path in paths]
        copilot.list_copilot_sessions('cwd', self.project)
        copilot.find_copilot_session_by_sdk_id(SDK_SESSION_ID)
        copilot.peer_progress(SESSION_ID, token=TOKEN)
        after = [hashlib.sha256(pathlib.Path(path).read_bytes()).hexdigest() for path in paths]
        self.assertEqual(before, after)

    def test_malformed_host_uri_and_invalid_metadata_do_not_crash(self) -> None:
        self._add_session(metadata={'copilot.project.uri': 'file://[invalid',
                                    'copilot.model': '{broken', 'defaultChatProviderData': 'null'})
        with sqlite3.connect(self.host) as connection:
            connection.execute('INSERT INTO sessions_v2 VALUES (?, ?, ?, ?)',
                               (f'copilotcli:/{OTHER_SESSION_ID}?query', 'copilotcli', 0, '{}'))
        sessions = copilot.list_copilot_sessions('any', self.project)
        self.assertEqual(len(sessions), 1)
        self.assertEqual(sessions[0]['cwd'], self.project)
        self.assertIsNone(sessions[0]['model_id'])

    def test_archived_string_boolean_and_pending_user(self) -> None:
        self._add_session(metadata={'isArchived': 'true'})
        self.assertFalse(copilot.find_copilot_session(SESSION_ID)['is_active'])
        self._write_events([{'type': 'user.message', 'data': {'content': TOKEN, 'turnId': 'pending'}}])
        self.assertTrue(copilot.peer_progress(SESSION_ID, token=TOKEN)['is_working'])


if __name__ == '__main__':
    try:
        unittest.main()
    finally:
        STATE_ROOT.cleanup()
