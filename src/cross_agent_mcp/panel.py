"""Shared plumbing for the editor-panel shims.

Both agents run their live session inside a process the VS Code extension spawns and talks to
over stdio. A shim sits in that pipe, forwards every byte, and opens a side socket so the
bridge can hand a message to the session the user is actually looking at.

This module holds the parts that do not depend on which agent is being wrapped: the registry
that lets the bridge find the shim belonging to its own editor window, and the socket server.
"""

import contextlib
import json
import logging
import logging.handlers
import os
import re
import socket
import subprocess
import threading
import time
import uuid
from typing import Any, Dict, List, Optional

from . import config


logger = logging.getLogger('cross_agent_mcp.panel')

REGISTRY_DIR: str = config.HOME_DIR + 'panels/'

SOCKET_BACKLOG = 4
MAX_REQUEST_BYTES = 4_000_000
DEFAULT_INJECT_TIMEOUT = 600

# A turn that ended while nobody was connected is kept this long for the bridge to collect. The
# bridge reconnects within seconds; an hour covers a bridge that was itself restarted meanwhile.
RETAIN_FINISHED_SECONDS = 3600

# our injected requests use string ids in a private namespace, so they can never collide with
# the integer ids the extension hands out
INJECT_ID_PREFIX = 'xagent-'


def process_ancestry(pid: int, depth: int = 12) -> List[int]:
    """Parent pid chain, used to tell which editor window a process belongs to."""
    chain: List[int] = []
    current = pid
    for _ in range(depth):
        if current <= 1:
            break
        try:
            completed = subprocess.run(['ps', '-o', 'ppid=', '-p', str(current)],
                                       capture_output=True, text=True, timeout=5)
            parent = int(completed.stdout.strip())
        except Exception:
            break
        chain.append(parent)
        current = parent
    return chain


def split_wrapper_argv(argv: List[str]) -> tuple:
    """Separate the wrapped executable from the arguments meant for it.

    A wrapper is invoked as `<wrapper> <real-binary> <args...>`, and sometimes as
    `<wrapper> <node> <cli.js> <args...>` when the extension falls back to the JS entry point.
    """
    if not argv:
        return [], []

    first = argv[0]
    if os.path.isfile(first) and os.access(first, os.X_OK):
        if len(argv) > 1 and argv[1].endswith('.js') and os.path.isfile(argv[1]):
            return [first, argv[1]], argv[2:]
        return [first], argv[1:]
    return [], argv


def new_inject_id() -> str:
    return INJECT_ID_PREFIX + uuid.uuid4().hex[:12]


# The request id the bridge puts in every envelope and asks the peer to echo. A message that
# carries it back is the answer to that request, whatever else the shim's bookkeeping says.
REQUEST_TOKEN_PATTERN = re.compile(r'\breq_\d+_[0-9a-f]{6}\b')


def request_token_in(text: Optional[str]) -> Optional[str]:
    match = REQUEST_TOKEN_PATTERN.search(text or '')
    return match.group(0) if match else None


class SecureRotatingFileHandler(logging.handlers.RotatingFileHandler):
    """A rotating log whose every generation is owner-only, including after a rollover.

    Rotation opens the next file itself, so setting the mode once on the first one would
    leave every later generation at whatever the umask gives it.
    """

    def _open(self):
        flags = os.O_WRONLY | os.O_CREAT | (os.O_TRUNC if 'w' in self.mode else os.O_APPEND)
        fd = os.open(self.baseFilename, flags, config.FILE_MODE)
        try:
            # open(2)'s mode argument applies only when it creates the file, so a log that
            # already exists keeps whatever it had - which is how a log written before any of
            # this stayed 0644 while being appended to through a handler that looks secure.
            os.fchmod(fd, config.FILE_MODE)
        except OSError:
            os.close(fd)
            raise
        return os.fdopen(fd, self.mode, encoding=self.encoding)


def shim_logger(agent: str) -> logging.Logger:
    """A per-process log for the shim, so a turn it lost track of can be reconstructed later.

    The shim runs inside the editor's process tree with no terminal; without this, "the shim
    never reported the turn over" is a dead end. Stdout is the extension's channel and must
    stay clean, so only a file is used.
    """
    log = logging.getLogger(f'cross_agent_mcp.shim.{agent}')
    if log.handlers:
        return log
    log.setLevel(logging.INFO)
    log.propagate = False
    try:
        # ensure_dirs rather than secure_makedirs: it also runs the once-per-process repair,
        # and a shim is a separate process that otherwise never calls it - which left the
        # shim logs as the one part of the state tree an upgrade never reached.
        config.ensure_dirs()
        handler = SecureRotatingFileHandler(
            config.LOG_DIR + f'shim-{agent}.log', maxBytes=1_000_000, backupCount=2,
            encoding='utf-8')
        handler.setFormatter(logging.Formatter(
            '%(asctime)s - pid %(process)d - %(levelname)s - %(message)s'))
        log.addHandler(handler)
    except Exception:
        log.addHandler(logging.NullHandler())
    return log


class Turn:
    """One bridged message and the turn it started, from hand-over to the peer's last word.

    The turn outlives the socket request that started it. A request used to wait for the whole
    turn and give up at a deadline, and giving up was final: the shim dropped its record while
    the peer kept working, so the answer, when it came, had nowhere to go but the transcript.
    Now the record stays until the bridge collects it, and the bridge may come back for it as
    often as it likes.

    Three moments matter to a caller and are kept apart:
      accepted - the peer's process took the message; it will be answered, or at least seen
      done     - the turn ended, with a reply or an error
      neither  - the message may not have landed at all
    """

    def __init__(self, session_id: Optional[str], is_created: bool,
                 token: Optional[str] = None) -> None:
        self.injection_id = new_inject_id()
        self.session_id = session_id
        self.is_created = is_created
        self.turn_id: Optional[str] = None
        self.messages: List[str] = []
        self.result: Optional[str] = None
        self.error: Optional[str] = None
        self.is_accepted = False
        # the request id inside the message, and the peer's message that echoed it back - the
        # one piece of evidence that does not depend on matching the app server's turn ids
        self.token = token
        self.echo: Optional[str] = None
        self.started_at = time.time()
        self.finished_at: Optional[float] = None
        # `progress` fires on acceptance and on completion; `done` only on completion
        self.progress = threading.Event()
        self.done = threading.Event()

    def accept(self) -> None:
        self.is_accepted = True
        self.progress.set()

    def finish(self, error: Optional[str] = None) -> None:
        if error:
            self.error = error
        self.finished_at = time.time()
        self.done.set()
        self.progress.set()

    def wait(self, timeout: float, until_accepted: bool = False) -> None:
        """Block until the turn ends - or, if asked, until it is merely accepted."""
        if until_accepted:
            self.progress.wait(timeout)
        else:
            self.done.wait(timeout)

    def describe(self, reply: str, waited: float) -> Dict[str, Any]:
        base: Dict[str, Any] = {
            'injectionId': self.injection_id,
            'sessionId': self.session_id,
            'threadId': self.session_id,
            'turnId': self.turn_id,
            'wasCreated': self.is_created,
            'accepted': self.is_accepted,
        }
        if self.error:
            return {**base, 'ok': False, 'error': self.error}
        if not self.done.is_set():
            return {**base, 'ok': False, 'pending': True,
                    'error': f'turn still running after {round(waited)}s',
                    'partial': '\n'.join(self.messages)}
        return {**base, 'ok': True, 'reply': reply}


class PanelShim:
    """Base for a shim that wraps one live agent process."""

    agent: str = ''

    def __init__(self, command: List[str], argv: List[str]) -> None:
        self.command = command
        self.argv = argv
        self.process: Optional[subprocess.Popen] = None
        self.stdin_lock = threading.Lock()
        self.state_lock = threading.Lock()
        self.socket_path = REGISTRY_DIR + f'{self.agent}-{os.getpid()}.sock'
        self.registry_path = REGISTRY_DIR + f'{self.agent}-{os.getpid()}.json'
        # turns the bridge has not collected yet, by injection id
        self.turns: Dict[str, Turn] = {}

    # ------------------------------------------------------------- subclasses

    def status(self) -> Dict[str, Any]:
        raise NotImplementedError

    def inject(self, text: str, session_id: Optional[str], timeout: int,
               cwd: Optional[str] = None, title: Optional[str] = None,
               accept_timeout: Optional[int] = None,
               create_new: bool = False) -> Dict[str, Any]:
        """Deliver a message into the panel, opening a conversation if none is running.

        With `accept_timeout` the call returns as soon as the peer has taken the message, and
        the turn is left for `await_turn` to collect. Without it the call waits `timeout` for
        the turn to end, as it always did.

        `create_new` is the caller saying the message must start a *new* conversation. A shim
        that cannot open one refuses the request outright rather than writing the message into
        whatever conversation it happens to be driving: the caller asked for a conversation
        with no context in it, and delivering into an existing one gives it the opposite.
        """
        raise NotImplementedError

    def reply_of(self, turn: Turn) -> str:
        """The text a finished turn is answered with."""
        raise NotImplementedError

    # ------------------------------------------------------------ turn ledger

    def _keep(self, turn: Turn) -> None:
        with self.state_lock:
            self.turns[turn.injection_id] = turn

    def _forget_turn(self, injection_id: str) -> None:
        with self.state_lock:
            self.turns.pop(injection_id, None)

    def _prune_turns(self) -> None:
        cutoff = time.time() - RETAIN_FINISHED_SECONDS
        with self.state_lock:
            for injection_id in [i for i, t in self.turns.items()
                                 if t.finished_at is not None and t.finished_at < cutoff]:
                self.turns.pop(injection_id, None)

    def settle(self, turn: Turn) -> Dict[str, Any]:
        """Report a turn as it stands, keeping it collectable while it is still running."""
        result = turn.describe(self.reply_of(turn) if turn.done.is_set() else '',
                               time.time() - turn.started_at)
        if result.get('pending'):
            self._keep(turn)
        else:
            self._forget_turn(turn.injection_id)
        return result

    def await_turn(self, injection_id: Optional[str], timeout: int) -> Dict[str, Any]:
        """Wait for a turn a previous `send` left running, and hand back what it produced."""
        with self.state_lock:
            turn = self.turns.get(injection_id or '')
        if turn is None:
            return {'ok': False, 'accepted': None,
                    'error': f'no turn {injection_id} is pending in this panel process: it was '
                             'never started here, was already collected, or this panel process '
                             'restarted since'}
        turn.wait(timeout)
        return self.settle(turn)

    # --------------------------------------------------------------- registry

    def register(self) -> None:
        config.secure_makedirs(REGISTRY_DIR)
        record = {
            'agent': self.agent,
            'pid': os.getpid(),
            'socket': self.socket_path,
            'ancestors': process_ancestry(os.getpid()),
            'started_at': time.time(),
            'argv': self.argv,
        }
        with config.secure_open(self.registry_path) as f:
            json.dump(record, f)

    def unregister(self) -> None:
        for path in (self.registry_path, self.socket_path):
            with contextlib.suppress(OSError):
                os.remove(path)

    # ----------------------------------------------------------- side channel

    def write_to_child(self, payload: str) -> None:
        with self.stdin_lock:
            assert self.process and self.process.stdin
            self.process.stdin.write(payload)
            self.process.stdin.flush()

    def _handle_request(self, request: Dict[str, Any]) -> Dict[str, Any]:
        self._prune_turns()
        operation = request.get('op')
        if operation == 'status':
            return self.status()
        if operation == 'send':
            accept_timeout = request.get('acceptTimeout')
            return self.inject(
                str(request.get('text') or ''),
                request.get('sessionId') or request.get('threadId'),
                int(request.get('timeout') or DEFAULT_INJECT_TIMEOUT),
                request.get('cwd'),
                request.get('title'),
                int(accept_timeout) if accept_timeout is not None else None,
                bool(request.get('createNew')),
            )
        if operation == 'await':
            return self.await_turn(
                request.get('injectionId'),
                int(request.get('timeout') or DEFAULT_INJECT_TIMEOUT),
            )
        return {'ok': False, 'error': f'unknown op: {operation}'}

    def _serve_client(self, connection: socket.socket) -> None:
        try:
            buffer = b''
            while b'\n' not in buffer:
                chunk = connection.recv(65536)
                if not chunk:
                    return
                buffer += chunk
                if len(buffer) > MAX_REQUEST_BYTES:
                    return
            response = self._handle_request(json.loads(buffer.split(b'\n', 1)[0].decode('utf-8')))
        except Exception as e:
            response = {'ok': False, 'error': f'{type(e).__name__}: {e}'}

        with contextlib.suppress(Exception):
            connection.sendall((json.dumps(response, ensure_ascii=False) + '\n').encode('utf-8'))
        with contextlib.suppress(Exception):
            connection.close()

    def serve_socket(self) -> None:
        with contextlib.suppress(OSError):
            os.remove(self.socket_path)

        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        server.bind(self.socket_path)
        os.chmod(self.socket_path, 0o600)
        server.listen(SOCKET_BACKLOG)

        while True:
            try:
                connection, _ = server.accept()
            except Exception:
                return
            threading.Thread(target=self._serve_client, args=(connection,), daemon=True).start()

    def start_side_channel(self) -> None:
        """The side channel must never be able to take the passthrough down with it."""
        try:
            self.register()
            threading.Thread(target=self.serve_socket, daemon=True).start()
        except Exception as e:
            print(f'cross-agent shim: side channel disabled ({e})', file=__import__('sys').stderr)
