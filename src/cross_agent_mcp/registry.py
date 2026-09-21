"""Persistent bridge state: session pins, conversation hop counters, busy locks.

All mutations go through a single flock-protected read-modify-write so that two agent
processes touching the registry at the same time cannot lose an update.
"""

import contextlib
import errno
import fcntl
import json
import logging
import os
import stat
import time
import uuid
from typing import Any, Callable, Dict, Iterator, List, Optional

from . import config


logger = logging.getLogger('cross_agent_mcp.registry')

EMPTY_REGISTRY: Dict[str, Any] = {'version': 1, 'pins': {}, 'conversations': {}}

# conversation records older than this are pruned on write
CONVERSATION_TTL_SECONDS = 24 * 3600

# an auto-created pin is only dropped once its directory has been gone for this long, so a
# momentary stat failure on a network share or a briefly relinked symlink cannot erase it
PIN_GRACE_SECONDS = 3600

# how old an unreadable lock file must be before it is treated as debris rather than as
# somebody else's claim being written right now
UNREADABLE_LOCK_GRACE_SECONDS = 30


class SessionBusyError(Exception):
    """Raised when a session is already holding a live busy lock."""

    def __init__(self, holder: Dict[str, Any]) -> None:
        super().__init__(f'session busy: {holder}')
        self.holder = holder


def _cwd_key(cwd: str) -> str:
    return os.path.realpath(os.path.expanduser(cwd))


def _read_unlocked(path: str) -> Dict[str, Any]:
    try:
        with open(path, 'r', encoding='utf-8') as f:
            data = json.load(f)
    except FileNotFoundError:
        return json.loads(json.dumps(EMPTY_REGISTRY))
    except Exception as e:
        logger.error(f'_read_unlocked [exception]: {e}')
        return json.loads(json.dumps(EMPTY_REGISTRY))

    for key, default in EMPTY_REGISTRY.items():
        data.setdefault(key, default)
    return data


def _prune(data: Dict[str, Any]) -> None:
    now = time.time()

    conversations = data.get('conversations', {})
    for conv_id in [k for k, v in conversations.items()
                    if now - float(v.get('updated_at', 0)) > CONVERSATION_TTL_SECONDS]:
        conversations.pop(conv_id, None)

    # Drop auto-created pins whose directory is gone. Sticky pins are never touched: they are
    # documented as permanent, and a single failed stat must not silently revoke one.
    for pins in data.get('pins', {}).values():
        for cwd in [k for k, v in pins.items()
                    if not v.get('is_sticky')
                    and now - float(v.get('updated_at', 0)) > PIN_GRACE_SECONDS
                    and not os.path.isdir(k)]:
            pins.pop(cwd, None)


def load_registry() -> Dict[str, Any]:
    return _read_unlocked(config.REGISTRY_PATH)


def update_registry(mutator: Callable[[Dict[str, Any]], Any]) -> Any:
    """Run `mutator` against the registry under an exclusive lock and persist the result."""
    config.ensure_dirs()
    lock_path = config.REGISTRY_PATH + '.lock'

    with config.secure_open(lock_path, 'a+') as lock_file:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        try:
            data = _read_unlocked(config.REGISTRY_PATH)
            result = mutator(data)
            _prune(data)

            tmp_path = config.REGISTRY_PATH + '.tmp'
            with config.secure_open(tmp_path) as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            os.replace(tmp_path, config.REGISTRY_PATH)
            return result
        finally:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


# ---------------------------------------------------------------- session pins

def get_pin(agent: str, cwd: str) -> Optional[Dict[str, Any]]:
    data = load_registry()
    return data.get('pins', {}).get(agent, {}).get(_cwd_key(cwd))


def set_pin(agent: str, cwd: str, session_id: str, session_cwd: str,
            is_sticky: bool = False, is_bridge_created: bool = False) -> None:
    def mutate(data: Dict[str, Any]) -> None:
        pins = data.setdefault('pins', {}).setdefault(agent, {})
        pins[_cwd_key(cwd)] = {
            'session_id': session_id,
            'cwd': session_cwd,
            'is_sticky': is_sticky,
            'is_bridge_created': is_bridge_created,
            'updated_at': time.time(),
        }

    update_registry(mutate)


def touch_pin(agent: str, cwd: str) -> None:
    def mutate(data: Dict[str, Any]) -> None:
        pin = data.get('pins', {}).get(agent, {}).get(_cwd_key(cwd))
        if pin:
            pin['updated_at'] = time.time()

    update_registry(mutate)


def clear_pin(agent: str, cwd: str) -> bool:
    def mutate(data: Dict[str, Any]) -> bool:
        pins = data.get('pins', {}).get(agent, {})
        return pins.pop(_cwd_key(cwd), None) is not None

    return update_registry(mutate)


def list_bridge_created_ids(agent: str) -> List[str]:
    data = load_registry()
    pins = data.get('pins', {}).get(agent, {})
    return [p['session_id'] for p in pins.values() if p.get('is_bridge_created')]


# ------------------------------------------------------------- hop accounting

def bump_conversation(conversation_id: str, sender: str, target: str) -> Dict[str, Any]:
    """Increment the hop counter and return the conversation record."""
    def mutate(data: Dict[str, Any]) -> Dict[str, Any]:
        conversations = data.setdefault('conversations', {})
        record = conversations.setdefault(conversation_id, {'hops': 0, 'trail': []})
        record['hops'] = int(record.get('hops', 0)) + 1
        record['updated_at'] = time.time()
        record.setdefault('trail', []).append(f'{sender}->{target}')
        record['trail'] = record['trail'][-20:]
        return json.loads(json.dumps(record))

    return update_registry(mutate)


def get_conversation(conversation_id: str) -> Dict[str, Any]:
    data = load_registry()
    return data.get('conversations', {}).get(conversation_id, {'hops': 0, 'trail': []})


# --------------------------------------------------------------- busy locking

class UnusableSessionId(ValueError):
    """A session id that must not be turned into a path."""


def _lock_path(agent: str, session_id: str) -> str:
    """The lock file for one session, refusing a name that would not stay in the lock directory.

    Not every id reaching here was resolved from a store. A reply is addressed with the session
    the calling agent declared in its own turn metadata, which is whatever the client sent, and
    that value is interpolated straight into this name. A `/` in it puts the lock somewhere
    other than where every other claim is looked for, so the bridge would hold a lock nobody
    else consults - exclusion that silently is not.
    """
    path = config.LOCK_DIR + f'{agent}__{session_id}.lock'
    if os.path.dirname(os.path.realpath(path)) != os.path.realpath(config.LOCK_DIR):
        raise UnusableSessionId(
            f'session id {session_id!r} does not name a lock inside {config.LOCK_DIR}. A '
            'session id is a name, not a path.')
    return path


def _guard_path() -> str:
    """The file whose flock serialises every decision about a busy lock.

    One guard for the whole lock directory rather than one per session. Everything held under
    it is a single small filesystem operation - read a record, link a file, unlink a file -
    with no waiting of any kind inside, so the contention it adds is not measurable, and a
    guard per session would leave a file behind for every session ever locked.
    """
    return config.LOCK_DIR + '.transitions.guard'


@contextlib.contextmanager
def _lock_transition() -> Iterator[None]:
    """Hold the lock directory still for one read-decide-write.

    Making the claim atomic stopped a loser corrupting a winner's lock, but left a second race
    in the other direction, between a reader and a claimer:

      1. A reads a lock whose holder is dead and decides to clear it;
      2. B clears it first and claims the session for itself;
      3. A, still acting on what it read, unlinks B's perfectly good lock.

    The session is then unlocked while B believes it holds it, which is the same ending by a
    different road. Neither step is atomic on its own and no amount of care inside one of them
    helps, because the decision and the act are separated by whatever the scheduler does in
    between. The guard puts them back together.

    The kernel drops an flock when the holder exits, so a process dying in here cannot wedge
    the directory. Another *user* could, though, which is why the mode is explicit: flock is
    granted on any open descriptor regardless of access mode, so a world-readable guard is one
    any account on the machine can hold exclusively and never release, stopping every delivery
    without touching anything else. It is created 0600, and fchmod'd on every open so a guard
    left behind by a build that created it with the umask is repaired rather than trusted.
    """
    config.ensure_dirs()
    fd = os.open(_guard_path(), os.O_CREAT | os.O_RDWR, 0o600)
    try:
        # Not best-effort. A guard that stays group- or world-writable is one anyone on the
        # machine can hold exclusively and never release, so failing to set the mode means
        # the protection is not there - and carrying on would take the lock anyway and call
        # it safe. Verified after the fact rather than assumed, because a filesystem that
        # ignores fchmod reports success.
        os.fchmod(fd, 0o600)
        mode = stat.S_IMODE(os.fstat(fd).st_mode)
        if mode & 0o077:
            raise OSError(
                f'{_guard_path()} is {oct(mode)} after being set to 0600. The bridge '
                'serialises its lock decisions through that file, so a filesystem that will '
                'not make it owner-only leaves every delivery stoppable by any account here.')
        fcntl.flock(fd, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


def _mtime(path: str) -> float:
    try:
        return os.path.getmtime(path)
    except OSError:
        return 0.0


def _is_pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except OSError as e:
        return e.errno == errno.EPERM
    return True


def read_busy_lock(agent: str, session_id: str) -> Optional[Dict[str, Any]]:
    """Return the live lock record for a session, clearing it when it is stale."""
    with _lock_transition():
        return _read_busy_lock_held(agent, session_id)


def _read_busy_lock_held(agent: str, session_id: str) -> Optional[Dict[str, Any]]:
    """As read_busy_lock, for a caller that is already holding the transition guard."""
    try:
        path = _lock_path(agent, session_id)
    except UnusableSessionId:
        # Asking whether an unusable id is busy is answerable - nothing can hold a lock that
        # could never be named - and only claiming one has to fail.
        return None
    try:
        with open(path, 'r', encoding='utf-8') as f:
            record = json.load(f)
    except FileNotFoundError:
        return None
    except Exception:
        # A lock that cannot be read is a leftover from a crash mid-write, and clearing it is
        # what stops one poisoning a session forever. It is only ever cleared once it is too
        # old to be anybody's live claim: the atomic claim above means a current build cannot
        # produce one of these, and the age is the second lock on that door - not the first.
        if time.time() - _mtime(path) > UNREADABLE_LOCK_GRACE_SECONDS:
            logger.info(f'read_busy_lock [unreadable]: clearing {path}')
            with contextlib.suppress(OSError):
                os.remove(path)
        return None

    # The holder says how long it may legitimately hold on; a lock without that field was
    # written by an older build that never held one past two turn budgets.
    ttl = float(record.get('ttl_seconds') or config.SEND_TIMEOUT_SECONDS * 2)
    is_stale = (not _is_pid_alive(int(record.get('pid', -1)))
                or time.time() - float(record.get('started_at', 0)) > ttl)
    if is_stale:
        with contextlib.suppress(OSError):
            os.remove(path)
        return None

    return record


def _claim_lock_file(path: str, payload: str) -> bool:
    """Create the lock file whole, or report that somebody else already owns it.

    The claim used to be an O_EXCL create followed by a separate write, which left the lock
    file existing and empty for as long as that took. Another claimer arriving in that window
    read it, failed to parse it, and - taking it for a corrupt leftover - deleted it. Its
    retry then won a lock somebody else was already holding: with six threads racing for one
    session, three of them have been seen to win.

    Writing the record to a temporary and linking it into place closes that window rather than
    narrowing it. `link` fails outright if the name is taken, so it is the same all-or-nothing
    claim O_EXCL gave; the difference is that the file is complete at the instant it appears,
    so there is no state another process can misread.
    """
    tmp = f'{path}.{os.getpid()}.{uuid.uuid4().hex[:8]}.tmp'
    try:
        fd = os.open(tmp, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            f.write(payload)

        try:
            os.link(tmp, path)
            return True
        except FileExistsError:
            return False
        except OSError as e:
            # A filesystem without hard links. Fall back to the create-then-write claim, which
            # is exclusive but leaves the window above; the age check in read_busy_lock is what
            # covers it there. Every store either agent keeps its sessions in supports links,
            # so this is for an unusual CROSS_AGENT_HOME rather than for the normal case.
            logger.warning(f'_claim_lock_file [no hard links]: {e}; falling back')
            try:
                with os.fdopen(os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600),
                               'w', encoding='utf-8') as f:
                    f.write(payload)
                return True
            except FileExistsError:
                return False
    finally:
        with contextlib.suppress(OSError):
            os.remove(tmp)


def _release_lock_file(path: str, token: str) -> None:
    """Remove the lock only while we still own it, never somebody else's fresh claim.

    Under the transition guard for the same reason the stale clear is: checking the token and
    acting on it are two steps, and between them the lock can become somebody else's.
    """
    with _lock_transition():
        _release_lock_file_held(path, token)


def _release_lock_file_held(path: str, token: str) -> None:
    try:
        with open(path, 'r', encoding='utf-8') as f:
            record = json.load(f)
    except FileNotFoundError:
        return
    except Exception:
        record = None

    if record is None or record.get('token') == token:
        with contextlib.suppress(OSError):
            os.remove(path)


@contextlib.contextmanager
def busy_lock(agent: str, session_id: str, conversation_id: str,
              ttl_seconds: Optional[float] = None) -> Iterator[None]:
    """Claim a session for the duration of the block.

    The record is written to a temporary and linked into place, so checking whether a session
    is busy and marking it busy are one step: two relays racing for the same session cannot
    both win, and the file is complete at the instant it appears.

    `ttl_seconds` is how long the claim stays believable to other processes. A panel delivery
    now listens for as long as the peer's turn takes, which is longer than two turn budgets;
    without saying so, another server would judge the lock abandoned and start a second turn
    on the same session.
    """
    config.ensure_dirs()
    path = _lock_path(agent, session_id)
    token = uuid.uuid4().hex
    payload = json.dumps({
        'pid': os.getpid(),
        'token': token,
        'agent': agent,
        'session_id': session_id,
        'conversation_id': conversation_id,
        'started_at': time.time(),
        'ttl_seconds': ttl_seconds or config.SEND_TIMEOUT_SECONDS * 2,
    })

    is_claimed = False
    # One transition: claim, or read what is there and clear it if it is abandoned, then claim
    # what we just cleared. Split across two guarded steps, the window between them is exactly
    # where somebody else's fresh claim would get deleted by our retry.
    with _lock_transition():
        for _ in range(2):
            if _claim_lock_file(path, payload):
                is_claimed = True
                break
            holder = _read_busy_lock_held(agent, session_id)
            if holder:
                raise SessionBusyError(holder)

    if not is_claimed:
        raise SessionBusyError({'agent': agent, 'session_id': session_id})

    try:
        yield
    finally:
        _release_lock_file(path, token)


def list_busy_locks() -> List[Dict[str, Any]]:
    config.ensure_dirs()
    locks: List[Dict[str, Any]] = []
    for name in sorted(os.listdir(config.LOCK_DIR)):
        if not name.endswith('.lock'):
            continue
        agent, _, rest = name[:-5].partition('__')
        record = read_busy_lock(agent, rest)
        if record:
            locks.append(record)
    return locks
