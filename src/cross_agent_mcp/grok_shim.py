"""Transparent stdio shim between the Grok Build VS Code extension and `grok agent stdio`.

The extension runs each panel session as

    grok agent [--reasoning-effort <level>] stdio

and speaks ACP to it: newline-delimited JSON-RPC 2.0, the extension sending `session/new`,
`session/load` and `session/prompt`, the agent answering with `session/update` notifications and
one response per prompt. A bridged message has the same shape as a prompt typed in the panel, so
this shim forwards every byte and writes one more `session/prompt` into the same pipe when the
bridge asks it to. The agent streams the turn on the shared stdout, so it appears in the panel.

Point the extension at this shim with the `grok.cliPath` setting. Unlike the Claude wrapper
setting the extension does not pass the real binary first: it runs `<cliPath> agent ... stdio`
directly, so the real one is found here (CROSS_AGENT_REAL_GROK, then Grok's own install).

Failure is always fail-open: unparsable traffic is still forwarded, and any setup problem
degrades to exec'ing the real binary.
"""

import contextlib
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from typing import Any, Dict, List, Optional, Set

from . import config
from .panel import PanelShim, Turn, shim_logger


# how long an injection waits for a user-initiated turn to finish before giving up
IDLE_WAIT_SECONDS = 120
IDLE_POLL_SECONDS = 0.2

# Grok's own words for what ended a turn that did not finish on its own
CANCELLED = 'cancelled'

TOOL_UPDATES = ('tool_call', 'tool_call_update')

# other subcommands of `grok agent` - a server, a relay, a shared leader - are not a panel
NON_PANEL_SUBCOMMANDS = ('serve', 'headless', 'leader')


def find_real_grok() -> Optional[str]:
    override = os.environ.get('CROSS_AGENT_REAL_GROK')
    if override and os.path.exists(override):
        return override

    ours = os.path.realpath(sys.argv[0])
    installed = config.GROK_HOME_DIR + 'bin/grok'
    for candidate in (installed, shutil.which('grok'), shutil.which(config.GROK_BIN)):
        if candidate and os.access(candidate, os.X_OK) and os.path.realpath(candidate) != ours:
            return candidate
    return None


def is_panel_invocation(args: List[str]) -> bool:
    """True only for the ACP session the extension drives the panel with: `agent ... stdio`."""
    if not args or args[0] != 'agent' or 'stdio' not in args[1:]:
        return False
    return not any(token in NON_PANEL_SUBCOMMANDS for token in args[1:])


class GrokTurn(Turn):
    """A bridged prompt, with what Grok has said of it so far.

    Grok narrates between tool calls, and the answer is what it says after the last one - the
    same cut `discovery._grok_turns` makes when it reads a transcript, so the two agree.
    """

    def __init__(self, session_id: Optional[str], is_created: bool) -> None:
        super().__init__(session_id, is_created)
        self.said = ''
        self.after_tools = ''


class GrokSession:
    """What the shim knows of one conversation the extension has open in this process."""

    def __init__(self, session_id: str, cwd: str, has_conversation: bool) -> None:
        self.session_id = session_id
        self.cwd = cwd
        # a session the extension opened with `session/new` is empty until somebody prompts it;
        # one it loaded already has a history
        self.has_conversation = has_conversation
        # the human's prompts the agent has not answered yet, by JSON-RPC id. More than one when
        # they typed ahead: Grok queues them, and the session is busy until the last is answered
        self.human_prompts: Set[Any] = set()
        self.last_seen = time.time()
        self.last_user_activity = 0.0
        self.awaiting_approval: Optional[Dict[str, Any]] = None
        self.injection: Optional[GrokTurn] = None

    @property
    def is_turn_active(self) -> bool:
        return bool(self.human_prompts) or self.injection is not None


class GrokAcpShim(PanelShim):

    agent = config.AGENT_GROK

    def __init__(self, command: List[str], args: List[str]) -> None:
        super().__init__(command + args, args)
        self.real_binary = command[0] if command else ''
        self.cwd: str = os.getcwd()
        self.sessions: Dict[str, GrokSession] = {}
        # requests the extension has made that are still unanswered: the cwd of a `session/new`,
        # and the session a `session/prompt` is running in, by JSON-RPC id
        self.pending_new: Dict[Any, str] = {}
        self.pending_prompts: Dict[Any, str] = {}
        # permission questions the agent has put to the extension, by JSON-RPC id
        self.pending_approvals: Dict[Any, str] = {}
        self.stdout_lock = threading.Lock()
        self.log = shim_logger(self.agent)

    # ------------------------------------------------------------ observation

    def _session(self, session_id: Any) -> Optional[GrokSession]:
        return self.sessions.get(session_id) if isinstance(session_id, str) else None

    def _observe_from_client(self, message: Dict[str, Any]) -> None:
        method = message.get('method')
        params = message.get('params') if isinstance(message.get('params'), dict) else {}
        request_id = message.get('id')

        if method == 'session/new' and request_id is not None:
            with self.state_lock:
                self.pending_new[request_id] = str(params.get('cwd') or self.cwd)
        elif method in ('session/load', 'session/resume') and isinstance(params.get('sessionId'), str):
            with self.state_lock:
                session = self.sessions.get(params['sessionId'])
                if session is None:
                    session = GrokSession(params['sessionId'],
                                          str(params.get('cwd') or self.cwd), True)
                    self.sessions[session.session_id] = session
                session.has_conversation = True
                session.last_seen = time.time()
        elif method == 'session/prompt' and request_id is not None:
            with self.state_lock:
                session = self._session(params.get('sessionId'))
                if session is not None:
                    session.human_prompts.add(request_id)
                    session.has_conversation = True
                    session.last_seen = time.time()
                    session.last_user_activity = session.last_seen
                    self.pending_prompts[request_id] = session.session_id
        elif method is None and request_id is not None:
            # a response of the extension's: the human answered (or the extension withdrew) a
            # permission prompt the agent had put to it
            with self.state_lock:
                answered = self.pending_approvals.pop(request_id, None)
                session = self._session(answered)
                if session is not None:
                    session.awaiting_approval = None
            if answered is not None:
                self.log.info(f'approval answered: request_id={request_id}')

    def _observe_from_agent(self, message: Dict[str, Any]) -> bool:
        """Watch one line the agent wrote. False means it is ours and must not reach the panel."""
        method = message.get('method')
        params = message.get('params') if isinstance(message.get('params'), dict) else {}
        message_id = message.get('id')

        if method is None and message_id is not None:
            return self._observe_response(message)

        session_id = params.get('sessionId')
        if method == 'session/request_permission' and message_id is not None:
            with self.state_lock:
                session = self._session(session_id)
                if session is not None:
                    tool = ((params.get('toolCall') or {}).get('title')
                            if isinstance(params.get('toolCall'), dict) else None)
                    session.awaiting_approval = {
                        'request_id': message_id, 'kind': 'permission', 'tool': tool,
                        'since': time.time()}
                    self.pending_approvals[message_id] = session.session_id
            self.log.info(f'approval requested: request_id={message_id} session={session_id}')
        elif method == 'session/update':
            return self._observe_update(session_id, params.get('update'))
        elif method == '_x.ai/session/prompt_complete':
            self.log.info(f'prompt complete: session={session_id} '
                          f'stop={params.get("stopReason")}')
        return True

    def _observe_response(self, message: Dict[str, Any]) -> bool:
        message_id = message.get('id')
        result = message.get('result') if isinstance(message.get('result'), dict) else {}

        with self.state_lock:
            new_cwd = self.pending_new.pop(message_id, None)
            prompt_session = self.pending_prompts.pop(message_id, None)
            injected = next((s for s in self.sessions.values()
                             if s.injection is not None
                             and s.injection.injection_id == message_id), None)

        if new_cwd is not None and isinstance(result.get('sessionId'), str):
            with self.state_lock:
                self.sessions.setdefault(
                    result['sessionId'], GrokSession(result['sessionId'], new_cwd, False))
            self.log.info(f'session opened: {result["sessionId"]} cwd={new_cwd}')

        if prompt_session is not None:
            with self.state_lock:
                session = self._session(prompt_session)
                if session is not None:
                    session.human_prompts.discard(message_id)
                    if not session.is_turn_active:
                        session.awaiting_approval = None

        if injected is not None:
            self._finish_injection(injected, message)
            return False
        return True

    def _observe_update(self, session_id: Any, update: Any) -> bool:
        """Watch one `session/update`. False means the panel is better off not seeing it."""
        if not isinstance(update, dict):
            return True
        with self.state_lock:
            session = self._session(session_id)
            turn = session.injection if session is not None else None
            # a turn is ours only while the human has nothing of their own running in the
            # session: what Grok streams then could be either, and is left alone
            is_ours = turn is not None and not session.human_prompts
            if session is not None:
                session.last_seen = time.time()
        if turn is None or turn.done.is_set():
            return True

        kind = update.get('sessionUpdate')
        if kind == 'agent_message_chunk':
            content = update.get('content')
            piece = content.get('text') if isinstance(content, dict) else None
            if isinstance(piece, str):
                turn.said += piece
                turn.after_tools += piece
        elif kind in TOOL_UPDATES:
            turn.after_tools = ''
        elif kind == 'agent_thought_chunk' and is_ours:
            # The panel opens a "Thinking" block for these and closes it when *its own* prompt
            # is answered. A bridged turn is not one of its prompts, so the block would spin
            # for good.
            return False
        return True

    def _finish_injection(self, session: GrokSession, response: Dict[str, Any]) -> None:
        turn = session.injection
        if turn is None:
            return
        error: Optional[str] = None
        if isinstance(response.get('error'), dict):
            error = str(response['error'].get('message') or 'grok reported an error')[:500]
        else:
            stop = (response.get('result') or {}).get('stopReason')
            if stop == CANCELLED:
                error = ('the turn was cancelled before it finished - a tool call was '
                         'declined, or the human pressed stop')
        with self.state_lock:
            session.injection = None
            if not session.is_turn_active:
                session.awaiting_approval = None
        turn.session_id = session.session_id
        turn.result = (turn.after_tools or turn.said).strip()
        turn.finish(error)
        self.log.info(f'turn finished: injection={turn.injection_id} chars={len(turn.result)} '
                      f'error={bool(error)}')

    # --------------------------------------------------------------- plumbing

    def write_to_child(self, payload: Any) -> None:
        data = payload if isinstance(payload, bytes) else payload.encode('utf-8')
        with self.stdin_lock:
            assert self.process and self.process.stdin
            self.process.stdin.write(data)
            self.process.stdin.flush()

    def _write_to_client(self, data: bytes) -> None:
        with self.stdout_lock:
            sys.stdout.buffer.write(data)
            sys.stdout.buffer.flush()

    def _pump_client_to_agent(self) -> None:
        try:
            for raw in iter(sys.stdin.buffer.readline, b''):
                try:
                    self._observe_from_client(json.loads(raw.decode('utf-8', 'replace')))
                except Exception:
                    pass
                self.write_to_child(raw)
        except Exception:
            pass
        finally:
            with contextlib.suppress(Exception):
                assert self.process and self.process.stdin
                self.process.stdin.close()

    def _pump_agent_to_client(self) -> None:
        assert self.process and self.process.stdout
        try:
            for raw in iter(self.process.stdout.readline, b''):
                forward = True
                try:
                    forward = self._observe_from_agent(json.loads(raw.decode('utf-8', 'replace')))
                except Exception:
                    pass
                if forward:
                    self._write_to_client(raw)
        except Exception:
            pass

    # -------------------------------------------------------- side channel ops

    def _describe(self, session: GrokSession) -> Dict[str, Any]:
        approval = dict(session.awaiting_approval) if session.awaiting_approval else None
        if approval:
            approval['waiting_seconds'] = round(time.time() - approval.get('since', 0))
        return {'session_id': session.session_id, 'thread_id': session.session_id,
                'cwd': session.cwd, 'last_seen': session.last_seen,
                'last_user_activity': session.last_user_activity,
                'is_turn_active': session.is_turn_active,
                'has_conversation': session.has_conversation,
                'awaiting_approval': approval}

    def status(self) -> Dict[str, Any]:
        with self.state_lock:
            sessions = [self._describe(s) for s in self.sessions.values()]
            activity = max((s.last_user_activity for s in self.sessions.values()), default=0.0)
            # A session the extension has opened and nobody has prompted is a conversation that
            # does not exist yet: writing into it starts one, and the panel renders it. The
            # shim cannot open another - that is the extension's list to keep, and a session
            # made here would be one the panel never heard of.
            can_create = any(not s.has_conversation for s in self.sessions.values())
        return {'ok': True, 'agent': self.agent, 'pid': os.getpid(), 'sessions': sessions,
                'threads': sessions, 'last_user_activity': activity,
                'can_create_session': can_create,
                'argv': self.argv, 'real_binary': self.real_binary}

    def reply_of(self, turn: Turn) -> str:
        return turn.result or ''

    def _pick_session(self, session_id: Optional[str],
                      create_new: bool) -> Optional[GrokSession]:
        """The session a message goes to. Caller holds `state_lock`."""
        if session_id:
            return self.sessions.get(session_id)
        candidates = [s for s in self.sessions.values()
                      if not (create_new and s.has_conversation)]
        return max(candidates, key=lambda s: (s.last_user_activity, s.last_seen),
                   default=None)

    def _wait_for_idle(self, session_id: Optional[str], deadline: float) -> bool:
        while time.time() < deadline:
            with self.state_lock:
                session = self._pick_session(session_id, False)
                if session is not None and not session.is_turn_active \
                        and session.injection is None:
                    return True
            time.sleep(IDLE_POLL_SECONDS)
        return False

    def inject(self, text: str, session_id: Optional[str], timeout: int,
               cwd: Optional[str] = None, title: Optional[str] = None,
               accept_timeout: Optional[int] = None,
               create_new: bool = False) -> Dict[str, Any]:
        with self.state_lock:
            known = self._pick_session(session_id, create_new)
            driven = list(self.sessions)
        if known is None:
            if session_id:
                return {'ok': False, 'accepted': False,
                        'error': f'this panel drives session(s) {", ".join(driven) or "none yet"}, '
                                 f'not {session_id}'}
            if create_new and driven:
                return {'ok': False, 'accepted': False,
                        'error': f'this panel already drives session {", ".join(driven)} and '
                                 'cannot open a new conversation; nothing was written to it'}
            return {'ok': False, 'accepted': False,
                    'error': 'this panel has no session to write into'}
        if create_new and known.has_conversation:
            return {'ok': False, 'accepted': False,
                    'error': f'this panel already drives session {known.session_id} and cannot '
                             'open a new conversation; nothing was written to it'}

        # Grok takes prompts one at a time per session; injecting mid-turn would make us
        # collect somebody else's reply
        idle_wait = min(IDLE_WAIT_SECONDS,
                        accept_timeout if accept_timeout is not None else timeout)
        if not self._wait_for_idle(known.session_id, time.time() + idle_wait):
            return {'ok': False, 'accepted': False,
                    'error': 'the panel session is busy with another turn'}

        with self.state_lock:
            session = self.sessions.get(known.session_id)
            if session is None:
                return {'ok': False, 'accepted': False,
                        'error': f'session {known.session_id} closed while the message waited'}
            if session.injection is not None:
                return {'ok': False, 'accepted': False,
                        'error': 'another bridged message is already in flight'}
            # what was checked before the wait may no longer hold: the human can have started
            # this conversation while the message waited
            if create_new and session.has_conversation:
                return {'ok': False, 'accepted': False,
                        'error': f'this panel started session {session.session_id} while the '
                                 'message waited, and cannot open a new conversation; nothing '
                                 'was written to it'}
            turn = GrokTurn(session.session_id, not session.has_conversation)
            session.has_conversation = True
            session.injection = turn

        request = json.dumps({
            'jsonrpc': '2.0', 'id': turn.injection_id, 'method': 'session/prompt',
            'params': {'sessionId': session.session_id,
                       'prompt': [{'type': 'text', 'text': text}]},
        }, ensure_ascii=False) + '\n'

        try:
            self._announce_to_panel(session.session_id, text)
            self.write_to_child(request)
        except Exception as e:
            with self.state_lock:
                session.injection = None
            return {'ok': False, 'accepted': False,
                    'error': f'failed to write to the grok process: {e}'}

        # ACP has no acknowledgement for a prompt; the write going through is the moment the
        # message is in the agent's hands
        turn.accept()

        if accept_timeout is None:
            turn.wait(timeout)
        return self.settle(turn)

    def _announce_to_panel(self, session_id: str, text: str) -> None:
        """Show the injected message in the panel, so an answer is not left with no question.

        The extension draws the messages it sends itself and throws away the agent's echo of a
        prompt (`user_message_chunk`) unless it is replaying a history, so a user bubble cannot
        be made from here. What it does draw is the agent speaking, so the message is shown as
        a labelled quote at the head of the reply, in a fence so that nothing in it is read as
        markdown, and set off from what Grok then says.
        """
        longest_run = max((len(run) for run in re.findall(r'`+', text)), default=0)
        fence = '`' * max(3, longest_run + 1)
        quoted = (f'**Message relayed by the cross-agent bridge**\n\n{fence}\n{text}\n{fence}'
                  '\n\n---\n\n')
        notice = json.dumps({
            'jsonrpc': '2.0', 'method': 'session/update',
            'params': {'sessionId': session_id, 'update': {
                'sessionUpdate': 'agent_message_chunk',
                'content': {'type': 'text', 'text': quoted}}},
        }, ensure_ascii=False) + '\n'
        self._write_to_client(notice.encode('utf-8'))

    # -------------------------------------------------------------------- run

    def run(self) -> int:
        self.process = subprocess.Popen(
            self.command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=None)
        self.start_side_channel()

        reader = threading.Thread(target=self._pump_agent_to_client, daemon=True)
        reader.start()
        self._pump_client_to_agent()

        code = self.process.wait()
        reader.join(timeout=2)
        self.unregister()
        return code


def main() -> int:
    args = sys.argv[1:]

    real = find_real_grok()
    if not real:
        print('cross-agent shim: could not locate the real grok binary; '
              'set CROSS_AGENT_REAL_GROK', file=sys.stderr)
        return 127
    command = [real]

    if not is_panel_invocation(args):
        os.execv(command[0], command + args)

    try:
        return GrokAcpShim(command, args).run()
    except Exception as e:
        print(f'cross-agent shim: falling back to direct exec ({e})', file=sys.stderr)
        os.execv(command[0], command + args)
        return 1


if __name__ == '__main__':
    sys.exit(main())
