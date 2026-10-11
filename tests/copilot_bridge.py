"""Copilot routing checks without connecting to a host or spending an agent turn."""

import asyncio
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import unittest
from typing import Tuple
from unittest.mock import patch


STATE_ROOT = tempfile.mkdtemp(prefix='cross-agent-copilot-routing-')
os.environ['CROSS_AGENT_HOME'] = STATE_ROOT + '/bridge'
os.environ['CROSS_AGENT_COPILOT_HOST_DB'] = STATE_ROOT + '/copilot/agent-host.db'
os.environ['CROSS_AGENT_COPILOT_SESSION_DIR'] = STATE_ROOT + '/copilot/sessions'
os.environ['CROSS_AGENT_COPILOT_SDK_SESSION_DIR'] = STATE_ROOT + '/copilot/sdk'
os.environ['CROSS_AGENT_COPILOT_ENDPOINT_DIR'] = STATE_ROOT + '/copilot/endpoints'
os.environ.pop('COPILOT_AGENT_SESSION_ID', None)
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))) + '/src')

from cross_agent_mcp import bridge, caller, config, copilot, server, uihook  # noqa: E402


SESSION_ID = '11111111-1111-4111-8111-111111111111'
SESSION_URI = 'agent-host-copilotcli:/' + SESSION_ID
SDK_SESSION_ID = '33333333-3333-4333-8333-333333333333'
SESSION = {'agent': 'copilot', 'session_id': SESSION_ID, 'cwd': STATE_ROOT}


class CopilotRoutingTests(unittest.TestCase):
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

    def test_sdk_environment_resolves_the_exact_host_address(self) -> None:
        with patch.dict(os.environ, {config.ENV_COPILOT_SESSION: SDK_SESSION_ID}), \
                patch.object(copilot, 'find_copilot_session_by_sdk_id', return_value=SESSION) as lookup:
            self.assertEqual(bridge._host_declared_session('copilot'), SESSION_ID)
            lookup.assert_called_once_with(SDK_SESSION_ID)
        self.assertEqual(caller.classify_process_name('/bin/copilot-runtime'), 'copilot')

    def test_inherited_sdk_environment_is_ignored_for_other_agents(self) -> None:
        with patch.dict(os.environ, {config.ENV_COPILOT_SESSION: SDK_SESSION_ID}), \
                patch.object(copilot, 'find_copilot_session_by_sdk_id') as lookup:
            for agent in ('codex', 'claude', 'grok'):
                bridge._host_declared_session(agent)
            lookup.assert_not_called()
        with patch.dict(os.environ, {config.ENV_COPILOT_SESSION: SDK_SESSION_ID}), \
                patch.object(caller, '_read_process', return_value={'ppid': 1, 'name': '/bin/codex'}):
            with patch.dict(os.environ):
                os.environ.pop('CROSS_AGENT_SELF', None)
                self.assertEqual(caller.detect_caller()['agent'], 'codex')

    def test_sdk_binding_is_not_forwarded_to_a_spawned_peer(self) -> None:
        with patch.dict(os.environ, {config.ENV_COPILOT_SESSION: SDK_SESSION_ID}):
            self.assertNotIn(config.ENV_COPILOT_SESSION, config.child_env())

    def test_a_generic_sdk_host_is_identified_without_a_product_override(self) -> None:
        with patch.dict(os.environ, {config.ENV_COPILOT_SESSION: SDK_SESSION_ID}), \
                patch.object(caller, '_read_process', return_value={'ppid': 1, 'name': '/bin/embedded-host'}):
            os.environ.pop('CROSS_AGENT_SELF', None)
            self.assertEqual(caller.detect_caller()['agent'], 'copilot')

    def test_invalid_missing_or_ambiguous_sdk_binding_refuses_a_send(self) -> None:
        with patch.dict(os.environ, {config.ENV_COPILOT_SESSION: '../bad'}):
            with self.assertRaisesRegex(bridge.BridgeError, 'invalid SDK'):
                bridge._host_declared_session('copilot')
        with patch.dict(os.environ, {config.ENV_COPILOT_SESSION: SDK_SESSION_ID}), \
                patch.object(copilot, 'find_copilot_session_by_sdk_id', return_value=None):
            with self.assertRaisesRegex(bridge.BridgeError, 'no unique registered'):
                bridge._host_declared_session('copilot')
        duplicates = [{**SESSION, 'host_db_path': 'fixture-a'},
                      {**SESSION, 'host_db_path': 'fixture-b'}]
        with patch.dict(os.environ, {config.ENV_COPILOT_SESSION: SDK_SESSION_ID}), \
                patch.object(copilot, 'find_copilot_session_by_sdk_id',
                             side_effect=copilot.AmbiguousCopilotSdkSession(SDK_SESSION_ID, duplicates)):
            with self.assertRaisesRegex(bridge.BridgeError, 'Nothing was sent'):
                bridge._host_declared_session('copilot')

    def test_host_metadata_cannot_conflict_with_the_sdk_binding(self) -> None:
        with patch.object(bridge, '_host_declared_session', return_value=SESSION_ID):
            self.assertEqual(bridge._resolve_copilot_caller_session(SESSION_ID.upper()), SESSION_ID)
            with self.assertRaisesRegex(bridge.BridgeError, 'conflicts'):
                bridge._resolve_copilot_caller_session('22222222-2222-4222-8222-222222222222')

    def test_sdk_background_return_is_available_without_claiming_an_open_panel(self) -> None:
        presence = {'ok': True, 'is_open_in_panel': False, 'is_caller_runtime_available': True}
        shim = {'transport': 'copilot-ahp'}
        with patch.object(uihook, 'read_presence', return_value=presence):
            self.assertTrue(uihook.is_return_available(shim, SESSION_ID))
        with patch.object(uihook, 'find_live_session', return_value={'shim': shim}), \
                patch.object(uihook, 'read_presence', return_value=presence):
            state = bridge.panel_state('copilot', SESSION_ID)
        self.assertFalse(state['is_open_in_a_panel'])
        self.assertTrue(state['is_caller_runtime_available'])

    def test_vscode_metadata_addresses_the_exact_copilot_session(self) -> None:
        for scheme in ('agent-host-copilotcli', 'copilotcli'):
            meta = {'vscode.conversationId': scheme + ':/' + SESSION_ID,
                    'vscode.requestId': 'request-1'}
            self.assertEqual(server.caller_session_from_meta(meta), SESSION_ID)
            self.assertEqual(server.caller_agent_from_meta(meta), 'copilot')
        for value in (SESSION_ID, 'agent-host-claude:/' + SESSION_ID,
                      SESSION_URI + '/other', 'copilotcli:/../outside'):
            self.assertIsNone(server.caller_session_from_meta({'vscode.conversationId': value}))
            self.assertIsNone(server.caller_agent_from_meta({'vscode.conversationId': value}))

    def test_codex_metadata_keeps_precedence(self) -> None:
        meta = {'x-codex-turn-metadata': {'thread_id': 'codex-thread'},
                'vscode.conversationId': SESSION_URI}
        self.assertEqual(server.caller_session_from_meta(meta), 'codex-thread')
        self.assertIsNone(server.caller_agent_from_meta(meta))

    def test_copilot_does_not_guess_the_sender_from_recent_activity(self) -> None:
        with patch.object(bridge, '_declared_session', return_value=None), \
                patch.object(bridge, '_host_declared_session', return_value=None), \
                patch.object(bridge.discovery, 'find_active_session') as discover, \
                patch.object(uihook, 'find_own_session') as own:
            self.assertIsNone(bridge._own_session_id('copilot'))
            discover.assert_not_called()
            own.assert_not_called()

    def test_existing_target_uses_its_host_route(self) -> None:
        live = {**SESSION, 'ui_shim': {'transport': 'copilot-ahp'}, 'source': 'ide-panel'}
        with patch.object(bridge, '_requested_session_id', return_value=(SESSION_ID, 'copilot-test')), \
                patch.object(bridge, '_panel_session', return_value=live) as panel:
            target = bridge._resolve_target('copilot', 'copilot-test', 'cwd', STATE_ROOT, False, [])
        self.assertEqual(target['session_id'], SESSION_ID)
        self.assertEqual(target['selected_by'], bridge.SELECTED_CALLER)
        panel.assert_called_once_with('copilot', SESSION_ID, [])

    def test_a_sticky_pin_cannot_bypass_duplicate_copilot_stores(self) -> None:
        with tempfile.TemporaryDirectory(dir=STATE_ROOT) as root:
            database, sessions = self._create_store(root + '/first', SDK_SESSION_ID)
            other_database, other_sessions = root + '/second/host.db', root + '/second/sessions'
            live = {**SESSION, 'shim': {'transport': 'copilot-ahp'}}
            with (patch.object(config, 'COPILOT_HOST_DBS', (database, other_database)),
                  patch.object(config, 'COPILOT_SESSION_DIRS', (sessions, other_sessions)),
                  patch.object(uihook, 'is_enabled', return_value=True),
                  patch.object(bridge.registry, 'get_pin',
                               return_value={'is_sticky': True, 'session_id': SESSION_ID}),
                  patch.object(uihook, 'find_live_session', return_value=live) as lookup):
                target = bridge._resolve_target('copilot', None, 'cwd', STATE_ROOT, False, [])
                self.assertEqual(bridge.SELECTED_PIN, target['selected_by'])
                lookup.reset_mock()
                self._create_store(root + '/second', '44444444-4444-4444-8444-444444444444')
                with self.assertRaises(copilot.AmbiguousCopilotSession):
                    bridge._resolve_target('copilot', None, 'cwd', STATE_ROOT, False, [])
                lookup.assert_not_called()

    def test_attached_return_cannot_bypass_duplicate_copilot_stores(self) -> None:
        with tempfile.TemporaryDirectory(dir=STATE_ROOT) as root:
            database, sessions = self._create_store(root + '/first', SDK_SESSION_ID)
            other_database, other_sessions = root + '/second/host.db', root + '/second/sessions'
            shim = {'transport': 'copilot-ahp'}
            live = {**SESSION, 'shim': shim}
            with (patch.object(config, 'COPILOT_HOST_DBS', (database, other_database)),
                  patch.object(config, 'COPILOT_SESSION_DIRS', (sessions, other_sessions)),
                  patch.object(uihook, 'is_enabled', return_value=True),
                  patch.object(uihook, 'find_live_session', return_value=live) as lookup,
                  patch.object(uihook, 'is_return_available', return_value=True) as presence):
                self.assertEqual(shim, bridge._return_shim('copilot', SESSION_ID, 'req_fixture', 'reply'))
                lookup.reset_mock()
                presence.reset_mock()
                self._create_store(root + '/second', '44444444-4444-4444-8444-444444444444')
                self.assertIsNone(bridge._return_shim('copilot', SESSION_ID, 'req_fixture', 'reply'))
                lookup.assert_not_called()
                presence.assert_not_called()

    def test_an_exact_live_only_copilot_host_does_not_require_catalog_metadata(self) -> None:
        live = {**SESSION, 'shim': {'transport': 'copilot-ahp'}}
        with patch.object(uihook, 'is_enabled', return_value=True), \
                patch.object(uihook, 'find_live_session', return_value=live):
            target = bridge._panel_session('copilot', SESSION_ID, [])
        self.assertEqual(SESSION_ID, target['session_id'])

    def test_unreachable_target_is_refused_without_cli_fallback(self) -> None:
        with patch.object(bridge, '_requested_session_id', return_value=(SESSION_ID, 'copilot-test')), \
                patch.object(bridge, '_panel_session', return_value=None), \
                patch.object(bridge.discovery, 'find_session') as discover:
            with self.assertRaisesRegex(bridge.BridgeError, 'no reachable'):
                bridge._resolve_target('copilot', 'copilot-test', 'cwd', STATE_ROOT, False, [])
            discover.assert_not_called()
        with self.assertRaisesRegex(bridge.BridgeError, 'existing session'):
            bridge._call_copilot('message', SESSION_ID, STATE_ROOT, {}, 60)

    def test_new_or_unaddressed_copilot_session_is_refused(self) -> None:
        with self.assertRaisesRegex(bridge.BridgeError, 'existing Agent Host'):
            bridge._resolve_target('copilot', None, 'cwd', STATE_ROOT, True, [])
        with patch.object(bridge, '_requested_session_id', return_value=(None, None)):
            with self.assertRaisesRegex(bridge.BridgeError, 'explicit session_id'):
                bridge._resolve_target('copilot', None, 'cwd', STATE_ROOT, False, [])

    def test_return_traffic_requires_an_attached_copilot_client(self) -> None:
        shim = {'transport': 'copilot-ahp'}
        with patch.object(bridge, '_panel_session', return_value={'ui_shim': shim}), \
                patch.object(uihook, 'is_return_available', return_value=False):
            self.assertIsNone(bridge._return_shim('copilot', SESSION_ID, 'req_fixture', 'reply'))
        with patch.object(bridge, '_panel_session', return_value={'ui_shim': shim}), \
                patch.object(uihook, 'is_return_available', return_value=True):
            self.assertEqual(bridge._return_shim('copilot', SESSION_ID, 'req_fixture', 'reply'), shim)

    def test_a_return_presence_probe_fails_closed(self) -> None:
        from cross_agent_mcp import copilot_transport
        shim = {'transport': 'copilot-ahp'}
        with patch.object(copilot_transport, 'request', return_value={'ok': True, 'is_open_in_panel': False}):
            self.assertFalse(uihook.is_return_available(shim, SESSION_ID))
        with patch.object(copilot_transport, 'request', side_effect=OSError('closed')):
            self.assertFalse(uihook.is_return_available(shim, SESSION_ID))
        self.assertTrue(uihook.is_return_available({'transport': 'stdio'}, SESSION_ID))

    def test_the_copilot_caller_is_passed_per_request(self) -> None:
        observed = {}

        async def run(func, *args, **kwargs):
            observed.update(kwargs)
            return {'ok': True}

        with patch.object(server, '_request_meta', return_value={'vscode.conversationId': SESSION_URI}), \
                patch.object(server, '_run_blocking', side_effect=run):
            asyncio.run(server._send('codex', message='hello'))
        self.assertEqual(observed['caller_session_id'], SESSION_ID)
        self.assertEqual(observed['caller_agent'], 'copilot')

    def test_agent_stores_are_protected_from_bridge_permissions_repair(self) -> None:
        for paths in (config.COPILOT_HOST_DBS, config.COPILOT_SESSION_DIRS,
                      config.COPILOT_SDK_SESSION_DIRS, config.COPILOT_ENDPOINT_DIRS):
            self.assertTrue(all(config.is_protected_path(path) for path in paths))


if __name__ == '__main__':
    try:
        unittest.main()
    finally:
        shutil.rmtree(STATE_ROOT, ignore_errors=True)
