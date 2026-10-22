"""Hot reload: the front keeps the editor's connection while the server behind it is replaced.

    PYTHONPATH=src .venv/bin/python -B tests/front_reload.py

Part one drives the front with a stand-in worker, so every path - a reload, a call carried
across one, a worker that will not start, a worker that dies - is checked without depending on
how fast a real server starts. Part two runs the real server behind the front over MCP and
reloads it. Everything runs against a throwaway CROSS_AGENT_HOME.
"""

import asyncio
import json
import os
import queue
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from typing import Optional

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT_DIR + '/src')

# set before the import: config reads them at import time
STATE_DIR = tempfile.mkdtemp(prefix='cross-agent-front-')
os.environ['CROSS_AGENT_HOME'] = STATE_DIR + '/bridge'
os.environ['CLAUDE_CONFIG_DIR'] = STATE_DIR + '/claude'
os.environ['CODEX_HOME'] = STATE_DIR + '/codex'
os.environ['GROK_HOME'] = STATE_DIR + '/grok'
os.environ['CROSS_AGENT_GROK_NAMES_DB'] = STATE_DIR + '/no-such-state.vscdb'
os.environ['CROSS_AGENT_COPILOT_HOST_DB'] = STATE_DIR + '/copilot/agent-host.db'
os.environ['CROSS_AGENT_COPILOT_SESSION_DIR'] = STATE_DIR + '/copilot/sessions'
os.environ['CROSS_AGENT_COPILOT_SDK_SESSION_DIR'] = STATE_DIR + '/copilot/sdk'
os.environ['CROSS_AGENT_COPILOT_ENDPOINT_DIR'] = STATE_DIR + '/copilot/endpoints'
os.environ['CROSS_AGENT_SELF'] = 'claude'
os.environ['PYTHONDONTWRITEBYTECODE'] = '1'
os.environ['PYTHONPATH'] = ROOT_DIR + '/src'
os.environ.pop('COPILOT_AGENT_SESSION_ID', None)
for _empty in ('/claude/projects', '/codex/sessions', '/grok/sessions'):
    os.makedirs(STATE_DIR + _empty, exist_ok=True)

from cross_agent_mcp import config, front, outbox, server  # noqa: E402

FAILURES = []

# Behaves like the mcp SDK where it matters to the front: a cancelled request is never
# answered, a listen stream is acknowledged first and stamps every frame with its id, and
# SIGUSR1 before stdin closes is remembered. FAKE_CONTROL steers how the next workers behave.
FAKE_WORKER = r'''
import json, os, signal, sys, threading, time
control = os.environ.get('FAKE_CONTROL')
mode = open(control).read().strip() if control and os.path.exists(control) else ''
if mode == 'fail':
    sys.exit(3)
if mode == 'slow-start':
    time.sleep(2)
generation = os.environ.get('CROSS_AGENT_WORKER_GENERATION')
def log(text):
    with open(os.environ['FAKE_LOG'], 'a') as f:
        f.write(f'{os.getpid()} {generation} {text}\n')
drained = []
signal.signal(signal.SIGUSR1, lambda *a: drained.append(1))
lock = threading.Lock()
cancelled = set()
def send(message):
    with lock:
        sys.stdout.write(json.dumps(message) + '\n')
        sys.stdout.flush()
def me(request_id):
    return {'jsonrpc': '2.0', 'id': request_id,
            'result': {'pid': os.getpid(), 'generation': generation}}
def later(request_id):
    if request_id not in cancelled:
        send(me(request_id))
SUB = 'io.modelcontextprotocol/subscriptionId'
asking = None
listening = None
for line in sys.stdin:
    m = json.loads(line)
    method, request_id = m.get('method'), m.get('id')
    if method == 'initialize':
        if mode == 'no-initialize':
            send({'jsonrpc': '2.0', 'id': request_id,
                  'error': {'code': -32602, 'message': 'unsupported'}})
            continue
        send({'jsonrpc': '2.0', 'id': request_id, 'result': {
            'protocolVersion': '2025-06-18', 'capabilities': {'tools': {'listChanged': False}},
            'serverInfo': {'name': 'fake', 'version': generation}}})
    elif method == 'server/discover':
        send({'jsonrpc': '2.0', 'id': request_id, 'result': {
            'supportedVersions': ['2026-07-28'], 'resultType': 'complete',
            'capabilities': {'tools': {'listChanged': True}}, 'instructions': generation}})
    elif method == 'subscriptions/listen':
        if mode == 'die-on-listen':
            os._exit(7)
        listening = request_id
        send({'jsonrpc': '2.0', 'method': 'notifications/subscriptions/acknowledged', 'params': {
            'notifications': m['params']['notifications'], '_meta': {SUB: request_id}}})
    elif method == 'notifications/cancelled':
        if not isinstance(m.get('params'), dict):
            continue  # malformed: the SDK drops it too
        # never answered, as the SDK does
        cancelled.add(m['params']['requestId'])
        log(f'cancelled {m["params"]["requestId"]}')
        if m['params']['requestId'] == listening:
            listening = None
    elif method == 'emit':
        send({'jsonrpc': '2.0', 'method': 'notifications/tools/list_changed',
              'params': {'_meta': {SUB: listening, 'from': generation}}})
        send(me(request_id))
    elif method == 'whoami':
        send(me(request_id))
    elif method == 'slow':
        threading.Timer(m['params']['seconds'], later, args=(request_id,)).start()
    elif method == 'die':
        os._exit(5)
    elif method == 'ask':
        asking = request_id
        send({'jsonrpc': '2.0', 'id': 0, 'method': 'roots/list',
              'params': {'odd': m['params'].get('odd', '')}})
        if m['params'].get('give_up'):
            send({'jsonrpc': '2.0', 'method': 'notifications/cancelled',
                  'params': {'requestId': 0}})
    elif method is None and request_id == 0 and asking is not None:
        send({'jsonrpc': '2.0', 'id': asking, 'result': {'answer': m.get('result')}})
        asking = None
log(f'drained={bool(drained)}')
'''


def check(label: str, condition: bool, detail: object = '') -> None:
    if condition:
        print(f'[ok] {label}')
    else:
        FAILURES.append(label)
        print(f'[FAIL] {label} {str(detail)[:600]}')


def _wait_for(predicate, seconds: float = 15.0):
    deadline = time.time() + seconds
    while time.time() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.1)
    return None


def _is_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    # a zombie child of the front still answers kill 0; ps says whether it is one
    state = subprocess.run(['ps', '-o', 'stat=', '-p', str(pid)], capture_output=True,
                           text=True).stdout.strip()
    return bool(state) and not state.startswith('Z')


class Editor:
    """Plays the editor: writes JSON-RPC lines to the front and reads what comes back."""

    def __init__(self, fake_path: str) -> None:
        harness = ('import sys; from cross_agent_mcp import front; '
                   'front.WORKER_COMMAND[:] = [sys.executable, "-B", sys.argv[1]]; front.main()')
        self.process = subprocess.Popen([sys.executable, '-B', '-c', harness, fake_path],
                                        stdin=subprocess.PIPE, stdout=subprocess.PIPE)
        self.messages: 'queue.Queue' = queue.Queue()
        self.notifications = []
        self.server_requests = []
        # answers by json.dumps(id), whatever order they come in
        self.responses = {}
        threading.Thread(target=self._read, daemon=True).start()

    def _read(self) -> None:
        for line in self.process.stdout:
            self.messages.put(json.loads(line))

    def send(self, message: dict) -> None:
        self.process.stdin.write((json.dumps(message) + '\n').encode())
        self.process.stdin.flush()

    def next_message(self, seconds: float = 15.0) -> dict:
        return self.messages.get(timeout=seconds)

    def answer(self, request_id, seconds: float = 15.0) -> dict:
        """Wait for the answer to one request, keeping whatever else arrives meanwhile."""
        deadline = time.time() + seconds
        key = json.dumps(request_id)
        while key not in self.responses:
            if time.time() >= deadline:
                raise TimeoutError(f'no answer to {request_id!r}')
            try:
                message = self.next_message(max(deadline - time.time(), 0.1))
            except queue.Empty:
                continue
            if 'method' in message and 'id' in message:
                self.server_requests.append(message)
            elif 'method' in message:
                self.notifications.append(message)
            else:
                self.responses[json.dumps(message.get('id'))] = message
        return self.responses[key]

    def next_request(self, method: str, seconds: float = 15.0) -> dict:
        """Wait for a request the worker sends the editor, keeping whatever else arrives."""
        deadline = time.time() + seconds
        while time.time() < deadline:
            message = self.next_message(max(deadline - time.time(), 0.1))
            if message.get('method') == method and 'id' in message:
                return message
            if 'method' in message:
                self.notifications.append(message)
            else:
                self.responses[json.dumps(message.get('id'))] = message
        raise TimeoutError(f'no {method} request')

    def call(self, request_id, method: str, params: dict = None) -> dict:
        self.send({'jsonrpc': '2.0', 'id': request_id, 'method': method,
                   'params': params or {}})
        return self.answer(request_id)


def _status(pid: int) -> dict:
    return front._read_json(f'{config.FRONT_DIR}{pid}.json') or {}


def _request_reload(request_id: str) -> None:
    front._write_json(config.RELOAD_REQUEST_PATH, {'id': request_id, 'requested_at': time.time()})


def _reloaded(pid: int, request_id: str) -> dict:
    return _wait_for(lambda: (_status(pid).get('last_reload') or {}).get('request_id') == request_id
                     and _status(pid)['last_reload']) or {}


def _log_lines(path: str) -> list:
    try:
        with open(path) as f:
            return f.read().splitlines()
    except OSError:
        return []


def test_the_front_with_a_stand_in_worker() -> None:
    config.ensure_dirs()
    fake_path = STATE_DIR + '/fake_worker.py'
    with open(fake_path, 'w') as f:
        f.write(FAKE_WORKER)
    control = STATE_DIR + '/fake_control'
    log = STATE_DIR + '/fake_log'
    os.environ['FAKE_CONTROL'] = control
    os.environ['FAKE_LOG'] = log

    editor = Editor(fake_path)
    pid = editor.process.pid
    try:
        init = editor.call(0, 'initialize', {'protocolVersion': '2025-06-18', 'capabilities': {},
                                             'clientInfo': {'name': 'test', 'version': '1'}})
        check('the editor handshake reaches the first worker',
              init.get('result', {}).get('serverInfo', {}).get('version') == '1', init)
        check('and the front says the tool list can change',
              init['result']['capabilities']['tools'].get('listChanged') is True, init)
        editor.send({'jsonrpc': '2.0', 'method': 'notifications/initialized'})

        first = editor.call(1, 'whoami')['result']
        check('a call is answered by generation 1', first['generation'] == '1', first)
        status = _wait_for(lambda: _status(pid).get('worker_pid') and _status(pid))
        check('the front records which worker it runs',
              status and status['worker_pid'] == first['pid'] and status['generation'] == 1,
              status)

        _request_reload('r1')
        outcome = _reloaded(pid, 'r1')
        check('a reload request starts and adopts a new worker',
              outcome.get('ok') is True and outcome.get('replaced_pid') == first['pid'], outcome)
        second = editor.call(2, 'whoami')['result']
        check('and the next call is answered by it', second['generation'] == '2', second)
        check('the editor is told the tool list may have changed',
              any(n.get('method') == 'notifications/tools/list_changed'
                  for n in editor.notifications), editor.notifications)
        check('the replaced worker exits once it owes nothing',
              _wait_for(lambda: not _is_alive(first['pid']), 10) is not None)
        check('after being told to drain rather than just losing stdin',
              f'{first["pid"]} 1 drained=True' in _log_lines(log), _log_lines(log))

        editor.send({'jsonrpc': '2.0', 'id': 3, 'method': 'slow', 'params': {'seconds': 2}})
        time.sleep(0.3)
        _request_reload('r2')
        outcome = _reloaded(pid, 'r2')
        check('a reload goes ahead while a call is still running', outcome.get('ok') is True,
              outcome)
        fourth = editor.call(4, 'whoami')['result']
        check('new calls go to the new worker at once', fourth['generation'] == '3', fourth)
        third = editor.answer(3)['result']
        check('and the running call is still answered by the worker that took it',
              third['generation'] == '2', third)
        check('which exits only after that, drained',
              _wait_for(lambda: f'{second["pid"]} 2 drained=True' in _log_lines(log), 10)
              is not None, _log_lines(log))

        with open(control, 'w') as f:
            f.write('fail')
        _request_reload('r3')
        outcome = _reloaded(pid, 'r3')
        os.remove(control)
        check('a worker that does not start is reported as a failed reload',
              outcome.get('ok') is False and 'exited with code 3' in outcome.get('error', ''),
              outcome)
        fifth = editor.call(5, 'whoami')['result']
        check('and the old worker keeps serving', fifth['generation'] == '3', fifth)

        editor.send({'jsonrpc': '2.0', 'id': 6, 'method': 'ask', 'params': {}})
        message = editor.next_request('roots/list')
        check('a request from the worker reaches the editor under an id of the front',
              str(message.get('id', '')).startswith(front.SERVER_REQUEST_ID_PREFIX), message)
        editor.send({'jsonrpc': '2.0', 'id': message.get('id'), 'result': {'roots': []}})
        answered = editor.answer(6)
        check("and the editor's answer finds the worker that asked",
              answered.get('result', {}).get('answer') == {'roots': []}, answered)

        died = editor.call(7, 'die')
        check('a worker that dies mid-call leaves the call answered with an error',
              'exited with code 5' in died.get('error', {}).get('message', ''), died)
        replaced = None
        for attempt in range(50):
            reply = editor.call(100 + attempt, 'whoami')
            if 'result' in reply:
                replaced = reply['result']
                break
            time.sleep(0.2)
        check('and a replacement takes over the connection',
              replaced is not None and replaced['pid'] != fifth['pid'], replaced)

        editor.process.stdin.close()
        code = editor.process.wait(15)
        check('the front exits when the editor closes the connection', code == 0, code)
        check('its worker exits with it, without a drain',
              _wait_for(lambda: f'{replaced["pid"]} {replaced["generation"]} drained=False'
                        in _log_lines(log), 10) is not None, _log_lines(log))
        check('and its status file is gone', not os.path.exists(f'{config.FRONT_DIR}{pid}.json'))
    finally:
        if editor.process.poll() is None:
            editor.process.kill()


def test_a_2026_editor_keeps_its_subscription_across_a_reload() -> None:
    """Claude Code speaks MCP 2026-07-28: no initialize, and one `subscriptions/listen` request
    left open for the whole session. Missed, every reload reported "the editor has not
    connected yet", and a replaced worker held that request open forever and never left."""
    fake_path = STATE_DIR + '/fake_worker.py'
    log = os.environ['FAKE_LOG']
    editor = Editor(fake_path)
    pid = editor.process.pid
    meta = {'io.modelcontextprotocol/protocolVersion': '2026-07-28'}
    try:
        discovered = editor.call('probe', 'server/discover', {'_meta': meta})
        check('a 2026 editor discovers the first worker',
              discovered.get('result', {}).get('instructions') == '1', discovered)
        editor.send({'jsonrpc': '2.0', 'id': 'listen:0', 'method': 'subscriptions/listen',
                     'params': {'_meta': meta, 'notifications': {'toolsListChanged': True}}})
        first = editor.call(1, 'whoami', {'_meta': meta})['result']

        _request_reload('m1')
        outcome = _reloaded(pid, 'm1')
        check('a reload goes ahead without any initialize to replay',
              outcome.get('ok') is True and outcome.get('replaced_pid') == first['pid'], outcome)
        second = editor.call(2, 'whoami', {'_meta': meta})['result']
        check('and the next call is served by the new worker', second['generation'] == '2',
              second)
        emitted = editor.call(3, 'emit', {'_meta': meta})['result']
        check('an event the new worker raises is served by it', emitted['generation'] == '2')

        stream = [n for n in editor.notifications
                  if (n.get('params') or {}).get('_meta', {}).get(
                      'io.modelcontextprotocol/subscriptionId') == 'listen:0']
        acks = [n for n in stream if n.get('method') == 'notifications/subscriptions/acknowledged']
        changes = [n for n in stream if n.get('method') == 'notifications/tools/list_changed']
        check('the editor is acknowledged once, by the worker it first listened to',
              len(acks) == 1, acks)
        check('it hears the tools may have changed on the stream it listens on',
              any('from' not in n['params']['_meta'] for n in changes), changes)
        check('and hears the new worker on that same stream',
              any(n['params']['_meta'].get('from') == '2' for n in changes), changes)
        check('the old stream ending is never shown to the editor',
              '"listen:0"' not in editor.responses, editor.responses)
        check('the replaced worker leaves although the subscription was open on it',
              _wait_for(lambda: f'{first["pid"]} 1 drained=True' in _log_lines(log), 10)
              is not None, _log_lines(log))

        editor.send({'jsonrpc': '2.0', 'method': 'notifications/cancelled',
                     'params': {'requestId': 'listen:0'}})
        check("the editor's own cancel reaches the worker that now holds the stream",
              _wait_for(lambda: f'{second["pid"]} 2 cancelled listen:0' in _log_lines(log), 5)
              is not None, _log_lines(log))
        before = len(editor.notifications)
        _request_reload('m2')
        outcome = _reloaded(pid, 'm2')
        third = editor.call(4, 'whoami', {'_meta': meta})['result']
        check('with no subscription open a reload still goes ahead',
              outcome.get('ok') is True and third['generation'] == '3', outcome)
        check('and sends no stream frame the editor no longer listens for',
              not any(n.get('method') == 'notifications/tools/list_changed'
                      for n in editor.notifications[before:]), editor.notifications[before:])
        check('a subscription the editor cancelled does not keep the replaced worker',
              _wait_for(lambda: f'{second["pid"]} 2 drained=True' in _log_lines(log), 10)
              is not None, _log_lines(log))

        editor.process.stdin.close()
        check('the front exits when the editor closes', editor.process.wait(15) == 0)
    finally:
        if editor.process.poll() is None:
            editor.process.kill()


META_2026 = {'io.modelcontextprotocol/protocolVersion': '2026-07-28'}


def _connected(kind: str) -> Editor:
    """A front with an editor that has connected the way Codex (2025) or Claude Code (2026) does."""
    editor = Editor(STATE_DIR + '/fake_worker.py')
    if kind == '2025':
        editor.call(0, 'initialize', {'protocolVersion': '2025-06-18', 'capabilities': {},
                                      'clientInfo': {'name': 'test', 'version': '1'}})
        editor.send({'jsonrpc': '2.0', 'method': 'notifications/initialized'})
    else:
        editor.call('probe', 'server/discover', {'_meta': META_2026})
        editor.send({'jsonrpc': '2.0', 'id': 'listen:0', 'method': 'subscriptions/listen',
                     'params': {'_meta': META_2026, 'notifications': {'toolsListChanged': True}}})
    return editor


def _children(pid: int) -> list:
    found = subprocess.run(['pgrep', '-P', str(pid)], capture_output=True, text=True).stdout
    return [int(p) for p in found.split() if _is_alive(int(p))]


def _set_control(mode: Optional[str]) -> None:
    control = os.environ['FAKE_CONTROL']
    if mode is None:
        if os.path.exists(control):
            os.remove(control)
        return
    with open(control, 'w') as f:
        f.write(mode)


def _stop(editor: Editor) -> None:
    if editor.process.poll() is None:
        editor.process.stdin.close()
        try:
            editor.process.wait(15)
        except subprocess.TimeoutExpired:
            editor.process.kill()


def test_a_cancelled_call_does_not_keep_a_replaced_worker() -> None:
    """The SDK never answers a request its peer cancelled. The front counted it as owed
    forever, so every reload after an interrupted call left the old worker running."""
    log = os.environ['FAKE_LOG']
    editor = _connected('2025')
    try:
        first = editor.call(1, 'whoami')['result']
        editor.send({'jsonrpc': '2.0', 'id': 2, 'method': 'slow', 'params': {'seconds': 30}})
        editor.send({'jsonrpc': '2.0', 'method': 'notifications/cancelled',
                     'params': {'requestId': 2}})
        _wait_for(lambda: f'{first["pid"]} 1 cancelled 2' in _log_lines(log), 5)
        _request_reload('c1')
        outcome = _reloaded(editor.process.pid, 'c1')
        check('a reload after a cancelled call goes ahead', outcome.get('ok') is True, outcome)
        check('and the worker that had the cancelled call is let go at once',
              _wait_for(lambda: f'{first["pid"]} 1 drained=True' in _log_lines(log), 5)
              is not None, _log_lines(log))
    finally:
        _stop(editor)


def test_a_worker_that_dies_taking_over_hands_back_to_the_old() -> None:
    """A source that dies on the subscription used to be reported as a good reload, while the
    old worker drained away and every call after it was refused until the editor restarted."""
    log = os.environ['FAKE_LOG']
    editor = _connected('2026')
    pid = editor.process.pid
    try:
        first = editor.call(1, 'whoami', {'_meta': META_2026})['result']
        _set_control('die-on-listen')
        _request_reload('d1')
        outcome = _reloaded(pid, 'd1')
        _set_control(None)
        check('a worker that dies while taking over is a failed reload',
              outcome.get('ok') is False and 'keeps serving' in outcome.get('error', ''), outcome)
        after = editor.call(2, 'whoami', {'_meta': META_2026})
        check('and the old worker goes on serving',
              after.get('result', {}).get('pid') == first['pid'], after)
        ended = editor.answer('listen:0', 5)
        check('the subscription ends with an error, so the editor listens again',
              'error' in ended, ended)
        editor.send({'jsonrpc': '2.0', 'id': 'listen:1', 'method': 'subscriptions/listen',
                     'params': {'_meta': META_2026, 'notifications': {'toolsListChanged': True}}})
        check('the old worker was never told to drain',
              not any(line.startswith(f'{first["pid"]} 1 drained') for line in _log_lines(log)))
        _request_reload('d2')
        outcome = _reloaded(pid, 'd2')
        check('and a later reload of a good source moves the new subscription',
              outcome.get('ok') is True, outcome)
        emitted = editor.call(3, 'emit', {'_meta': META_2026})['result']
        stream = [n for n in editor.notifications
                  if (n.get('params') or {}).get('_meta', {}).get('from') == emitted['generation']]
        check('which the editor then hears from the new worker',
              any(n['params']['_meta'].get(front.SUBSCRIPTION_ID_META_KEY) == 'listen:1'
                  for n in stream), editor.notifications[-5:])
    finally:
        _set_control(None)
        _stop(editor)


def test_a_crash_during_a_reload_leaves_one_worker() -> None:
    """The worker died while a reload was starting the next one. The crash handler then
    replaced the worker the reload had just adopted, which stayed behind with nobody using it."""
    editor = _connected('2025')
    pid = editor.process.pid
    try:
        editor.call(1, 'whoami')
        _set_control('slow-start')
        _request_reload('x1')
        time.sleep(1.5)
        _set_control(None)
        died = editor.call(2, 'die')
        check('the call that killed the worker is answered with an error', 'error' in died, died)
        outcome = _reloaded(pid, 'x1')
        check('the reload adopts its worker in place of the dead one',
              outcome.get('ok') is True, outcome)
        reply = None
        for attempt in range(30):
            reply = editor.call(100 + attempt, 'whoami')
            if 'result' in reply:
                break
            time.sleep(0.2)
        check('calls are served again', 'result' in (reply or {}), reply)
        time.sleep(2)
        check('and exactly one worker is left behind the front', len(_children(pid)) == 1,
              _children(pid))
    finally:
        _set_control(None)
        _stop(editor)


def test_calls_racing_a_reload_are_all_answered() -> None:
    """A call routed to a worker in the instant a reload released it got no answer until that
    worker finished draining - up to a day."""
    editor = _connected('2025')
    pid = editor.process.pid
    try:
        ids = list(range(1000, 1400))

        def spam() -> None:
            for request_id in ids:
                editor.send({'jsonrpc': '2.0', 'id': request_id, 'method': 'whoami',
                             'params': {}})
                time.sleep(0.005)

        sender = threading.Thread(target=spam)
        sender.start()
        time.sleep(0.3)
        _request_reload('s1')
        _request_reload_after = _reloaded(pid, 's1')
        sender.join()
        answered = {}
        try:
            for request_id in ids:
                answered[request_id] = editor.answer(request_id, 15)
        except TimeoutError:
            pass
        generations = {a.get('result', {}).get('generation') for a in answered.values()}
        check('every call sent while a reload ran is answered',
              len(answered) == len(ids) and all('result' in a for a in answered.values()),
              f'{len(answered)}/{len(ids)}')
        check('by both the old worker and the new one',
              _request_reload_after.get('ok') is True and {'1', '2'} <= generations, generations)
    finally:
        _stop(editor)


def test_the_handshake_that_worked_is_the_one_replayed() -> None:
    """An editor that fell back from one handshake to the other runs on the second. Replaying
    the first to a new worker would fail every reload of that session."""
    _set_control('no-initialize')
    editor = Editor(STATE_DIR + '/fake_worker.py')
    pid = editor.process.pid
    try:
        refused = editor.call(0, 'initialize', {'protocolVersion': '2025-06-18'})
        discovered = editor.call(1, 'server/discover', {'_meta': META_2026})
        check('the editor falls back from initialize to discover',
              'error' in refused and 'result' in discovered, (refused, discovered))
        _request_reload('h1')
        outcome = _reloaded(pid, 'h1')
        check('a reload replays the discover that worked', outcome.get('ok') is True, outcome)
    finally:
        _set_control(None)
        _stop(editor)


def test_odd_frames_do_not_break_the_relay() -> None:
    editor = _connected('2025')
    try:
        editor.send({'jsonrpc': '2.0', 'method': 'notifications/cancelled', 'params': [1, 2]})
        check('a notification with params that are not an object leaves the front up',
              'result' in editor.call(1, 'whoami'))

        editor.send({'jsonrpc': '2.0', 'id': 2, 'method': 'ask', 'params': {'odd': '\ud800'}})
        asked = editor.next_request('roots/list')
        check('a worker request carrying a lone surrogate still reaches the editor',
              asked.get('params', {}).get('odd') == '\ud800', asked)
        editor.send({'jsonrpc': '2.0', 'id': asked['id'], 'result': {}})
        editor.answer(2)

        editor.send({'jsonrpc': '2.0', 'id': 3, 'method': 'ask', 'params': {'give_up': True}})
        asked = editor.next_request('roots/list')
        deadline = time.time() + 5
        cancel = None
        while time.time() < deadline and cancel is None:
            message = editor.next_message(5)
            if message.get('method') == 'notifications/cancelled':
                cancel = message
        check("a worker's cancel names its request by the id the editor knows",
              cancel is not None and cancel['params']['requestId'] == asked['id'], cancel)
    finally:
        _stop(editor)


def test_a_replaced_server_finishes_what_it_carries() -> None:
    check('an outbox carrying nothing is idle', outbox.Outbox().is_idle())
    busy = outbox.Outbox()
    busy._pending['req_x'] = object()
    check('one with a delivery pending is not', not busy.is_idle())

    started = time.time()
    server._finish_carrying()
    check('a server nobody replaced exits at once', time.time() - started < 0.5)

    class Draining:
        def __init__(self) -> None:
            self.until = time.time() + 1.5

        def is_idle(self) -> bool:
            return time.time() >= self.until

    original = outbox.OUTBOX
    outbox.OUTBOX = Draining()
    server._drain_requested.set()
    try:
        started = time.time()
        server._finish_carrying()
        waited = time.time() - started
    finally:
        outbox.OUTBOX = original
        server._drain_requested.clear()
    check('a replaced server stays until its last delivery is done', 1.0 <= waited < 5, waited)


def _real_front() -> StdioServerParameters:
    return StdioServerParameters(command=sys.executable,
                                 args=['-B', '-m', 'cross_agent_mcp', '--reload'],
                                 env=dict(os.environ), cwd=ROOT_DIR)


def _text_of(result) -> str:
    for block in result.content:
        if getattr(block, 'type', None) == 'text':
            return block.text
    return ''


def _run_reload_command(pythonpath: str) -> subprocess.CompletedProcess:
    env = {**os.environ, 'PYTHONPATH': pythonpath}
    return subprocess.run([sys.executable, '-B', '-m', 'cross_agent_mcp', 'reload'],
                          capture_output=True, text=True, env=env, cwd=ROOT_DIR, timeout=180)


async def test_the_real_server_reloads_behind_the_front() -> None:
    async with stdio_client(_real_front()) as (read, write):
        async with ClientSession(read, write) as session:
            init = await session.initialize()
            check('the real server answers the handshake through the front',
                  init.server_info.name is not None and 'Bridge' in (init.instructions or ''))
            before = json.loads(_text_of(await session.call_tool('bridge_status', {})))
            process = before.get('server_process') or {}
            check('bridge_status says the server runs behind a front, generation 1',
                  process.get('front_pid') and process.get('worker_generation') == '1', process)
            check('and identity still comes from the parent chain, through the front',
                  before.get('running_under') == 'claude', before.get('running_under'))

            done = await asyncio.to_thread(_run_reload_command, ROOT_DIR + '/src')
            check('the reload command reports the front as reloaded',
                  done.returncode == 0 and '  ok ' in done.stdout, done.stdout + done.stderr)

            after = json.loads(_text_of(await session.call_tool('bridge_status', {})))
            process_after = after.get('server_process') or {}
            check('the next call is served by generation 2, a different process',
                  process_after.get('worker_generation') == '2'
                  and process_after.get('pid') != process.get('pid'), process_after)
            tools = await session.list_tools()
            check('and the tool list is served by it too',
                  'bridge_status' in [t.name for t in tools.tools])
            old_pid = process.get('pid')
            check('the old server exits once it carries nothing',
                  await asyncio.to_thread(_wait_for, lambda: not _is_alive(old_pid), 15)
                  is not None, old_pid)


def test_a_source_that_does_not_import_is_never_sent_to_the_fronts() -> None:
    broken = STATE_DIR + '/broken_src'
    shutil.copytree(ROOT_DIR + '/src/cross_agent_mcp', broken + '/cross_agent_mcp',
                    ignore=shutil.ignore_patterns('__pycache__'))
    with open(broken + '/cross_agent_mcp/server.py', 'a') as f:
        f.write('\nthis is not python\n')
    before = front._read_json(config.RELOAD_REQUEST_PATH)
    done = _run_reload_command(broken)
    check('a reload of a source that does not import is refused',
          done.returncode == 1 and 'reload refused' in done.stderr, done.stdout + done.stderr)
    check('and no front is asked to load it',
          front._read_json(config.RELOAD_REQUEST_PATH) == before)


def main() -> int:
    try:
        test_a_replaced_server_finishes_what_it_carries()
        test_the_front_with_a_stand_in_worker()
        test_a_2026_editor_keeps_its_subscription_across_a_reload()
        test_a_cancelled_call_does_not_keep_a_replaced_worker()
        test_a_worker_that_dies_taking_over_hands_back_to_the_old()
        test_a_crash_during_a_reload_leaves_one_worker()
        test_calls_racing_a_reload_are_all_answered()
        test_the_handshake_that_worked_is_the_one_replayed()
        test_odd_frames_do_not_break_the_relay()
        asyncio.run(test_the_real_server_reloads_behind_the_front())
        test_a_source_that_does_not_import_is_never_sent_to_the_fronts()
    finally:
        shutil.rmtree(STATE_DIR, ignore_errors=True)
    print(f'\n{"ALL FRONT CHECKS PASSED" if not FAILURES else str(len(FAILURES)) + " CHECK(S) FAILED"}')
    return 1 if FAILURES else 0


if __name__ == '__main__':
    sys.exit(main())
