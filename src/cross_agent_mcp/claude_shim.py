"""Transparent stdio shim between the Claude Code VS Code extension and the Claude CLI.

The extension runs the panel session as

    claude --input-format stream-json --output-format stream-json --resume=<session-id> ...

and feeds user turns in as newline-delimited stream-json on stdin. That is exactly the shape
a bridged message needs, so this shim forwards every byte and writes an extra user message
into the same pipe when the bridge asks it to. The CLI answers on the shared stdout, so the
reply lands in the panel.

Point the extension at this shim with the `claudeCode.claudeProcessWrapper` setting. The
extension invokes a wrapper as `<wrapper> <real-claude-binary> <args...>`.

Failure is always fail-open: unparsable traffic is still forwarded, and any setup problem
degrades to exec'ing the real binary.
"""

import contextlib
import glob
import json
import os
import shutil
import subprocess
import sys
import threading
import time
from typing import Any, Dict, List, Optional

from . import config
from .panel import PanelShim, Turn, shim_logger, split_wrapper_argv


# how long an injection waits for a user-initiated turn to finish before giving up
IDLE_WAIT_SECONDS = 120
IDLE_POLL_SECONDS = 0.2

# control requests the CLI raises over stdout that stop the turn until a human answers
APPROVAL_SUBTYPES = ('can_use_tool', 'request_user_dialog')


def find_real_claude() -> Optional[str]:
    override = os.environ.get('CROSS_AGENT_REAL_CLAUDE')
    if override and os.path.exists(override):
        return override

    candidates = glob.glob(os.path.expanduser(
        '~/.vscode/extensions/anthropic.claude-code-*/resources/native-binary/claude'))
    candidates += glob.glob(os.path.expanduser(
        '~/.vscode-insiders/extensions/anthropic.claude-code-*/resources/native-binary/claude'))
    if candidates:
        return sorted(candidates)[-1]

    found = shutil.which('claude')
    return found if found and os.path.realpath(found) != os.path.realpath(sys.argv[0]) else None


def is_panel_invocation(args: List[str]) -> bool:
    """True only for the stream-json session the extension drives the panel with."""
    joined = ' '.join(args)
    return '--input-format' in joined and 'stream-json' in joined


def session_id_from_args(args: List[str]) -> Optional[str]:
    for index, token in enumerate(args):
        if token.startswith('--resume='):
            return token.split('=', 1)[1] or None
        if token == '--resume' and index + 1 < len(args):
            return args[index + 1]
    return None


class ClaudeStreamShim(PanelShim):

    agent = config.AGENT_CLAUDE

    def __init__(self, command: List[str], args: List[str]) -> None:
        super().__init__(command + args, args)
        self.real_binary = command[0] if command else ''
        self.session_id: Optional[str] = session_id_from_args(args)
        self.cwd: str = os.getcwd()
        self.is_turn_active = False
        # the bridged turn the CLI is running right now; cleared by the CLI's own `result`
        self.injection: Optional[Turn] = None
        self.last_seen = time.time()
        # only the human typing in the panel updates this; injected turns must not, or the
        # bridge would keep reinforcing whichever tab it last wrote to
        self.last_user_activity = 0.0
        # A permission prompt the CLI has raised and the human has not answered. While it is
        # up the turn is not working; it is waiting for a click, and from the transcript the
        # two look the same. The CLI asks over stdout and the extension answers over stdin,
        # and both pass through here.
        self.awaiting_approval: Optional[Dict[str, Any]] = None
        self.log = shim_logger(self.agent)

    # ------------------------------------------------------------ observation

    def _observe_from_client(self, message: Dict[str, Any]) -> None:
        """A user turn from the panel: nothing may be injected until it finishes."""
        kind = message.get('type')
        if kind == 'user':
            with self.state_lock:
                self.is_turn_active = True
                self.last_seen = time.time()
                self.last_user_activity = self.last_seen
        elif kind in ('control_response', 'control_cancel_request'):
            # the human answered (or the extension withdrew) the prompt
            response = message.get('response') if isinstance(message.get('response'), dict) else {}
            request_id = response.get('request_id') or message.get('request_id')
            with self.state_lock:
                pending = self.awaiting_approval
                if pending and (request_id is None or pending.get('request_id') == request_id):
                    self.awaiting_approval = None
            if pending:
                self.log.info(f'approval answered: request_id={request_id} kind={pending.get("kind")}')

    def _observe_from_agent(self, message: Dict[str, Any]) -> None:
        kind = message.get('type')

        if isinstance(message.get('session_id'), str):
            with self.state_lock:
                self.session_id = message['session_id']
                self.last_seen = time.time()
        if isinstance(message.get('cwd'), str):
            self.cwd = message['cwd']

        if kind == 'control_request':
            request = message.get('request') if isinstance(message.get('request'), dict) else {}
            subtype = request.get('subtype')
            if subtype in APPROVAL_SUBTYPES:
                with self.state_lock:
                    self.awaiting_approval = {
                        'request_id': message.get('request_id'),
                        'kind': subtype,
                        'tool': request.get('tool_name'),
                        'since': time.time(),
                    }
                self.log.info(f'approval requested: request_id={message.get("request_id")} '
                              f'kind={subtype} tool={request.get("tool_name")}')
            return

        with self.state_lock:
            injection = self.injection

        if kind == 'assistant' and injection and not injection.done.is_set():
            text = _text_of(message.get('message'))
            if text:
                injection.messages.append(text)
        elif kind == 'result':
            with self.state_lock:
                self.is_turn_active = False
                self.awaiting_approval = None
                # the turn is over, so the slot is free whether or not anyone is listening
                if injection is self.injection:
                    self.injection = None
            if injection and not injection.done.is_set():
                injection.session_id = self.session_id
                injection.result = str(message.get('result') or '')
                error = (str(message.get('result') or 'claude reported an error')[:500]
                         if message.get('is_error') else None)
                injection.finish(error)
                self.log.info(f'turn finished: injection={injection.injection_id} '
                              f'chars={len(injection.result)} error={bool(error)}')

    # --------------------------------------------------------------- plumbing

    def _pump_client_to_agent(self) -> None:
        try:
            for line in sys.stdin:
                try:
                    self._observe_from_client(json.loads(line))
                except Exception:
                    pass
                self.write_to_child(line)
        except Exception:
            pass
        finally:
            with contextlib.suppress(Exception):
                assert self.process and self.process.stdin
                self.process.stdin.close()

    def _pump_agent_to_client(self) -> None:
        assert self.process and self.process.stdout
        try:
            for line in self.process.stdout:
                try:
                    self._observe_from_agent(json.loads(line))
                except Exception:
                    pass
                sys.stdout.write(line)
                sys.stdout.flush()
        except Exception:
            pass

    # -------------------------------------------------------- side channel ops

    def status(self) -> Dict[str, Any]:
        with self.state_lock:
            approval = dict(self.awaiting_approval) if self.awaiting_approval else None
            if approval:
                approval['waiting_seconds'] = round(time.time() - approval.get('since', 0))
            can_create = self.session_id is None
            sessions = ([{'session_id': self.session_id, 'thread_id': self.session_id,
                          'cwd': self.cwd, 'last_seen': self.last_seen,
                          'last_user_activity': self.last_user_activity,
                          'is_turn_active': self.is_turn_active,
                          'awaiting_approval': approval}]
                        if self.session_id else [])
            activity = self.last_user_activity
        # The extension owns the conversation list; this shim can only write into the one
        # stdin it was started with. So a panel already driving a session cannot open another,
        # and saying so here is what stops the bridge from choosing it for a fresh one.
        return {'ok': True, 'agent': self.agent, 'pid': os.getpid(), 'sessions': sessions,
                'threads': sessions, 'last_user_activity': activity,
                'can_create_session': can_create,
                'argv': self.argv, 'real_binary': self.real_binary}

    def _wait_for_idle(self, deadline: float) -> bool:
        while time.time() < deadline:
            with self.state_lock:
                if not self.is_turn_active and self.injection is None:
                    return True
            time.sleep(IDLE_POLL_SECONDS)
        return False

    def reply_of(self, turn: Turn) -> str:
        return turn.result or (turn.messages[-1] if turn.messages else '')

    def inject(self, text: str, session_id: Optional[str], timeout: int,
               cwd: Optional[str] = None, title: Optional[str] = None,
               accept_timeout: Optional[int] = None,
               create_new: bool = False) -> Dict[str, Any]:
        with self.state_lock:
            current = self.session_id
        if session_id and session_id != current:
            return {'ok': False, 'accepted': False,
                    'error': f'this panel drives session {current}, not {session_id}'}

        # A panel sitting on its conversation list has a process but no conversation yet.
        # Writing the message anyway makes the CLI open one, and the panel renders it.
        is_created = current is None

        # Asked for a fresh conversation while already driving one, the only thing this shim
        # could do is write into that one - which is the opposite of what was asked for, and
        # it used to do exactly that, because a null session id skipped the check above.
        if create_new and not is_created:
            return {'ok': False, 'accepted': False,
                    'error': f'this panel already drives session {current} and cannot open a '
                             'new conversation; nothing was written to it'}

        # the CLI serialises turns; injecting mid-turn would make us collect the wrong reply
        idle_wait = min(IDLE_WAIT_SECONDS, accept_timeout if accept_timeout is not None else timeout)
        if not self._wait_for_idle(time.time() + idle_wait):
            return {'ok': False, 'accepted': False,
                    'error': 'the panel session is busy with another turn'}

        turn = Turn(current, is_created)
        with self.state_lock:
            if self.injection is not None:
                return {'ok': False, 'accepted': False,
                        'error': 'another bridged message is already in flight'}
            self.injection = turn
            self.is_turn_active = True

        payload = json.dumps({
            'type': 'user',
            'message': {'role': 'user', 'content': [{'type': 'text', 'text': text}]},
        }, ensure_ascii=False) + '\n'

        try:
            self.write_to_child(payload)
        except Exception as e:
            with self.state_lock:
                self.injection = None
                self.is_turn_active = False
            return {'ok': False, 'accepted': False,
                    'error': f'failed to write to the claude process: {e}'}

        # The CLI reads its stdin as a queue of user turns and never acknowledges one; the
        # write going through is the moment the message is in the peer's hands.
        turn.accept()

        # With an acceptance deadline the caller wants the receipt now and the answer later;
        # without one it is an older caller, waiting for the whole turn as before.
        if accept_timeout is None:
            turn.wait(timeout)
        with self.state_lock:
            if turn.session_id is None:
                turn.session_id = self.session_id
        return self.settle(turn)

    # -------------------------------------------------------------------- run

    def run(self) -> int:
        self.process = subprocess.Popen(
            self.command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=None,
            text=True, bufsize=1,
        )
        self.start_side_channel()

        reader = threading.Thread(target=self._pump_agent_to_client, daemon=True)
        reader.start()
        self._pump_client_to_agent()

        code = self.process.wait()
        reader.join(timeout=2)
        self.unregister()
        return code


def _text_of(message: Any) -> str:
    if not isinstance(message, dict):
        return ''
    content = message.get('content')
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = [b.get('text', '') for b in content
                 if isinstance(b, dict) and b.get('type') == 'text']
        return ' '.join(p for p in parts if p).strip()
    return ''


def main() -> int:
    argv = sys.argv[1:]
    command, args = split_wrapper_argv(argv)

    if not command:
        found = find_real_claude()
        if not found:
            print('cross-agent shim: could not locate the real claude binary; '
                  'set CROSS_AGENT_REAL_CLAUDE', file=sys.stderr)
            return 127
        command = [found]

    if not is_panel_invocation(args):
        os.execv(command[0], command + args)

    try:
        return ClaudeStreamShim(command, args).run()
    except Exception as e:
        print(f'cross-agent shim: falling back to direct exec ({e})', file=sys.stderr)
        os.execv(command[0], command + args)
        return 1


if __name__ == '__main__':
    sys.exit(main())
