"""Background delivery of bridged messages, so no agent ever blocks on a peer's turn.

A relay used to be a blocking call: the sender waited inside the tool while the peer ran a
whole turn, which is why both sessions had to be locked - a peer relaying back into a sender
that is parked waiting would deadlock. Peer turns in real use run for minutes (216s..660s
observed), so the wait was also the thing that hit the timeout.

Here the tool call only hands the message over. A worker carries out the delivery, and when
the peer's turn produces an answer the worker relays that answer back into the sender's
session as its own message. The sender sees the reply as an inbound turn instead of a return
value, which is the same information without anybody holding a lock.

Ordering: one worker thread per target session, so two messages aimed at the same session are
delivered one after another. Different sessions proceed in parallel. This matters beyond
fairness - the agent CLIs keep a single writer per transcript, so overlapping turns on one
session are rejected by the CLI itself.
"""

import contextlib
import json
import logging
import os
import re
import threading
import time
import uuid
from typing import Any, Callable, Dict, List, Optional, Tuple

from . import config, registry


logger = logging.getLogger('cross_agent_mcp.outbox')

# a worker with an empty queue waits this long for more work before retiring, so a burst of
# messages to one session reuses a single thread instead of starting one per message
IDLE_LINGER_SECONDS = 30

# how long a worker keeps retrying a session another process holds a busy lock on
BUSY_RETRY_SECONDS = 5

# completed jobs kept for status reporting
HISTORY_LIMIT = 50

# how much of a peer's answer the delivery record carries. The answer is normally read in the
# session it was delivered into; this copy is what a caller with no session of its own - a
# headless call, a test harness - has to work with.
REPLY_PREVIEW_LIMIT = 2000

STATE_QUEUED = 'queued'
STATE_DELIVERING = 'delivering'
STATE_AWAITING = 'awaiting-peer'
STATE_DELIVERED = 'delivered'
STATE_FAILED = 'failed'

# How long to keep looking for an answer after the transport gave up, and how often to look.
#
# Giving up listening is not the same as the peer giving up working. On the panel path the
# peer is a session we neither own nor can stop: the socket wait ended, the turn did not.
# Thirteen deliveries were closed as failed today while their answers were being written.
RECOVERY_WINDOW_SECONDS = 900
RECOVERY_POLL_SECONDS = 15

# A reply or a notice that could not be handed over is tried again, from a freshly resolved
# route: the sender's panel may have closed, moved or reopened under a new process since the
# request left. Only a message that provably never landed is retried - re-sending one that did
# would have the sender read the same answer twice.
UNDELIVERED_RETRY_ATTEMPTS = 3
UNDELIVERED_RETRY_SECONDS = 20

# How long `send_message` stays on the line after queuing, to hand back a failure the worker
# hits straight away - a thread the app server no longer has, a session the panel does not
# drive. Those come back within a second; a healthy hand-over is acknowledged in about that
# time too, so the wait ends as soon as either happens. Only a peer that is busy, or a queue
# with something ahead, makes the caller sit out the whole window.
EARLY_FAILURE_WINDOW_SECONDS = 8

# Deliveries still under way are recorded one directory below finished ones. Servers from before
# in-flight records existed prune every `*.json` in the delivery directory whose `finished_at` is
# older than the TTL, and an in-flight record has none - the first `bridge_status` any of them
# answered would delete it. They never look into a subdirectory.
IN_FLIGHT_SUBDIR = 'in-flight/'

# a delivery id names a file here; anything that is not an id is refused, never joined to a path
DELIVERY_ID_PATTERN = re.compile(r'^[A-Za-z0-9_-]{1,128}$')

# A writer that died between writing its temporary file and renaming it leaves the temporary
# behind. Past this age it belongs to no write still in progress.
STALE_TEMP_SECONDS = 3600

# Margin on top of the longest a delivery can legitimately take. Past that an in-flight record is
# not believed however its server's pid looks - pids are reused.
IN_FLIGHT_GRACE_SECONDS = 300

KIND_REQUEST = 'request'
KIND_REPLY = 'reply'
KIND_NOTICE = 'failure-notice'


def new_request_id() -> str:
    """`req_<epoch ms>_<6 hex>` — short enough for a peer to copy back without mangling it.

    The millisecond prefix is the useful half: a token read out of a log, a delivery record or
    the peer's transcript says when its request went out, and sorting the tokens sorts the
    requests. The random tail only has to survive two requests leaving in the same millisecond.
    """
    return f'req_{int(time.time() * 1000)}_{uuid.uuid4().hex[:6]}'


def _in_flight_dir() -> str:
    return config.DELIVERY_DIR + IN_FLIGHT_SUBDIR


def _record_path(delivery_id: str, is_finished: bool) -> str:
    return (config.DELIVERY_DIR if is_finished else _in_flight_dir()) + f'{delivery_id}.json'


def _write_json_atomically(path: str, record: Dict[str, Any]) -> None:
    """Write to a temporary file of this write's own, then rename it into place.

    A reader in any process sees the previous record or the new one, never half of either, and
    two writers never truncate each other's temporary.

    The temporary is owner-only from the moment it exists, so the rename never publishes a
    record that was briefly world-readable under it.
    """
    tmp = f'{path}.{os.getpid()}.{uuid.uuid4().hex[:8]}.tmp'
    try:
        with config.secure_open(tmp) as f:
            json.dump(record, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.remove(tmp)
        raise


def _write_record(record: Dict[str, Any], is_finished: bool = True) -> None:
    """Keep a delivery where every server process - this one's successor included - can read it.

    Written when the delivery is queued and again at every change of state, not only when it
    ends. A server exits with whatever started it - an app that quits takes its server along -
    and a delivery that lived only in that server's memory left nothing behind: the peer still
    did the work, and every later `bridge_status` answered "no delivery is known". The record is
    for reporting only. Nothing reads it back to resume or resend a delivery, which would ask the
    peer for the same work twice.

    Only what `describe()` returns is written - never the payload or the child environment,
    which carries every variable this process was started with.
    """
    try:
        config.ensure_dirs()
        config.secure_makedirs(_in_flight_dir())
        now = time.time()
        stamped = {**record, 'origin_pid': record.get('origin_pid') or os.getpid(),
                   'updated_at': now}
        delivery_id = record['delivery_id']
        if is_finished:
            _write_json_atomically(_record_path(delivery_id, is_finished=True),
                                   {**stamped, 'finished_at': now, 'expires_at': None})
            # the finished record is in place before the in-flight one goes, so a reader that
            # looks in both never finds neither
            with contextlib.suppress(OSError):
                os.remove(_record_path(delivery_id, is_finished=False))
        else:
            _write_json_atomically(_record_path(delivery_id, is_finished=False),
                                   {**stamped, 'finished_at': None})
    except Exception as e:
        logger.error(f'_write_record [exception]: {e}')


def persist(job: 'Job') -> None:
    """Record a delivery as it stands now. Called when it is queued and at every change of state.

    The record is described inside the job's own lock, at the moment of writing, so writes from
    different threads may arrive in any order and the last one still says the latest thing. Once
    the finished record is down, nothing reopens it.
    """
    with job.record_guard:
        if job.is_record_final:
            return
        is_finished = job.finished_at is not None
        _write_record(job.describe(), is_finished=is_finished)
        job.is_record_final = is_finished


def _is_in_flight(record: Dict[str, Any]) -> bool:
    return not record.get('finished_at')


def _prune_at(record: Dict[str, Any]) -> float:
    """When a record is removed from disk.

    A finished record keeps the TTL from when it finished. An in-flight one keeps the same TTL
    from `expires_at`, the point past which no server can still be carrying it. Removing it at
    that point instead would answer "no delivery is known" exactly when the question gets asked -
    the day after the app that sent it quit - about a request the peer may well have carried out.
    """
    if _is_in_flight(record):
        base = record.get('expires_at') or record.get('updated_at') or 0
    else:
        base = record.get('finished_at') or 0
    return float(base) + config.DELIVERY_TTL_SECONDS


def _remove_if_unchanged(path: str, inode: int) -> None:
    """Remove a record only while it is still the file that was judged.

    A writer may have renamed a fresh record into place since this one was read; that one stays.
    """
    with contextlib.suppress(OSError):
        if os.stat(path).st_ino == inode:
            os.remove(path)


def _load_record(path: str) -> Optional[Dict[str, Any]]:
    """Read one record, removing it when it is unreadable or has aged out."""
    try:
        handle = open(path, 'r', encoding='utf-8')
    except OSError:
        return None

    with handle:
        inode = os.fstat(handle.fileno()).st_ino
        try:
            record = json.load(handle)
            is_expired = time.time() > _prune_at(record)
        except Exception:
            record, is_expired = None, True

    if is_expired or not isinstance(record, dict):
        _remove_if_unchanged(path, inode)
        return None
    return record


def _scan_dir(directory: str) -> List[Dict[str, Any]]:
    try:
        names = os.listdir(directory)
    except OSError:
        return []

    now = time.time()
    records: List[Dict[str, Any]] = []
    for name in names:
        path = directory + name
        if name.endswith('.tmp'):
            with contextlib.suppress(OSError):
                if now - os.stat(path).st_mtime > STALE_TEMP_SECONDS:
                    os.remove(path)
        elif name.endswith('.json'):
            record = _load_record(path)
            if record is not None:
                records.append(record)
    return records


def _scan_records() -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """Finished and in-flight records on disk, pruning what has aged out.

    In-flight records are read first. A delivery that finishes between the two reads then shows
    up in both, and the finished record wins; read the other way round it would show up in
    neither.
    """
    with contextlib.suppress(OSError):
        config.ensure_dirs()
    in_flight = _scan_dir(_in_flight_dir())
    finished = _scan_dir(config.DELIVERY_DIR)

    finished_ids = {r.get('delivery_id') for r in finished}
    return finished, [r for r in in_flight if r.get('delivery_id') not in finished_ids]


def read_records(limit: int = 20) -> List[Dict[str, Any]]:
    """Finished deliveries from disk, newest first, pruning what has aged out."""
    finished, _ = _scan_records()
    finished.sort(key=lambda r: float(r.get('finished_at') or 0), reverse=True)
    return finished[:limit]


def read_record(delivery_id: str) -> Optional[Dict[str, Any]]:
    """One delivery's record, finished or still in flight - None when none is kept.

    None means it was never recorded, or has aged out: the only cases left in which "no delivery
    is known" is the answer. Looked for finished, in flight, then finished again - a delivery
    that finishes meanwhile is written to its finished place before it leaves the in-flight one.
    """
    if not isinstance(delivery_id, str) or not DELIVERY_ID_PATTERN.match(delivery_id):
        return None
    finished_path = _record_path(delivery_id, is_finished=True)
    return (_load_record(finished_path)
            or _load_record(_record_path(delivery_id, is_finished=False))
            or _load_record(finished_path))


def describe_origin(record: Dict[str, Any], is_carried_here: bool = False) -> Dict[str, Any]:
    """A record, plus whether the server that carried it can still move it on.

    A finished record needs nobody. An in-flight one is only as good as the process that wrote
    it: once that process is gone nothing will finish the delivery, and the record says no more
    than what it last saw - an answer, if there is one, is in the peer's transcript. Two things
    tell: whether the writer's pid still runs, and whether the record is past `expires_at`,
    beyond which no server could still be carrying it, whatever that pid belongs to now.

    Reporting only. Nothing here resumes or resends a delivery.
    """
    described = dict(record)
    if not _is_in_flight(record):
        described.update(is_origin_alive=None, is_orphaned=False)
        return described
    if is_carried_here:
        described.update(is_origin_alive=True, is_orphaned=False)
        return described

    now = time.time()
    try:
        pid = int(record.get('origin_pid') or 0)
        expires_at = record.get('expires_at')
        is_expired = expires_at is not None and now > float(expires_at)
        # durations as of now, the way a carried delivery reports them, not as of the last write
        if record.get('created_at'):
            started_at = record.get('started_at')
            described['queued_seconds'] = round(float(started_at or now)
                                                - float(record['created_at']), 1)
            described['elapsed_seconds'] = (round(now - float(started_at), 1)
                                            if started_at else None)
    except (TypeError, ValueError):
        pid, is_expired = 0, True
    # a record naming our own pid that we are not carrying was written by an earlier process
    is_alive = pid > 0 and pid != os.getpid() and registry._is_pid_alive(pid)
    described.update(is_origin_alive=is_alive, is_orphaned=not is_alive or is_expired)
    return described


class PeerBusyError(Exception):
    """The peer could not take the message yet — but it will.

    Both agents already have an answer for concurrent input: Claude waits for the turn to end,
    Codex queues. They just have a limit, and past it the shim says so. That is a "come back
    shortly", not a verdict, and turning it into a delivery failure threw away messages the
    peer would have accepted a minute later.
    """


class NotDeliveredError(Exception):
    """The message never reached the peer, and this is known for certain.

    Different from a transport that broke *after* the hand-over: there the peer is working and
    an answer will appear in its transcript, so waiting for one is right. Here nothing was
    handed over - the app server had no such thread, the panel drives another session, the
    pipe was closed - and waiting fifteen minutes for an answer that cannot come is exactly
    how three requests sat in `awaiting-peer` while nobody was told.
    """


class Job:
    """One message on its way to a peer session."""

    def __init__(self, target_agent: str, target_session_id: Optional[str], payload: str,
                 run_cwd: str, pin_cwd: str, env: Dict[str, str], timeout: int,
                 ui_shim: Optional[Dict[str, Any]], title: Optional[str],
                 conversation_id: str, hop: int, sender_agent: str,
                 sender_session_id: Optional[str], wants_reply: bool,
                 summary: str, delivery_id: Optional[str] = None,
                 kind: Optional[str] = None) -> None:
        self.delivery_id = delivery_id or new_request_id()
        self.target_agent = target_agent
        self.target_session_id = target_session_id
        self.payload = payload
        self.run_cwd = run_cwd
        # the caller's own directory, which keys the session pin - not necessarily run_cwd
        self.pin_cwd = pin_cwd
        self.env = env
        self.timeout = timeout
        self.ui_shim = ui_shim
        self.title = title
        self.conversation_id = conversation_id
        self.hop = hop
        self.sender_agent = sender_agent
        self.sender_session_id = sender_session_id
        # A reply carries no reply of its own: that is what terminates the exchange.
        self.wants_reply = wants_reply
        self.summary = summary
        self.kind = kind or (KIND_REQUEST if wants_reply else KIND_REPLY)

        self.state = STATE_QUEUED
        self.created_at = time.time()
        self.started_at: Optional[float] = None
        # the moment the peer's process took the message; None while that is not known
        self.accepted_at: Optional[float] = None
        self.finished_at: Optional[float] = None
        self.error: Optional[str] = None
        self.reply = ''
        self.reply_length = 0
        self.is_reply_recovered = False
        # the answer was the peer's own finished turn, echoing this request's token, read from
        # its transcript while the shim still reported the turn as running. Delivered, not
        # recovered: the token proves it is the answer, whatever the shim's bookkeeping said.
        self.is_reply_confirmed_by_transcript = False
        # the message never landed - set only when the transport says so, never inferred
        self.is_undelivered = False
        self.attempts = 0
        self.resolved_session_id: Optional[str] = None
        # Until this instant the caller of send_message is still on the line and will be handed
        # a failure directly. After it, a failure is announced into the sender's session.
        self.report_failures_until = 0.0
        # past this instant no server can still be carrying the delivery; set when it is queued
        # and moved when a worker starts it (Outbox._longest_run)
        self.expires_at: Optional[float] = None
        # serialises this delivery's record writes, see persist()
        self.record_guard = threading.Lock()
        self.is_record_final = False

    def key(self) -> str:
        """Deliveries sharing this key are serialised."""
        return f'{self.target_agent}:{self.target_session_id or "new"}'

    def mark_accepted(self, session_id: Optional[str] = None) -> None:
        """The peer has the message. From here on its turn is running and we are listening."""
        before = (self.accepted_at, self.resolved_session_id, self.state)
        if self.accepted_at is None:
            self.accepted_at = time.time()
        if session_id:
            self.resolved_session_id = session_id
        if self.state == STATE_DELIVERING:
            self.state = STATE_AWAITING
        if (self.accepted_at, self.resolved_session_id, self.state) != before:
            persist(self)

    def is_failure_reportable_synchronously(self) -> bool:
        """Whether the send_message caller, not a notice, is the one to hear about a failure."""
        return (self.finished_at is not None and self.accepted_at is None
                and self.finished_at < self.report_failures_until)

    def describe(self) -> Dict[str, Any]:
        return {
            'delivery_id': self.delivery_id,
            'state': self.state,
            'kind': self.kind,
            'target_agent': self.target_agent,
            'target_session_id': self.resolved_session_id or self.target_session_id,
            'sender_agent': self.sender_agent,
            'sender_session_id': self.sender_session_id,
            'conversation_id': self.conversation_id,
            'hop': self.hop,
            'is_reply': not self.wants_reply,
            'summary': self.summary,
            'created_at': self.created_at,
            'started_at': self.started_at,
            'accepted_at': self.accepted_at,
            'finished_at': self.finished_at,
            # while in flight: past this no server can still be carrying the delivery
            'expires_at': self.expires_at if self.finished_at is None else None,
            # the server process carrying the delivery; on disk, the one that wrote the record
            'origin_pid': os.getpid(),
            'queued_seconds': round((self.started_at or time.time()) - self.created_at, 1),
            'elapsed_seconds': (round((self.finished_at or time.time()) - self.started_at, 1)
                                if self.started_at else None),
            'attempts': self.attempts or None,
            'reply_length': self.reply_length or None,
            'reply_preview': self.reply[:REPLY_PREVIEW_LIMIT] or None,
            # true when the answer was read out of the peer's transcript instead of being
            # handed back by the process this server started
            'is_reply_recovered': self.is_reply_recovered or None,
            # true when the peer's echoed, finished answer was read from its transcript while
            # the panel still reported the turn as running - a delivered answer, not a guess
            'is_reply_confirmed_by_transcript': self.is_reply_confirmed_by_transcript or None,
            # true when the peer provably never received the message
            'is_undelivered': self.is_undelivered or None,
            'error': self.error,
        }


class Outbox:
    """Queues, workers and the small amount of history `bridge_status` reports."""

    def __init__(self) -> None:
        self._guard = threading.Lock()
        self._queues: Dict[str, List[Job]] = {}
        self._wakeups: Dict[str, threading.Condition] = {}
        self._workers: Dict[str, threading.Thread] = {}
        self._pending: Dict[str, Job] = {}
        self._history: List[Job] = []
        # the job each worker is running right now, by key
        self._running: Dict[str, Job] = {}

        # injected by bridge to avoid an import cycle
        self.deliver: Optional[Callable[[Job], Dict[str, Any]]] = None
        self.build_reply: Optional[Callable[[Job, str], Optional['Job']]] = None
        self.build_notice: Optional[Callable[[Job], Optional['Job']]] = None
        self.recover: Optional[Callable[[Job], Optional[str]]] = None
        self.reroute: Optional[Callable[[Job], None]] = None

    # ------------------------------------------------------------- submission

    def submit(self, job: Job) -> str:
        key = job.key()
        with self._guard:
            ahead = list(self._queues.get(key, []))
            if key in self._running:
                ahead.append(self._running[key])
            # until a worker starts it, a delivery may wait out everything ahead of it in full
            job.expires_at = (time.time() + self._longest_run(job)
                              + sum(self._longest_run(j) for j in ahead))
            self._pending[job.delivery_id] = job
            self._queues.setdefault(key, []).append(job)
            condition = self._wakeups.setdefault(key, threading.Condition())

            worker = self._workers.get(key)
            if worker is None or not worker.is_alive():
                worker = threading.Thread(
                    target=self._work, args=(key,), name=f'outbox:{key}', daemon=True)
                self._workers[key] = worker
                worker.start()

        # On disk before the caller hears "accepted": from here on, a server that exits still
        # leaves the delivery findable. A worker already writing a later state is not undone -
        # persist() describes the job at the moment it writes.
        persist(job)

        with condition:
            condition.notify_all()

        logger.info(f'submit [queued]: {job.delivery_id} -> {key} conv={job.conversation_id}')
        return job.delivery_id

    def depth(self, key: str) -> int:
        with self._guard:
            return len(self._queues.get(key, []))

    def await_outcome(self, job: Job) -> None:
        """Stay with a just-queued job until the peer takes it, it fails, or the window passes.

        The point is the failure case: a message the worker cannot hand over at all is known
        within a second, and the caller is still here to be told. `job.report_failures_until`
        must be set before the job is submitted - the worker reads it to decide whether the
        caller is hearing about a failure directly, or a notice has to carry it into the
        sender's session. Set afterwards, a fast failure would be reported both ways.
        """
        while time.time() < job.report_failures_until:
            if job.accepted_at is not None or job.finished_at is not None:
                return
            time.sleep(0.05)

    # ---------------------------------------------------------------- workers

    def _next(self, key: str) -> Optional[Job]:
        with self._guard:
            queue = self._queues.get(key) or []
            return queue.pop(0) if queue else None

    def _retire(self, key: str) -> bool:
        """Drop this worker's registration, unless work arrived while it was deciding."""
        with self._guard:
            if self._queues.get(key):
                return False
            self._queues.pop(key, None)
            self._workers.pop(key, None)
            self._wakeups.pop(key, None)
            return True

    def _work(self, key: str) -> None:
        while True:
            job = self._next(key)
            if job is None:
                condition = self._wakeups.get(key)
                if condition is None:
                    return
                # Re-check while holding the condition. A submit that landed between the
                # failed _next and here cannot notify until we wait, so without this the
                # worker would sleep out the full linger on an already-queued message.
                with condition:
                    if self._next_is_empty(key):
                        condition.wait(IDLE_LINGER_SECONDS)
                if self._next_is_empty(key) and self._retire(key):
                    return
                continue

            with self._guard:
                self._running[key] = job
            try:
                self._run(job)
            finally:
                with self._guard:
                    self._running.pop(key, None)

    def _next_is_empty(self, key: str) -> bool:
        with self._guard:
            return not self._queues.get(key)

    def _run(self, job: Job) -> None:
        job.state = STATE_DELIVERING
        job.started_at = time.time()
        job.expires_at = job.started_at + self._longest_run(job)
        persist(job)
        logger.info(f'_run [BEGIN]: {job.delivery_id} {job.sender_agent}->{job.target_agent} '
                    f'session={job.target_session_id or "NEW"} conv={job.conversation_id}')

        try:
            result = self._deliver_with_retries(job)
            job.resolved_session_id = result.get('session_id') or job.target_session_id
            reply = str(result.get('reply') or '').strip()
            job.reply = reply
            job.reply_length = len(reply)
            job.is_reply_confirmed_by_transcript = bool(
                result.get('is_reply_confirmed_by_transcript'))
            job.state = STATE_DELIVERED
        except NotDeliveredError as e:
            job.state = STATE_FAILED
            job.is_undelivered = True
            job.error = f'{type(e).__name__}: {e}'
            logger.error(f'_run [not delivered]: {job.delivery_id} {job.error}')
        except Exception as e:
            job.state = STATE_FAILED
            job.error = f'{type(e).__name__}: {e}'
            logger.error(f'_run [exception]: {job.delivery_id} {job.error}')

        # The peer may have answered even when we did not receive it - a transport that broke
        # after the turn, a process that died holding the result. The answer is on disk in the
        # peer's own transcript either way, so ask there before giving up on it. Only a request
        # has an answer to look for: a reply is complete the moment it lands, and reading the
        # sender's transcript after one would only pull its own words back as a "reply".
        if (job.wants_reply and not job.reply and job.state == STATE_FAILED
                and not job.is_undelivered):
            recovered = self._recover_with_patience(job)
            if recovered:
                job.reply = recovered
                job.reply_length = len(recovered)
                job.is_reply_recovered = True
                logger.info(f'_run [recovered]: {job.delivery_id} read the answer from the '
                            f'{job.target_agent} transcript ({len(recovered)} chars)')

        # the outcome is settled here; what follows only tells people about it
        job.finished_at = time.time()
        try:
            if job.wants_reply and job.reply:
                self._send_reply(job, job.reply)
            elif job.wants_reply:
                self._send_notice(job)
        finally:
            self._archive(job)
            logger.info(f'_run [END]: {job.delivery_id} state={job.state} '
                        f'reply_chars={job.reply_length}')

    def _deliver_with_retries(self, job: Job) -> Dict[str, Any]:
        """A reply that never landed is re-aimed and tried again; a request is not.

        The sender's panel process may have gone away between the request and its answer - a
        reopened tab is a new process with a new socket. Re-resolving the route finds it. A
        request gets no such retry: its failure is reported to the caller, who knows better
        than a blind retry whether it should go out again.
        """
        attempts = 1 if job.wants_reply else UNDELIVERED_RETRY_ATTEMPTS
        for attempt in range(1, attempts + 1):
            job.attempts = attempt
            if attempt > 1:
                persist(job)
            try:
                return self._deliver_with_lock(job)
            except NotDeliveredError as e:
                if attempt >= attempts:
                    raise
                logger.info(f'_deliver_with_retries [rerouting]: {job.delivery_id} attempt '
                            f'{attempt} did not land ({e}); trying again in '
                            f'{UNDELIVERED_RETRY_SECONDS}s')
                if self.reroute is not None:
                    with contextlib.suppress(Exception):
                        self.reroute(job)
                time.sleep(UNDELIVERED_RETRY_SECONDS)
        raise RuntimeError('unreachable')

    def _patience(self, job: Job) -> float:
        """How long a delivery may take before we stop holding on to it.

        On the panel path the peer's turn runs as long as it runs, whatever we do, so the only
        question is how long we keep listening; the configured patience answers it. On the CLI
        path the turn is our subprocess and the budget is enforced by killing it.
        """
        if job.ui_shim is not None:
            return max(job.timeout, config.PANEL_PATIENCE_SECONDS)
        return job.timeout

    def _longest_run(self, job: Job) -> float:
        """The longest one delivery can legitimately take once a worker starts it.

        Per attempt: waiting out a busy session, then the turn, each up to the patience. Then the
        transcript watch after a transport failure, and a margin. Past it no server is still
        carrying the delivery, which is what keeps a record honest when its server's pid is reused.
        """
        attempts = 1 if job.wants_reply else UNDELIVERED_RETRY_ATTEMPTS
        return (attempts * (2 * self._patience(job) + UNDELIVERED_RETRY_SECONDS)
                + RECOVERY_WINDOW_SECONDS + IN_FLIGHT_GRACE_SECONDS)

    def _deliver_with_lock(self, job: Job) -> Dict[str, Any]:
        """Hold the cross-process busy lock only while the turn actually runs.

        Same-session ordering is already handled by this worker being the only one for the key.
        The file lock still matters because a second MCP server process - the peer's own - can
        aim at the same session from outside this process.
        """
        if self.deliver is None:
            raise RuntimeError('outbox has no delivery function installed')

        patience = self._patience(job)
        deadline = time.time() + patience
        while True:
            try:
                if not job.target_session_id:
                    return self.deliver(job)
                with registry.busy_lock(job.target_agent, job.target_session_id,
                                        job.conversation_id, ttl_seconds=patience + 60):
                    return self.deliver(job)
            except (registry.SessionBusyError, PeerBusyError) as e:
                # Two ways of hearing the same thing: another delivery holds the session, or
                # the peer itself is mid-turn. Neither says the message cannot be delivered,
                # only that now is not the moment.
                if time.time() >= deadline:
                    raise
                logger.info(f'_deliver_with_lock [busy]: {job.delivery_id} → '
                            f'{job.target_session_id or "NEW"} not ready ({type(e).__name__}), '
                            f'retrying in {BUSY_RETRY_SECONDS}s')
                time.sleep(BUSY_RETRY_SECONDS)

    def _recover_with_patience(self, job: Job) -> Optional[str]:
        """Look once, then keep looking while the peer could still be writing.

        Only the panel path waits. There the peer is a session we neither started nor stopped,
        so our socket giving up says nothing about its turn. On the CLI path the turn *was*
        our subprocess and a timeout killed its process group, so nothing further will be
        written and waiting would only stall the queue behind it.
        """
        text = self._recover(job)
        if text or job.ui_shim is None:
            return text

        job.state = STATE_AWAITING
        persist(job)
        logger.info(f'_recover_with_patience [waiting]: {job.delivery_id} the transport gave '
                    f'up but {job.target_agent} may still be working; watching its transcript')

        deadline = time.time() + RECOVERY_WINDOW_SECONDS
        try:
            while time.time() < deadline:
                time.sleep(RECOVERY_POLL_SECONDS)
                text = self._recover(job)
                if text:
                    logger.info(f'_recover_with_patience [answered]: {job.delivery_id} after '
                                f'{round(time.time() - (job.started_at or time.time()))}s')
                    return text
            logger.info(f'_recover_with_patience [gave up]: {job.delivery_id} no answer within '
                        f'{RECOVERY_WINDOW_SECONDS}s of the transport failing')
            return None
        finally:
            # The wait is over either way, and "awaiting-peer" was only true while it lasted.
            # Left standing, twenty-six finished records read as still in flight. The transport
            # did fail; whether an answer was then read from the transcript is recorded on
            # the side (is_reply_recovered), not by rewriting what happened.
            job.state = STATE_FAILED

    def _recover(self, job: Job) -> Optional[str]:
        if self.recover is None:
            return None
        try:
            return self.recover(job)
        except Exception as e:
            logger.error(f'_recover [exception]: {job.delivery_id} {e}')
            return None

    def _send_reply(self, job: Job, reply: str) -> None:
        if self.build_reply is None:
            return
        try:
            reply_job = self.build_reply(job, reply)
        except Exception as e:
            logger.error(f'_send_reply [exception]: {job.delivery_id} {e}')
            return
        if reply_job is not None:
            self.submit(reply_job)

    def _send_notice(self, job: Job) -> None:
        """Tell the sender a request produced no answer - unless the caller was told directly.

        A request that fails silently is the worst outcome the bridge has: the sender believes
        the work is under way and finds out hours later, by asking. So a request that ends
        without an answer is announced into the sender's session, whether it never landed or
        landed and went unanswered - the wording differs, the announcement does not.
        """
        if job.is_failure_reportable_synchronously():
            logger.info(f'_send_notice [skipped]: {job.delivery_id} the caller is being told '
                        'directly')
            return
        if self.build_notice is None:
            return
        try:
            notice = self.build_notice(job)
        except Exception as e:
            logger.error(f'_send_notice [exception]: {job.delivery_id} {e}')
            return
        if notice is not None:
            self.submit(notice)

    # --------------------------------------------------------------- reporting

    def _archive(self, job: Job) -> None:
        # The finished record goes down before the job leaves `pending`: a process that exits as
        # soon as nothing is pending must not take the last word with it.
        persist(job)
        with self._guard:
            self._pending.pop(job.delivery_id, None)
            self._history.append(job)
            del self._history[:-HISTORY_LIMIT]

    def find(self, delivery_id: str) -> Optional[Job]:
        with self._guard:
            job = self._pending.get(delivery_id)
            if job:
                return job
            return next((j for j in reversed(self._history)
                         if j.delivery_id == delivery_id), None)

    def snapshot(self, limit: int = 20) -> Dict[str, Any]:
        with self._guard:
            pending = [j.describe() for j in self._pending.values()]
            recent = [j.describe() for j in reversed(self._history[-limit:])]
        pending.sort(key=lambda d: d['queued_seconds'], reverse=True)

        # Deliveries this process carries are in memory. Everything else is on disk: finished
        # ones, which is how an answer outlives the server that received it, and ones other
        # servers recorded as in flight - still carried there, or orphaned by a server now gone.
        carried = {d['delivery_id'] for d in pending + recent}
        finished, in_flight = _scan_records()
        finished.sort(key=lambda r: float(r.get('finished_at') or 0), reverse=True)
        in_flight.sort(key=lambda r: float(r.get('updated_at') or 0), reverse=True)
        earlier = [r for r in finished if r.get('delivery_id') not in carried][:limit]
        elsewhere = [describe_origin(r) for r in in_flight
                     if r.get('delivery_id') not in carried][:limit]
        return {'pending': pending, 'recent': recent, 'earlier': earlier,
                'in_flight_elsewhere': elsewhere}


OUTBOX = Outbox()
