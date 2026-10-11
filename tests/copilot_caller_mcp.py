"""Exercise SDK caller binding over real MCP stdio with isolated SQLite stores.

No live agent, editor endpoint, model or real session is used.
"""

import asyncio
import json
import os
import pathlib
import sqlite3
import sys
import tempfile
import time
import unittest
from typing import Any, Dict

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


ROOT_DIR = pathlib.Path(__file__).resolve().parent.parent
HOST_IDS = ('11111111-1111-4111-8111-111111111111',
            '22222222-2222-4222-8222-222222222222')
SDK_IDS = ('33333333-3333-4333-8333-333333333333',
           '44444444-4444-4444-8444-444444444444')


def _result(result: Any) -> Dict[str, Any]:
    return json.loads(next(block.text for block in result.content if block.type == 'text'))


class CopilotCallerMcpTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix='cross-agent-sdk-caller-')
        self.addCleanup(self.temporary.cleanup)
        self.root = pathlib.Path(self.temporary.name)
        self.database = self.root / 'host.db'
        with sqlite3.connect(self.database) as connection:
            connection.execute('CREATE TABLE sessions_v2 (session_uri TEXT, provider TEXT, '
                               'modified_time INTEGER, payload TEXT)')
            for host_id, sdk_id in zip(HOST_IDS, SDK_IDS):
                connection.execute('INSERT INTO sessions_v2 VALUES (?, ?, ?, ?)', (
                    'copilotcli:/' + host_id, 'copilotcli', int(time.time() * 1000),
                    json.dumps({'data': {'summary': 'SDK caller fixture'}})))
                directory = self.root / 'sessions' / host_id
                directory.mkdir(parents=True)
                with sqlite3.connect(directory / 'session.db') as metadata:
                    metadata.execute('CREATE TABLE session_metadata (key TEXT PRIMARY KEY, value TEXT)')
                    metadata.executemany('INSERT INTO session_metadata VALUES (?, ?)', (
                        ('defaultChatProviderData', json.dumps({'sdkSessionId': sdk_id})),
                        ('copilot.project.uri', self.root.as_uri())))
        self.environment = {
            **os.environ, 'PYTHONPATH': str(ROOT_DIR / 'src'),
            'PYTHONDONTWRITEBYTECODE': '1', 'CROSS_AGENT_SELF': 'copilot',
            'CROSS_AGENT_HOME': str(self.root / 'bridge'),
            'CLAUDE_CONFIG_DIR': str(self.root / 'claude'),
            'CODEX_HOME': str(self.root / 'codex'), 'GROK_HOME': str(self.root / 'grok'),
            'CROSS_AGENT_GROK_NAMES_DB': str(self.root / 'missing-names.db'),
            'CROSS_AGENT_COPILOT_HOST_DB': str(self.database),
            'CROSS_AGENT_COPILOT_SESSION_DIR': str(self.root / 'sessions'),
            'CROSS_AGENT_COPILOT_SDK_SESSION_DIR': str(self.root / 'sdk'),
            'CROSS_AGENT_COPILOT_ENDPOINT_DIR': str(self.root / 'endpoints'),
        }
        for key in ('CROSS_AGENT_SELF_SESSION', 'CROSS_AGENT_CONVERSATION_ID',
                    'CROSS_AGENT_HOP', 'CROSS_AGENT_SENDER', 'CROSS_AGENT_BUSY',
                    'GROK_SESSION_ID', 'COPILOT_AGENT_SESSION_ID'):
            self.environment.pop(key, None)

    def _parameters(self, sdk_id: str, agent: str = 'copilot') -> StdioServerParameters:
        return StdioServerParameters(
            command=sys.executable, args=['-B', '-m', 'cross_agent_mcp'], cwd=str(ROOT_DIR),
            env={**self.environment, 'CROSS_AGENT_SELF': agent,
                 'COPILOT_AGENT_SESSION_ID': sdk_id})

    async def _status(self, sdk_id: str, agent: str = 'copilot') -> Dict[str, Any]:
        async with stdio_client(self._parameters(sdk_id, agent)) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                return _result(await session.call_tool('bridge_status', {'cwd': str(self.root)}))

    async def test_two_sdk_contexts_under_the_same_driver_have_distinct_addresses(self) -> None:
        results = await asyncio.gather(*(self._status(sdk_id) for sdk_id in SDK_IDS))
        self.assertEqual([result['caller_session_id'] for result in results], list(HOST_IDS))
        self.assertTrue(all(result['running_under'] == 'copilot' for result in results))
        self.assertTrue(all(result['caller_session_error'] is None for result in results))

    async def test_another_agent_ignores_the_inherited_sdk_identity(self) -> None:
        result = await self._status(SDK_IDS[0], 'codex')
        self.assertEqual(result['running_under'], 'codex')
        self.assertIsNone(result['caller_session_id'])

    async def test_invalid_sdk_identity_is_reported_without_leaking_its_value(self) -> None:
        result = await self._status('../not-a-session')
        self.assertIsNone(result['caller_session_id'])
        self.assertIn('invalid SDK', result['caller_session_error'])
        self.assertNotIn('../not-a-session', result['caller_session_error'])

    async def test_ambiguous_sdk_identity_cannot_queue_a_peer_request(self) -> None:
        path = self.root / 'sessions' / HOST_IDS[1] / 'session.db'
        with sqlite3.connect(path) as connection:
            connection.execute('UPDATE session_metadata SET value = ? WHERE key = ?', (
                json.dumps({'sdkSessionId': SDK_IDS[0]}), 'defaultChatProviderData'))
        async with stdio_client(self._parameters(SDK_IDS[0])) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                result = _result(await session.call_tool('send_to_codex', {
                    'message': 'Caller identity must be rejected before routing',
                    'session_id': '55555555-5555-4555-8555-555555555555'}))
        self.assertFalse(result['ok'])
        self.assertIn('Nothing was sent', result['error'])
        self.assertEqual([], list((self.root / 'bridge' / 'deliveries').rglob('req_*.json')))


if __name__ == '__main__':
    unittest.main()
