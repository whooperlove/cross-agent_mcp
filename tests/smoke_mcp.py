"""Smoke test: speak MCP to the bridge over stdio without touching any agent CLI.

    PYTHONPATH=src .venv/bin/python tests/smoke_mcp.py

Verifies the handshake, the advertised tool list, and the read-only tools. Runs against a
throwaway CROSS_AGENT_HOME and forces the agent identity with CROSS_AGENT_SELF, so no
`send_to_*` call here can resolve a real session whoever runs it.
"""

import asyncio
import json
import os
import shutil
import sys
import tempfile

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))) + '/src')

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# set before the import: config reads all three at import time
STATE_DIR = tempfile.mkdtemp(prefix='cross-agent-smoke-')
os.environ['CROSS_AGENT_HOME'] = STATE_DIR + '/bridge'
os.environ['CLAUDE_CONFIG_DIR'] = STATE_DIR + '/claude'
os.environ['CODEX_HOME'] = STATE_DIR + '/codex'
for _empty in ('/claude/projects', '/codex/sessions'):
    os.makedirs(STATE_DIR + _empty, exist_ok=True)

from cross_agent_mcp import config, registry  # noqa: E402


def _text_of(result) -> str:
    for block in result.content:
        if getattr(block, 'type', None) == 'text':
            return block.text
    return ''


def _server(agent: str, ambient: bool = False, is_strict: bool = False) -> StdioServerParameters:
    """A bridge server told which agent it is, rather than inferring it from its parent.

    `ambient=True` drops the override, to prove the isolation holds without it. `agent` is
    then unused: the server infers its identity from its parent, which is the whole point.
    """
    env = {**os.environ, 'PYTHONPATH': ROOT_DIR + '/src'}
    env.pop('CROSS_AGENT_SELF', None)
    if not ambient:
        env['CROSS_AGENT_SELF'] = agent
    env.pop(config.ENV_REQUIRE_EXPLICIT_TARGET, None)
    if is_strict:
        env[config.ENV_REQUIRE_EXPLICIT_TARGET] = '1'
    # sys.executable, not a .venv path: a fresh checkout has no .venv, and the interpreter
    # running this file is by definition one that can import mcp
    return StdioServerParameters(command=sys.executable, args=['-m', 'cross_agent_mcp'],
                                 env=env, cwd=ROOT_DIR)


def _delivery_records() -> list:
    """Every delivery record in the throwaway state, finished or in flight."""
    found = []
    for current, _, files in os.walk(config.DELIVERY_DIR):
        found += [os.path.join(current, name) for name in files if name.endswith('.json')]
    return found


async def _refuses_its_own_agent(agent: str) -> int:
    """A send aimed at the caller's own agent is refused before anything is resolved."""
    tool = f'send_to_{agent}'
    async with stdio_client(_server(agent)) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            refused = json.loads(_text_of(await session.call_tool(
                tool, {'message': f'self-call guard check ({agent})'})))

    if refused.get('ok') or 'refusing to relay' not in refused.get('error', ''):
        print(f'[FAIL] self-call guard did not trigger for {agent}: {refused}')
        return 1
    print(f'[ok] self-call guard ({agent}) -> refused as expected')
    return 0


STRICT_INSTRUCTION = 'refuses a send that does not name its target'


async def _strict_mode_refuses_an_unaddressed_send() -> int:
    """Under CROSS_AGENT_REQUIRE_EXPLICIT_TARGET a send naming nothing is refused, pin or not.

    A send that names a session gets past the mode to resolution, which fails here because the
    stores are empty - so no send in this check reaches an agent.
    """
    conversation_id = 'conv_smoke_strict'
    registry.set_pin('codex', ROOT_DIR, '0a1b2c3d-0000-4000-8000-00000000c0de', ROOT_DIR,
                     is_sticky=True)
    async with stdio_client(_server('claude', is_strict=True)) as (read, write):
        async with ClientSession(read, write) as session:
            init = await session.initialize()
            if STRICT_INSTRUCTION not in (init.instructions or ''):
                print(f'[FAIL] a strict server does not say so in its instructions: '
                      f'{init.instructions}')
                return 1
            status = json.loads(_text_of(await session.call_tool('bridge_status', {})))
            if status['settings'].get('require_explicit_target') is not True:
                print(f'[FAIL] a strict server does not report the mode: {status["settings"]}')
                return 1
            for args in ({}, {'session_id': ''}, {'session_id': '   '}):
                refused = json.loads(_text_of(await session.call_tool(
                    'send_to_codex', {'message': 'strict mode check',
                                      'conversation_id': conversation_id, **args})))
                if refused.get('ok') or config.ENV_REQUIRE_EXPLICIT_TARGET not in refused.get(
                        'error', ''):
                    print(f'[FAIL] strict mode did not refuse {args}: {refused}')
                    return 1
            named = json.loads(_text_of(await session.call_tool(
                'send_to_codex', {'message': 'strict mode check', 'session_id': 'no-such-session',
                                  'conversation_id': conversation_id})))

    error = named.get('error', '')
    if named.get('ok') or 'is named' not in error or config.ENV_REQUIRE_EXPLICIT_TARGET in error:
        print(f'[FAIL] a send naming a session did not get past the mode: {named}')
        return 1
    hops = registry.get_conversation(conversation_id).get('hops', 0)
    if hops:
        print(f'[FAIL] the refused sends spent {hops} hop(s)')
        return 1
    print('[ok] strict mode -> refused a send naming nothing, blank or pinned; let a named one '
          'through to resolution; spent no hop')
    return 0


async def main() -> int:
    async with stdio_client(_server('claude')) as (read, write):
        async with ClientSession(read, write) as session:
            init = await session.initialize()
            print(f'[ok] initialize -> {init.server_info.name} v{init.server_info.version}')
            if STRICT_INSTRUCTION in (init.instructions or ''):
                print('[FAIL] a server without the strict mode claims it in its instructions')
                return 1

            tools = await session.list_tools()
            names = [t.name for t in tools.tools]
            print(f'[ok] tools/list -> {names}')

            expected = {'send_to_codex', 'send_to_claude', 'list_agent_sessions',
                        'bridge_status', 'pin_agent_session'}
            missing = expected - set(names)
            if missing:
                print(f'[FAIL] missing tools: {missing}')
                return 1

            status = json.loads(_text_of(await session.call_tool('bridge_status', {})))
            print(f'[ok] bridge_status -> running_under={status["running_under"]} '
                  f'max_hops={status["settings"]["max_hops"]}')
            for agent, resolved in status['resolved_sessions'].items():
                label = resolved.get('session_id') if resolved else None
                print(f'       resolved {agent}: {label}')

            listing = json.loads(_text_of(await session.call_tool(
                'list_agent_sessions', {'agent': 'both', 'scope': 'cwd'})))
            print(f'[ok] list_agent_sessions -> claude={len(listing["claude"])} '
                  f'codex={len(listing["codex"])}')

            # the hop check runs before the target is resolved, so nothing is ever addressed
            conversation_id = 'conv_smoke_hop_guard'
            for _ in range(config.MAX_HOPS):
                registry.bump_conversation(conversation_id, 'claude', 'codex')
            capped = json.loads(_text_of(await session.call_tool(
                'send_to_codex', {'message': 'hop guard check',
                                  'conversation_id': conversation_id})))
            if capped.get('ok') or 'hop limit' not in capped.get('error', ''):
                print(f'[FAIL] hop guard did not trigger: {capped}')
                return 1
            print(f'[ok] hop guard -> refused after {config.MAX_HOPS} hops')

            status = json.loads(_text_of(await session.call_tool(
                'bridge_status', {'cwd': ROOT_DIR})))
            deliveries = status.get('deliveries') or {}
            if 'pending' not in deliveries or 'recent' not in deliveries:
                print(f'[FAIL] bridge_status does not report deliveries: {deliveries}')
                return 1
            print(f'[ok] bridge_status -> deliveries pending={len(deliveries["pending"])} '
                  f'recent={len(deliveries["recent"])}')

            # A busy target is no longer refused - the outbox waits for the lock and then
            # delivers - so exercising it here would spend a real agent turn. That behaviour
            # is covered without any turn by unit_guards.test_a_busy_session_is_waited_out.

    # both identities: under ambient detection exactly one of these was a real send
    for agent in ('claude', 'codex'):
        if await _refuses_its_own_agent(agent):
            return 1

    # The ambient path, with nothing forcing the identity - the shape in which a Codex runner
    # used to reach a real Claude session. No send is attempted here: it lists the sessions the
    # server can see, and finding none is what would have made such a send harmless.
    async with stdio_client(_server('claude', ambient=True)) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            listing = json.loads(_text_of(await session.call_tool(
                'list_agent_sessions', {'agent': 'both', 'scope': 'any'})))
    if listing['claude'] or listing['codex']:
        print(f'[FAIL] the isolated stores were not empty: {listing}')
        return 1
    print('[ok] ambient identity sees no real session to reach for')

    if await _strict_mode_refuses_an_unaddressed_send():
        return 1

    records = _delivery_records()
    if records:
        print(f'[FAIL] the run created delivery records: {records}')
        return 1
    print('[ok] no delivery record was created by any of it')

    for label, path, wanted in (
            ('bridge', config.HOME_DIR, STATE_DIR + '/bridge'),
            ('claude', config.CLAUDE_PROJECTS_DIR, STATE_DIR + '/claude/projects'),
            ('codex', config.CODEX_SESSIONS_DIR, STATE_DIR + '/codex/sessions')):
        if os.path.realpath(path) != os.path.realpath(wanted):
            print(f'[FAIL] the {label} store was not the isolated one: {path}')
            return 1
    print(f'[ok] bridge, claude and codex state were all the ones under {STATE_DIR}')

    print('\nALL CHECKS PASSED')
    return 0


if __name__ == '__main__':
    try:
        sys.exit(asyncio.run(main()))
    finally:
        shutil.rmtree(STATE_DIR, ignore_errors=True)
