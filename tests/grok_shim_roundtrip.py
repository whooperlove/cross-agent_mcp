"""Prove the Grok shim delivers into a live panel session.

    PYTHONPATH=src .venv/bin/python tests/grok_shim_roundtrip.py

Impersonates the Grok Build extension: launches `grok-shim.sh agent stdio`, opens a session and
drives one ordinary turn over ACP the way the panel does, then hands a bridged message to the
shim's side socket. What matters is that the injected turn's output reaches the *extension*
stream, because that is what the panel renders, and that the answer the bridge collects is the
one Grok gave after its last tool call.

Spends two real Grok turns. Touches no VS Code settings, and keeps the bridge's own state in a
throwaway directory - so the shim it starts is never mistaken for a panel of the user's.
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT_DIR + '/src')

# A unix socket path is limited to about a hundred bytes, and the platform's temp directory can
# use most of them. Set before the import: config reads it at import time.
STATE_DIR = tempfile.mkdtemp(prefix='ca-', dir='/tmp')
os.environ['CROSS_AGENT_HOME'] = STATE_DIR + '/bridge'
for _inherited in ('CROSS_AGENT_CONVERSATION_ID', 'CROSS_AGENT_HOP', 'CROSS_AGENT_SENDER',
                   'CROSS_AGENT_BUSY', 'CROSS_AGENT_SELF_SESSION', 'GROK_SESSION_ID'):
    os.environ.pop(_inherited, None)

from cross_agent_mcp import bridge, config, uihook  # noqa: E402


FAILURES = []

TOKEN = 'req_1790763000000_a1b2c3'


def check(label: str, condition: bool, detail: str = '') -> None:
    if condition:
        print(f'[ok] {label}')
    else:
        print(f'[FAIL] {label} {detail}')
        FAILURES.append(label)


class FakeExtension:
    """Drives the shim over stdio the way the Grok Build extension does."""

    def __init__(self, work_dir: str) -> None:
        env = {**os.environ, 'CROSS_AGENT_REAL_GROK': config.GROK_BIN}
        self.proc = subprocess.Popen(
            [ROOT_DIR + '/grok-shim.sh', 'agent', '--always-approve', 'stdio'],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            text=True, bufsize=1, cwd=work_dir, env=env)
        self.work_dir = work_dir
        self.next_id = 1
        self.lock = threading.Lock()
        self.inbox = []
        self.responses = {}
        threading.Thread(target=self._read, daemon=True).start()

    def _read(self) -> None:
        for line in self.proc.stdout:
            try:
                message = json.loads(line)
            except Exception:
                continue
            with self.lock:
                if 'method' not in message and 'id' in message:
                    self.responses[message['id']] = message
                elif 'method' in message and 'id' in message:
                    # a question for the extension: decline what it does not implement
                    self._write({'jsonrpc': '2.0', 'id': message['id'], 'result': {
                        'outcome': {'outcome': 'cancelled'}}})
                else:
                    self.inbox.append(message)

    def _write(self, message: dict) -> None:
        self.proc.stdin.write(json.dumps(message, ensure_ascii=False) + '\n')
        self.proc.stdin.flush()

    def request(self, method: str, params: dict, timeout: float = 240.0) -> dict:
        with self.lock:
            request_id = self.next_id
            self.next_id += 1
        self._write({'jsonrpc': '2.0', 'id': request_id, 'method': method, 'params': params})
        deadline = time.time() + timeout
        while time.time() < deadline:
            with self.lock:
                if request_id in self.responses:
                    return self.responses[request_id]
            time.sleep(0.05)
        raise TimeoutError(method)

    def updates(self, session_id: str, kind: str, since: int = 0) -> list:
        with self.lock:
            batch = list(self.inbox[since:])
        return [m['params']['update'] for m in batch
                if m.get('method') == 'session/update'
                and m['params'].get('sessionId') == session_id
                and m['params'].get('update', {}).get('sessionUpdate') == kind]

    def close(self) -> None:
        with_suppressed = (OSError, ValueError)
        try:
            self.proc.stdin.close()
        except with_suppressed:
            pass
        try:
            self.proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self.proc.kill()


def main() -> int:
    work_dir = STATE_DIR + '/work'
    os.makedirs(work_dir)
    extension = FakeExtension(work_dir)
    try:
        initialised = extension.request('initialize', {
            'protocolVersion': 1, 'clientCapabilities': {
                'fs': {'readTextFile': False, 'writeTextFile': False}, 'terminal': False}})
        check('the shim passes the handshake through', 'result' in initialised, str(initialised))

        session_id = extension.request('session/new', {'cwd': work_dir, 'mcpServers': []})[
            'result']['sessionId']
        time.sleep(1)

        live = uihook.find_live_session(config.AGENT_GROK, session_id)
        check('the shim registers, and reports the session the panel opened',
              live is not None and live['has_conversation'] is False, str(live))
        check('an empty session is one a new conversation can go into',
              uihook.find_panel_host(config.AGENT_GROK) is not None)

        human = extension.request('session/prompt', {'sessionId': session_id, 'prompt': [
            {'type': 'text', 'text': 'Reply with exactly the word HUMAN-OK and nothing else.'}]})
        check('the human\'s own turn goes through untouched',
              human.get('result', {}).get('stopReason') == 'end_turn', str(human))
        live = uihook.find_live_session(config.AGENT_GROK, session_id)
        check('and the session now has a conversation, so it cannot host a new one',
              live['has_conversation'] is True and uihook.find_panel_host(config.AGENT_GROK) is None,
              str(live))

        seen_before = len(extension.inbox)
        result = bridge._call_grok(
            'Reply with exactly the word BRIDGE-OK, then write this on its own final line: '
            + TOKEN, session_id, work_dir, {}, 180, ui_shim=live['shim'], title=None,
            target_agent=config.AGENT_GROK, request_token=TOKEN, wants_result=True, patience=180)

        check('a bridged message is answered through the panel, in the same session',
              result['session_id'] == session_id and 'BRIDGE-OK' in result['reply'],
              str(result))
        check('and the answer echoes the request id, which is how it is matched',
              TOKEN in result['reply'], result['reply'])

        spoken = ''.join(u['content']['text']
                         for u in extension.updates(session_id, 'agent_message_chunk', seen_before))
        check('the extension\'s own stream carries the relayed message and Grok\'s answer',
              'relayed by the cross-agent bridge' in spoken and 'BRIDGE-OK' in spoken, spoken[:300])
        check('no "Thinking" block is opened for a turn the panel did not start',
              extension.updates(session_id, 'agent_thought_chunk', seen_before) == [])
        check('and the shim\'s own request never reaches the extension as a response',
              not any(str(key).startswith('xagent-') for key in extension.responses))
    finally:
        extension.close()

    return 1 if FAILURES else 0


if __name__ == '__main__':
    try:
        code = main()
    finally:
        shutil.rmtree(STATE_DIR, ignore_errors=True)

    print(f'\n{"ALL CHECKS PASSED" if not code else str(len(FAILURES)) + " CHECK(S) FAILED"}')
    sys.exit(code)
