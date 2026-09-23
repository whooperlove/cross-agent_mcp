"""Prove the Claude wrapper shim delivers into a live panel session.

    PYTHONPATH=src .venv/bin/python tests/claude_shim_roundtrip.py

Impersonates the Claude Code extension: launches `claude-shim.sh <real-binary> <panel args>`,
drives one ordinary turn over stream-json the way the panel does, then asks the shim's side
socket to inject a bridged message. What matters is that the injected turn's output reaches
the *extension* stream, because that is what the panel renders.

Spends four real Claude turns. Touches no VS Code settings.
"""

import json
import os
import socket
import subprocess
import sys
import threading
import time

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT_DIR + '/src')

from cross_agent_mcp import claude_shim, uihook  # noqa: E402


FAILURES = []

PANEL_ARGS = [
    '--output-format', 'stream-json', '--verbose', '--input-format', 'stream-json',
    '--setting-sources=user', '--permission-mode', 'acceptEdits',
]


def check(label: str, condition: bool, detail: str = '') -> None:
    if condition:
        print(f'[ok] {label}')
    else:
        print(f'[FAIL] {label} {detail}')
        FAILURES.append(label)


class FakeExtension:
    """Drives the shim over stdio the way the Claude Code extension does."""

    def __init__(self, real_binary: str) -> None:
        self.proc = subprocess.Popen(
            [ROOT_DIR + '/claude-shim.sh', real_binary] + PANEL_ARGS,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            text=True, bufsize=1, cwd=ROOT_DIR,
        )
        self.inbox = []
        self.lock = threading.Lock()
        threading.Thread(target=self._read, daemon=True).start()

    def _read(self) -> None:
        for line in self.proc.stdout:
            line = line.strip()
            if not line:
                continue
            try:
                message = json.loads(line)
            except Exception:
                continue
            with self.lock:
                self.inbox.append(message)

    def send_user(self, text: str) -> None:
        self.proc.stdin.write(json.dumps({
            'type': 'user',
            'message': {'role': 'user', 'content': [{'type': 'text', 'text': text}]},
        }) + '\n')
        self.proc.stdin.flush()

    def wait_for_result(self, since: int, timeout: float = 180.0):
        deadline = time.time() + timeout
        while time.time() < deadline:
            with self.lock:
                for message in self.inbox[since:]:
                    if message.get('type') == 'result':
                        return message
            time.sleep(0.1)
        return None

    def count(self) -> int:
        with self.lock:
            return len(self.inbox)

    def messages(self, kind: str, since: int = 0) -> list:
        with self.lock:
            return [m for m in self.inbox[since:] if m.get('type') == kind]

    def stop(self) -> None:
        try:
            self.proc.stdin.close()
        except Exception:
            pass
        try:
            self.proc.wait(timeout=10)
        except Exception:
            self.proc.kill()


def socket_request(path: str, payload: dict, timeout: float) -> dict:
    connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    connection.settimeout(timeout)
    try:
        connection.connect(path)
        connection.sendall((json.dumps(payload) + '\n').encode('utf-8'))
        buffer = b''
        while b'\n' not in buffer:
            chunk = connection.recv(65536)
            if not chunk:
                break
            buffer += chunk
        return json.loads(buffer.split(b'\n', 1)[0].decode('utf-8'))
    finally:
        connection.close()


def main() -> int:
    real_binary = claude_shim.find_real_claude()
    if not real_binary:
        print('[FAIL] could not locate the real claude binary')
        return 1
    print(f'       wrapping: {real_binary}')

    extension = FakeExtension(real_binary)
    try:
        extension.send_user('Reply with exactly: EXT_OK')
        result = extension.wait_for_result(0)
        check('shim forwards an ordinary panel turn',
              bool(result) and 'EXT_OK' in str(result.get('result')), str(result)[:200])

        session_id = (result or {}).get('session_id')
        check('session id observed from the stream', bool(session_id), str(session_id))
        print(f'       panel session: {session_id}')

        shims = [s for s in uihook.list_shims('claude') if s['pid'] == extension.proc.pid]
        check('shim registered itself', bool(shims), str(uihook.list_shims('claude'))[:200])
        if not shims:
            return 1
        socket_path = shims[0]['socket']

        status = socket_request(socket_path, {'op': 'status'}, 5)
        seen = [s.get('session_id') for s in status.get('sessions', [])]
        check('shim reports the panel session', session_id in seen, str(seen))

        before = extension.count()
        response = socket_request(
            socket_path,
            {'op': 'send', 'text': 'Reply with exactly: PANEL_OK', 'timeout': 240},
            260)
        print(f'       inject -> {json.dumps(response, ensure_ascii=False)[:220]}')
        check('injected turn returned a reply',
              response.get('ok') and 'PANEL_OK' in (response.get('reply') or ''), str(response)[:250])
        check('reply came from the panel session', response.get('sessionId') == session_id)

        assistants = [m for m in extension.messages('assistant', before)]
        check('extension stream received the assistant message (panel would render it)',
              bool(assistants), f'{len(assistants)} assistant messages after injection')

        results = extension.messages('result', before)
        check('extension stream received the turn result', bool(results), str(len(results)))

        # the two-phase hand-over: a receipt at acceptance, the answer on await
        receipt = socket_request(
            socket_path,
            {'op': 'send', 'text': 'Reply with exactly: AWAIT_OK', 'timeout': 240,
             'acceptTimeout': 30},
            60)
        print(f'       receipt -> {json.dumps(receipt, ensure_ascii=False)[:220]}')
        check('a send with acceptTimeout returns a receipt as soon as the CLI has the message',
              receipt.get('pending') is True and receipt.get('accepted') is True
              and bool(receipt.get('injectionId')), str(receipt)[:250])

        answer = socket_request(
            socket_path,
            {'op': 'await', 'injectionId': receipt.get('injectionId'), 'timeout': 240},
            260)
        print(f'       await -> {json.dumps(answer, ensure_ascii=False)[:220]}')
        check('await collects the answer of the turn the receipt was for',
              answer.get('ok') and 'AWAIT_OK' in (answer.get('reply') or ''), str(answer)[:250])
        check('the answer came from the same panel session',
              answer.get('sessionId') == session_id, str(answer.get('sessionId')))
    finally:
        extension.stop()

    check_idle_panel_opens_a_session(real_binary)
    check_fresh_conversation_as_the_bridge_asks(real_binary)

    print('\n' + ('ALL CLAUDE SHIM CHECKS PASSED' if not FAILURES
                  else f'{len(FAILURES)} CHECK(S) FAILED'))
    return 1 if FAILURES else 0


def check_idle_panel_opens_a_session(real_binary: str) -> None:
    """A panel that has not been talked to yet must still get a visible conversation."""
    print('\n--- panel with no conversation yet ---')
    extension = FakeExtension(real_binary)
    try:
        time.sleep(0.5)
        shims = [s for s in uihook.list_shims('claude') if s['pid'] == extension.proc.pid]
        if not shims:
            check('idle panel: shim registered itself', False, str(uihook.list_shims('claude'))[:150])
            return
        socket_path = shims[0]['socket']

        status = socket_request(socket_path, {'op': 'status'}, 5)
        check('idle panel: no session is open yet', not status.get('sessions'), str(status)[:150])

        response = socket_request(
            socket_path,
            {'op': 'send', 'text': 'Reply with exactly: OPENED_OK',
             'cwd': ROOT_DIR, 'timeout': 240},
            260)
        print(f'       inject -> {json.dumps(response, ensure_ascii=False)[:220]}')
        check('idle panel: a session was opened for the message',
              response.get('ok') and response.get('wasCreated') is True, str(response)[:250])
        check('idle panel: the reply came back',
              'OPENED_OK' in (response.get('reply') or ''), str(response.get('reply'))[:120])
        check('idle panel: the new session id is reported',
              bool(response.get('sessionId')), str(response.get('sessionId')))
        check('idle panel: extension stream received the assistant message',
              bool(extension.messages('assistant')), str(len(extension.messages('assistant'))))
    finally:
        extension.stop()


def check_fresh_conversation_as_the_bridge_asks(real_binary: str) -> None:
    """new_session=true as the bridge sends it: a receipt first, the answer on await."""
    from cross_agent_mcp import bridge

    print('\n--- a fresh conversation, asked for the way the bridge asks ---')
    extension = FakeExtension(real_binary)
    try:
        # long enough for startup hooks to run, which name the session before anyone types
        time.sleep(3)
        shims = [s for s in uihook.list_shims('claude') if s['pid'] == extension.proc.pid]
        if not shims:
            check('fresh: shim registered itself', False, str(uihook.list_shims('claude'))[:150])
            return
        socket_path = shims[0]['socket']

        status = socket_request(socket_path, {'op': 'status'}, 5)
        check('fresh: a panel with no conversation offers to host one',
              status.get('can_create_session') is True, str(status)[:200])

        receipt = socket_request(
            socket_path,
            {'op': 'send', 'text': 'Reply with exactly: FRESH_OK', 'cwd': ROOT_DIR,
             'timeout': 240, 'acceptTimeout': 30, 'createNew': True},
            60)
        print(f'       receipt -> {json.dumps(receipt, ensure_ascii=False)[:220]}')
        check('fresh: the message is taken as opening a conversation',
              receipt.get('accepted') is True and receipt.get('wasCreated') is True,
              str(receipt)[:250])
        try:
            bridge._raise_unless_really_new(receipt, set())
            check('fresh: the bridge does not refuse the hand-over', True)
        except bridge.BridgeError as e:
            check('fresh: the bridge does not refuse the hand-over', False, str(e))

        answer = socket_request(
            socket_path,
            {'op': 'await', 'injectionId': receipt.get('injectionId'), 'timeout': 240},
            260)
        print(f'       await -> {json.dumps(answer, ensure_ascii=False)[:220]}')
        check('fresh: the answer comes back under the id the CLI gave the conversation',
              answer.get('ok') and 'FRESH_OK' in (answer.get('reply') or '')
              and bool(answer.get('sessionId')), str(answer)[:250])
        check('fresh: a hand-over that named a session named that same one',
              not receipt.get('sessionId') or receipt.get('sessionId') == answer.get('sessionId'),
              f'{receipt.get("sessionId")} vs {answer.get("sessionId")}')
        check('fresh: and the bridge accepts it as new',
              bridge._raise_unless_really_new(answer, set()) is True)

        status = socket_request(socket_path, {'op': 'status'}, 5)
        check('fresh: once it has one, the panel no longer offers to host another',
              status.get('can_create_session') is False, str(status)[:200])

        before = extension.count()
        refused = socket_request(
            socket_path,
            {'op': 'send', 'text': 'Reply with exactly: SHOULD_NOT_RUN', 'cwd': ROOT_DIR,
             'timeout': 240, 'acceptTimeout': 30, 'createNew': True},
            60)
        check('fresh: a second fresh conversation is refused there',
              refused.get('accepted') is False, str(refused)[:200])
        time.sleep(3)
        written = extension.messages('assistant', before)
        check('fresh: and nothing was written into the first', not written, str(written)[:200])
    finally:
        extension.stop()


if __name__ == '__main__':
    sys.exit(main())
