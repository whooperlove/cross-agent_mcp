"""A front that keeps the editor's MCP connection while the server behind it is replaced.

Every editor session starts its own server over stdio, and Python reads its source once, when
it starts. A change to the bridge therefore reached a session only when the editor restarted
that server - a window reload, which also orphaned whatever the server was carrying.

With `--reload` the process the editor starts is this front. It knows nothing of the bridge:
it relays newline-delimited JSON-RPC between the editor and a worker, which is the ordinary
server (`python -m cross_agent_mcp`) started as its child. `run-server.sh reload` asks every
front to start a fresh worker. The front replays the editor's handshake to it, and only once
that worker has answered does new traffic go to it; a worker that fails to start leaves the
old one serving. The old worker answers the calls it already has, is told to drain, and exits
after its last delivery - so a reload never cuts off a message in flight.

Editors speak two versions of MCP here. Codex opens with `initialize`; Claude Code speaks the
2026-07-28 protocol, which has no handshake - it asks `server/discover` and keeps a
`subscriptions/listen` request open for the whole session as the channel server events come
back on. The front replays whichever handshake the first worker accepted, and moves every open
subscription to the new worker under the editor's own request id, so the editor keeps one
stream throughout.

The worker runs one process below where the server used to, with the editor's environment and
working directory. Caller identity is read from the parent chain by executable name, and this
front is a plain Python process, so the chain still leads to the same editor.

This file is loaded once per editor session. A change to it still needs the editor to restart
the server, so it stays small and imports nothing of the bridge itself.
"""

import contextlib
import json
import logging
import os
import signal
import subprocess
import sys
import threading
import time
from typing import Any, Dict, List, Optional, Tuple

from . import config, panel


logger = logging.getLogger('cross_agent_mcp.front')

RELOAD_POLL_SECONDS = 1.0
# how long a new worker may take to answer the replayed handshake
WORKER_START_TIMEOUT_SECONDS = 60
# how long the editor closing waits for the current worker to exit on its own
WORKER_EXIT_GRACE_SECONDS = 10
# A worker that keeps dying is not restarted forever: more deaths than this within the window,
# or this many replacements in a row that never start, and the front exits like a dead server.
CRASH_LIMIT = 3
CRASH_WINDOW_SECONDS = 60
# how long `reload` waits for the running fronts to report
REPORT_WAIT_SECONDS = 90

INIT_ID_PREFIX = 'cross-agent-front-init-'
SERVER_REQUEST_ID_PREFIX = 'cross-agent-front-'
HANDSHAKE_METHODS = ('initialize', 'server/discover')

# every frame of a `subscriptions/listen` stream carries the listen request's id under this key
SUBSCRIPTION_ID_META_KEY = 'io.modelcontextprotocol/subscriptionId'
SUBSCRIPTION_ACK = 'notifications/subscriptions/acknowledged'
# how long a moved subscription may take to be acknowledged before the reload reports
LISTEN_ACK_TIMEOUT_SECONDS = 10

# the server a worker runs; tests put a stand-in here
WORKER_COMMAND: List[str] = [sys.executable, '-m', 'cross_agent_mcp']


def _key(request_id: Any) -> str:
    """A JSON-RPC id as a dict key. Ids are numbers or strings, and 1 and "1" are two ids."""
    return json.dumps(request_id)


def _parse(line: bytes) -> Optional[Dict[str, Any]]:
    try:
        message = json.loads(line)
    except ValueError:
        return None
    return message if isinstance(message, dict) else None


def _dump(message: Dict[str, Any]) -> bytes:
    try:
        text = json.dumps(message, ensure_ascii=False, separators=(',', ':'))
        return text.encode('utf-8') + b'\n'
    except UnicodeEncodeError:
        # a lone surrogate arrived as an escape and has no UTF-8 form; keep it an escape
        return json.dumps(message, separators=(',', ':')).encode('ascii') + b'\n'


def _params(message: Dict[str, Any]) -> Dict[str, Any]:
    params = message.get('params')
    return params if isinstance(params, dict) else {}


def _subscription_of(message: Dict[str, Any]) -> Any:
    meta = _params(message).get('_meta')
    return meta.get(SUBSCRIPTION_ID_META_KEY) if isinstance(meta, dict) else None


def _read_json(path: str) -> Optional[Dict[str, Any]]:
    try:
        with open(path, encoding='utf-8') as f:
            value = json.load(f)
    except (OSError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def _write_json(path: str, value: Dict[str, Any]) -> None:
    """Write beside the target and rename: a reader sees the old file or the new one."""
    tmp = f'{path}.{os.getpid()}.tmp'
    try:
        with config.secure_open(tmp) as f:
            json.dump(value, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.remove(tmp)
        raise


def _is_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _advertise_tool_list_changes(result: Dict[str, Any]) -> None:
    """Tell the editor the tool list can change: after a reload it may, and the front says so."""
    capabilities = result.get('capabilities')
    if not isinstance(capabilities, dict):
        return
    tools = capabilities.get('tools')
    if isinstance(tools, dict):
        tools['listChanged'] = True


class Worker:
    """One server process behind the front."""

    def __init__(self, generation: int) -> None:
        self.generation = generation
        env = dict(os.environ)
        env[config.ENV_FRONT_PID] = str(os.getpid())
        env[config.ENV_WORKER_GENERATION] = str(generation)
        # stderr is inherited, so the editor keeps reading the server's log lines as before
        self.process = subprocess.Popen(
            list(WORKER_COMMAND), stdin=subprocess.PIPE, stdout=subprocess.PIPE, env=env)
        self.write_lock = threading.Lock()
        # Everything below is guarded by the front's lock.
        # the editor's requests this worker has not answered yet, by _key
        self.open_requests: set = set()
        # editor requests moved to another worker: what this one still says about them drops
        self.handed_over: set = set()
        # subscriptions moved here, by _key: their acknowledgement is the front's to take
        self.acks: Dict[str, threading.Event] = {}
        self.is_draining = False
        self.is_released = False
        self.has_exited = False
        self.init_answered = threading.Event()
        self.init_reply: Optional[Dict[str, Any]] = None

    @property
    def pid(self) -> int:
        return self.process.pid

    def send(self, line: bytes) -> bool:
        with self.write_lock:
            try:
                self.process.stdin.write(line if line.endswith(b'\n') else line + b'\n')
                self.process.stdin.flush()
                return True
            except (OSError, ValueError):
                return False

    def close_stdin(self) -> None:
        with self.write_lock:
            with contextlib.suppress(OSError, ValueError):
                self.process.stdin.close()


class Front:
    """Relays the editor's connection to whichever worker is current.

    The current worker is never released, and a worker is released only once it holds no
    open request. Both are decided under `lock`, together with every request handed to a
    worker - which is what keeps a request from landing on a worker that is going away.
    """

    def __init__(self) -> None:
        self.lock = threading.RLock()
        self.output_lock = threading.Lock()
        self.reload_lock = threading.Lock()
        self.status_lock = threading.Lock()
        self.is_status_removed = False
        self.generation = 0
        self.current: Optional[Worker] = None
        self.workers: List[Worker] = []
        # The editor's handshake requests still waiting for their answer, by _key, and the one
        # whose answer succeeded last: that is the one this connection runs on, and the one a
        # new worker is given.
        self.handshakes: Dict[str, Dict[str, Any]] = {}
        self.handshake: Optional[Dict[str, Any]] = None
        self.initialized: Optional[Dict[str, Any]] = None
        # the editor's open `subscriptions/listen` requests, by _key, moved along every reload
        self.listens: Dict[str, Dict[str, Any]] = {}
        # Requests a worker sent the editor, renamed to ids of the front's own: two workers
        # both number theirs from zero, and the answer has to find the one that asked.
        self.server_requests: Dict[str, Tuple[Worker, Any]] = {}
        self.next_server_request = 0
        self.crashes: List[float] = []
        self.is_closing = False
        # a request made before this front started is already in the code it is running
        self.seen_request_id = (_read_json(config.RELOAD_REQUEST_PATH) or {}).get('id')
        self.last_reload: Optional[Dict[str, Any]] = None
        self.started_at = time.time()
        try:
            self.cwd: Optional[str] = os.getcwd()
        except OSError:
            self.cwd = None
        self.status_path = f'{config.FRONT_DIR}{os.getpid()}.json'

    # ------------------------------------------------------------------ running

    def serve(self) -> None:
        worker = self._start_worker()
        with self.lock:
            self.current = worker
        self._write_status()
        threading.Thread(target=self._watch_reload_requests, name='front:reload',
                         daemon=True).start()
        for line in sys.stdin.buffer:
            if not line.strip():
                continue
            try:
                self._from_editor(line)
            except Exception as e:
                logger.exception(f'serve [exception]: {e}')
                self._refuse(_parse(line), f'the front failed to relay it: {e}')
        logger.info('serve [END]: the editor closed the connection')
        self._close()

    def _start_worker(self) -> Worker:
        with self.lock:
            self.generation += 1
            worker = Worker(self.generation)
            self.workers.append(worker)
        threading.Thread(target=self._from_worker, args=(worker,),
                         name=f'front:worker-{worker.generation}', daemon=True).start()
        logger.info(f'_start_worker: generation {worker.generation} pid={worker.pid}')
        return worker

    def _close(self) -> None:
        """The editor is done: let the current worker exit the way a server always has.

        A worker a reload released is left to finish: it was already told to, and it holds
        the busy locks of what it carries under its own live pid.
        """
        with self.lock:
            self.is_closing = True
            current = self.current
        if current is not None:
            current.close_stdin()
            try:
                current.process.wait(WORKER_EXIT_GRACE_SECONDS)
            except subprocess.TimeoutExpired:
                logger.info(f'_close: worker pid={current.pid} is still exiting; left to finish')
        self.remove_status()

    def on_signal(self, signum: int, frame: Any) -> None:
        """Stopped by the editor: stop every worker too, as a stopped server stopped itself."""
        with self.lock:
            self.is_closing = True
            workers = list(self.workers)
        logger.info(f'on_signal: {signum}, stopping {[w.pid for w in workers]}')
        for worker in workers:
            with contextlib.suppress(OSError):
                worker.process.send_signal(signal.SIGTERM)
        raise SystemExit(128 + signum)

    # ------------------------------------------------------------ editor -> worker

    def _from_editor(self, line: bytes) -> None:
        message = _parse(line)
        if message is None:
            # not a single JSON object: the worker answers it as the server always did
            self._send_request(line, None)
            return
        method = message.get('method')
        if method is not None and 'id' in message:
            self._send_request(line, message)
        elif method is not None:
            self._send_notification(line, message)
        else:
            self._answer_to_worker(message)

    def _send_request(self, line: bytes, message: Optional[Dict[str, Any]]) -> None:
        """Hand a request to the current worker, or refuse it when there is none to take it.

        The worker is chosen and the request recorded on it under one lock, so a reload that
        swaps workers in between cannot release the one this request is going to.
        """
        request_id = message.get('id') if message is not None else None
        with self.lock:
            worker = self.current
            is_open = worker is not None and not worker.has_exited
            if is_open and message is not None:
                key = _key(request_id)
                worker.open_requests.add(key)
                method = message.get('method')
                if method in HANDSHAKE_METHODS:
                    self.handshakes[key] = message
                elif method == 'subscriptions/listen':
                    self.listens[key] = message
        if is_open:
            # a failed write means the worker is gone; its exit answers what it held
            worker.send(line)
            return
        self._refuse(message, 'the cross-agent server is restarting after it exited; call '
                              'again in a few seconds')

    def _send_notification(self, line: bytes, message: Dict[str, Any]) -> None:
        method = message.get('method')
        owner = None
        with self.lock:
            if method == 'notifications/initialized':
                self.initialized = message
            target = self.current
            if method == 'notifications/cancelled':
                key = _key(_params(message).get('requestId'))
                owner = next((w for w in self.workers if key in w.open_requests), None)
                if owner is not None:
                    # the SDK never answers a request its peer cancelled: nothing is owed now
                    owner.open_requests.discard(key)
                    target = owner
                self.listens.pop(key, None)
                self.handshakes.pop(key, None)
        if target is not None:
            target.send(line)
        if owner is not None:
            self._release_if_quiet(owner)

    def _answer_to_worker(self, message: Dict[str, Any]) -> None:
        """The editor answering a request a worker sent it."""
        with self.lock:
            entry = self.server_requests.pop(_key(message.get('id')), None)
        if entry is None:
            logger.info(f'_answer_to_worker [dropped]: unknown id {message.get("id")!r}')
            return
        worker, original_id = entry
        message['id'] = original_id
        worker.send(_dump(message))
        self._release_if_quiet(worker)

    def _refuse(self, message: Optional[Dict[str, Any]], reason: str) -> None:
        if message is None or 'id' not in message or message.get('method') is None:
            return
        self._to_editor(_dump({'jsonrpc': '2.0', 'id': message['id'],
                               'error': {'code': -32603, 'message': reason}}))

    # ------------------------------------------------------------ worker -> editor

    def _from_worker(self, worker: Worker) -> None:
        try:
            for line in worker.process.stdout:
                try:
                    self._from_worker_line(worker, line)
                except Exception as e:
                    # one frame the front cannot handle must not end the relay
                    logger.exception(f'_from_worker [exception]: pid={worker.pid} {e}')
        finally:
            self._worker_exited(worker)

    def _from_worker_line(self, worker: Worker, line: bytes) -> None:
        message = _parse(line)
        if message is None:
            self._to_editor(line)
            return
        method = message.get('method')
        if method is None:
            self._answer_from(worker, message, line)
        elif 'id' in message:
            with self.lock:
                self.next_server_request += 1
                front_id = f'{SERVER_REQUEST_ID_PREFIX}{self.next_server_request}'
                self.server_requests[_key(front_id)] = (worker, message['id'])
            message['id'] = front_id
            self._to_editor(_dump(message))
        else:
            self._notification_from(worker, message, line)

    def _notification_from(self, worker: Worker, message: Dict[str, Any], line: bytes) -> None:
        subscription = _subscription_of(message)
        if subscription is not None:
            key = _key(subscription)
            with self.lock:
                if key in worker.handed_over:
                    # a stream the editor now gets from another worker
                    return
                event = worker.acks.pop(key, None) if message.get('method') == SUBSCRIPTION_ACK \
                    else None
            if event is not None:
                # the editor was acknowledged once, when it opened the stream
                event.set()
                return
        if message.get('method') == 'notifications/cancelled':
            # the worker giving up on a request it sent the editor, which knows it by our id
            params = _params(message)
            original = _key(params.get('requestId'))
            with self.lock:
                front_key = next((k for k, (w, o) in self.server_requests.items()
                                  if w is worker and _key(o) == original), None)
                if front_key is not None:
                    del self.server_requests[front_key]
            if front_key is not None:
                params['requestId'] = json.loads(front_key)
                self._to_editor(_dump(message))
                self._release_if_quiet(worker)
                return
        self._to_editor(line)

    def _answer_from(self, worker: Worker, message: Dict[str, Any], line: bytes) -> None:
        request_id = message.get('id')
        if isinstance(request_id, str) and request_id.startswith(INIT_ID_PREFIX):
            # the answer to a replayed handshake is the front's own, never the editor's
            worker.init_reply = message
            worker.init_answered.set()
            return
        key = _key(request_id)
        is_success = isinstance(message.get('result'), dict)
        with self.lock:
            if key in worker.handed_over:
                # the end of a request another worker now serves; the editor's is still open
                worker.handed_over.discard(key)
                return
            worker.open_requests.discard(key)
            handshake = self.handshakes.pop(key, None)
            if handshake is not None and is_success:
                self.handshake = handshake
            # an answer to a subscription is its end: nothing left to move
            self.listens.pop(key, None)
        if handshake is not None and is_success:
            _advertise_tool_list_changes(message['result'])
            line = _dump(message)
        self._to_editor(line)
        self._release_if_quiet(worker)

    def _to_editor(self, line: bytes) -> None:
        with self.output_lock:
            try:
                sys.stdout.buffer.write(line if line.endswith(b'\n') else line + b'\n')
                sys.stdout.buffer.flush()
            except (OSError, ValueError) as e:
                logger.info(f'_to_editor [failed]: {e}')

    # ------------------------------------------------------------------ reloading

    def _watch_reload_requests(self) -> None:
        last_mtime = None
        while True:
            time.sleep(RELOAD_POLL_SECONDS)
            try:
                mtime = os.stat(config.RELOAD_REQUEST_PATH).st_mtime_ns
            except OSError:
                continue
            if mtime == last_mtime:
                continue
            last_mtime = mtime
            request_id = (_read_json(config.RELOAD_REQUEST_PATH) or {}).get('id')
            if not request_id or request_id == self.seen_request_id:
                continue
            self.seen_request_id = request_id
            with self.lock:
                if self.is_closing:
                    return
            try:
                self.reload(request_id)
            except Exception as e:
                logger.exception(f'_watch_reload_requests [exception]: {request_id} {e}')

    def reload(self, request_id: str) -> Dict[str, Any]:
        """Start a worker on the current source and move the editor onto it.

        The new worker becomes current first and the subscriptions move after, so one the
        editor opens meanwhile goes straight to the new worker instead of pinning the old.
        """
        started = time.time()
        with self.reload_lock:
            outcome = self._reload()
            outcome.update(request_id=request_id, at=time.time(),
                           seconds=round(time.time() - started, 2))
            self.last_reload = outcome
        logger.info(f'reload: {outcome}')
        self._write_status()
        return outcome

    def _reload(self) -> Dict[str, Any]:
        if self.handshake is None:
            return {'ok': None, 'note': 'the editor has not connected yet; nothing to replace'}
        worker = self._start_worker()
        error = self._handshake(worker)
        with self.lock:
            if error is None and worker.has_exited:
                error = f'it exited with code {worker.process.returncode} right after answering'
            is_closing = self.is_closing
            old = self.current
            is_adopted = error is None and not is_closing
            if is_adopted:
                self.current = worker
                if old is not None:
                    old.is_draining = True
        if not is_adopted:
            self._discard(worker)
            if is_closing:
                return {'ok': None, 'note': 'the editor closed the connection'}
            return {'ok': False, 'error': (f'the new server (pid {worker.pid}) failed: {error}; '
                                           'the old one keeps serving')}

        # The old worker is let go only once the new one has taken the subscriptions and is
        # still alive: a source that dies on them goes back to the old worker, not to a crash.
        self._move_listens(worker, release_holders=False)
        with self.lock:
            is_rolled_back = worker.has_exited and self.current is worker and old is not None
            if is_rolled_back:
                self.current = old
                old.is_draining = False
        if is_rolled_back:
            # the subscriptions ended with the new worker; the editor listens again, on the old
            return {'ok': False, 'error': (f'the new server (pid {worker.pid}) exited while '
                                           'taking over; the old one keeps serving')}
        if old is not None:
            self._release_if_quiet(old)
        self._announce_tool_list_change()
        return {'ok': True, 'worker_pid': worker.pid, 'generation': worker.generation,
                'replaced_pid': old.pid if old is not None else None}

    def _handshake(self, worker: Worker) -> Optional[str]:
        """Replay the editor's handshake to a new worker. Returns why it failed, or None.

        `initialize` sets up a session the worker needs. `server/discover` sets up nothing -
        every 2026-07-28 request carries its own protocol version - but it is the editor's own
        question, so a worker that answers it is a worker the editor can use.
        """
        with self.lock:
            handshake = self.handshake or {}
            initialized = self.initialized
        method = handshake.get('method') or 'initialize'
        request = {'jsonrpc': '2.0', 'id': f'{INIT_ID_PREFIX}{worker.generation}',
                   'method': method, 'params': _params(handshake)}
        if not worker.send(_dump(request)):
            return 'it could not be written to'
        if not worker.init_answered.wait(WORKER_START_TIMEOUT_SECONDS):
            return f'it did not answer {method} within {WORKER_START_TIMEOUT_SECONDS}s'
        reply = worker.init_reply
        if reply is None:
            return f'it exited with code {worker.process.poll()} before answering {method}'
        if not isinstance(reply.get('result'), dict):
            return f'it refused {method}: {json.dumps(reply.get("error"))[:300]}'
        if method == 'initialize':
            notification = initialized or {'jsonrpc': '2.0', 'method': 'notifications/initialized'}
            if not worker.send(_dump(notification)):
                return 'it closed before the handshake finished'
        return None

    def _move_listens(self, target: Worker, release_holders: bool = True) -> None:
        """Bring every open subscription to `target`, under the editor's own ids.

        Taken from its holder and given to the target in one step, so a subscription is never
        open on two workers at once: the holder's stream is cancelled and what it still sends
        is dropped, and the target's acknowledgement is swallowed - the editor was
        acknowledged when it opened the stream. A holder the subscription was all that kept is
        released, unless the caller still needs it.
        """
        moves = []
        with self.lock:
            # a worker already gone would never answer them; its replacement takes them instead
            if target.has_exited:
                return
            for key, listen in self.listens.items():
                holder = next((w for w in self.workers
                               if w is not target and key in w.open_requests), None)
                if holder is None:
                    continue
                holder.open_requests.discard(key)
                holder.handed_over.add(key)
                target.open_requests.add(key)
                target.acks[key] = threading.Event()
                moves.append((holder, listen, target.acks[key]))
        for holder, listen, _ in moves:
            target.send(_dump(listen))
            holder.send(_dump({'jsonrpc': '2.0', 'method': 'notifications/cancelled',
                               'params': {'requestId': listen.get('id'),
                                          'reason': 'moved to the reloaded server'}}))
            if release_holders:
                self._release_if_quiet(holder)
        for _, listen, acknowledged in moves:
            if not acknowledged.wait(LISTEN_ACK_TIMEOUT_SECONDS):
                logger.info(f'_move_listens: worker pid={target.pid} did not acknowledge '
                            f'{listen.get("id")!r} within {LISTEN_ACK_TIMEOUT_SECONDS}s')

    def _announce_tool_list_change(self) -> None:
        """Tell the editor the tools may have changed, the way it listens for that."""
        with self.lock:
            listens = list(self.listens.values())
            is_initialize = (self.handshake or {}).get('method') == 'initialize'
        notices = []
        for listen in listens:
            wanted = _params(listen).get('notifications')
            if isinstance(wanted, dict) and wanted.get('toolsListChanged'):
                notices.append({'jsonrpc': '2.0', 'method': 'notifications/tools/list_changed',
                                'params': {'_meta': {SUBSCRIPTION_ID_META_KEY: listen.get('id')}}})
        if not listens and is_initialize:
            notices.append({'jsonrpc': '2.0', 'method': 'notifications/tools/list_changed'})
        # a 2026-07-28 editor with no open subscription has nowhere to be told
        for notice in notices:
            self._to_editor(_dump(notice))

    def _discard(self, worker: Worker) -> None:
        with contextlib.suppress(OSError):
            worker.process.kill()

    def _release_if_quiet(self, worker: Worker) -> None:
        """Let a replaced worker go once it owes the editor nothing.

        SIGUSR1 first, then stdin closes: the server reads the signal as "finish what you
        carry" and stays until its last delivery is done, instead of exiting on the closed
        stdin and taking those deliveries with it.
        """
        with self.lock:
            if (not worker.is_draining or worker.is_released or worker.has_exited
                    or worker is self.current):
                return
            if worker.open_requests or any(w is worker for w, _ in self.server_requests.values()):
                return
            worker.is_released = True
        # send_signal checks the process has not been reaped, so the pid cannot be someone else's
        with contextlib.suppress(OSError):
            worker.process.send_signal(signal.SIGUSR1)
        worker.close_stdin()
        logger.info(f'_release_if_quiet: worker pid={worker.pid} released; it exits after its '
                    'last delivery')
        self._write_status()

    # --------------------------------------------------------------- worker exits

    def _worker_exited(self, worker: Worker) -> None:
        code = worker.process.wait()
        with self.lock:
            worker.has_exited = True
            if worker in self.workers:
                self.workers.remove(worker)
            unanswered = list(worker.open_requests)
            worker.open_requests.clear()
            for key in unanswered:
                # a subscription ends with the error below; the editor listens again on its own
                self.listens.pop(key, None)
                self.handshakes.pop(key, None)
            for key in [k for k, (w, _) in self.server_requests.items() if w is worker]:
                del self.server_requests[key]
            for event in worker.acks.values():
                event.set()
            worker.acks.clear()
            is_crash = worker is self.current and not self.is_closing
        # a reload waiting on this worker's handshake learns it is gone
        worker.init_answered.set()
        for key in unanswered:
            self._to_editor(_dump({
                'jsonrpc': '2.0', 'id': json.loads(key),
                'error': {'code': -32603, 'message': (
                    f'the cross-agent server (pid {worker.pid}) exited with code {code} before '
                    'answering. A send may or may not have been queued: check bridge_status '
                    'before sending again.')}}))
        logger.info(f'_worker_exited: generation {worker.generation} pid={worker.pid} '
                    f'code={code} unanswered={len(unanswered)} crash={is_crash}')
        self._write_status()
        if is_crash:
            self._replace_crashed(worker)

    def _replace_crashed(self, dead: Worker) -> None:
        """The current worker died under the editor: start another, unless they keep dying."""
        with self.reload_lock:
            with self.lock:
                # a reload that was running may already have put a live worker in its place
                if self.current is not dead or self.is_closing:
                    return
            now = time.time()
            self.crashes = [t for t in self.crashes if now - t < CRASH_WINDOW_SECONDS] + [now]
            failures = 0
            while True:
                if len(self.crashes) > CRASH_LIMIT or failures >= CRASH_LIMIT:
                    logger.error(f'_replace_crashed: {len(self.crashes)} deaths within '
                                 f'{CRASH_WINDOW_SECONDS}s, {failures} replacements that did '
                                 'not start; the front gives up')
                    self.remove_status()
                    logging.shutdown()
                    os._exit(1)
                worker = self._start_worker()
                error = self._handshake(worker) if self.handshake is not None else None
                with self.lock:
                    if error is None and worker.has_exited:
                        error = 'it exited right after answering'
                    if error is None:
                        self.current = worker
                        break
                failures += 1
                logger.error(f'_replace_crashed: replacement pid={worker.pid} failed: {error}')
                self._discard(worker)
            # subscriptions a replaced worker still held, when a reload adopted the worker that died
            self._move_listens(worker)
        self._announce_tool_list_change()
        self._write_status()

    # ------------------------------------------------------------------ status

    def _write_status(self) -> None:
        # one writer at a time: threads share the temporary, and a late write of an older
        # state must not land over a newer one - or bring back a file the exit removed
        with self.status_lock:
            if self.is_status_removed:
                return
            with self.lock:
                current = self.current
                status = {
                    'front_pid': os.getpid(),
                    'cwd': self.cwd,
                    'started_at': self.started_at,
                    'worker_pid': current.pid if current is not None else None,
                    'generation': current.generation if current is not None else None,
                    'draining_pids': [w.pid for w in self.workers if w.is_draining],
                    'last_reload': self.last_reload,
                }
            try:
                _write_json(self.status_path, status)
            except OSError as e:
                logger.info(f'_write_status [failed]: {e}')

    def remove_status(self) -> None:
        with self.status_lock:
            self.is_status_removed = True
            with contextlib.suppress(OSError):
                os.remove(self.status_path)


# ------------------------------------------------------------------ reload command

def _live_fronts() -> List[int]:
    """Fronts with a status file and a live process; files left by dead ones are removed."""
    pids = []
    with contextlib.suppress(OSError):
        for name in os.listdir(config.FRONT_DIR):
            if not name.endswith('.json') or not name[:-5].isdigit():
                continue
            pid = int(name[:-5])
            if _is_alive(pid):
                pids.append(pid)
            else:
                with contextlib.suppress(OSError):
                    os.remove(config.FRONT_DIR + name)
    return sorted(pids)


def request_reload() -> int:
    """`run-server.sh reload`: ask every running front to load the current source.

    The source is imported in a fresh process first. A tree that does not import would only
    fail in every front at once, so it is refused here, before anything is asked of them.
    Exits 0 only when every front reported a reload or had nothing to replace.
    """
    config.ensure_dirs()
    check = subprocess.run([sys.executable, '-B', '-c', 'import cross_agent_mcp.server'],
                           capture_output=True, text=True)
    if check.returncode != 0:
        print('reload refused: the current source does not import, so no server was asked '
              'to load it.', file=sys.stderr)
        print(check.stderr.strip()[-2000:], file=sys.stderr)
        return 1

    fronts = _live_fronts()
    request_id = f'{int(time.time() * 1000)}-{os.getpid()}'
    _write_json(config.RELOAD_REQUEST_PATH, {'id': request_id, 'requested_at': time.time()})
    print(f'reload {request_id}: asked {len(fronts)} running front(s)')

    outcomes: Dict[int, Tuple[Dict[str, Any], Dict[str, Any]]] = {}
    deadline = time.time() + REPORT_WAIT_SECONDS
    while len(outcomes) < len(fronts) and time.time() < deadline:
        time.sleep(0.5)
        for pid in fronts:
            if pid in outcomes:
                continue
            status = _read_json(f'{config.FRONT_DIR}{pid}.json') or {}
            last = status.get('last_reload') or {}
            if last.get('request_id') == request_id:
                outcomes[pid] = (status, last)
            elif not _is_alive(pid):
                outcomes[pid] = (status, {'ok': None, 'note': 'the front exited'})

    unsettled = 0
    for pid in fronts:
        status, last = outcomes.get(pid, ({}, None))
        if last is None:
            unsettled += 1
            line = (f'  ?       front {pid}: no answer within {REPORT_WAIT_SECONDS}s; it still '
                    'reloads when it gets to it')
        elif last.get('ok') is True:
            line = (f'  ok      front {pid}: server {last.get("replaced_pid")} -> '
                    f'{last.get("worker_pid")} (generation {last.get("generation")})')
        elif last.get('ok') is False:
            unsettled += 1
            line = f'  FAILED  front {pid}: {last.get("error")}'
        else:
            line = f'  -       front {pid}: {last.get("note")}'
        print(f'{line}  cwd={status.get("cwd")}')
    return 1 if unsettled else 0


def _init_logging() -> None:
    """A file of its own: stdout is the editor's channel, and the servers write bridge.log."""
    config.ensure_dirs()
    package_logger = logging.getLogger('cross_agent_mcp')
    package_logger.setLevel(logging.INFO)
    package_logger.propagate = False
    handler = panel.SecureRotatingFileHandler(
        config.LOG_DIR + 'front.log', maxBytes=1_000_000, backupCount=2, encoding='utf-8')
    handler.setFormatter(logging.Formatter(
        '%(asctime)s - pid %(process)d - %(levelname)s - %(message)s'))
    package_logger.addHandler(handler)


def main() -> None:
    if sys.argv[1:2] == ['reload']:
        sys.exit(request_reload())

    _init_logging()
    front = Front()
    for number in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        with contextlib.suppress(OSError, ValueError):
            signal.signal(number, front.on_signal)
    logger.info(f'main [BEGIN]: front pid={os.getpid()} cwd={front.cwd}')
    code = 0
    try:
        front.serve()
    except SystemExit as e:
        code = e.code if isinstance(e.code, int) else 0
    except BaseException as e:
        logger.exception(f'main [exception]: {e}')
        code = 1
    finally:
        front.remove_status()
        logger.info(f'main [END]: code={code}')
        logging.shutdown()
    # Skip interpreter teardown. A relay thread blocked writing to an editor that stopped
    # reading holds stdout's lock, and finalization aborts the process over it. Every write
    # was flushed as it was made, so nothing is lost.
    os._exit(code)
