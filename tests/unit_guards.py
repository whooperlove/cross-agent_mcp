"""Unit checks for the bridge's guards and discovery rules. Spends no agent turns.

    PYTHONPATH=src .venv/bin/python tests/unit_guards.py
"""

import contextlib
import errno
import json
import os
import sys
import tempfile
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))) + '/src')

from cross_agent_mcp import bridge, config, discovery, outbox, registry  # noqa: E402


FAILURES = []


def check(label: str, condition: bool, detail: str = '') -> None:
    if condition:
        print(f'[ok] {label}')
    else:
        print(f'[FAIL] {label} {detail}')
        FAILURES.append(label)


def _quiet_log():
    """A logger for shims built without __init__; keeps test runs out of the real shim log."""
    import logging
    log = logging.getLogger('cross_agent_mcp.test.shim')
    log.propagate = False
    if not log.handlers:
        log.addHandler(logging.NullHandler())
    return log


# ------------------------------------------------- busy lock is really exclusive

def test_busy_lock_is_exclusive() -> None:
    session_id = 'unit-lock-' + os.urandom(4).hex()
    outcomes = []
    barrier = threading.Barrier(6)

    def worker() -> None:
        barrier.wait()
        try:
            with registry.busy_lock(config.AGENT_CODEX, session_id, 'conv_unit'):
                outcomes.append('won')
                time.sleep(0.3)
        except registry.SessionBusyError:
            outcomes.append('refused')

    threads = [threading.Thread(target=worker) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)

    check('busy lock admits exactly one concurrent claimer',
          outcomes.count('won') == 1 and outcomes.count('refused') == 5, str(outcomes))
    check('busy lock is released afterwards',
          registry.read_busy_lock(config.AGENT_CODEX, session_id) is None)


def test_busy_lock_release_respects_owner() -> None:
    session_id = 'unit-owner-' + os.urandom(4).hex()
    path = registry._lock_path(config.AGENT_CODEX, session_id)

    with registry.busy_lock(config.AGENT_CODEX, session_id, 'conv_a'):
        # a foreign holder overwrites the record; our release must leave it alone
        with open(path, 'w', encoding='utf-8') as f:
            json.dump({'pid': os.getpid(), 'token': 'someone-else',
                       'conversation_id': 'conv_b', 'started_at': time.time()}, f)

    survivor = registry.read_busy_lock(config.AGENT_CODEX, session_id)
    check('release does not delete another holder\'s lock',
          survivor is not None and survivor.get('token') == 'someone-else', str(survivor))
    os.remove(path)


# ------------------------------------------------------------ pin pruning rules

def test_prune_spares_sticky_pins() -> None:
    missing_dir = '/nonexistent-cross-agent-unit/' + os.urandom(4).hex()
    stale = time.time() - registry.PIN_GRACE_SECONDS - 60

    registry.set_pin(config.AGENT_CODEX, missing_dir, 'sticky-sid', missing_dir, is_sticky=True)
    registry.set_pin(config.AGENT_CLAUDE, missing_dir, 'auto-sid', missing_dir, is_sticky=False)

    def age_them(data):
        for agent in (config.AGENT_CODEX, config.AGENT_CLAUDE):
            data['pins'][agent][registry._cwd_key(missing_dir)]['updated_at'] = stale

    registry.update_registry(age_them)
    registry.update_registry(lambda data: None)  # any write triggers _prune

    check('sticky pin survives a missing directory',
          registry.get_pin(config.AGENT_CODEX, missing_dir) is not None)
    check('stale auto pin on a missing directory is pruned',
          registry.get_pin(config.AGENT_CLAUDE, missing_dir) is None)

    registry.clear_pin(config.AGENT_CODEX, missing_dir)


def test_prune_keeps_fresh_auto_pins() -> None:
    missing_dir = '/nonexistent-cross-agent-unit/' + os.urandom(4).hex()
    registry.set_pin(config.AGENT_CODEX, missing_dir, 'fresh-sid', missing_dir, is_sticky=False)
    registry.update_registry(lambda data: None)

    check('fresh auto pin survives a transient directory miss',
          registry.get_pin(config.AGENT_CODEX, missing_dir) is not None)
    registry.clear_pin(config.AGENT_CODEX, missing_dir)


# ------------------------------------------------------------ cwd scope rules

def test_cwd_relations() -> None:
    rel = discovery._cwd_relation
    check('exact cwd', rel('/a/b', '/a/b') == discovery.CWD_EXACT)
    check('session below search dir', rel('/a/b/c', '/a/b') == discovery.CWD_DESCENDANT)
    check('session above search dir', rel('/a', '/a/b') == discovery.CWD_ANCESTOR)
    check('sibling path is unrelated', rel('/a/bb', '/a/b') is None)
    check("scope 'cwd' rejects ancestors",
          not discovery._is_in_scope(discovery.CWD_ANCESTOR, discovery.SCOPE_CWD))
    check("scope 'tree' accepts ancestors",
          discovery._is_in_scope(discovery.CWD_ANCESTOR, discovery.SCOPE_TREE))
    check("scope 'any' accepts anything",
          discovery._is_in_scope(None, discovery.SCOPE_ANY))


# -------------------------------------- codex scan filters before it truncates

def _write_rollout(store: str, session_id: str, cwd: str, mtime: float) -> None:
    day_dir = store + '/2026/08/08'
    os.makedirs(day_dir, exist_ok=True)
    path = f'{day_dir}/rollout-2026-08-08T00-00-00-{session_id}.jsonl'
    with open(path, 'w', encoding='utf-8') as f:
        f.write(json.dumps({'type': 'session_meta', 'payload': {
            'session_id': session_id, 'cwd': cwd, 'originator': 'codex_vscode',
            'thread_source': 'user'}}) + '\n')
    os.utime(path, (mtime, mtime))


def test_codex_scan_filters_before_limit() -> None:
    original_dir, original_limit = config.CODEX_SESSIONS_DIR, config.CODEX_SCAN_LIMIT
    with tempfile.TemporaryDirectory(prefix='codex-store-') as store:
        target_cwd = store + '/wanted'
        os.makedirs(target_cwd, exist_ok=True)
        now = time.time()

        # 20 newer threads from unrelated directories, then the one we actually want
        for i in range(20):
            _write_rollout(store, f'0000000{i:04d}-0000-0000-0000-00000000000{i % 10}',
                           store + f'/other-{i}', now - i)
        _write_rollout(store, 'aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee', target_cwd, now - 100)

        config.CODEX_SESSIONS_DIR = store + '/'
        config.CODEX_SCAN_LIMIT = 5  # smaller than the number of unrelated newer rollouts
        try:
            found = discovery.list_codex_sessions(discovery.SCOPE_CWD, target_cwd, limit=5)
        finally:
            config.CODEX_SESSIONS_DIR, config.CODEX_SCAN_LIMIT = original_dir, original_limit

    check('cwd filter runs before the scan cap',
          len(found) == 1 and found[0]['session_id'] == 'aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee',
          str([s['session_id'] for s in found]))


# --------------------------------------- timeout kills the whole process group

def test_timeout_kills_descendants() -> None:
    with tempfile.TemporaryDirectory(prefix='killgroup-') as work_dir:
        marker = work_dir + '/child.pid'
        command = ['/bin/bash', '-c', f'sleep 45 & echo $! > {marker}; wait']

        raised = False
        try:
            bridge._run_cli(command, work_dir, dict(os.environ), timeout=2)
        except bridge.BridgeError as e:
            raised = 'did not answer within' in str(e)

        check('timeout surfaces as a BridgeError', raised)

        # the group gets SIGTERM first, so give it a moment - polling, because a loaded
        # machine can take noticeably longer than a fixed sleep would allow
        grandchild = int(open(marker).read().strip())
        deadline = time.time() + 10
        while registry._is_pid_alive(grandchild) and time.time() < deadline:
            time.sleep(0.1)

        check('grandchild process is killed with the group',
              not registry._is_pid_alive(grandchild), f'pid {grandchild} still alive')


# ------------------------------- sub-agent threads must never receive a relay

def test_subagent_threads_are_rejected() -> None:
    from cross_agent_mcp.appserver_shim import CodexAppServerShim

    accepts = CodexAppServerShim._accepts_direct_input
    check('a plain panel thread accepts direct input', accepts({'id': 'a'}))
    check('a thread with a parent is a sub-agent',
          not accepts({'id': 'b', 'parentThreadId': 'a'}))
    check('a thread with an agent nickname is a sub-agent',
          not accepts({'id': 'c', 'agentNickname': 'Turing'}))
    check('a thread with an agent role is a sub-agent',
          not accepts({'id': 'd', 'agentRole': 'reviewer'}))
    check('an explicit canAcceptDirectInput=false is honoured',
          not accepts({'id': 'e', 'canAcceptDirectInput': False}))

    # a notification must never be able to introduce an unvetted thread
    shim = CodexAppServerShim.__new__(CodexAppServerShim)
    shim.threads = {}
    shim.injections = {}
    shim.opens = {}
    shim.approvals = {}
    shim.item_threads = {}
    shim.open_turns = {}
    shim.state_lock = threading.Lock()
    shim.last_user_activity = 0.0
    shim.log = _quiet_log()

    shim._observe_from_server({'method': 'item/started',
                               'params': {'threadId': 'sub-agent-thread'}})
    check('a bare threadId in a notification does not create a target',
          'sub-agent-thread' not in shim.threads, str(shim.threads))

    shim._observe_from_server({'method': 'thread/started',
                               'params': {'thread': {'id': 'sub', 'parentThreadId': 'main'}}})
    check('thread/started for a sub-agent is ignored', 'sub' not in shim.threads, str(shim.threads))

    shim._observe_from_server({'method': 'thread/started',
                               'params': {'thread': {'id': 'main', 'cwd': '/w'}}})
    check('thread/started for a user thread is recorded', 'main' in shim.threads, str(shim.threads))

    # and only the extension's own thread-driving requests may register one
    shim._observe_from_client({'method': 'thread/read', 'params': {'threadId': 'peeked'}})
    check('an unrelated client request does not register a thread',
          'peeked' not in shim.threads, str(shim.threads))

    shim._observe_from_client({'method': 'turn/start', 'params': {'threadId': 'driven'}})
    check('a turn the extension started registers its thread',
          'driven' in shim.threads, str(shim.threads))


# ------------------- a new conversation is the last resort, never a silent one

def test_new_session_is_the_last_resort() -> None:
    from cross_agent_mcp import uihook

    calls = []
    originals = (uihook.is_enabled, uihook.find_live_session, uihook.find_panel_host,
                 discovery.find_active_session, discovery.find_session,
                 discovery.find_session_by_name, registry.get_pin)

    uihook.is_enabled = lambda: True
    uihook.find_panel_host = lambda agent: {'shim': {'pid': 1, 'socket': '/s'}}
    registry.get_pin = lambda agent, cwd: None

    def restore():
        (uihook.is_enabled, uihook.find_live_session, uihook.find_panel_host,
         discovery.find_active_session, discovery.find_session,
         discovery.find_session_by_name, registry.get_pin) = originals

    try:
        # an existing session on disk must win over opening a fresh conversation
        uihook.find_live_session = lambda agent, session_id=None: None
        discovery.find_active_session = lambda agent, scope, cwd, exclude=None: {
            'session_id': 'on-disk', 'source': 'discovery', 'cwd': '/w'}
        target = bridge._resolve_target('codex', None, 'cwd', '/w', False, [])
        check('an existing session outranks opening a new conversation',
              target.get('session_id') == 'on-disk', str(target))

        # only when nothing can be resumed anywhere
        discovery.find_active_session = lambda agent, scope, cwd, exclude=None: None
        target = bridge._resolve_target('codex', None, 'cwd', '/w', False, [])
        check('a new conversation is opened only when nothing exists',
              target.get('source') == 'ide-panel-new', str(target))

        # a named session that matches nothing must fail instead of starting over
        discovery.find_session = lambda agent, sid: None
        discovery.find_session_by_name = lambda agent, name, limit=500: None
        raised = ''
        try:
            bridge._resolve_target('codex', 'studio_v4_orginial', 'cwd', '/w', False, [])
        except bridge.BridgeError as e:
            raised = str(e)
        check('an unmatched session name fails loudly',
              'no codex session is named' in raised and 'no session was created' in raised,
              raised[:160])

        # a name that does match is resolved to its id
        discovery.find_session_by_name = lambda agent, name, limit=500: {
            'session_id': 'named-id', 'title': name, 'mtime': 0}
        calls.clear()

        def find_session(agent, sid):
            calls.append(sid)
            return {'session_id': sid} if sid == 'named-id' else None

        discovery.find_session = find_session
        target = bridge._resolve_target('codex', 'studio_v4_orginial', 'cwd', '/w', False, [])
        check('a conversation name resolves to its session',
              target.get('session_id') == 'named-id' and target.get('source') == 'name',
              str(target))
    finally:
        restore()


# --------------------------------------- which conversation tab a relay lands in

def test_panel_session_selection() -> None:
    from cross_agent_mcp import uihook

    now = time.time()
    shims = [
        {'agent': 'claude', 'pid': 1, 'socket': '/s1', 'ancestors': [99], 'started_at': now - 300},
        {'agent': 'claude', 'pid': 2, 'socket': '/s2', 'ancestors': [99], 'started_at': now - 200},
        {'agent': 'claude', 'pid': 3, 'socket': '/s3', 'ancestors': [99], 'started_at': now - 100},
    ]
    statuses = {
        '/s1': {'ok': True, 'last_user_activity': 0,
                'sessions': [{'session_id': 'old-tab', 'cwd': '/w'}]},
        '/s2': {'ok': True, 'last_user_activity': now - 5,
                'sessions': [{'session_id': 'typed-tab', 'cwd': '/w'}]},
        '/s3': {'ok': True, 'last_user_activity': 0,
                'sessions': [{'session_id': 'newest-tab', 'cwd': '/w'}]},
    }
    transcripts = {'old-tab': now - 900, 'typed-tab': now - 900, 'newest-tab': now - 10}

    originals = (uihook.list_shims, uihook.process_ancestry,
                 uihook.read_status, uihook._transcript_mtime)
    uihook.list_shims = lambda agent=None: [s for s in shims if not agent or s['agent'] == agent]
    uihook.process_ancestry = lambda pid, depth=12: [99]
    uihook.read_status = lambda shim: statuses[shim['socket']]
    uihook._transcript_mtime = lambda agent, session_id: transcripts.get(session_id, 0.0)

    try:
        check('the tab the human typed into wins',
              uihook.find_live_session('claude')['session_id'] == 'typed-tab',
              str([s['session_id'] for s in uihook.find_live_sessions('claude')]))

        check('an explicit session id overrides the ordering',
              uihook.find_live_session('claude', 'old-tab')['session_id'] == 'old-tab')
        check('a session that is not open in any panel is not matched',
              uihook.find_live_session('claude', 'not-open') is None)

        # nobody has typed since the shims started: fall back to the freshest transcript
        statuses['/s2']['last_user_activity'] = 0
        check('with no observed input the freshest transcript wins',
              uihook.find_live_session('claude')['session_id'] == 'newest-tab',
              str([s['session_id'] for s in uihook.find_live_sessions('claude')]))

        # and with no evidence at all, the most recently opened tab
        for session_id in transcripts:
            transcripts[session_id] = 0.0
        check('with no evidence at all the newest tab wins',
              uihook.find_live_session('claude')['session_id'] == 'newest-tab')
    finally:
        (uihook.list_shims, uihook.process_ancestry,
         uihook.read_status, uihook._transcript_mtime) = originals


# ------------------------------------------------------ the outbox never blocks

def _job(box: outbox.Outbox, target_session_id, wants_reply=False, summary='msg'):
    return outbox.Job(
        target_agent=config.AGENT_CODEX, target_session_id=target_session_id,
        payload='hello', run_cwd='/tmp', pin_cwd='/tmp', env={}, timeout=5,
        ui_shim=None, title=None, conversation_id='conv_outbox', hop=1,
        sender_agent=config.AGENT_CLAUDE, sender_session_id='sender-sid',
        wants_reply=wants_reply, summary=summary)


def _drain(box: outbox.Outbox, deadline_seconds: float = 5.0) -> None:
    deadline = time.time() + deadline_seconds
    while time.time() < deadline and box.snapshot()['pending']:
        time.sleep(0.02)


def test_submit_does_not_block_the_caller() -> None:
    box = outbox.Outbox()
    released = threading.Event()

    def slow_deliver(job):
        released.wait(3)
        return {'session_id': job.target_session_id, 'reply': '', 'is_new_session': False}

    box.deliver = slow_deliver

    started = time.time()
    delivery_id = box.submit(_job(box, 'sid-slow'))
    elapsed = time.time() - started

    check('submit returns without waiting for the turn', elapsed < 0.5, f'{elapsed:.2f}s')
    check('submit hands back a delivery id', delivery_id.startswith('req_'), delivery_id)
    check('the delivery is reported as in flight',
          any(d['delivery_id'] == delivery_id for d in box.snapshot()['pending']))

    released.set()
    _drain(box)
    check('the delivery finishes on its own',
          box.find(delivery_id).state == outbox.STATE_DELIVERED)


def test_same_session_deliveries_are_serialised() -> None:
    box = outbox.Outbox()
    concurrent = []
    live = []
    guard = threading.Lock()

    def watching_deliver(job):
        with guard:
            live.append(job.delivery_id)
            concurrent.append(len(live))
        time.sleep(0.05)
        with guard:
            live.remove(job.delivery_id)
        return {'session_id': job.target_session_id, 'reply': '', 'is_new_session': False}

    box.deliver = watching_deliver
    for _ in range(4):
        box.submit(_job(box, 'sid-shared'))
    _drain(box)

    check('two turns never run on the same session at once',
          concurrent and max(concurrent) == 1, f'peak={max(concurrent) if concurrent else 0}')
    check('every queued delivery still ran', len(concurrent) == 4, str(len(concurrent)))


def test_different_sessions_deliver_in_parallel() -> None:
    box = outbox.Outbox()
    entered = threading.Barrier(2, timeout=3)
    failures = []

    def blocking_deliver(job):
        try:
            entered.wait()
        except threading.BrokenBarrierError:
            failures.append(job.delivery_id)
        return {'session_id': job.target_session_id, 'reply': '', 'is_new_session': False}

    box.deliver = blocking_deliver
    box.submit(_job(box, 'sid-a'))
    box.submit(_job(box, 'sid-b'))
    _drain(box)

    check('deliveries to different sessions are not serialised', not failures,
          f'barrier broke for {failures}')


def test_a_reply_is_delivered_back_and_stops_there() -> None:
    box = outbox.Outbox()
    delivered = []

    def echo_deliver(job):
        delivered.append((job.target_session_id, job.wants_reply))
        return {'session_id': job.target_session_id, 'reply': 'the answer',
                'is_new_session': False}

    box.deliver = echo_deliver
    box.build_reply = lambda job, reply: _job(box, job.sender_session_id, wants_reply=False,
                                              summary=reply)

    box.submit(_job(box, 'sid-peer', wants_reply=True))
    _drain(box)

    check('the request is delivered to the peer', ('sid-peer', True) in delivered)
    check('the answer is delivered back to the sender', ('sender-sid', False) in delivered)
    check('the answer does not trigger another answer', len(delivered) == 2, str(delivered))


def test_a_busy_session_is_waited_out_not_refused() -> None:
    box = outbox.Outbox()
    session_id = 'sid-busy-' + uuid_hex()
    attempts = []

    box.deliver = lambda job: (attempts.append(time.time()) or
                               {'session_id': job.target_session_id, 'reply': '',
                                'is_new_session': False})

    original_retry = outbox.BUSY_RETRY_SECONDS
    outbox.BUSY_RETRY_SECONDS = 0.05
    holder = threading.Event()

    def hold_the_lock():
        with registry.busy_lock(config.AGENT_CODEX, session_id, 'conv_other'):
            holder.set()
            time.sleep(0.3)

    keeper = threading.Thread(target=hold_the_lock, daemon=True)
    keeper.start()
    holder.wait(2)

    try:
        delivery_id = box.submit(_job(box, session_id))
        _drain(box)
        job = box.find(delivery_id)
        check('a session another process holds is waited out, not refused',
              job.state == outbox.STATE_DELIVERED, f'state={job.state} error={job.error}')
        check('the delivery ran only after the lock was released', len(attempts) == 1,
              str(len(attempts)))
    finally:
        outbox.BUSY_RETRY_SECONDS = original_retry
        keeper.join(timeout=2)


def test_another_window_is_reachable_only_when_named() -> None:
    """Auto-selection stays in this window; an explicitly named session is chased anywhere."""
    from cross_agent_mcp import uihook

    now = time.time()
    shims = [
        # this window: the extension host is pid 99, which our own chain shares
        {'agent': 'codex', 'pid': 1, 'socket': '/here', 'ancestors': [99], 'started_at': now},
        # another VS Code window: a different extension host entirely
        {'agent': 'codex', 'pid': 2, 'socket': '/there', 'ancestors': [77], 'started_at': now},
    ]
    statuses = {
        '/here': {'ok': True, 'last_user_activity': now - 5,
                  'sessions': [{'session_id': 'this-window', 'cwd': '/w'}]},
        '/there': {'ok': True, 'last_user_activity': now - 1,
                   'sessions': [{'session_id': 'other-window', 'cwd': '/w2'}]},
    }

    originals = (uihook.list_shims, uihook.process_ancestry,
                 uihook.read_status, uihook._transcript_mtime)
    uihook.list_shims = lambda agent=None: [s for s in shims if not agent or s['agent'] == agent]
    uihook.process_ancestry = lambda pid, depth=12: [99]
    uihook.read_status = lambda shim: statuses[shim['socket']]
    uihook._transcript_mtime = lambda agent, session_id: 0.0

    try:
        local_ids = [s['session_id'] for s in uihook.find_live_sessions('codex')]
        check('this window lists only its own tabs', local_ids == ['this-window'], str(local_ids))

        foreign_ids = [s['session_id'] for s in uihook.find_foreign_sessions('codex')]
        check('other windows are listed separately', foreign_ids == ['other-window'],
              str(foreign_ids))

        # the other window's tab was typed into more recently, so a heuristic that ignored
        # window boundaries would pick it - auto-selection must not
        check('auto-selection never leaves this window',
              uihook.find_live_session('codex')['session_id'] == 'this-window')

        named = uihook.find_live_session('codex', 'other-window')
        check('a named session in another window is found', named is not None)
        check('it is delivered through that window\'s own shim',
              named is not None and named['shim']['socket'] == '/there')
        check('it is marked as belonging to another window',
              named is not None and named.get('is_foreign_window') is True)

        check('a named session in this window still resolves locally',
              uihook.find_live_session('codex', 'this-window')['shim']['socket'] == '/here')
        check('a session open in no window at all is still unmatched',
              uihook.find_live_session('codex', 'nowhere') is None)
    finally:
        (uihook.list_shims, uihook.process_ancestry,
         uihook.read_status, uihook._transcript_mtime) = originals


def test_the_caller_identifies_its_own_session_exactly() -> None:
    """Who we are comes from the shim hosting us, never from a pin saying where to send."""
    from cross_agent_mcp import uihook

    now = time.time()
    shims = [
        # the shim this process is running under: its pid is in our own ancestry
        {'agent': 'claude', 'pid': 500, 'socket': '/mine', 'ancestors': [99], 'started_at': now},
        # a sibling tab in the same window - local, but not hosting us
        {'agent': 'claude', 'pid': 501, 'socket': '/sibling', 'ancestors': [99],
         'started_at': now},
    ]
    statuses = {
        '/mine': {'ok': True, 'last_user_activity': 0,
                  'sessions': [{'session_id': 'me', 'cwd': '/w'}]},
        '/sibling': {'ok': True, 'last_user_activity': now,
                     'sessions': [{'session_id': 'not-me', 'cwd': '/w'}]},
    }

    originals = (uihook.list_shims, uihook.process_ancestry,
                 uihook.read_status, uihook._transcript_mtime)
    uihook.list_shims = lambda agent=None: [s for s in shims if not agent or s['agent'] == agent]
    uihook.process_ancestry = lambda pid, depth=12: [500, 99]
    uihook.read_status = lambda shim: statuses[shim['socket']]
    uihook._transcript_mtime = lambda agent, session_id: 0.0

    try:
        own = uihook.find_own_session('claude')
        # the sibling was typed into more recently, so "the active tab" would pick it
        check('our own session is the one whose shim hosts us',
              own is not None and own['session_id'] == 'me',
              str(own and own['session_id']))
    finally:
        (uihook.list_shims, uihook.process_ancestry,
         uihook.read_status, uihook._transcript_mtime) = originals


def test_a_pin_never_answers_who_the_caller_is() -> None:
    """The regression: a stale pin stood in as the return address and the reply went nowhere."""
    from cross_agent_mcp import uihook

    originals = (uihook.is_enabled, uihook.find_own_session, discovery.find_active_session)
    recorded = {}

    def record_lookup(agent, scope, cwd, exclude_ids=None, use_pin=True):
        recorded['use_pin'] = use_pin
        return {'session_id': 'from-transcript'}

    uihook.is_enabled = lambda: True
    uihook.find_own_session = lambda agent: {'session_id': 'from-panel'}
    discovery.find_active_session = record_lookup

    try:
        check('the panel answers before anything on disk is consulted',
              bridge._own_session_id('claude') == 'from-panel')

        uihook.find_own_session = lambda agent: None
        check('with no panel it falls back to the transcript',
              bridge._own_session_id('claude') == 'from-transcript')
        check('and that fallback is asked WITHOUT pins',
              recorded.get('use_pin') is False, str(recorded))
    finally:
        (uihook.is_enabled, uihook.find_own_session, discovery.find_active_session) = originals


def test_the_envelope_carries_a_return_address() -> None:
    envelope = bridge._build_envelope('claude', 'codex', 'conv_x', 1, 3, 'body', 'sender-sid')
    check('the envelope states where a reply should go',
          'reply-to: claude session sender-sid' in envelope)
    check('and tells the peer how to address a new request',
          'session_id="sender-sid"' in envelope)

    anonymous = bridge._build_envelope('claude', 'codex', 'conv_x', 1, 3, 'body', None)
    check('a sender with no session says so instead of leaving it blank',
          'reply-to: (unknown' in anonymous)
    check('and does not hand out a bogus session id',
          'session_id="' not in anonymous)


def test_a_short_timeout_is_raised_to_the_configured_budget() -> None:
    """Callers kept passing 30s and 120s from when this blocked. Peer turns run 216s..660s,
    so the short value did nothing but cut them off."""
    captured = {}
    originals = (bridge.caller.detect_caller, bridge._own_session_id,
                 bridge._resolve_target, outbox.OUTBOX.submit, outbox.OUTBOX.await_outcome)

    bridge.caller.detect_caller = lambda: {'agent': config.AGENT_CLAUDE, 'chain': []}
    bridge._own_session_id = lambda agent: 'sender-sid'
    bridge._resolve_target = lambda *a, **kw: {
        'agent': config.AGENT_CODEX, 'session_id': 'peer-sid', 'cwd': None,
        'source': 'name', 'ui_shim': None}
    outbox.OUTBOX.submit = lambda job: (captured.update(timeout=job.timeout)
                                        or 'dlv_test')
    outbox.OUTBOX.await_outcome = lambda job: None

    try:
        result = bridge.send_message(config.AGENT_CODEX, 'hello', timeout=30)
        check('a timeout below the budget is raised to it',
              captured.get('timeout') == config.SEND_TIMEOUT_SECONDS, str(captured))
        check('and the caller is told, not silently overridden',
              'was raised to' in (result.get('warning') or ''), str(result.get('warning')))

        captured.clear()
        bridge.send_message(config.AGENT_CODEX, 'hello', timeout=1800)
        check('a longer timeout is honoured', captured.get('timeout') == 1800, str(captured))
    finally:
        (bridge.caller.detect_caller, bridge._own_session_id,
         bridge._resolve_target, outbox.OUTBOX.submit, outbox.OUTBOX.await_outcome) = originals


def test_an_echoed_token_identifies_which_request_was_answered() -> None:
    """The timestamp rule cannot tell two requests apart, and the panel is the human's session
    too — anything they type there also postdates our request. An echoed id settles it."""
    import datetime

    spoke_at = datetime.datetime(2026, 9, 1, 3, 0, 0, tzinfo=datetime.timezone.utc)
    mine = 'req_1788197970127_ea5c29'
    other = 'req_1788197970127_bbbbbb'

    def transcript(text: str) -> str:
        store = tempfile.mkdtemp(prefix='token-')
        path = store + '/session.jsonl'
        with open(path, 'w', encoding='utf-8') as f:
            f.write(json.dumps({
                'type': 'assistant',
                'timestamp': spoke_at.isoformat().replace('+00:00', 'Z'),
                'message': {'content': [{'type': 'text', 'text': text}]},
            }) + '\n')
        return path

    original = discovery.find_session
    try:
        # A matching token counts as the answer even if it predates the request — evidence beats inference.
        discovery.find_session = lambda a, s: {'path': transcript(f'끝났습니다.\n{mine}')}
        check('a matching token is accepted even against the clock',
              discovery.last_agent_message(
                  'claude', 'sid', after=spoke_at.timestamp() + 999, token=mine) is not None)

        discovery.find_session = lambda a, s: {'path': transcript(f'다른 답입니다.\n{other}')}
        check('a different token is refused even though it is newer',
              discovery.last_agent_message(
                  'claude', 'sid', after=spoke_at.timestamp() - 999, token=mine) is None)

        # If the peer omits the token, it falls back to the old timing rule — cooperation is a bonus, not a requirement.
        discovery.find_session = lambda a, s: {'path': transcript('토큰 없이 답합니다.')}
        check('a peer that ignored the token still falls back to the clock',
              discovery.last_agent_message(
                  'claude', 'sid', after=spoke_at.timestamp() - 1, token=mine) is not None)
        check('and the clock still refuses what predates the request',
              discovery.last_agent_message(
                  'claude', 'sid', after=spoke_at.timestamp() + 1, token=mine) is None)
    finally:
        discovery.find_session = original


def test_a_request_id_is_short_and_carries_its_time() -> None:
    token = outbox.new_request_id()
    check('the token is short enough to copy back', len(token) <= 26, token)
    check('and its millisecond prefix is readable',
          abs(int(token.split('_')[1]) / 1000 - time.time()) < 5, token)
    check('two tokens in the same millisecond still differ',
          outbox.new_request_id() != outbox.new_request_id())


def test_recovery_refuses_a_message_older_than_the_question() -> None:
    """It happened twice: one paragraph written before either request existed came back as the
    answer to both. A message that predates its own question is not an answer."""
    import datetime

    spoke_at = datetime.datetime(2026, 9, 1, 2, 5, 21, tzinfo=datetime.timezone.utc)
    with tempfile.TemporaryDirectory(prefix='transcript-') as store:
        path = store + '/session.jsonl'
        with open(path, 'w', encoding='utf-8') as f:
            f.write(json.dumps({
                'type': 'assistant',
                'timestamp': spoke_at.isoformat().replace('+00:00', 'Z'),
                'message': {'content': [{'type': 'text', 'text': '이전에 하던 말'}]},
            }) + '\n')

        original = discovery.find_session
        discovery.find_session = lambda agent, session_id: {'path': path}
        try:
            check('without a cutoff the last message is returned',
                  discovery.last_agent_message('claude', 'sid') == '이전에 하던 말')
            check('a message written before the request is not an answer',
                  discovery.last_agent_message(
                      'claude', 'sid', after=spoke_at.timestamp() + 1) is None)
            check('a message written after it still is',
                  discovery.last_agent_message(
                      'claude', 'sid', after=spoke_at.timestamp() - 1) == '이전에 하던 말')
        finally:
            discovery.find_session = original


def test_recovery_refuses_a_message_with_no_timestamp_when_asked_for_one() -> None:
    with tempfile.TemporaryDirectory(prefix='transcript-') as store:
        path = store + '/session.jsonl'
        with open(path, 'w', encoding='utf-8') as f:
            f.write(json.dumps({
                'type': 'assistant',
                'message': {'content': [{'type': 'text', 'text': '시각 없는 발화'}]},
            }) + '\n')

        original = discovery.find_session
        discovery.find_session = lambda agent, session_id: {'path': path}
        try:
            check('an undatable message cannot be shown to be an answer',
                  discovery.last_agent_message('claude', 'sid', after=0) is None)
        finally:
            discovery.find_session = original


def test_a_recovered_answer_says_it_may_not_be_finished() -> None:
    """Recovery reads the peer's last message, which is not always its answer.

    It happened: a relay timed out while the peer was still working, recovery picked up the
    line it had just written about what it was doing next, and that arrived looking exactly
    like a finished report.
    """
    plain = bridge._build_reply_envelope('codex', 'claude', 'conv_x', 1, 3, '작업 완료', 'sid')
    check('a received answer is not hedged', 'RECOVERED' not in plain)
    check('and says plainly where it came from',
          'This is the answer to a message you relayed earlier.' in plain)

    recovered = bridge._build_reply_envelope(
        'codex', 'claude', 'conv_x', 1, 3, '확인 중입니다', 'sid', is_recovered=True)
    check('a recovered answer is marked in the header',
          'recovered from transcript' in recovered)
    check('and warns it may be mid-work rather than an answer',
          'RECOVERED, NOT RECEIVED' in recovered
          and 'still doing rather than its' in recovered, recovered)


def test_the_recovered_flag_reaches_the_envelope() -> None:
    original = discovery.find_session
    discovery.find_session = lambda agent, session_id: None
    try:
        request = outbox.Job(
            target_agent=config.AGENT_CODEX, target_session_id='peer-sid', payload='x',
            run_cwd='/w', pin_cwd='/w', env={}, timeout=5, ui_shim=None, title=None,
            conversation_id='conv_r', hop=1, sender_agent=config.AGENT_CLAUDE,
            sender_session_id='sender-sid', wants_reply=True, summary='req')
        request.is_reply_recovered = True

        reply = bridge._build_reply_job(request, '확인 중입니다')
        check('a job recovered from a transcript builds a marked envelope',
              reply is not None and 'RECOVERED, NOT RECEIVED' in reply.payload)

        request.is_reply_recovered = False
        plain = bridge._build_reply_job(request, '작업 완료')
        check('and one that arrived normally does not',
              plain is not None and 'RECOVERED' not in plain.payload)
    finally:
        discovery.find_session = original


def test_a_reply_runs_where_the_senders_session_lives() -> None:
    """A Claude transcript is filed under its own project dir; resuming elsewhere fails."""
    with tempfile.TemporaryDirectory(prefix='sender-home-') as sender_home:
        original = discovery.find_session
        discovery.find_session = lambda agent, session_id: (
            {'session_id': session_id, 'cwd': sender_home} if session_id == 'sender-sid' else None)
        try:
            request = outbox.Job(
                target_agent=config.AGENT_CODEX, target_session_id='peer-sid', payload='x',
                run_cwd='/elsewhere', pin_cwd='/elsewhere', env={}, timeout=5, ui_shim=None,
                title=None, conversation_id='conv_reply', hop=1,
                sender_agent=config.AGENT_CLAUDE, sender_session_id='sender-sid',
                wants_reply=True, summary='req')
            reply = bridge._build_reply_job(request, 'the answer')

            check('the reply is aimed at the sender session',
                  reply is not None and reply.target_session_id == 'sender-sid')
            check('and runs in that session\'s own directory, not the request\'s',
                  reply is not None and reply.run_cwd == sender_home, str(reply and reply.run_cwd))
            check('a reply expects no reply of its own',
                  reply is not None and reply.wants_reply is False)
            # The request carried timeout=5, the sort of value a peer on an older build sends.
            # Inheriting it let that peer decide how long we may spend delivering our own
            # answer, and a reply that times out falls back to transcript recovery.
            check('and waits by our floor, not the timeout the requester happened to send',
                  reply is not None and reply.timeout == config.SEND_TIMEOUT_SECONDS,
                  str(reply and reply.timeout))
        finally:
            discovery.find_session = original

    orphan = outbox.Job(
        target_agent=config.AGENT_CODEX, target_session_id='peer-sid', payload='x',
        run_cwd='/elsewhere', pin_cwd='/elsewhere', env={}, timeout=5, ui_shim=None, title=None,
        conversation_id='conv_reply', hop=1, sender_agent=config.AGENT_CLAUDE,
        sender_session_id=None, wants_reply=True, summary='req')
    check('a sender with no session produces no undeliverable reply job',
          bridge._build_reply_job(orphan, 'the answer') is None)


def test_a_message_queued_in_the_wakeup_gap_is_not_lost() -> None:
    """The lost-wakeup window: a submit that notifies before the worker starts waiting.

    Reproduced deterministically by submitting from inside the worker's own empty _next, so
    the notify provably lands while nobody is waiting on the condition. A worker that then
    waits unconditionally sleeps out the full idle linger on an already-queued message.
    """
    box = outbox.Outbox()
    box.deliver = lambda job: {'session_id': job.target_session_id, 'reply': '',
                               'is_new_session': False}

    original_next = box._next
    injected = {'delivery_id': None}

    def next_with_injection(key):
        job = original_next(key)
        if job is None and injected['delivery_id'] is None:
            injected['delivery_id'] = box.submit(_job(box, 'sid-gap'))
        return job

    box._next = next_with_injection
    box.submit(_job(box, 'sid-gap'))

    _drain(box, deadline_seconds=3)

    injected_job = box.find(injected['delivery_id']) if injected['delivery_id'] else None
    check('a message queued in the wakeup gap is picked up, not slept through',
          injected_job is not None and injected_job.state == outbox.STATE_DELIVERED,
          f'state={getattr(injected_job, "state", None)} '
          f'(idle linger is {outbox.IDLE_LINGER_SECONDS}s)')


def test_a_finished_delivery_outlives_the_process_that_carried_it() -> None:
    """The observed loss: the answer arrived, then the editor reloaded and took it away."""
    original_dir = outbox.config.DELIVERY_DIR
    with tempfile.TemporaryDirectory(prefix='cross-agent-deliveries-') as store:
        outbox.config.DELIVERY_DIR = store + '/'
        try:
            box = outbox.Outbox()
            box.deliver = lambda job: {'session_id': job.target_session_id,
                                       'reply': 'the answer', 'is_new_session': False}
            delivery_id = box.submit(_job(box, 'sid-persist'))
            _drain(box)

            # a new Outbox is what the next server process starts with
            revived = outbox.Outbox().snapshot()
            kept = next((r for r in revived['earlier']
                         if r['delivery_id'] == delivery_id), None)
            check('a finished delivery is readable by the next process', kept is not None)
            check('and it still carries the answer',
                  kept is not None and kept.get('reply_preview') == 'the answer',
                  str(kept))

            raw = open(store + f'/{delivery_id}.json', encoding='utf-8').read()
            check('the record does not persist the message payload', 'hello' not in raw)
            check('nor the child environment', 'PATH' not in raw)
        finally:
            outbox.config.DELIVERY_DIR = original_dir


def test_an_answer_is_recovered_from_the_peer_transcript() -> None:
    """A delivery that breaks after the peer answered must not throw the answer away."""
    original_dir = outbox.config.DELIVERY_DIR
    with tempfile.TemporaryDirectory(prefix='cross-agent-deliveries-') as store:
        outbox.config.DELIVERY_DIR = store + '/'
        try:
            box = outbox.Outbox()
            asked = []

            def broken_deliver(job):
                raise RuntimeError('peer agent did not answer within 600s')

            box.deliver = broken_deliver
            box.recover = lambda job: (asked.append(job.target_session_id)
                                       or '트랜스크립트에서 회수한 답')

            delivery_id = box.submit(_job(box, 'sid-recover', wants_reply=True))
            replies = []
            box.build_reply = lambda job, reply: (replies.append(reply) or None)
            _drain(box)

            job = box.find(delivery_id)
            check('the transport failure is still recorded honestly',
                  job.state == outbox.STATE_FAILED and job.error is not None)
            check('but the answer is recovered from the peer transcript',
                  job.reply == '트랜스크립트에서 회수한 답', job.reply)
            check('and it is marked as recovered, not received',
                  job.is_reply_recovered is True)
            check('recovery asks about the target session', asked == ['sid-recover'], str(asked))
            check('a recovered answer is still relayed to the sender',
                  replies == ['트랜스크립트에서 회수한 답'], str(replies))
        finally:
            outbox.config.DELIVERY_DIR = original_dir


def test_a_busy_peer_is_retried_rather_than_failed() -> None:
    """Both agents handle concurrent input already — Claude waits out the turn, Codex queues —
    they just have a limit. Past it the shim says "busy", which is a moment, not a verdict."""
    original = outbox.BUSY_RETRY_SECONDS
    outbox.BUSY_RETRY_SECONDS = 0.02
    try:
        box = outbox.Outbox()
        attempts = {'count': 0}

        def busy_until_the_third_try(job):
            attempts['count'] += 1
            if attempts['count'] < 3:
                raise outbox.PeerBusyError('the panel session is busy with another turn')
            return {'session_id': job.target_session_id, 'reply': '늦게 받았습니다',
                    'is_new_session': False}

        box.deliver = busy_until_the_third_try
        delivery_id = box.submit(_job(box, 'sid-busy-peer'))
        _drain(box)

        job = box.find(delivery_id)
        check('a busy peer is retried until it is free',
              job.state == outbox.STATE_DELIVERED and attempts['count'] == 3,
              f'{job.state} after {attempts["count"]} attempts')
        check('and the message is delivered, not recovered',
              job.reply == '늦게 받았습니다' and not job.is_reply_recovered)
    finally:
        outbox.BUSY_RETRY_SECONDS = original


def test_a_shim_busy_answer_is_told_apart_from_a_real_failure() -> None:
    busy = {'ok': False, 'error': 'the panel session is busy with another turn'}
    inflight = {'ok': False, 'error': 'another bridged message is already in flight'}
    # a write that never went through: the message did not leave, so nothing is waited for
    broken = {'ok': False, 'error': 'failed to write to the claude process: EPIPE'}
    # a turn that ended badly after the hand-over: the peer has the message, its transcript
    # is where the answer (or the failure) will be
    aborted = {'ok': False, 'accepted': True, 'error': '{"message": "model overloaded"}'}

    original = bridge.uihook.send
    try:
        for response, expected, label in [
            (busy, outbox.PeerBusyError, 'a busy panel'),
            (inflight, outbox.PeerBusyError, 'a message already in flight'),
            (broken, outbox.NotDeliveredError, 'a broken pipe'),
            (aborted, bridge.BridgeError, 'a turn that failed after the hand-over'),
        ]:
            bridge.uihook.send = lambda *a, **kw: response
            try:
                bridge._call_via_panel('hi', 'sid', {'socket': '/s'}, 5, '/w')
                check(f'{label} raises something', False, 'nothing raised')
            except Exception as e:
                check(f'{label} is classified correctly', isinstance(e, expected),
                      f'{type(e).__name__} for {response["error"]!r}')
    finally:
        bridge.uihook.send = original


def test_a_panel_delivery_keeps_watching_after_the_transport_gives_up() -> None:
    """Giving up listening is not the peer giving up working.

    On the panel path the peer is a session we neither started nor stopped, so the socket
    timing out says nothing about its turn — thirteen deliveries were closed as failed today
    while their answers were being written.
    """
    original = (outbox.RECOVERY_POLL_SECONDS, outbox.RECOVERY_WINDOW_SECONDS)
    outbox.RECOVERY_POLL_SECONDS, outbox.RECOVERY_WINDOW_SECONDS = 0.02, 2
    try:
        box = outbox.Outbox()
        box.deliver = lambda job: (_ for _ in ()).throw(
            RuntimeError('IDE panel relay failed: turn did not complete within 600s'))

        looks = {'count': 0}

        def answer_on_the_third_look(job):
            looks['count'] += 1
            return '늦게 도착한 답' if looks['count'] >= 3 else None

        box.recover = answer_on_the_third_look

        job = _job(box, 'sid-panel', wants_reply=True)
        job.ui_shim = {'socket': '/panel'}  # panel path — the peer isn't something we can kill
        delivery_id = box.submit(job)
        _drain(box)

        finished = box.find(delivery_id)
        check('an answer written after the transport failed is still collected',
              finished.reply == '늦게 도착한 답', f'{finished.state} {finished.reply!r}')
        check('and it is marked as recovered', finished.is_reply_recovered is True)
        check('the transport failure is still recorded', finished.error is not None)
        check('and the record closes as failed, not left as awaiting-peer',
              finished.state == outbox.STATE_FAILED, finished.state)
    finally:
        outbox.RECOVERY_POLL_SECONDS, outbox.RECOVERY_WINDOW_SECONDS = original


def test_a_cli_delivery_does_not_wait_for_a_turn_that_was_killed() -> None:
    """The CLI turn was our own subprocess and the timeout killed its process group, so
    nothing more will be written and waiting would only stall the queue behind it."""
    original = (outbox.RECOVERY_POLL_SECONDS, outbox.RECOVERY_WINDOW_SECONDS)
    outbox.RECOVERY_POLL_SECONDS, outbox.RECOVERY_WINDOW_SECONDS = 0.02, 5
    try:
        box = outbox.Outbox()
        box.deliver = lambda job: (_ for _ in ()).throw(
            RuntimeError('peer agent did not answer within 600s'))
        looks = {'count': 0}
        box.recover = lambda job: (looks.update(count=looks['count'] + 1) or None)

        started = time.time()
        delivery_id = box.submit(_job(box, 'sid-cli', wants_reply=True))  # no ui_shim = CLI path
        _drain(box)
        elapsed = time.time() - started

        check('a killed turn is not waited on', looks['count'] == 1, str(looks))
        check('so the queue is not held open', elapsed < 1.0, f'{elapsed:.2f}s')
        check('and the delivery closes as failed',
              box.find(delivery_id).state == outbox.STATE_FAILED)
    finally:
        outbox.RECOVERY_POLL_SECONDS, outbox.RECOVERY_WINDOW_SECONDS = original


def test_recovery_is_skipped_when_the_transport_already_answered() -> None:
    box = outbox.Outbox()
    box.deliver = lambda job: {'session_id': job.target_session_id,
                               'reply': 'received normally', 'is_new_session': False}
    attempts = []
    box.recover = lambda job: attempts.append(job.delivery_id)

    delivery_id = box.submit(_job(box, 'sid-normal'))
    _drain(box)

    job = box.find(delivery_id)
    check('a delivered answer is not second-guessed', not attempts, str(attempts))
    check('and is not labelled recovered', job.is_reply_recovered is False)


def _write_claude_transcript(path: str, names: list, first_message: str) -> None:
    """A transcript shaped like Claude Code's: the name entry repeats as the session grows."""
    lines = []
    for name in names[:1]:
        lines.append(json.dumps({'type': 'custom-title', 'customTitle': name}))
    lines.append(json.dumps({
        'type': 'user', 'isSidechain': False, 'cwd': '/w', 'entrypoint': 'claude-vscode',
        'message': {'content': first_message}}))
    for name in names[1:]:
        lines.append(json.dumps({'type': 'custom-title', 'customTitle': name}))
    with open(path, 'w', encoding='utf-8') as f:
        f.write('\n'.join(lines) + '\n')


def test_a_conversations_own_name_is_what_it_is_called_by() -> None:
    """The incident's real cause: the name was there and the bridge was not reading it.

    A Claude conversation carries the name the human gave it, shown at the top of its panel and
    stored in the transcript. The bridge was inventing a title from the first message instead,
    so "the koppa_studio session" matched nothing it should have - and matched a path quoted
    inside an unrelated session's opening prompt.
    """
    with tempfile.TemporaryDirectory(prefix='claude-titles-') as store:
        named = store + '/11111111-1111-1111-1111-111111111111.jsonl'
        _write_claude_transcript(named, ['koppa_studio'], '.')
        parsed = discovery._parse_claude_session(named)
        check('a named conversation is titled by its name, not its first message',
              parsed is not None and parsed['title'] == 'koppa_studio',
              str(parsed and parsed['title']))
        check('and is marked as named', parsed is not None and parsed['is_named'] is True)

        # renames happen - 3 of the 11 named sessions on this machine had been renamed
        renamed = store + '/22222222-2222-2222-2222-222222222222.jsonl'
        _write_claude_transcript(renamed, ['studio_v4', 'studio_v4_orginial', 'studio_v4 2nd'],
                                 'first prompt')
        parsed = discovery._parse_claude_session(renamed)
        check('a renamed conversation answers to its current name',
              parsed is not None and parsed['title'] == 'studio_v4 2nd',
              str(parsed and parsed['title']))

        unnamed = store + '/33333333-3333-3333-3333-333333333333.jsonl'
        _write_claude_transcript(unnamed, [], 'implement the thing in /src/koppa_studio')
        parsed = discovery._parse_claude_session(unnamed)
        check('an unnamed conversation still falls back to its first message',
              parsed is not None and 'koppa_studio' in parsed['title'])
        check('but is not marked as named', parsed is not None and parsed['is_named'] is False)

        # the fallback title is not a name: it must not answer to a word inside it
        originals = discovery.list_sessions
        discovery.list_sessions = lambda agent, scope, cwd, limit=500, **kw: [
            discovery._parse_claude_session(unnamed)]
        try:
            check('a word quoted in an unnamed conversation is still not a name',
                  discovery.find_session_by_name('claude', 'koppa_studio') is None)
        finally:
            discovery.list_sessions = originals


def test_a_session_name_matches_exactly_or_not_at_all() -> None:
    """The incident: 'koppa_studio' matched a path quoted inside an old session's first message.

    A Claude session has no name of its own - its title is whatever the human typed first - so
    substring matching turned any quoted path into a name and resumed a months-old session
    headlessly, where nobody was watching it work.
    """
    sessions = [
        {'session_id': 'old-one', 'title': 'K-Oppa Studio 4를 구현하라. 쓰기는 '
                                            '/Users/x/source_code/koppa_studio 아래', 'mtime': 200.0},
        {'session_id': 'audit', 'title': 'Codex 감사 — koppa_studio_v4 결함', 'mtime': 100.0},
        {'session_id': 'named', 'title': 'Studio primer', 'mtime': 50.0},
    ]
    original = discovery.list_sessions
    discovery.list_sessions = lambda agent, scope, cwd, limit=500, **kw: list(sessions)

    try:
        check('a name that only appears inside a title does not match',
              discovery.find_session_by_name('claude', 'koppa_studio') is None)
        check('an exact title still matches',
              (discovery.find_session_by_name('claude', 'Studio primer') or {})
              .get('session_id') == 'named')
        check('matching ignores case and spacing',
              (discovery.find_session_by_name('claude', '  studio   PRIMER ') or {})
              .get('session_id') == 'named')

        near = discovery.suggest_session_names('claude', 'koppa_studio')
        check('the near misses are offered as suggestions instead', len(near) == 2, str(near))
    finally:
        discovery.list_sessions = original


def test_a_named_session_is_never_silently_created() -> None:
    originals = (discovery.find_session, discovery.find_session_by_name,
                 discovery.suggest_session_names)
    discovery.find_session = lambda agent, session_id: None
    discovery.find_session_by_name = lambda agent, name: None
    discovery.suggest_session_names = lambda agent, name, **kw: ['Some other title']

    try:
        bridge._requested_session_id('codex', 'no-such-name', '/w')
        check('an unknown session name fails instead of opening a new conversation', False,
              'no error raised')
    except bridge.BridgeError as e:
        message = str(e)
        check('an unknown session name fails instead of opening a new conversation',
              'no session was created' in message, message)
        check('and the error offers the titles it did see',
              'Some other title' in message, message)
    finally:
        (discovery.find_session, discovery.find_session_by_name,
         discovery.suggest_session_names) = originals


def test_naming_a_session_and_forcing_a_new_one_is_refused() -> None:
    try:
        bridge.send_message('codex', 'hello', session_id='some-name', is_new_session=True)
        check('session_id and new_session cannot be combined', False, 'no error raised')
    except bridge.BridgeError as e:
        check('session_id and new_session cannot be combined',
              'cannot be combined' in str(e) and 'Nothing was sent' in str(e), str(e))


def test_a_delivery_does_not_outlive_the_server_that_started_it() -> None:
    """An orphaned delivery keeps working unwatched, and frees the lock guarding its session.

    So the delivery's *process* must not outlive the server: shutting down kills the CLI and its
    tool tree, and that still holds. What changed on 2026-09-14, at the user's request relayed
    by koppa, is the delivery's *record*: it now does outlive the server, written from the moment
    the delivery is queued, so another server can report it as orphaned and read its answer from
    the peer transcript. Nothing is resumed or resent from it - see
    test_an_orphaned_delivery_is_reported_by_another_server.
    """
    with tempfile.TemporaryDirectory(prefix='orphan-') as work_dir:
        marker = work_dir + '/child.pid'
        command = ['/bin/bash', '-c', f'sleep 45 & echo $! > {marker}; wait']
        started = threading.Event()

        def deliver():
            started.set()
            with contextlib_suppress():
                bridge._run_cli(command, work_dir, dict(os.environ), timeout=40)

        worker = threading.Thread(target=deliver, daemon=True)
        worker.start()
        started.wait(5)

        deadline = time.time() + 5
        while not os.path.exists(marker) and time.time() < deadline:
            time.sleep(0.05)
        grandchild = int(open(marker).read().strip())
        check('the delivery is running before we shut down',
              registry._is_pid_alive(grandchild))

        bridge.terminate_live_children()

        deadline = time.time() + 10
        while registry._is_pid_alive(grandchild) and time.time() < deadline:
            time.sleep(0.1)
        check('shutting the server down takes its deliveries with it',
              not registry._is_pid_alive(grandchild), f'pid {grandchild} still alive')
        worker.join(timeout=5)


# ------------------------------------------- recovery reads finished turns, not fragments

def _claude_line(text: str, stop_reason, request_id: str, at: float, kind: str = 'text') -> str:
    import datetime
    stamp = datetime.datetime.fromtimestamp(at, datetime.timezone.utc).isoformat()
    block = ({'type': 'thinking', 'thinking': text} if kind == 'thinking'
             else {'type': 'text', 'text': text})
    return json.dumps({
        'type': 'assistant', 'requestId': request_id,
        'timestamp': stamp.replace('+00:00', 'Z'),
        'message': {'stop_reason': stop_reason, 'content': [block]},
    })


def _human_line(text: str, at: float) -> str:
    import datetime
    stamp = datetime.datetime.fromtimestamp(at, datetime.timezone.utc).isoformat()
    return json.dumps({'type': 'user', 'timestamp': stamp.replace('+00:00', 'Z'),
                       'message': {'role': 'user', 'content': [{'type': 'text', 'text': text}]}})


def test_recovery_waits_for_a_claude_turn_to_finish() -> None:
    """The incident: a 600s relay timed out, recovery read "reverting provenance now", and the
    requester acted on it as a report. That line was written between two tool calls, and the
    transcript says so - stop_reason=tool_use. Only a turn that ended has an answer."""
    sent_at = 1_788_000_000.0
    with tempfile.TemporaryDirectory(prefix='claude-turns-') as store:
        path = store + '/session.jsonl'
        original = discovery.find_session
        discovery.find_session = lambda agent, sid: {'path': path}
        try:
            lines = [_claude_line('`--source`를 넣느라 provenance가 갈렸습니다. 원래 값으로 되돌립니다.',
                                  'tool_use', 'r1', sent_at + 60)]
            open(path, 'w', encoding='utf-8').write('\n'.join(lines) + '\n')
            progress = discovery.peer_progress('claude', 'sid', after=sent_at)
            check('a message written between tool calls is not an answer',
                  progress['answer'] is None, str(progress))
            check('and the peer is reported as still working', progress['is_working'] is True)

            # the turn ends: thinking and text arrive as two entries of one response
            lines += [_claude_line('정리하자면', 'end_turn', 'r2', sent_at + 300, kind='thinking'),
                      _claude_line('원복 완료. 11장 등록했습니다.', 'end_turn', 'r2', sent_at + 301)]
            open(path, 'w', encoding='utf-8').write('\n'.join(lines) + '\n')
            progress = discovery.peer_progress('claude', 'sid', after=sent_at)
            check('the finished turn is the answer',
                  progress['answer'] == '원복 완료. 11장 등록했습니다.', str(progress['answer']))
            check('and the peer is no longer working', progress['is_working'] is False)
            check('last_agent_message reads the same finished turn',
                  discovery.last_agent_message('claude', 'sid', after=sent_at)
                  == '원복 완료. 11장 등록했습니다.')

            # the human asks something next: the answer stands, and the peer is busy again
            lines.append(_human_line('다음 건 진행해', sent_at + 400))
            open(path, 'w', encoding='utf-8').write('\n'.join(lines) + '\n')
            progress = discovery.peer_progress('claude', 'sid', after=sent_at)
            check('a new human question does not unmake the finished answer',
                  progress['answer'] == '원복 완료. 11장 등록했습니다.')
            check('but does mean the peer is working again', progress['is_working'] is True)
        finally:
            discovery.find_session = original


def _codex_line(kind: str, payload: dict, at: float) -> str:
    import datetime
    stamp = datetime.datetime.fromtimestamp(at, datetime.timezone.utc).isoformat()
    return json.dumps({'timestamp': stamp.replace('+00:00', 'Z'), 'type': kind,
                       'payload': payload})


def test_recovery_reads_the_codex_turn_the_app_server_closed() -> None:
    """A rollout brackets each turn with task_started/task_complete, and the completion names
    the answer. Anything the agent said in between is narration between tool calls."""
    sent_at = 1_788_000_000.0
    token = 'req_1788000000000_abcdef'
    with tempfile.TemporaryDirectory(prefix='codex-turns-') as store:
        path = store + '/rollout.jsonl'
        original = discovery.find_session
        discovery.find_session = lambda agent, sid: {'path': path}
        try:
            lines = [
                _codex_line('event_msg', {'type': 'task_started', 'turn_id': 'tA'}, sent_at + 5),
                _codex_line('response_item', {'type': 'message', 'role': 'assistant',
                                              'content': [{'type': 'output_text',
                                                           'text': '2차 요청서를 확인했습니다. 시작합니다.'}]},
                            sent_at + 30),
            ]
            open(path, 'w', encoding='utf-8').write('\n'.join(lines) + '\n')
            progress = discovery.peer_progress('codex', 'sid', after=sent_at, token=token)
            check('an assistant message inside an open turn is not an answer',
                  progress['answer'] is None, str(progress))
            check('and the thread is reported as working', progress['is_working'] is True)

            lines.append(_codex_line('event_msg', {
                'type': 'task_complete', 'turn_id': 'tA',
                'last_agent_message': f'생성 완료: 11장.\n{token}'}, sent_at + 500))
            open(path, 'w', encoding='utf-8').write('\n'.join(lines) + '\n')
            progress = discovery.peer_progress('codex', 'sid', after=sent_at, token=token)
            check('task_complete carries the answer',
                  (progress['answer'] or '').startswith('생성 완료: 11장.'), str(progress['answer']))
            check('and the thread is idle', progress['is_working'] is False)

            # a completion that names no message falls back to the turn's last spoken item
            lines = [
                _codex_line('event_msg', {'type': 'task_started', 'turn_id': 'tB'}, sent_at + 5),
                _codex_line('event_msg', {'type': 'item_completed', 'turn_id': 'tB',
                                          'item': {'type': 'AgentMessage', 'text': '마지막 항목 발화'}},
                            sent_at + 40),
                _codex_line('event_msg', {'type': 'task_complete', 'turn_id': 'tB',
                                          'last_agent_message': None}, sent_at + 41),
            ]
            open(path, 'w', encoding='utf-8').write('\n'.join(lines) + '\n')
            check('an empty completion is filled from the turn\'s last agent item',
                  discovery.last_agent_message('codex', 'sid', after=sent_at) == '마지막 항목 발화')

            # a rollout without task events (older Codex) still yields its last message
            lines = [_codex_line('response_item', {'type': 'message', 'role': 'assistant',
                                                   'content': [{'type': 'output_text',
                                                                'text': '옛 형식의 답'}]},
                                 sent_at + 10)]
            open(path, 'w', encoding='utf-8').write('\n'.join(lines) + '\n')
            check('a legacy rollout falls back to its last message',
                  discovery.last_agent_message('codex', 'sid', after=sent_at) == '옛 형식의 답')
        finally:
            discovery.find_session = original


def test_a_later_human_turn_does_not_hide_the_echoed_answer() -> None:
    """The panel is the human's session too. After the peer answered us, the human asked it
    something else - both turns postdate our request, and only one of them echoes our id."""
    sent_at = 1_788_000_000.0
    mine = 'req_1788000000000_aaaaaa'
    with tempfile.TemporaryDirectory(prefix='claude-turns-') as store:
        path = store + '/session.jsonl'
        original = discovery.find_session
        discovery.find_session = lambda agent, sid: {'path': path}
        try:
            lines = [_claude_line(f'등록 완료했습니다.\n{mine}', 'end_turn', 'r1', sent_at + 100),
                     _claude_line('네, 다음은 소라 세트입니다.', 'end_turn', 'r2', sent_at + 900)]
            open(path, 'w', encoding='utf-8').write('\n'.join(lines) + '\n')
            check('the turn that echoes our id is the answer, not the newer human exchange',
                  (discovery.last_agent_message('claude', 'sid', after=sent_at, token=mine) or '')
                  .startswith('등록 완료했습니다.'))

            # a peer that echoes nothing: timing decides, as before
            lines = [_claude_line('토큰 없이 답합니다.', 'end_turn', 'r1', sent_at + 100)]
            open(path, 'w', encoding='utf-8').write('\n'.join(lines) + '\n')
            check('a peer that echoed nothing still falls back to the clock',
                  discovery.last_agent_message('claude', 'sid', after=sent_at, token=mine)
                  == '토큰 없이 답합니다.')
        finally:
            discovery.find_session = original


# ------------------------------------ the outbox tells the truth about how a delivery ended

def test_giving_up_watching_closes_the_delivery_as_failed() -> None:
    """The residue: 26 records sat in awaiting-peer forever, because the watch set that state
    and nothing set it back when the watch ended empty."""
    original = (outbox.RECOVERY_POLL_SECONDS, outbox.RECOVERY_WINDOW_SECONDS)
    outbox.RECOVERY_POLL_SECONDS, outbox.RECOVERY_WINDOW_SECONDS = 0.02, 0.1
    try:
        box = outbox.Outbox()
        box.deliver = lambda job: (_ for _ in ()).throw(
            RuntimeError('IDE panel relay failed: turn still running after 3600s'))
        box.recover = lambda job: None

        job = _job(box, 'sid-silent', wants_reply=True)
        job.ui_shim = {'socket': '/panel'}
        delivery_id = box.submit(job)
        _drain(box)

        finished = box.find(delivery_id)
        check('a watch that ends empty closes the delivery as failed',
              finished.state == outbox.STATE_FAILED, finished.state)
        check('and keeps the transport error', finished.error is not None)
    finally:
        outbox.RECOVERY_POLL_SECONDS, outbox.RECOVERY_WINDOW_SECONDS = original


def test_a_reply_counts_as_delivered_once_the_peer_takes_it() -> None:
    """36 replies were recorded as failed today. Each had landed; what timed out was the
    sender's own next turn, which nobody needed to wait for."""
    awaits = []
    original = (bridge.uihook.send, bridge.uihook.await_turn)
    bridge.uihook.send = lambda *a, **kw: {
        'ok': False, 'pending': True, 'accepted': True, 'injectionId': 'xagent-1',
        'sessionId': 'sender-sid', 'error': 'turn still running after 0s'}
    bridge.uihook.await_turn = lambda shim, injection_id, timeout: (
        awaits.append(injection_id) or {'ok': True, 'sessionId': 'sender-sid', 'reply': 'x'})
    accepted = []
    try:
        result = bridge._call_via_panel('answer', 'sender-sid', {'socket': '/s'}, 600, '/w',
                                        on_accepted=lambda r: accepted.append(r), wants_result=False)
        check('a reply returns as soon as the peer has it', result['session_id'] == 'sender-sid')
        check('without waiting for the sender\'s next turn', awaits == [], str(awaits))
        check('and acceptance is reported', len(accepted) == 1)
    finally:
        bridge.uihook.send, bridge.uihook.await_turn = original


def test_a_panel_request_is_listened_to_until_the_turn_ends() -> None:
    """Turns of 500..820s were routine and the socket wait was 600s. Now the first wait only
    covers the hand-over; the answer is collected with as many awaits as the turn takes."""
    awaits = []
    original = (bridge.uihook.send, bridge.uihook.await_turn)
    bridge.uihook.send = lambda *a, **kw: {
        'ok': False, 'pending': True, 'accepted': True, 'injectionId': 'xagent-2',
        'sessionId': 'peer-sid'}

    def await_turn(shim, injection_id, timeout):
        awaits.append(timeout)
        if len(awaits) < 3:
            return {'ok': False, 'pending': True, 'accepted': True, 'injectionId': injection_id,
                    'sessionId': 'peer-sid', 'partial': '아직'}
        return {'ok': True, 'sessionId': 'peer-sid', 'reply': '11장 등록 완료'}

    bridge.uihook.await_turn = await_turn
    try:
        result = bridge._call_via_panel('do it', 'peer-sid', {'socket': '/s'}, 600, '/w',
                                        wants_result=True, patience=3600)
        check('the answer is collected after the turn ends', result['reply'] == '11장 등록 완료')
        check('across as many awaits as it took', len(awaits) == 3, str(awaits))
        check('each bounded by the await chunk',
              all(t <= bridge.PANEL_AWAIT_CHUNK_SECONDS for t in awaits), str(awaits))

        # patience is a ceiling, not a verdict: past it the transcript watch takes over
        def never_ends(shim, injection_id, timeout):
            time.sleep(0.02)
            return {'ok': False, 'pending': True, 'accepted': True, 'injectionId': injection_id}

        bridge.uihook.await_turn = never_ends
        try:
            bridge._call_via_panel('do it', 'peer-sid', {'socket': '/s'}, 600, '/w',
                                   wants_result=True, patience=0.01)
            check('running out of patience raises', False, 'nothing raised')
        except bridge.BridgeError as e:
            check('running out of patience hands over to the transcript watch',
                  'still running' in str(e) and 'transcript' in str(e), str(e))
        except Exception as e:
            check('running out of patience raises a BridgeError', False, repr(e))
    finally:
        bridge.uihook.send, bridge.uihook.await_turn = original


def test_a_refusal_is_told_apart_from_a_broken_transport() -> None:
    cases = [
        ({'ok': False, 'accepted': False, 'error': 'thread x is not open in this panel'},
         outbox.NotDeliveredError, 'a current shim saying it never landed'),
        ({'ok': False, 'accepted': True, 'error': '{"message": "turn failed"}'},
         bridge.BridgeError, 'a current shim reporting a failed turn'),
        ({'ok': False, 'error': 'IDE panel relay failed: {"code": -32600, "message": "thread not found: 01a0"}'},
         outbox.NotDeliveredError, 'an older shim saying thread not found'),
        ({'ok': False, 'error': 'turn did not complete within 600s'},
         bridge.BridgeError, 'an older shim timing out'),
        ({'ok': False, 'error': 'the panel session is busy with another turn'},
         outbox.PeerBusyError, 'a busy peer'),
        ({'ok': False, 'accepted': False, 'error': 'panel shim unreachable: ConnectionRefusedError'},
         outbox.NotDeliveredError, 'a socket nobody answers'),
    ]
    for response, expected, label in cases:
        try:
            bridge._raise_for_panel_failure(response)
            check(f'{label} raises', False, 'nothing raised')
        except Exception as e:
            check(f'{label} is classified as {expected.__name__}', isinstance(e, expected),
                  type(e).__name__)
    check('a pending answer is not a failure',
          bridge._raise_for_panel_failure({'ok': False, 'pending': True}) is None)


def test_an_undelivered_request_is_refused_while_the_caller_is_still_there() -> None:
    """The lost-order case: send_to_codex said accepted=true, the app server said "thread not
    found" a second later, and nobody was told. The caller is still on the line for that
    second, so the refusal goes straight back to it."""
    notices = []
    originals = (bridge.caller.detect_caller, bridge._own_session_id, bridge._resolve_target,
                 bridge.uihook.send, outbox.OUTBOX.build_notice, bridge.registry.touch_pin)
    bridge.caller.detect_caller = lambda: {'agent': config.AGENT_CLAUDE, 'chain': []}
    bridge._own_session_id = lambda agent: 'sender-sid'
    bridge._resolve_target = lambda *a, **kw: {
        'agent': config.AGENT_CODEX, 'session_id': 'peer-' + uuid_hex(), 'cwd': None,
        'source': 'ide-panel', 'ui_shim': {'socket': '/nowhere', 'pid': 1}}
    bridge.uihook.send = lambda *a, **kw: {
        'ok': False, 'accepted': False,
        'error': '{"code": -32600, "message": "thread not found: 01a06f72"}'}
    outbox.OUTBOX.build_notice = lambda job: notices.append(job.delivery_id)
    bridge.registry.touch_pin = lambda agent, cwd: None
    try:
        started = time.time()
        result = bridge.send_message(config.AGENT_CODEX, 'register the images',
                                     conversation_id='conv_refused_' + uuid_hex())
        check('the refusal comes back in the tool result',
              result.get('ok') is False and result.get('accepted') is False, str(result)[:200])
        check('naming the cause', 'thread not found' in (result.get('error') or ''),
              str(result.get('error')))
        check('and saying the peer never got it', result.get('is_undelivered') is True)
        check('within seconds, not after a recovery window',
              time.time() - started < outbox.EARLY_FAILURE_WINDOW_SECONDS + 2,
              f'{time.time() - started:.1f}s')
        time.sleep(0.2)
        check('no notice is sent on top of the direct answer', notices == [], str(notices))
    finally:
        (bridge.caller.detect_caller, bridge._own_session_id, bridge._resolve_target,
         bridge.uihook.send, outbox.OUTBOX.build_notice, bridge.registry.touch_pin) = originals


def test_a_late_failure_is_announced_into_the_senders_session() -> None:
    """When the caller has already been told "accepted" and the delivery then fails, the
    failure has to travel the same way an answer would."""
    box = outbox.Outbox()
    outcomes = []

    def deliver(job):
        if job.kind == outbox.KIND_NOTICE:
            outcomes.append(('notice', job.target_session_id))
            return {'session_id': job.target_session_id, 'reply': '', 'is_new_session': False}
        raise outbox.NotDeliveredError('IDE panel relay refused: thread not found: 01a0')

    box.deliver = deliver
    box.recover = lambda job: outcomes.append(('recover', job.delivery_id))
    box.build_notice = lambda job: _notice(box, job)

    job = _job(box, 'sid-gone', wants_reply=True)
    job.report_failures_until = 0.0  # the caller left long ago
    delivery_id = box.submit(job)
    _drain(box)

    failed = box.find(delivery_id)
    check('the request closes as failed', failed.state == outbox.STATE_FAILED)
    check('marked as never delivered', failed.is_undelivered is True)
    check('nothing is recovered for a message that never landed',
          ('recover', delivery_id) not in outcomes, str(outcomes))
    check('and a notice is delivered to the sender', ('notice', 'sender-sid') in outcomes,
          str(outcomes))

    # the same failure inside the caller's window is the caller's to hear, not a notice
    outcomes.clear()
    job = _job(box, 'sid-gone', wants_reply=True)
    job.report_failures_until = time.time() + 5
    box.submit(job)
    _drain(box)
    check('a failure the caller is told about directly is not also announced',
          not any(kind == 'notice' for kind, _ in outcomes), str(outcomes))


def _notice(box: outbox.Outbox, failed: outbox.Job) -> outbox.Job:
    return outbox.Job(
        target_agent=failed.sender_agent, target_session_id=failed.sender_session_id,
        payload='=== CROSS-AGENT BRIDGE DELIVERY FAILED ===', run_cwd='/tmp', pin_cwd='/tmp',
        env={}, timeout=5, ui_shim=None, title=None, conversation_id=failed.conversation_id,
        hop=failed.hop, sender_agent=failed.target_agent,
        sender_session_id=failed.target_session_id, wants_reply=False, summary='notice',
        kind=outbox.KIND_NOTICE)


def test_a_reply_that_did_not_land_is_rerouted_and_retried() -> None:
    """The sender's tab was reopened while the peer worked: new process, new socket, and the
    old route refuses the connection. The answer is re-aimed and tried again."""
    original = outbox.UNDELIVERED_RETRY_SECONDS
    outbox.UNDELIVERED_RETRY_SECONDS = 0.01
    try:
        box = outbox.Outbox()
        attempts = {'count': 0}
        rerouted = []

        def deliver(job):
            attempts['count'] += 1
            if attempts['count'] == 1:
                raise outbox.NotDeliveredError('panel shim unreachable: ConnectionRefusedError')
            return {'session_id': job.target_session_id, 'reply': '', 'is_new_session': False}

        box.deliver = deliver
        box.reroute = lambda job: rerouted.append(job.delivery_id)

        delivery_id = box.submit(_job(box, 'sender-sid'))  # wants_reply=False: a reply
        _drain(box)
        job = box.find(delivery_id)
        check('a reply that never landed is tried again', job.state == outbox.STATE_DELIVERED,
              f'{job.state} {job.error}')
        check('after being re-aimed', rerouted == [delivery_id], str(rerouted))
        check('and the attempt count says so', job.attempts == 2, str(job.attempts))

        # a request is not retried blindly: its sender is told and decides
        attempts['count'] = 0
        rerouted.clear()
        delivery_id = box.submit(_job(box, 'peer-sid', wants_reply=True))
        _drain(box)
        job = box.find(delivery_id)
        check('a request that never landed is not resent on its own',
              job.state == outbox.STATE_FAILED and job.attempts == 1 and not rerouted,
              f'{job.state} attempts={job.attempts} rerouted={rerouted}')
    finally:
        outbox.UNDELIVERED_RETRY_SECONDS = original


# --------------------------------------------------- the shims keep a turn past the socket

class FakeAppServer:
    """Answers the shim's injected requests the way `codex app-server` would."""

    def __init__(self, shim, is_thread_loaded: bool) -> None:
        self.shim = shim
        self.is_thread_loaded = is_thread_loaded
        self.writes = []
        self.completes_turns = True

    def write(self, payload: str) -> None:
        message = json.loads(payload)
        self.writes.append(message['method'])
        threading.Timer(0.01, self._respond, args=(message,)).start()

    def _respond(self, message: dict) -> None:
        method, request_id, params = message['method'], message['id'], message['params']
        if method == 'thread/resume':
            self.is_thread_loaded = True
            self.shim._observe_from_server({'id': request_id, 'result': {
                'thread': {'id': params['threadId']}}})
        elif method == 'turn/start':
            if not self.is_thread_loaded:
                self.shim._observe_from_server({'id': request_id, 'error': {
                    'code': -32600, 'message': f'thread not found: {params["threadId"]}'}})
                return
            self.shim._observe_from_server({'id': request_id, 'result': {'turn': {'id': 'turn-1'}}})
            if self.completes_turns:
                self.complete(params['threadId'])

    def complete(self, thread_id: str) -> None:
        self.shim._observe_from_server({'method': 'item/completed', 'params': {
            'threadId': thread_id, 'turnId': 'turn-1',
            'item': {'type': 'agentMessage', 'text': 'PANEL_OK'}}})
        self.shim._observe_from_server({'method': 'turn/completed', 'params': {
            'threadId': thread_id, 'turn': {'id': 'turn-1', 'status': 'completed'}}})


def _codex_shim():
    from cross_agent_mcp.appserver_shim import CodexAppServerShim
    shim = CodexAppServerShim.__new__(CodexAppServerShim)
    shim.threads = {}
    shim.injections = {}
    shim.opens = {}
    shim.turns = {}
    shim.approvals = {}
    shim.item_threads = {}
    shim.open_turns = {}
    shim.state_lock = threading.Lock()
    shim.stdin_lock = threading.Lock()
    shim.last_user_activity = 0.0
    shim.argv = []
    shim.real_codex = ''
    shim.log = _quiet_log()
    return shim


def _claude_shim(session_id: str = 'sess-1'):
    from cross_agent_mcp.claude_shim import ClaudeStreamShim
    shim = ClaudeStreamShim.__new__(ClaudeStreamShim)
    shim.session_id = session_id
    shim.cwd = '/w'
    shim.is_turn_active = False
    shim.injection = None
    shim.turns = {}
    shim.awaiting_approval = None
    shim.state_lock = threading.Lock()
    shim.stdin_lock = threading.Lock()
    shim.last_seen = time.time()
    shim.last_user_activity = 0.0
    shim.argv = []
    shim.real_binary = ''
    shim.log = _quiet_log()
    return shim


def test_the_codex_shim_reloads_a_thread_the_app_server_forgot() -> None:
    """Six orders were lost to "thread not found" today, and the only cure was a human typing
    into the Codex panel - which makes the extension send thread/resume. The shim sends it."""
    shim = _codex_shim()
    server = FakeAppServer(shim, is_thread_loaded=False)
    shim.write_to_child = server.write
    shim._observe_from_client({'method': 'turn/start', 'params': {'threadId': 'thr-1'}})

    result = shim.inject('hello', 'thr-1', timeout=5)
    check('a thread the app server forgot is resumed and the turn goes through',
          result.get('ok') is True and result.get('reply') == 'PANEL_OK', str(result)[:200])
    check('by resuming between the two attempts',
          server.writes == ['turn/start', 'thread/resume', 'turn/start'], str(server.writes))
    check('and the thread is known to be loaded again', shim._is_loaded('thr-1'))

    # a resume that fails leaves an honest refusal, marked as never delivered
    shim = _codex_shim()
    server = FakeAppServer(shim, is_thread_loaded=False)
    original_respond = server._respond

    def never_resumes(message):
        if message['method'] == 'thread/resume':
            shim._observe_from_server({'id': message['id'], 'error': {
                'code': -32600, 'message': 'no rollout found for thread'}})
            return
        original_respond(message)

    server._respond = never_resumes
    shim.write_to_child = server.write
    shim._observe_from_client({'method': 'turn/start', 'params': {'threadId': 'thr-2'}})
    result = shim.inject('hello', 'thr-2', timeout=5)
    check('a thread that cannot be brought back is refused, not waited on',
          result.get('ok') is False and result.get('accepted') is False
          and 'thread not found' in result.get('error', '')
          and 'thread/resume' in result.get('error', ''), str(result)[:300])


def test_the_codex_shim_resumes_first_when_it_saw_the_thread_close() -> None:
    shim = _codex_shim()
    server = FakeAppServer(shim, is_thread_loaded=True)
    shim.write_to_child = server.write
    shim._observe_from_client({'method': 'turn/start', 'params': {'threadId': 'thr-3'}})

    shim._observe_from_server({'method': 'thread/closed', 'params': {'threadId': 'thr-3'}})
    check('thread/closed marks the thread as unloaded', not shim._is_loaded('thr-3'))
    check('but does not forget it', 'thr-3' in shim.threads)

    server.is_thread_loaded = False
    result = shim.inject('hello', None, timeout=5)
    check('a thread seen closing is resumed before the turn is started',
          server.writes[:2] == ['thread/resume', 'turn/start'] and result.get('ok') is True,
          f'{server.writes} {str(result)[:120]}')

    shim._observe_from_server({'method': 'thread/status/changed', 'params': {
        'threadId': 'thr-3', 'status': {'type': 'notLoaded'}}})
    check('status notLoaded marks it as unloaded too', not shim._is_loaded('thr-3'))


def test_a_codex_turn_outliving_the_first_wait_can_be_awaited() -> None:
    """The 600s cut. The shim used to drop the turn when the socket wait ended; now the socket
    returns a receipt at acceptance and the turn is collected later, however long it takes."""
    shim = _codex_shim()
    server = FakeAppServer(shim, is_thread_loaded=True)
    server.completes_turns = False
    shim.write_to_child = server.write
    shim._observe_from_client({'method': 'turn/start', 'params': {'threadId': 'thr-4'}})

    receipt = shim.inject('long task', 'thr-4', timeout=600, accept_timeout=2)
    check('the hand-over returns a receipt, not an answer',
          receipt.get('pending') is True and receipt.get('accepted') is True
          and receipt.get('injectionId'), str(receipt)[:200])
    check('with the turn id the app server assigned', receipt.get('turnId') == 'turn-1')

    still = shim.await_turn(receipt['injectionId'], timeout=0.05)
    check('awaiting before the turn ends says so', still.get('pending') is True, str(still)[:120])

    threading.Timer(0.05, server.complete, args=('thr-4',)).start()
    done = shim.await_turn(receipt['injectionId'], timeout=2)
    check('awaiting after the turn ends returns the answer',
          done.get('ok') is True and done.get('reply') == 'PANEL_OK', str(done)[:200])

    again = shim.await_turn(receipt['injectionId'], timeout=0.05)
    check('a collected turn is gone', again.get('ok') is False and 'no turn' in again.get('error', ''))

    server.completes_turns = True
    old_style = shim.inject('x', 'thr-4', timeout=5)
    check('an old-style send without acceptTimeout still waits for the whole turn',
          old_style.get('ok') is True and old_style.get('reply') == 'PANEL_OK', str(old_style)[:160])


def test_the_claude_shim_hands_over_and_reports_later() -> None:
    from cross_agent_mcp.claude_shim import ClaudeStreamShim
    shim = ClaudeStreamShim.__new__(ClaudeStreamShim)
    shim.session_id = 'sess-1'
    shim.cwd = '/w'
    shim.is_turn_active = False
    shim.injection = None
    shim.turns = {}
    shim.awaiting_approval = None
    shim.state_lock = threading.Lock()
    shim.stdin_lock = threading.Lock()
    shim.last_seen = time.time()
    shim.last_user_activity = 0.0
    shim.log = _quiet_log()
    writes = []
    shim.write_to_child = lambda payload: writes.append(json.loads(payload))

    receipt = shim.inject('hello', 'sess-1', timeout=600, accept_timeout=2)
    check('the write going through is the receipt',
          receipt.get('pending') is True and receipt.get('accepted') is True, str(receipt)[:160])
    check('and the panel session is marked busy with it', shim.is_turn_active is True)

    shim._observe_from_agent({'type': 'assistant', 'session_id': 'sess-1',
                              'message': {'content': [{'type': 'text', 'text': '작업 중'}]}})
    still = shim.await_turn(receipt['injectionId'], timeout=0.05)
    check('mid-turn narration is only a partial', still.get('pending') is True
          and still.get('partial') == '작업 중', str(still)[:160])

    shim._observe_from_agent({'type': 'result', 'session_id': 'sess-1', 'result': '완료했습니다'})
    done = shim.await_turn(receipt['injectionId'], timeout=1)
    check('the CLI\'s own result ends the turn', done.get('ok') is True
          and done.get('reply') == '완료했습니다', str(done)[:160])
    check('and frees the session for the next message',
          shim.injection is None and shim.is_turn_active is False)

    check('a message for another session is refused as never landed',
          shim.inject('x', 'other', timeout=1).get('accepted') is False)


def test_a_long_panel_turn_keeps_its_busy_lock() -> None:
    """Patience now runs past two turn budgets, which is where another process used to judge a
    lock abandoned and start a second turn on the same session."""
    session_id = 'unit-ttl-' + os.urandom(4).hex()
    path = registry._lock_path(config.AGENT_CODEX, session_id)

    def age(by_seconds: float) -> None:
        record = json.load(open(path, encoding='utf-8'))
        record['started_at'] -= by_seconds
        json.dump(record, open(path, 'w', encoding='utf-8'))

    with registry.busy_lock(config.AGENT_CODEX, session_id, 'conv_ttl', ttl_seconds=5000):
        age(config.SEND_TIMEOUT_SECONDS * 2 + 60)
        check('a lock that declared its patience survives past two turn budgets',
              registry.read_busy_lock(config.AGENT_CODEX, session_id) is not None)
        age(5000)
        check('but not past its own patience',
              registry.read_busy_lock(config.AGENT_CODEX, session_id) is None)

    with registry.busy_lock(config.AGENT_CODEX, session_id, 'conv_ttl'):
        age(config.SEND_TIMEOUT_SECONDS * 2 + 60)
        check('a lock without a declared patience is judged as before',
              registry.read_busy_lock(config.AGENT_CODEX, session_id) is None)


def test_delivery_report_rereads_the_peer_transcript() -> None:
    """The follow-up read koppa asked for: a recovered fragment, or a notice that the peer may
    still be working, and a way to look again once it has finished."""
    original_dir = outbox.config.DELIVERY_DIR
    original_progress = bridge.discovery.peer_progress
    with tempfile.TemporaryDirectory(prefix='cross-agent-deliveries-') as store:
        outbox.config.DELIVERY_DIR = store + '/'
        try:
            box = outbox.Outbox()
            box.deliver = lambda job: (_ for _ in ()).throw(RuntimeError('turn still running'))
            box.recover = lambda job: None
            job = _job(box, 'peer-sid', wants_reply=True)
            delivery_id = box.submit(job)
            _drain(box)
            outbox._write_record(box.find(delivery_id).describe())

            asked = {}

            def progress(agent, session_id, after=None, token=None):
                asked.update(agent=agent, session_id=session_id, after=after, token=token)
                return {'answer': '늦게 완성된 최종 보고', 'answered_at': 1.0, 'is_working': False,
                        'last_turn_finished_at': 1.0, 'transcript_mtime': 1.0}

            bridge.discovery.peer_progress = progress
            report = bridge.delivery_report(delivery_id)
            check('a known delivery is reported', report.get('ok') is True, str(report)[:200])
            check('with a fresh read of the peer transcript',
                  report.get('peer_transcript', {}).get('answer') == '늦게 완성된 최종 보고')
            check('filtered by the request time and matched by its id',
                  asked.get('after') == job.started_at and asked.get('token') == delivery_id,
                  str(asked))
            check('an unknown delivery says so',
                  bridge.delivery_report('req_0_000000').get('ok') is False)
        finally:
            outbox.config.DELIVERY_DIR = original_dir
            bridge.discovery.peer_progress = original_progress


# ------------------------------------- the koppa report: four defects seen from the other side

def test_a_finished_turn_that_echoes_the_request_ends_the_wait() -> None:
    """Defect 1: the shim said "still running" for an hour while the answer - request id and
    all - sat finished in the transcript, and five messages queued up behind the lock."""
    original = (bridge.uihook.send, bridge.uihook.await_turn, bridge.discovery.peer_progress)
    awaits = []
    asked = []
    bridge.uihook.send = lambda *a, **kw: {
        'ok': False, 'pending': True, 'accepted': True, 'injectionId': 'inj-1',
        'sessionId': 'peer-sid'}

    def never_ends(shim, injection_id, timeout):
        awaits.append(timeout)
        time.sleep(0.01)
        return {'ok': False, 'pending': True, 'accepted': True, 'injectionId': injection_id,
                'sessionId': 'peer-sid'}

    def progress(agent, session_id, after=None, token=None):
        asked.append((agent, session_id, token))
        return {'answer': f'{token} 등록 완료' if token else '등록 완료',
                'answered_at': (after or 0) + 1, 'is_working': False}

    bridge.uihook.await_turn = never_ends
    bridge.discovery.peer_progress = progress
    try:
        result = bridge._call_via_panel('do it', 'peer-sid', {'socket': '/s'}, 600, '/w',
                                        wants_result=True, patience=5, target_agent='codex',
                                        request_token='req_1_abcdef')
        check('a finished turn that echoes the request id is the answer',
              result['reply'] == 'req_1_abcdef 등록 완료'
              and result['is_reply_confirmed_by_transcript'] is True, str(result)[:200])
        check('found without waiting for the shim to say so', not awaits, str(awaits))
        check('and looked up by the request id',
              asked and asked[0] == ('codex', 'peer-sid', 'req_1_abcdef'), str(asked))

        # a finished turn that does not echo the request is somebody else's turn
        bridge.discovery.peer_progress = lambda agent, session_id, after=None, token=None: {
            'answer': '다른 턴의 답', 'answered_at': 1.0, 'is_working': False}
        try:
            bridge._call_via_panel('do it', 'peer-sid', {'socket': '/s'}, 600, '/w',
                                   wants_result=True, patience=0.05, target_agent='codex',
                                   request_token='req_1_abcdef')
            check('a finished turn without the echo does not end the wait', False, 'nothing raised')
        except bridge.BridgeError as e:
            check('a finished turn without the echo does not end the wait',
                  'still running' in str(e), str(e))

        # with no request id there is nothing to match, so the transcript is not consulted
        asked.clear()
        bridge.discovery.peer_progress = lambda *a, **kw: asked.append(kw) or {'answer': 'x'}
        try:
            bridge._call_via_panel('do it', 'peer-sid', {'socket': '/s'}, 600, '/w',
                                   wants_result=True, patience=0.05, target_agent='codex',
                                   request_token=None)
        except bridge.BridgeError:
            pass
        check('without a request id the transcript is not consulted', not asked, str(asked))
        check('and a shim that lost the turn is asked again within half a minute',
              bridge.PANEL_AWAIT_CHUNK_SECONDS <= 30, str(bridge.PANEL_AWAIT_CHUNK_SECONDS))
    finally:
        bridge.uihook.send, bridge.uihook.await_turn, bridge.discovery.peer_progress = original


def test_the_caller_named_by_its_metadata_is_the_return_address() -> None:
    """Defect 2: four replies went to the busiest thread of the window instead of the thread
    that asked. Codex names its thread on every tool call; that name outranks the guess."""
    from cross_agent_mcp import server
    check('codex names its thread on every tool call',
          server.caller_session_from_meta(
              {'x-codex-turn-metadata': {'thread_id': 'thr-9', 'turn_id': 't-1'}}) == 'thr-9')
    check('claude attaches nothing of the kind, and that is fine',
          server.caller_session_from_meta({'progressToken': 3}) is None
          and server.caller_session_from_meta(None) is None)

    captured = []
    originals = (bridge.caller.detect_caller, bridge._own_session_id, bridge._resolve_target,
                 outbox.OUTBOX.submit, outbox.OUTBOX.await_outcome, bridge.registry.touch_pin)
    bridge.caller.detect_caller = lambda: {'agent': config.AGENT_CODEX, 'chain': []}
    bridge._own_session_id = lambda agent: 'busiest-thread'
    bridge._resolve_target = lambda *a, **kw: {
        'agent': config.AGENT_CLAUDE, 'session_id': 'peer-sid', 'cwd': None,
        'source': 'ide-panel', 'ui_shim': {'socket': '/s', 'pid': 1}}
    outbox.OUTBOX.submit = lambda job: captured.append(job) or job.delivery_id
    outbox.OUTBOX.await_outcome = lambda job: None
    bridge.registry.touch_pin = lambda agent, cwd: None
    try:
        bridge.send_message(config.AGENT_CLAUDE, 'question', caller_session_id='thr-9')
        check('the thread that called is the return address',
              captured[-1].sender_session_id == 'thr-9', str(captured[-1].sender_session_id))
        check('and is what the envelope tells the peer to answer',
              'thr-9' in captured[-1].payload and 'busiest-thread' not in captured[-1].payload)

        bridge.send_message(config.AGENT_CLAUDE, 'question')
        check('a caller that does not name itself is placed by the process tree, as before',
              captured[-1].sender_session_id == 'busiest-thread',
              str(captured[-1].sender_session_id))
    finally:
        (bridge.caller.detect_caller, bridge._own_session_id, bridge._resolve_target,
         outbox.OUTBOX.submit, outbox.OUTBOX.await_outcome, bridge.registry.touch_pin) = originals


def test_a_recovered_reply_says_why_the_transport_failed() -> None:
    """Defect 3: "RECOVERED, NOT RECEIVED" with no reason left the reader unable to tell a panel
    that refused the message from a socket that ran out of patience."""
    reason = 'IDE panel relay failed: the peer turn is still running after 3600s'
    told = bridge._build_reply_envelope('codex', 'claude', 'conv_x', 1, 3, '답', 'sid',
                                        is_recovered=True, failure_reason=reason)
    check('a recovered reply names the transport failure in its header',
          f'transport failure: {reason}' in told, told)
    check('and again in the note', f'Why the transport failed: {reason}' in told)

    untold = bridge._build_reply_envelope('codex', 'claude', 'conv_x', 1, 3, '답', 'sid',
                                          is_recovered=True)
    check('a reason that was not recorded says so rather than nothing',
          'Why the transport failed: not recorded' in untold)
    plain = bridge._build_reply_envelope('codex', 'claude', 'conv_x', 1, 3, '답', 'sid')
    check('a reply that arrived normally carries no failure talk',
          'transport failure' not in plain and 'Why the transport failed' not in plain)

    original = discovery.find_session
    discovery.find_session = lambda agent, session_id: None
    try:
        request = outbox.Job(
            target_agent=config.AGENT_CODEX, target_session_id='peer-sid', payload='x',
            run_cwd='/w', pin_cwd='/w', env={}, timeout=5, ui_shim=None, title=None,
            conversation_id='conv_r', hop=1, sender_agent=config.AGENT_CLAUDE,
            sender_session_id='sender-sid', wants_reply=True, summary='req')
        request.is_reply_recovered = True
        request.error = 'BridgeError: ' + reason
        reply = bridge._build_reply_job(request, '확인 중입니다')
        check('the job\'s recorded error is the reason the reader gets',
              reply is not None and f'Why the transport failed: BridgeError: {reason}' in reply.payload,
              str(reply and reply.payload)[:300])
    finally:
        discovery.find_session = original


def test_the_claude_shim_sees_a_turn_waiting_on_a_human() -> None:
    """Defect 4: a turn paused on a permission prompt writes nothing, so from the transcript
    it looks exactly like a turn that is working. The shim sees the prompt go by."""
    shim = _claude_shim()
    shim._observe_from_client({'type': 'user', 'message': {'role': 'user', 'content': 'go'}})
    shim._observe_from_agent({'type': 'control_request', 'request_id': 'r1',
                              'request': {'subtype': 'can_use_tool', 'tool_name': 'Bash'}})
    session = shim.status()['sessions'][0]
    check('a permission prompt marks the session as waiting on a human',
          (session.get('awaiting_approval') or {}).get('kind') == 'can_use_tool'
          and session['awaiting_approval'].get('tool') == 'Bash'
          and session.get('is_turn_active') is True, str(session)[:200])
    check('and says how long it has waited',
          'waiting_seconds' in (session.get('awaiting_approval') or {}), str(session)[:200])

    shim._observe_from_client({'type': 'control_response',
                               'response': {'request_id': 'r1', 'subtype': 'success'}})
    check('the human answering clears it',
          shim.status()['sessions'][0].get('awaiting_approval') is None)

    shim._observe_from_agent({'type': 'control_request', 'request_id': 'r2',
                              'request': {'subtype': 'request_user_dialog'}})
    check('a dialog counts too',
          (shim.status()['sessions'][0].get('awaiting_approval') or {}).get('kind')
          == 'request_user_dialog')
    shim._observe_from_agent({'type': 'result', 'session_id': 'sess-1', 'result': 'done'})
    session = shim.status()['sessions'][0]
    check('the turn ending clears it whatever happened to the prompt',
          session.get('awaiting_approval') is None and session.get('is_turn_active') is False,
          str(session)[:200])

    shim._observe_from_agent({'type': 'control_request', 'request_id': 'r3',
                              'request': {'subtype': 'initialize'}})
    check('a control request that is not a prompt is not a wait',
          shim.status()['sessions'][0].get('awaiting_approval') is None)


def test_the_codex_shim_sees_a_turn_waiting_on_a_human() -> None:
    shim = _codex_shim()
    shim._observe_from_client({'method': 'turn/start', 'params': {'threadId': 'thr-1'}})
    shim._observe_from_server({'method': 'turn/started',
                               'params': {'threadId': 'thr-1', 'turn': {'id': 'turn-1'}}})
    shim._observe_from_server({'method': 'item/started', 'params': {
        'threadId': 'thr-1', 'item': {'id': 'item-1', 'type': 'commandExecution'}}})

    is_swallowed = shim._observe_from_server({
        'id': 7, 'method': 'item/commandExecution/requestApproval',
        'params': {'itemId': 'item-1', 'threadId': 'thr-1', 'command': 'rm -rf build'}})
    check('an approval request still reaches the extension', is_swallowed is False)
    thread = shim.status()['threads'][0]
    check('and the thread is reported as waiting on a human',
          thread.get('is_turn_active') is True
          and (thread.get('awaiting_approval') or {}).get('kind')
          == 'item/commandExecution/requestApproval', str(thread)[:240])

    shim._observe_from_server({'id': 8, 'method': 'item/permissions/requestApproval',
                               'params': {'itemId': 'item-1'}})
    check('a request naming only the item is tied to its thread through the item',
          shim.approvals[8]['thread_id'] == 'thr-1', str(shim.approvals.get(8)))

    shim._observe_from_client({'id': 7, 'result': {'decision': 'accept'}})
    check('the human answering clears that prompt and no other',
          7 not in shim.approvals and 8 in shim.approvals, str(list(shim.approvals)))

    shim._observe_from_server({'method': 'turn/completed', 'params': {
        'threadId': 'thr-1', 'turn': {'id': 'turn-1', 'status': 'completed'}}})
    thread = shim.status()['threads'][0]
    check('the turn ending clears the rest',
          thread.get('is_turn_active') is False and thread.get('awaiting_approval') is None,
          str(thread)[:240])

    is_swallowed = shim._observe_from_server({'id': 9, 'method': 'item/tool/call',
                                              'params': {'threadId': 'thr-1'}})
    check('a server request that is not a prompt is forwarded and not counted',
          is_swallowed is False and not shim.approvals)


def test_a_codex_turn_is_settled_by_its_echo_when_the_ids_never_match() -> None:
    """The mechanism behind defect 1: the app server answered `turn/start` with one id and ran
    the turn under another, so no completion event ever matched and the shim waited forever.
    The peer's own words carry the request id, and those are evidence enough."""
    shim = _codex_shim()
    sent = []

    def write(payload):
        message = json.loads(payload)
        sent.append(message)
        threading.Timer(0.01, shim._observe_from_server, args=(
            {'id': message['id'], 'result': {'turn': {'id': 'turn-A'}}},)).start()

    shim.write_to_child = write
    shim._observe_from_client({'method': 'turn/start', 'params': {'threadId': 'thr-1'}})
    receipt = shim.inject('hello req_5_abcdef', 'thr-1', timeout=5, accept_timeout=2)
    check('the message is taken under one turn id',
          receipt.get('accepted') is True and receipt.get('pending') is True
          and receipt.get('turnId') == 'turn-A', str(receipt)[:200])

    # ...and run under another; nothing the app server sends will ever say "turn-A"
    shim._observe_from_server({'method': 'turn/started',
                               'params': {'threadId': 'thr-1', 'turn': {'id': 'turn-B'}}})
    shim._observe_from_server({'method': 'turn/completed', 'params': {
        'threadId': 'thr-1', 'turn': {'id': 'turn-C', 'status': 'completed'}}})
    still = shim.await_turn(receipt['injectionId'], timeout=0.05)
    check('another turn ending on the thread does not end ours',
          still.get('pending') is True, str(still)[:200])

    shim._observe_from_server({'method': 'item/completed', 'params': {
        'threadId': 'thr-1', 'turnId': 'turn-B',
        'item': {'type': 'agentMessage', 'text': 'req_5_abcdef 처리 완료'}}})
    shim._observe_from_server({'method': 'turn/completed', 'params': {
        'threadId': 'thr-1', 'turn': {'id': 'turn-B', 'status': 'completed'}}})
    done = shim.await_turn(receipt['injectionId'], timeout=1)
    check('a turn that echoed our request id ends ours when it ends',
          done.get('ok') is True and done.get('reply') == 'req_5_abcdef 처리 완료',
          str(done)[:200])
    check('and the slot is released', not shim.injections, str(shim.injections))

    # an echo on another thread is somebody else quoting us, not our answer
    receipt = shim.inject('again req_6_abcdef', 'thr-1', timeout=5, accept_timeout=2)
    shim._observe_from_client({'method': 'turn/start', 'params': {'threadId': 'thr-2'}})
    shim._observe_from_server({'method': 'item/completed', 'params': {
        'threadId': 'thr-2', 'turnId': 'turn-Z',
        'item': {'type': 'agentMessage', 'text': 'req_6_abcdef 를 봤다'}}})
    shim._observe_from_server({'method': 'turn/completed', 'params': {
        'threadId': 'thr-2', 'turn': {'id': 'turn-Z', 'status': 'completed'}}})
    still = shim.await_turn(receipt['injectionId'], timeout=0.05)
    check('an echo on another thread does not count', still.get('pending') is True,
          str(still)[:200])


# --------------- a delivery is on disk from the moment it is queued (koppa, 2026-09-14)

# A bridge server that carries requests and is then killed with them still in flight.
_ORIGIN_SERVER = r'''
import sys, threading
sys.path.insert(0, sys.argv[1])
from cross_agent_mcp import outbox

mode = sys.argv[2]
box = outbox.Outbox()


def deliver(job):
    if mode == 'accepted':
        job.mark_accepted(job.target_session_id)
    threading.Event().wait()  # the peer's turn outlives this server


box.deliver = deliver


def request(summary):
    return outbox.Job(
        target_agent='codex', target_session_id='peer-sid', payload='SECRET-PAYLOAD',
        run_cwd='/w', pin_cwd='/w', env={'SECRET_ENV': 'SECRET-VALUE'}, timeout=600,
        ui_shim={'socket': '/nowhere'}, title=None, conversation_id='conv_orphan', hop=1,
        sender_agent='claude', sender_session_id='sender-sid', wants_reply=True, summary=summary)


ids = [box.submit(request('first'))]
if mode == 'queued':
    ids.append(box.submit(request('second')))
print(' '.join(ids), flush=True)
threading.Event().wait()
'''

# A bridge server that carries a burst of requests to the end and exits.
_BUSY_SERVER = r'''
import os, sys, time
sys.path.insert(0, sys.argv[1])
from cross_agent_mcp import outbox

box = outbox.Outbox()
box.deliver = lambda job: {'session_id': job.target_session_id, 'reply': 'ok',
                           'is_new_session': False}
for index in range(int(sys.argv[2])):
    box.submit(outbox.Job(
        target_agent='codex', target_session_id=f'peer-{os.getpid()}-{index % 3}', payload='x',
        run_cwd='/w', pin_cwd='/w', env={}, timeout=5, ui_shim=None, title=None,
        conversation_id='conv_race', hop=1, sender_agent='claude', sender_session_id=None,
        wants_reply=True, summary=str(index)))
deadline = time.time() + 30
while box._pending and time.time() < deadline:
    time.sleep(0.01)
print('done' if not box._pending else 'stuck', flush=True)
'''


def _spawn_server(script: str, home: str, *args: str):
    import subprocess
    src_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__))) + '/src'
    return subprocess.Popen([sys.executable, '-c', script, src_dir, *args],
                            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
                            env={**os.environ, 'CROSS_AGENT_HOME': home})


def _kill(process) -> None:
    with contextlib_suppress():
        process.kill()
    with contextlib_suppress():
        process.wait(timeout=5)


def _await_record(delivery_id: str, state: str, deadline_seconds: float = 5.0):
    deadline = time.time() + deadline_seconds
    record = None
    while time.time() < deadline:
        record = outbox.read_record(delivery_id)
        if record and record.get('state') == state:
            return record
        time.sleep(0.02)
    return record


def _collapse(states: list) -> list:
    return [s for index, s in enumerate(states) if index == 0 or states[index - 1] != s]


def test_a_delivery_is_on_disk_from_the_moment_it_is_queued() -> None:
    """koppa, 2026-09-14: Studio started a bridge server, sent a request and quit before the
    peer's turn ended. The server went with it, and every later bridge_status answered "no
    delivery is known" for a request the peer did carry out - three times that day. A record
    written only at the end cannot outlive the server it lives in; this one is written as the
    delivery is queued, and again at every change of state."""
    original_write = outbox._write_record
    writes = []

    def spy(record, is_finished=True):
        writes.append((record['delivery_id'], record['state'], is_finished))
        original_write(record, is_finished)

    outbox._write_record = spy
    try:
        box = outbox.Outbox()
        entered, go_accept, accepted, go_finish = (threading.Event() for _ in range(4))

        def deliver(job):
            if job.summary == 'first':
                entered.set()
                go_accept.wait(5)
                job.mark_accepted(job.target_session_id)
                accepted.set()
                go_finish.wait(5)
            return {'session_id': job.target_session_id, 'reply': '', 'is_new_session': False}

        box.deliver = deliver
        first = box.submit(_job(box, 'sid-record', summary='first'))
        entered.wait(5)
        second = box.submit(_job(box, 'sid-record', summary='second'))

        queued = outbox.read_record(second) or {}
        check('a delivery is on disk as soon as submit returns',
              queued.get('state') == outbox.STATE_QUEUED, str(queued)[:200])
        check('apart from finished records, where an older server would prune it',
              not os.path.exists(outbox.config.DELIVERY_DIR + f'{second}.json')
              and os.path.exists(outbox._in_flight_dir() + f'{second}.json'))
        check('with no finish time, and an expiry to be judged by',
              'finished_at' in queued and queued['finished_at'] is None
              and (queued.get('expires_at') or 0) > time.time(), str(queued)[:200])

        delivering = outbox.read_record(first) or {}
        check('a started delivery is recorded as delivering',
              delivering.get('state') == outbox.STATE_DELIVERING
              and delivering.get('started_at') is not None, str(delivering)[:200])
        check('expiring after the longest it may legitimately take',
              abs((delivering.get('expires_at') or 0) - (delivering.get('started_at') or 0)
                  - box._longest_run(box.find(first))) < 1, str(delivering)[:200])
        check('while the one queued behind it allows for it too',
              (queued.get('expires_at') or 0) > (delivering.get('expires_at') or 0))

        go_accept.set()
        accepted.wait(5)
        awaiting = outbox.read_record(first) or {}
        check('the hand-over is recorded the moment the peer takes the message',
              awaiting.get('state') == outbox.STATE_AWAITING
              and awaiting.get('accepted_at') is not None, str(awaiting)[:200])

        go_finish.set()
        _drain(box)
        done = outbox.read_record(first) or {}
        check('the end is recorded where finished records have always been',
              done.get('state') == outbox.STATE_DELIVERED and done.get('finished_at')
              and done.get('expires_at') is None
              and os.path.exists(outbox.config.DELIVERY_DIR + f'{first}.json'), str(done)[:200])
        check('and the in-flight record is gone',
              not os.path.exists(outbox._in_flight_dir() + f'{first}.json'))

        states = _collapse([state for delivery_id, state, _ in writes if delivery_id == first])
        check('every change of state was written, in order',
              states[-3:] == [outbox.STATE_DELIVERING, outbox.STATE_AWAITING,
                              outbox.STATE_DELIVERED]
              and states[0] in (outbox.STATE_QUEUED, outbox.STATE_DELIVERING), str(states))
        check('only the last write is the finished one',
              [f for delivery_id, _, f in writes if delivery_id == first][-1] is True
              and not any([f for delivery_id, _, f in writes if delivery_id == first][:-1]))

        outbox.persist(box.find(first))
        check('a finished record is never reopened as in flight',
              not os.path.exists(outbox._in_flight_dir() + f'{first}.json'))
    finally:
        outbox._write_record = original_write


def test_a_watched_transport_failure_is_recorded_as_awaiting_the_peer() -> None:
    original = (outbox.RECOVERY_POLL_SECONDS, outbox.RECOVERY_WINDOW_SECONDS,
                outbox._write_record)
    seen = []

    def spy(record, is_finished=True):
        seen.append(dict(record, is_finished_write=is_finished))
        original[2](record, is_finished)

    outbox.RECOVERY_POLL_SECONDS, outbox.RECOVERY_WINDOW_SECONDS = 0.02, 2
    outbox._write_record = spy
    try:
        box = outbox.Outbox()

        def deliver(job):
            job.mark_accepted(job.target_session_id)
            raise RuntimeError('IDE panel relay failed: the peer turn is still running after 3600s')

        box.deliver = deliver
        looks = {'count': 0}
        box.recover = lambda job: (looks.update(count=looks['count'] + 1)
                                   or ('늦게 도착한 답' if looks['count'] >= 3 else None))
        job = _job(box, 'sid-watch', wants_reply=True)
        job.ui_shim = {'socket': '/panel'}
        delivery_id = box.submit(job)
        _drain(box)

        mine = [r for r in seen if r['delivery_id'] == delivery_id]
        watching = [r for r in mine if r['state'] == outbox.STATE_AWAITING and r.get('error')]
        check('while the transcript is watched the record says awaiting-peer, with the failure',
              bool(watching) and watching[0]['is_finished_write'] is False,
              str([(r['state'], r.get('error')) for r in mine])[:300])
        check('and it closes as failed with the recovered answer',
              bool(mine) and mine[-1]['state'] == outbox.STATE_FAILED
              and mine[-1]['is_finished_write'] is True
              and mine[-1].get('is_reply_recovered') is True, str(mine[-1:])[:300])
    finally:
        outbox.RECOVERY_POLL_SECONDS, outbox.RECOVERY_WINDOW_SECONDS, outbox._write_record = original


def test_a_delivery_record_is_replaced_whole_or_not_at_all() -> None:
    box = outbox.Outbox()
    job = _job(box, 'sid-atomic', summary='atomic')
    job.expires_at = time.time() + 60
    outbox.persist(job)
    path = outbox._in_flight_dir() + f'{job.delivery_id}.json'
    temporaries = lambda: [n for n in os.listdir(outbox._in_flight_dir())
                           if n.startswith(job.delivery_id) and n.endswith('.tmp')]

    original_replace = outbox.os.replace
    original_error = outbox.logger.error
    logged = []
    outbox.logger.error = lambda message: logged.append(message)

    def failing_replace(source, destination):
        raise OSError('the disk went away mid-write')

    outbox.os.replace = failing_replace
    try:
        job.state = outbox.STATE_DELIVERING
        outbox.persist(job)
    finally:
        outbox.os.replace = original_replace
    kept = json.load(open(path, encoding='utf-8'))
    check('a write that fails leaves the previous record whole',
          kept.get('state') == outbox.STATE_QUEUED, str(kept)[:200])
    check('and no temporary behind', not temporaries(), str(temporaries()))

    logged.clear()
    errors = []
    stop = threading.Event()

    def writer() -> None:
        for attempt in range(150):
            job.attempts = attempt
            outbox.persist(job)

    def reader() -> None:
        while not stop.is_set():
            try:
                with open(path, encoding='utf-8') as f:
                    json.load(f)
            except Exception as e:
                errors.append(repr(e))

    try:
        watcher = threading.Thread(target=reader, daemon=True)
        watcher.start()
        writers = [threading.Thread(target=writer) for _ in range(6)]
        for thread in writers:
            thread.start()
        for thread in writers:
            thread.join(30)
        stop.set()
        watcher.join(5)
    finally:
        outbox.logger.error = original_error
    check('a reader never sees a torn or missing record while writers replace it',
          not errors, str(errors[:3]))
    check('writers never trip over each other\'s temporary', not logged, str(logged[:3]))
    check('and leave none behind', not temporaries(), str(temporaries()))


def test_an_orphaned_delivery_is_reported_by_another_server() -> None:
    """The koppa case end to end: the server carrying a request is killed mid-turn, and a
    server that never saw the request answers for it - still carried while that server runs,
    orphaned once it is gone, with the answer read from the peer transcript. Nothing is resent."""
    original_dir = outbox.config.DELIVERY_DIR
    originals = (bridge.discovery.peer_progress, bridge.panel_state, outbox.OUTBOX.submit,
                 bridge.uihook.send)
    original_callers = dict(bridge.CALLERS)
    resent = []
    asked = []
    with tempfile.TemporaryDirectory(prefix='orphan-home-') as home:
        outbox.config.DELIVERY_DIR = home + '/deliveries/'
        bridge.panel_state = lambda agent, session_id: {'is_open_in_a_panel': False}
        outbox.OUTBOX.submit = lambda job: resent.append(('submit', job.delivery_id))
        bridge.uihook.send = lambda *a, **kw: resent.append(('send', a[:1]))
        for name in list(bridge.CALLERS):
            bridge.CALLERS[name] = lambda *a, **kw: resent.append(('call', a[:1]))
        server = _spawn_server(_ORIGIN_SERVER, home, 'accepted')
        try:
            ids = server.stdout.readline().split()
            check('the carrying server queued a request', len(ids) == 1, str(ids))
            delivery_id = ids[0] if ids else 'req_0_000000'
            carried = _await_record(delivery_id, outbox.STATE_AWAITING)
            raw = (open(outbox._in_flight_dir() + f'{delivery_id}.json', encoding='utf-8').read()
                   if carried else '')
            check('its in-flight record is on disk, without the payload or the environment',
                  bool(raw) and 'SECRET-PAYLOAD' not in raw and 'SECRET' not in raw, raw[:200])

            def working(agent, session_id, after=None, token=None):
                asked.append((agent, session_id, after, token))
                return {'answer': None, 'answered_at': None, 'is_working': True,
                        'last_turn_finished_at': None, 'transcript_mtime': 1.0}

            bridge.discovery.peer_progress = working
            live = bridge.delivery_report(delivery_id)
            record = live.get('delivery') or {}
            check('another server reports a delivery it never carried',
                  live.get('ok') is True and record.get('state') == outbox.STATE_AWAITING,
                  str(live)[:300])
            check('as still carried while its server runs',
                  record.get('is_origin_alive') is True and record.get('is_orphaned') is False
                  and record.get('origin_pid') == server.pid and 'note' not in live,
                  str(record)[:300])

            _kill(server)
            orphaned = bridge.delivery_report(delivery_id)
            record = orphaned.get('delivery') or {}
            note = str(orphaned.get('note') or '')
            check('once its server is gone it is reported as orphaned, not unknown',
                  orphaned.get('ok') is True and record.get('is_orphaned') is True
                  and record.get('is_origin_alive') is False, str(orphaned)[:300])
            check('in the state its server last recorded',
                  record.get('state') == outbox.STATE_AWAITING, str(record.get('state')))
            check('saying so, and that it is not resent',
                  note.startswith('ORPHANED') and 'not resent' in note, note)
            check('with the peer read as still working',
                  (orphaned.get('peer_transcript') or {}).get('is_working') is True)
            check('read as for a live delivery: from the request time, matched by its id',
                  bool(asked) and asked[-1] == ('codex', 'peer-sid', record.get('started_at'),
                                                delivery_id), str(asked[-1:]))

            answer = f'이미지 11장 등록 완료\n{delivery_id}'
            bridge.discovery.peer_progress = lambda agent, session_id, after=None, token=None: {
                'answer': answer, 'answered_at': time.time(), 'is_working': False,
                'last_turn_finished_at': time.time(), 'transcript_mtime': 1.0}
            answered = bridge.delivery_report(delivery_id)
            check('once the peer finishes, its answer comes back through the orphaned record',
                  (answered.get('peer_transcript') or {}).get('answer') == answer,
                  str(answered)[:300])
            check('and nothing was resent at any point', not resent, str(resent))

            listing = outbox.Outbox().snapshot()
            listed = next((r for r in listing['in_flight_elsewhere']
                           if r.get('delivery_id') == delivery_id), None)
            check('the overall status lists it among other servers\' in-flight deliveries',
                  listed is not None and listed.get('is_orphaned') is True,
                  str(listing['in_flight_elsewhere'])[:300])
            check('and not among finished ones',
                  all(r.get('delivery_id') != delivery_id for r in listing['earlier']))
        finally:
            _kill(server)
            outbox.config.DELIVERY_DIR = original_dir
            (bridge.discovery.peer_progress, bridge.panel_state, outbox.OUTBOX.submit,
             bridge.uihook.send) = originals
            bridge.CALLERS.update(original_callers)


def test_a_delivery_orphaned_in_the_queue_is_known_never_to_have_landed() -> None:
    original_dir = outbox.config.DELIVERY_DIR
    originals = (bridge.discovery.peer_progress, bridge.panel_state)
    asked = []
    with tempfile.TemporaryDirectory(prefix='orphan-queue-') as home:
        outbox.config.DELIVERY_DIR = home + '/deliveries/'
        bridge.panel_state = lambda agent, session_id: {'is_open_in_a_panel': False}
        # a peer whose last word is about something else entirely
        bridge.discovery.peer_progress = lambda agent, session_id, after=None, token=None: (
            asked.append(token) or {'answer': '다른 요청에 대한 답', 'answered_at': 1.0,
                                    'is_working': False})
        server = _spawn_server(_ORIGIN_SERVER, home, 'queued')
        try:
            ids = server.stdout.readline().split()
            check('the carrying server has one delivery started and one queued behind it',
                  len(ids) == 2, str(ids))
            first, second = (ids + ['req_0_000000', 'req_0_000001'])[:2]
            _await_record(first, outbox.STATE_DELIVERING)
            _await_record(second, outbox.STATE_QUEUED)
            _kill(server)

            report = bridge.delivery_report(second)
            record = report.get('delivery') or {}
            check('a delivery orphaned in the queue is reported as queued and orphaned',
                  report.get('ok') is True and record.get('state') == outbox.STATE_QUEUED
                  and record.get('is_orphaned') is True, str(report)[:300])
            check('its peer transcript is not searched for an answer it cannot have',
                  (report.get('peer_transcript') or {}).get('answer') is None
                  and second not in asked, str(report.get('peer_transcript')))
            check('and the note says the peer never received it',
                  'never received' in str(report.get('note')), str(report.get('note')))

            started = bridge.delivery_report(first)
            check('one orphaned mid-hand-over says it may or may not have landed',
                  (started.get('delivery') or {}).get('is_orphaned') is True
                  and 'never acknowledged' in str(started.get('note')), str(started)[:300])
        finally:
            _kill(server)
            outbox.config.DELIVERY_DIR = original_dir
            bridge.discovery.peer_progress, bridge.panel_state = originals


def test_in_flight_records_expire_and_are_pruned() -> None:
    original = (outbox.config.DELIVERY_DIR, outbox.config.DELIVERY_TTL_SECONDS)
    with tempfile.TemporaryDirectory(prefix='delivery-ttl-') as store:
        outbox.config.DELIVERY_DIR = store + '/'
        outbox.config.DELIVERY_TTL_SECONDS = 100
        try:
            in_flight_dir = outbox._in_flight_dir()
            os.makedirs(in_flight_dir, exist_ok=True)
            now = time.time()
            alive_pid = os.getppid()

            def put(directory, delivery_id, **fields):
                with open(directory + f'{delivery_id}.json', 'w', encoding='utf-8') as f:
                    json.dump({'delivery_id': delivery_id, 'state': outbox.STATE_AWAITING,
                               'target_agent': 'codex', 'target_session_id': 'peer-sid',
                               **fields}, f)

            put(in_flight_dir, 'req_1_aaaaaa', finished_at=None, origin_pid=alive_pid,
                updated_at=now, expires_at=now + 50, created_at=now - 40, started_at=now - 30,
                queued_seconds=0.0, elapsed_seconds=0.0)
            put(in_flight_dir, 'req_2_aaaaaa', finished_at=None, origin_pid=alive_pid,
                updated_at=now - 60, expires_at=now - 50)
            put(in_flight_dir, 'req_3_aaaaaa', finished_at=None, origin_pid=alive_pid,
                updated_at=now - 300, expires_at=now - 200)
            put(in_flight_dir, 'req_4_aaaaaa', finished_at=None, origin_pid=os.getpid(),
                updated_at=now, expires_at=now + 50)
            put(in_flight_dir, 'req_5_aaaaaa', finished_at=None, updated_at=now - 200)
            put(store + '/', 'req_6_aaaaaa', state=outbox.STATE_DELIVERED, finished_at=now - 50)
            put(store + '/', 'req_7_aaaaaa', state=outbox.STATE_DELIVERED, finished_at=now - 200)
            with open(in_flight_dir + 'req_8_aaaaaa.json', 'w', encoding='utf-8') as f:
                f.write('{"delivery_id": "req_8_')
            stale_temp = in_flight_dir + 'req_9_aaaaaa.json.1.deadbeef.tmp'
            fresh_temp = in_flight_dir + 'req_10_aaaaaa.json.2.deadbeef.tmp'
            for path in (stale_temp, fresh_temp):
                with open(path, 'w', encoding='utf-8') as f:
                    f.write('{')
            os.utime(stale_temp, (now - outbox.STALE_TEMP_SECONDS - 10,) * 2)

            def origin(delivery_id):
                record = outbox.read_record(delivery_id)
                return outbox.describe_origin(record) if record else None

            fresh = origin('req_1_aaaaaa') or {}
            check('an in-flight record whose server runs, before its expiry, is carried',
                  fresh.get('is_origin_alive') is True and fresh.get('is_orphaned') is False,
                  str(fresh))
            check('its durations are as of now, not as of the write that froze them',
                  fresh.get('queued_seconds') == 10.0
                  and 29 <= (fresh.get('elapsed_seconds') or 0) <= 35, str(fresh))
            reused = origin('req_2_aaaaaa') or {}
            check('past its expiry it is orphaned even if the pid runs - pids are reused',
                  reused.get('is_origin_alive') is True and reused.get('is_orphaned') is True,
                  str(reused))
            mine = origin('req_4_aaaaaa') or {}
            check('a record naming our own pid that we do not carry was a previous process\'s',
                  mine.get('is_origin_alive') is False and mine.get('is_orphaned') is True,
                  str(mine))

            gone = bridge.delivery_report('req_3_aaaaaa')
            check('an in-flight record a TTL past its expiry is pruned, and is unknown again',
                  gone == {'ok': False, 'error': 'no delivery req_3_aaaaaa is known to this '
                                                 'server or kept on disk'}
                  and not os.path.exists(in_flight_dir + 'req_3_aaaaaa.json'), str(gone))
            check('one that never recorded an expiry ages out from its last update',
                  outbox.read_record('req_5_aaaaaa') is None
                  and not os.path.exists(in_flight_dir + 'req_5_aaaaaa.json'))
            check('finished records keep their TTL from when they finished',
                  outbox.read_record('req_6_aaaaaa') is not None
                  and outbox.read_record('req_7_aaaaaa') is None)
            check('a torn record is removed',
                  outbox.read_record('req_8_aaaaaa') is None
                  and not os.path.exists(in_flight_dir + 'req_8_aaaaaa.json'))

            outbox.read_records()
            check('a temporary no write can still own is removed, a fresh one is left alone',
                  not os.path.exists(stale_temp) and os.path.exists(fresh_temp))

            escape = bridge.delivery_report('../req_6_aaaaaa')
            check('a delivery id is never joined to a path',
                  outbox.read_record('../req_6_aaaaaa') is None and escape.get('error')
                  == 'no delivery ../req_6_aaaaaa is known to this server or kept on disk',
                  str(escape))
        finally:
            outbox.config.DELIVERY_DIR, outbox.config.DELIVERY_TTL_SECONDS = original


def test_servers_sharing_the_delivery_directory_do_not_trip_over_each_other() -> None:
    """Every bridge server of every editor window writes and prunes the same directory."""
    original_dir = outbox.config.DELIVERY_DIR
    with tempfile.TemporaryDirectory(prefix='delivery-race-') as home:
        outbox.config.DELIVERY_DIR = home + '/deliveries/'
        servers = []
        try:
            servers = [_spawn_server(_BUSY_SERVER, home, '40') for _ in range(4)]
            failures = []
            stop = threading.Event()

            def reader() -> None:
                # reads and prunes both directories, as every bridge_status does
                while not stop.is_set():
                    try:
                        outbox.Outbox().snapshot()
                    except Exception as e:
                        failures.append(repr(e))

            watcher = threading.Thread(target=reader, daemon=True)
            watcher.start()
            outputs = [server.communicate(timeout=60)[0].strip() for server in servers]
            stop.set()
            watcher.join(10)

            names = [n for n in os.listdir(outbox.config.DELIVERY_DIR) if n.endswith('.json')]
            records = [json.load(open(outbox.config.DELIVERY_DIR + n, encoding='utf-8'))
                       for n in names]
            left = os.listdir(outbox._in_flight_dir()) if os.path.isdir(outbox._in_flight_dir()) else []
            check('four servers finished their deliveries side by side',
                  outputs == ['done'] * 4, str(outputs))
            check('every delivery of every server has its finished record - none lost to a '
                  'reader pruning at the same moment', len(records) == 160, str(len(records)))
            check('all readable, all delivered',
                  all(r.get('state') == outbox.STATE_DELIVERED for r in records))
            check('from four distinct servers',
                  len({r.get('origin_pid') for r in records}) == 4)
            check('no in-flight record or temporary left behind', not left, str(left[:5]))
            check('the reader never tripped', not failures, str(failures[:3]))
        finally:
            for server in servers:
                _kill(server)
            outbox.config.DELIVERY_DIR = original_dir


def _legacy_write_record(record) -> None:
    """`outbox._write_record` as of 9327e26, verbatim: how a server from before in-flight
    records writes a finished record."""
    try:
        config.ensure_dirs()
        path = config.DELIVERY_DIR + f"{record['delivery_id']}.json"
        tmp = path + '.tmp'
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump({**record, 'finished_at': time.time()}, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
    except Exception:
        pass


def _legacy_read_records(limit: int = 20) -> list:
    """`outbox.read_records` as of 9327e26, verbatim: what such a server runs over the shared
    delivery directory on every bridge_status, deleting whatever it judges aged out."""
    records = []
    try:
        config.ensure_dirs()
        names = os.listdir(config.DELIVERY_DIR)
    except OSError:
        return records

    now = time.time()
    for name in names:
        if not name.endswith('.json'):
            continue
        path = config.DELIVERY_DIR + name
        try:
            with open(path, 'r', encoding='utf-8') as f:
                record = json.load(f)
        except Exception:
            with contextlib_suppress():
                os.remove(path)
            continue

        if now - float(record.get('finished_at') or 0) > config.DELIVERY_TTL_SECONDS:
            with contextlib_suppress():
                os.remove(path)
            continue
        records.append(record)

    records.sort(key=lambda r: float(r.get('finished_at') or 0), reverse=True)
    return records[:limit]


def test_servers_from_before_in_flight_records_share_the_directory_safely() -> None:
    """koppa, 2026-09-14: until every running server has been restarted, old and new code write
    and prune one directory. The old pruning deletes every `*.json` there without a recent
    finished_at - which is exactly what an in-flight record would be, kept beside finished ones."""
    original_dir = outbox.config.DELIVERY_DIR
    originals = (bridge.discovery.peer_progress, bridge.panel_state)
    with tempfile.TemporaryDirectory(prefix='delivery-mixed-') as store:
        outbox.config.DELIVERY_DIR = store + '/'
        bridge.discovery.peer_progress = lambda agent, session_id, after=None, token=None: {
            'answer': None, 'answered_at': None, 'is_working': True}
        bridge.panel_state = lambda agent, session_id: {'is_open_in_a_panel': False}
        try:
            box = outbox.Outbox()
            in_flight = _job(box, 'sid-mixed', wants_reply=True, summary='in flight')
            in_flight.expires_at = time.time() + 600
            outbox.persist(in_flight)

            finished_new = _job(box, 'sid-mixed', summary='finished by a new server')
            finished_new.state, finished_new.finished_at = outbox.STATE_DELIVERED, time.time()
            outbox.persist(finished_new)

            finished_old = _job(box, 'sid-mixed', summary='finished by an old server')
            finished_old.state, finished_old.finished_at = outbox.STATE_DELIVERED, time.time()
            _legacy_write_record({k: v for k, v in finished_old.describe().items()
                                  if k not in ('created_at', 'expires_at', 'origin_pid')})

            seen_by_old = {r['delivery_id'] for r in _legacy_read_records(limit=1000)}
            in_flight_path = outbox._in_flight_dir() + f'{in_flight.delivery_id}.json'
            check('an old server leaves a record still in flight alone',
                  os.path.exists(in_flight_path))
            check('and does not mistake it for a finished delivery',
                  in_flight.delivery_id not in seen_by_old, str(seen_by_old))
            check('it reads a new server\'s finished record as it always read finished records',
                  {finished_new.delivery_id, finished_old.delivery_id} <= seen_by_old,
                  str(seen_by_old))

            carried = outbox.read_record(in_flight.delivery_id) or {}
            check('the in-flight record is whole after the old server has been through',
                  carried.get('state') == outbox.STATE_QUEUED and carried.get('finished_at') is None,
                  str(carried)[:200])

            report = bridge.delivery_report(finished_old.delivery_id)
            record = report.get('delivery') or {}
            check('a new server reports a record an old server wrote',
                  report.get('ok') is True and record.get('state') == outbox.STATE_DELIVERED
                  and record.get('is_orphaned') is False and 'note' not in report, str(report)[:300])

            # an old server's write in progress uses one fixed temporary name
            legacy_temporary = store + f'/{finished_old.delivery_id}.json.tmp'
            with open(legacy_temporary, 'w', encoding='utf-8') as f:
                f.write('{')
            outbox.read_records()
            check('a new server leaves an old server\'s fresh temporary alone',
                  os.path.exists(legacy_temporary))
        finally:
            outbox.config.DELIVERY_DIR = original_dir
            bridge.discovery.peer_progress, bridge.panel_state = originals


def contextlib_suppress():
    import contextlib
    return contextlib.suppress(Exception)


def uuid_hex() -> str:
    import uuid
    return uuid.uuid4().hex[:8]


def test_the_busy_lock_survives_a_sustained_race() -> None:
    """One round of six threads let the old claim through about one run in five. This runs the
    race often enough that a claim which is only nearly exclusive cannot pass it."""
    rounds, racers = 40, 8
    bad_rounds = []

    for round_number in range(rounds):
        session_id = f'unit-race-{round_number}-' + os.urandom(3).hex()
        outcomes = []
        guard = threading.Lock()
        barrier = threading.Barrier(racers)

        def worker() -> None:
            barrier.wait()
            try:
                with registry.busy_lock(config.AGENT_CODEX, session_id, 'conv_race'):
                    with guard:
                        outcomes.append('won')
                    time.sleep(0.01)
            except registry.SessionBusyError:
                with guard:
                    outcomes.append('refused')

        threads = [threading.Thread(target=worker) for _ in range(racers)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)

        if outcomes.count('won') != 1 or len(outcomes) != racers:
            bad_rounds.append((round_number, list(outcomes)))
        check_lock = registry.read_busy_lock(config.AGENT_CODEX, session_id)
        if check_lock is not None:
            bad_rounds.append((round_number, 'lock left behind'))

    check(f'exactly one of {racers} claimers wins, in every one of {rounds} rounds',
          not bad_rounds, str(bad_rounds[:3]))


def test_a_lock_being_written_is_never_mistaken_for_debris() -> None:
    """The mechanism of the race: the loser read a file the winner had not finished writing."""
    session_id = 'unit-halfwritten-' + os.urandom(4).hex()
    path = registry._lock_path(config.AGENT_CODEX, session_id)
    config.ensure_dirs()

    # exactly what the old claim left on disk between its create and its write
    with open(path, 'w', encoding='utf-8') as f:
        f.write('')
    try:
        check('an unreadable lock that has just appeared is left alone',
              registry.read_busy_lock(config.AGENT_CODEX, session_id) is None
              and os.path.exists(path))

        os.utime(path, (time.time() - registry.UNREADABLE_LOCK_GRACE_SECONDS - 5,) * 2)
        check('while one old enough to be real debris is cleared',
              registry.read_busy_lock(config.AGENT_CODEX, session_id) is None
              and not os.path.exists(path))
    finally:
        with contextlib.suppress(OSError):
            os.remove(path)


def test_a_claimed_lock_is_readable_the_instant_it_exists() -> None:
    """Whatever a concurrent claimer sees, it sees a whole record."""
    session_id = 'unit-whole-' + os.urandom(4).hex()
    path = registry._lock_path(config.AGENT_CODEX, session_id)
    seen = []
    stop = threading.Event()

    def reader() -> None:
        while not stop.is_set():
            try:
                with open(path, 'r', encoding='utf-8') as f:
                    seen.append(json.load(f).get('conversation_id'))
            except FileNotFoundError:
                pass
            except Exception as e:
                seen.append(f'torn: {type(e).__name__}')

    watcher = threading.Thread(target=reader, daemon=True)
    watcher.start()
    try:
        for _ in range(200):
            with registry.busy_lock(config.AGENT_CODEX, session_id, 'conv_whole'):
                pass
    finally:
        stop.set()
        watcher.join(timeout=5)

    torn = [s for s in seen if s != 'conv_whole']
    check('a reader racing 200 claims never sees a partial lock file', not torn,
          str(torn[:3]) + f' of {len(seen)} reads')
    check('and it did actually observe the lock', len(seen) > 0, str(len(seen)))


def test_a_stale_clear_cannot_delete_the_lock_that_replaced_it() -> None:
    """The second race, in the other direction: a reader decides a lock is abandoned, somebody
    else clears and reclaims it first, and the reader's unlink then deletes their good lock."""
    session_id = 'unit-replace-' + os.urandom(4).hex()
    path = registry._lock_path(config.AGENT_CODEX, session_id)
    config.ensure_dirs()
    dead_pid = 2 ** 22 - 1

    # a lock whose holder is long gone: the reader will decide to clear it
    with open(path, 'w', encoding='utf-8') as f:
        json.dump({'pid': dead_pid, 'token': 'ghost', 'agent': config.AGENT_CODEX,
                   'session_id': session_id, 'conversation_id': 'conv_ghost',
                   'started_at': time.time(), 'ttl_seconds': 1}, f)

    reader_is_inside = threading.Event()
    let_reader_finish = threading.Event()
    claimed = threading.Event()
    may_release = threading.Event()
    held = {}

    original_is_alive = registry._is_pid_alive
    pause_once = threading.Lock()
    has_paused = []

    def pausing_is_alive(pid: int) -> bool:
        # Pause the FIRST reader mid-decision and nobody else. Pausing every caller would stop
        # the claimer inside this patch rather than on the guard, and the test would then pass
        # with no guard at all - which is what it did before this line existed.
        if pid == dead_pid:
            with pause_once:
                is_first = not has_paused
                has_paused.append(pid)
            if is_first:
                reader_is_inside.set()
                let_reader_finish.wait(timeout=10)
            return False
        return original_is_alive(pid)

    def reader() -> None:
        registry.read_busy_lock(config.AGENT_CODEX, session_id)

    def claimer() -> None:
        try:
            with registry.busy_lock(config.AGENT_CODEX, session_id, 'conv_real'):
                with open(path, 'r', encoding='utf-8') as f:
                    held['token'] = json.load(f).get('token')
                claimed.set()
                # keep holding it until the assertions below have looked
                may_release.wait(timeout=10)
        except Exception as e:
            held['error'] = f'{type(e).__name__}: {e}'
            claimed.set()

    registry._is_pid_alive = pausing_is_alive
    reading = threading.Thread(target=reader)
    claiming = threading.Thread(target=claimer)
    try:
        reading.start()
        check('the reader reached its decision about the stale lock',
              reader_is_inside.wait(timeout=10))

        claiming.start()
        # the reader is holding the transition, so the claimer cannot act on the same lock yet
        check('a claimer cannot slip in while a stale clear is half-done',
              not claimed.wait(timeout=1.0), str(held))

        let_reader_finish.set()
        reading.join(timeout=10)
        check('and once the clear is finished the claim goes through',
              claimed.wait(timeout=10) and 'error' not in held, str(held))

        # read while the claimer is still inside its `with`, and after the reader has done
        # whatever unlinking it was going to do
        survivor = None
        if os.path.exists(path):
            with open(path, 'r', encoding='utf-8') as f:
                survivor = json.load(f).get('token')
        check("the new holder's lock is still on disk, not deleted by the older reader",
              survivor is not None and survivor == held.get('token'),
              f'on_disk={survivor} held={held.get("token")}')
    finally:
        may_release.set()
        let_reader_finish.set()
        claiming.join(timeout=10)
        reading.join(timeout=10)
        registry._is_pid_alive = original_is_alive
        with contextlib.suppress(OSError):
            os.remove(path)


def test_the_no_hard_link_fallback_is_still_exclusive_under_contention() -> None:
    """A filesystem without hard links takes the older claim; the guard has to carry it."""
    original_link = os.link

    def no_links(src, dst):
        raise OSError(errno.EPERM, 'hard links not supported here')

    rounds, racers = 12, 6
    bad_rounds = []
    os.link = no_links
    try:
        for round_number in range(rounds):
            session_id = f'unit-nolink-{round_number}-' + os.urandom(3).hex()
            outcomes = []
            guard = threading.Lock()
            barrier = threading.Barrier(racers)

            def worker() -> None:
                barrier.wait()
                try:
                    with registry.busy_lock(config.AGENT_CODEX, session_id, 'conv_nolink'):
                        with guard:
                            outcomes.append('won')
                        time.sleep(0.01)
                except registry.SessionBusyError:
                    with guard:
                        outcomes.append('refused')

            threads = [threading.Thread(target=worker) for _ in range(racers)]
            for t in threads:
                t.start()
            for t in threads:
                t.join(timeout=30)
            if outcomes.count('won') != 1 or len(outcomes) != racers:
                bad_rounds.append((round_number, list(outcomes)))
    finally:
        os.link = original_link

    check('the fallback claim admits exactly one winner too', not bad_rounds,
          str(bad_rounds[:3]))
    check('and hard links are back for everything else', os.link is original_link)


def run_all() -> None:
    test_busy_lock_is_exclusive()
    test_busy_lock_release_respects_owner()
    test_prune_spares_sticky_pins()
    test_prune_keeps_fresh_auto_pins()
    test_cwd_relations()
    test_codex_scan_filters_before_limit()
    test_timeout_kills_descendants()
    test_subagent_threads_are_rejected()
    test_new_session_is_the_last_resort()
    test_panel_session_selection()
    test_another_window_is_reachable_only_when_named()
    test_the_caller_identifies_its_own_session_exactly()
    test_a_pin_never_answers_who_the_caller_is()
    test_the_envelope_carries_a_return_address()
    test_a_short_timeout_is_raised_to_the_configured_budget()
    test_an_echoed_token_identifies_which_request_was_answered()
    test_a_request_id_is_short_and_carries_its_time()
    test_recovery_refuses_a_message_older_than_the_question()
    test_recovery_refuses_a_message_with_no_timestamp_when_asked_for_one()
    test_a_recovered_answer_says_it_may_not_be_finished()
    test_the_recovered_flag_reaches_the_envelope()
    test_a_reply_runs_where_the_senders_session_lives()
    test_submit_does_not_block_the_caller()
    test_same_session_deliveries_are_serialised()
    test_different_sessions_deliver_in_parallel()
    test_a_reply_is_delivered_back_and_stops_there()
    test_a_busy_session_is_waited_out_not_refused()
    test_a_message_queued_in_the_wakeup_gap_is_not_lost()
    test_a_finished_delivery_outlives_the_process_that_carried_it()
    test_an_answer_is_recovered_from_the_peer_transcript()
    test_a_busy_peer_is_retried_rather_than_failed()
    test_a_shim_busy_answer_is_told_apart_from_a_real_failure()
    test_a_panel_delivery_keeps_watching_after_the_transport_gives_up()
    test_a_cli_delivery_does_not_wait_for_a_turn_that_was_killed()
    test_recovery_is_skipped_when_the_transport_already_answered()
    test_a_conversations_own_name_is_what_it_is_called_by()
    test_a_session_name_matches_exactly_or_not_at_all()
    test_a_named_session_is_never_silently_created()
    test_naming_a_session_and_forcing_a_new_one_is_refused()
    test_a_delivery_does_not_outlive_the_server_that_started_it()
    test_recovery_waits_for_a_claude_turn_to_finish()
    test_recovery_reads_the_codex_turn_the_app_server_closed()
    test_a_later_human_turn_does_not_hide_the_echoed_answer()
    test_giving_up_watching_closes_the_delivery_as_failed()
    test_a_reply_counts_as_delivered_once_the_peer_takes_it()
    test_a_panel_request_is_listened_to_until_the_turn_ends()
    test_a_refusal_is_told_apart_from_a_broken_transport()
    test_an_undelivered_request_is_refused_while_the_caller_is_still_there()
    test_a_late_failure_is_announced_into_the_senders_session()
    test_a_reply_that_did_not_land_is_rerouted_and_retried()
    test_the_codex_shim_reloads_a_thread_the_app_server_forgot()
    test_the_codex_shim_resumes_first_when_it_saw_the_thread_close()
    test_a_codex_turn_outliving_the_first_wait_can_be_awaited()
    test_the_claude_shim_hands_over_and_reports_later()
    test_a_long_panel_turn_keeps_its_busy_lock()
    test_delivery_report_rereads_the_peer_transcript()
    test_a_finished_turn_that_echoes_the_request_ends_the_wait()
    test_the_caller_named_by_its_metadata_is_the_return_address()
    test_a_recovered_reply_says_why_the_transport_failed()
    test_the_claude_shim_sees_a_turn_waiting_on_a_human()
    test_the_codex_shim_sees_a_turn_waiting_on_a_human()
    test_a_codex_turn_is_settled_by_its_echo_when_the_ids_never_match()
    test_a_delivery_is_on_disk_from_the_moment_it_is_queued()
    test_a_watched_transport_failure_is_recorded_as_awaiting_the_peer()
    test_a_delivery_record_is_replaced_whole_or_not_at_all()
    test_an_orphaned_delivery_is_reported_by_another_server()
    test_a_delivery_orphaned_in_the_queue_is_known_never_to_have_landed()
    test_in_flight_records_expire_and_are_pruned()
    test_servers_sharing_the_delivery_directory_do_not_trip_over_each_other()
    test_servers_from_before_in_flight_records_share_the_directory_safely()
    test_the_busy_lock_survives_a_sustained_race()
    test_a_lock_being_written_is_never_mistaken_for_debris()
    test_a_claimed_lock_is_readable_the_instant_it_exists()
    test_a_stale_clear_cannot_delete_the_lock_that_replaced_it()
    test_the_no_hard_link_fallback_is_still_exclusive_under_contention()

if __name__ == '__main__':
    # Delivery records are written by any finished job, so a test run left rows like
    # "sid-normal" in the real ~/.cross-agent/deliveries/ and they sat there among genuine
    # ones - which cost real time when a lost reply had to be found among them. Redirect the
    # whole run rather than each test: the next test to submit a job is covered without
    # anyone remembering to.
    with tempfile.TemporaryDirectory(prefix='cross-agent-test-deliveries-') as store:
        outbox.config.DELIVERY_DIR = store + '/'
        run_all()

    print(f'\n{"ALL UNIT CHECKS PASSED" if not FAILURES else str(len(FAILURES)) + " CHECK(S) FAILED"}')
    sys.exit(1 if FAILURES else 0)
