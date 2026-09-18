"""Unit checks for the bridge's guards and discovery rules. Spends no agent turns.

    PYTHONPATH=src .venv/bin/python tests/unit_guards.py
"""

import contextlib
import errno
import json
import os
import shutil
import stat
import sys
import tempfile
import threading
import time

# `config` resolves every state path at import time, so the run has to be pointed somewhere
# else before `cross_agent_mcp` is imported - not patched afterwards. Without this the suite
# writes to the user's own bridge: the pin tests call set_pin/update_registry against the real
# registry.json, and the busy-lock tests take locks in the real lock directory. A pin for
# `/home/u/git/sophos` - a fixture, not a directory - was found in a real registry that way.
STATE_ROOT = tempfile.mkdtemp(prefix='cross-agent-unit-')
os.environ['CROSS_AGENT_HOME'] = STATE_ROOT + '/state'
os.environ['CLAUDE_CONFIG_DIR'] = STATE_ROOT + '/claude'
os.environ['CODEX_HOME'] = STATE_ROOT + '/codex'

# The bridge hands these to the turns it starts, and a suite run from inside such a turn - a
# peer reviewing a branch through the bridge, say - inherits that conversation. The hop
# counter comes with it, so `send_message` raises "reached the hop limit" and the run ends
# partway through rather than reporting anything. The suite is not part of anyone's exchange.
for _inherited in ('CROSS_AGENT_CONVERSATION_ID', 'CROSS_AGENT_HOP', 'CROSS_AGENT_SENDER',
                   'CROSS_AGENT_BUSY', 'CROSS_AGENT_SELF_SESSION'):
    os.environ.pop(_inherited, None)

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))) + '/src')

from cross_agent_mcp import bridge, config, discovery, outbox, panel, registry  # noqa: E402


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


def _fake_panel(session_id: str):
    """A stand-in for `bridge._panel_session` that answers for one open panel.

    Return traffic is delivered into a panel or not at all, so a test that means to exercise
    anything *else* about a reply - its envelope, the directory it runs in, its timeout - has
    to say the panel is there. Before that rule the panel was optional, and these tests ran
    through the CLI resume without saying so; that is why none of them noticed it.
    """
    def panel(agent: str, wanted, exclude):
        if wanted != session_id:
            return None
        return {'agent': agent, 'session_id': session_id, 'cwd': None, 'source': 'ide-panel',
                'ui_shim': {'pid': 4242, 'socket': '/tmp/panel.sock'}, 'is_active': True,
                'mtime': time.time()}
    return panel


# ------------------------------------------------- the suite cannot touch real bridge state

def test_the_suite_writes_nowhere_near_the_real_bridge() -> None:
    """Every configured root must be the temporary one, compared as a real path.

    A prefix test is not a containment test: `<root>-elsewhere` starts with `<root>`. These
    are exact identities, so a root that was resolved from the environment before the
    overrides above - or from a `~` that is a symlink - fails here rather than silently
    writing to the user's own state.
    """
    expected = {
        'bridge state': (config.HOME_DIR, STATE_ROOT + '/state/'),
        'claude store': (config.CLAUDE_HOME_DIR, STATE_ROOT + '/claude/'),
        'codex store': (config.CODEX_HOME_DIR, STATE_ROOT + '/codex/'),
    }
    inherited = [name for name in (config.ENV_CONVERSATION_ID, config.ENV_HOP,
                                   config.ENV_SENDER, config.ENV_BUSY,
                                   config.ENV_SELF_SESSION)
                 if os.environ.get(name)]
    check('and the run is not inside somebody else\'s bridge conversation',
          inherited == [], f'inherited {inherited}')

    for label, (configured, intended) in expected.items():
        check(f'the {label} root is the temporary one',
              os.path.realpath(configured) == os.path.realpath(intended),
              f'{configured!r} != {intended!r}')

    real_home = os.path.realpath(os.path.expanduser('~/.cross-agent'))
    for label, path in (('registry', config.REGISTRY_PATH), ('locks', config.LOCK_DIR),
                        ('deliveries', config.DELIVERY_DIR), ('logs', config.LOG_DIR)):
        check(f'the {label} path is outside the real ~/.cross-agent',
              os.path.commonpath([os.path.realpath(path), real_home]) != real_home,
              os.path.realpath(path))


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

def test_the_receipt_says_who_chose_the_conversation() -> None:
    """The receipt is the contract, so it is what gets asserted - not the resolver behind it.

    A caller has only this to go on: a send that went where it asked and one that went
    wherever the human was looking are otherwise identical. Every way a target can be arrived
    at is checked here, including the two that produce no target object at all.
    """
    originals = (bridge.caller.detect_caller, bridge._own_session_id, bridge._resolve_target,
                 outbox.OUTBOX.submit, outbox.OUTBOX.await_outcome)
    bridge.caller.detect_caller = lambda: {'agent': config.AGENT_CLAUDE, 'chain': []}
    bridge._own_session_id = lambda agent: 'sender-sid'
    outbox.OUTBOX.submit = lambda job: 'dlv_receipt'
    outbox.OUTBOX.await_outcome = lambda job: None

    def receipt(selected_by, session_id='peer-sid', is_new_session=False, no_target=False):
        bridge._resolve_target = lambda *a, **kw: None if no_target else {
            'agent': config.AGENT_CODEX, 'session_id': session_id, 'cwd': None,
            'source': 'stub', 'ui_shim': None, 'selected_by': selected_by}
        return bridge.send_message(config.AGENT_CODEX, 'hello',
                                   is_new_session=is_new_session)

    try:
        for how, expected_flag in ((bridge.SELECTED_CALLER, True),
                                   (bridge.SELECTED_PIN, False),
                                   (bridge.SELECTED_PANEL_FOCUS, False),
                                   (bridge.SELECTED_DISCOVERY, False),
                                   (bridge.SELECTED_CREATED, False)):
            got = receipt(how)
            check(f'a {how} target is reported as {how}',
                  got.get('target_selected_by') == how, str(got.get('target_selected_by')))
            check(f'and caller_supplied_session_id is {expected_flag} for {how}',
                  got.get('caller_supplied_session_id') is expected_flag,
                  str(got.get('caller_supplied_session_id')))

        warned = receipt(bridge.SELECTED_PANEL_FOCUS, session_id='whichever-tab')
        check('an unaddressed relay warns, and names the session it actually reached',
              'whichever-tab' in (warned.get('warning') or ''), str(warned.get('warning')))
        for how in (bridge.SELECTED_CALLER, bridge.SELECTED_PIN):
            quiet = receipt(how)
            check(f'a {how} target draws no addressing warning',
                  'did not say which' not in (quiet.get('warning') or ''),
                  str(quiet.get('warning')))

        hosted = receipt(bridge.SELECTED_FORCED_NEW, session_id=None, is_new_session=True)
        check('a forced-new conversation a panel can host says forced-new',
              hosted.get('target_selected_by') == bridge.SELECTED_FORCED_NEW,
              str(hosted.get('target_selected_by')))

        # nothing could host it, so there is no target object to carry the label
        homeless = receipt(bridge.SELECTED_FORCED_NEW, is_new_session=True, no_target=True)
        check('and one nothing could host still says forced-new, not created',
              homeless.get('target_selected_by') == bridge.SELECTED_FORCED_NEW,
              str(homeless.get('target_selected_by')))

        ordinary = receipt(bridge.SELECTED_CREATED, no_target=True)
        check('while a conversation nobody asked for is created',
              ordinary.get('target_selected_by') == bridge.SELECTED_CREATED,
              str(ordinary.get('target_selected_by')))
    finally:
        (bridge.caller.detect_caller, bridge._own_session_id, bridge._resolve_target,
         outbox.OUTBOX.submit, outbox.OUTBOX.await_outcome) = originals


def test_an_unaddressed_relay_says_it_was_aimed_by_the_human() -> None:
    """A send with no session_id is addressed by panel focus, and must say so.

    A relay that names no session lands in whichever conversation tab the human most recently
    typed into. That is a reasonable convenience when someone is watching, and a moving target
    for anything else: a status update in an ongoing exchange was delivered into an unrelated
    thread this way, which then began acting on it. The receipt is the only place that can say
    the address came from the human rather than from the caller.
    """
    from cross_agent_mcp import uihook

    now = time.time()
    shim = {'agent': 'codex', 'pid': 1, 'socket': '/s1', 'ancestors': [99], 'started_at': now}
    status = {'ok': True, 'last_user_activity': now - 5,
              'sessions': [{'session_id': 'whatever-tab-is-open', 'cwd': '/w'}]}

    originals = (uihook.is_enabled, uihook.list_shims, uihook.process_ancestry,
                 uihook.read_status, uihook._transcript_mtime, discovery.find_session)
    uihook.is_enabled = lambda: True
    uihook.list_shims = lambda agent=None: [shim] if agent in (None, 'codex') else []
    uihook.process_ancestry = lambda pid, depth=12: [99]
    uihook.read_status = lambda s: status
    uihook._transcript_mtime = lambda agent, session_id: now - 10
    discovery.find_session = lambda agent, session_id: (
        {'agent': agent, 'session_id': session_id, 'cwd': '/w', 'mtime': now}
        if session_id == 'whatever-tab-is-open' else None)
    try:
        unaddressed = bridge._resolve_target('codex', None, 'cwd', '/w', False, [])
        addressed = bridge._resolve_target('codex', 'whatever-tab-is-open', 'cwd', '/w',
                                           False, [])
    finally:
        (uihook.is_enabled, uihook.list_shims, uihook.process_ancestry,
         uihook.read_status, uihook._transcript_mtime, discovery.find_session) = originals

    check('a relay naming no session is recorded as aimed by panel focus',
          (unaddressed or {}).get('selected_by') == bridge.SELECTED_PANEL_FOCUS,
          str(unaddressed and unaddressed.get('selected_by')))
    check('and one naming a session is recorded as the caller\'s own address',
          (addressed or {}).get('selected_by') == bridge.SELECTED_CALLER,
          str(addressed and addressed.get('selected_by')))
    check('panel focus is not treated as an address the caller chose',
          bridge.SELECTED_PANEL_FOCUS in bridge.UNADDRESSED_SELECTIONS
          and bridge.SELECTED_CALLER not in bridge.UNADDRESSED_SELECTIONS)
    check('both reached the same conversation, so only the addressing differs',
          (unaddressed or {}).get('session_id') == (addressed or {}).get('session_id')
          == 'whatever-tab-is-open')


def test_the_resolver_labels_a_pin_a_disk_find_and_a_fresh_start() -> None:
    """The labels nothing reaches through the real resolver.

    The receipt check stubs `_resolve_target`, so it pins what the receipt does with a label
    rather than which label the resolver assigns, and the addressing check covers `caller`
    and `panel-focus` only. That left the rest free to be reported as each other - a pin
    read as `caller` would claim the call named a session it never named - with nothing
    failing. `_new_panel_conversation` is stubbed because it is a leaf the resolver calls,
    not the unit under test.
    """
    from cross_agent_mcp import uihook

    now = time.time()
    pinned_session = {'agent': 'codex', 'session_id': 'pinned-sid', 'cwd': '/w', 'mtime': now}
    disk_session = {'agent': 'codex', 'session_id': 'on-disk-sid', 'cwd': '/w', 'mtime': now}
    fresh_panel = {'agent': 'codex', 'session_id': None, 'cwd': None,
                   'source': 'ide-panel-new', 'ui_shim': '/s1', 'mtime': now}

    originals = (uihook.is_enabled, registry.get_pin, discovery.find_session,
                 discovery.find_active_session, bridge._new_panel_conversation)
    uihook.is_enabled = lambda: False
    discovery.find_session = lambda agent, sid: (
        dict(pinned_session) if sid == 'pinned-sid' else None)
    bridge._new_panel_conversation = lambda agent: dict(fresh_panel)
    try:
        discovery.find_active_session = (
            lambda agent, scope, cwd, exclude=None: dict(disk_session))
        registry.get_pin = lambda agent, cwd: {'session_id': 'pinned-sid', 'is_sticky': True}
        pinned = bridge._resolve_target('codex', None, 'cwd', '/w', False, [])

        registry.get_pin = lambda agent, cwd: None
        discovered = bridge._resolve_target('codex', None, 'cwd', '/w', False, [])

        discovery.find_active_session = lambda agent, scope, cwd, exclude=None: None
        created = bridge._resolve_target('codex', None, 'cwd', '/w', False, [])
        forced = bridge._resolve_target('codex', None, 'cwd', '/w', True, [])
    finally:
        (uihook.is_enabled, registry.get_pin, discovery.find_session,
         discovery.find_active_session, bridge._new_panel_conversation) = originals

    check('a sticky pin the caller never named is recorded as a pin',
          (pinned or {}).get('selected_by') == bridge.SELECTED_PIN,
          str(pinned and pinned.get('selected_by')))
    check('and the conversation it reached is the pinned one, sourced as a pin',
          (pinned or {}).get('session_id') == 'pinned-sid'
          and (pinned or {}).get('source') == 'pin',
          f"{pinned and pinned.get('session_id')} {pinned and pinned.get('source')}")
    check('a session found on disk is recorded as discovery',
          (discovered or {}).get('selected_by') == bridge.SELECTED_DISCOVERY,
          str(discovered and discovered.get('selected_by')))
    check('and it reached the session discovery turned up',
          (discovered or {}).get('session_id') == 'on-disk-sid',
          str(discovered and discovered.get('session_id')))
    check('a conversation opened because nothing could be resumed is recorded as created',
          (created or {}).get('selected_by') == bridge.SELECTED_CREATED,
          str(created and created.get('selected_by')))
    check('and one opened because the caller asked for a new one is recorded as forced-new',
          (forced or {}).get('selected_by') == bridge.SELECTED_FORCED_NEW,
          str(forced and forced.get('selected_by')))
    check('so the two fresh conversations are told apart by the request, not by the panel '
          'they came from',
          (created or {}).get('source') == (forced or {}).get('source') == 'ide-panel-new',
          f"{created and created.get('source')} {forced and forced.get('source')}")


# ------------------------------- a delivery lives and dies with the server that carries it

@contextlib.contextmanager
def _environ(**changes):
    """Set (or, given None, remove) environment variables for the block, then put them back."""
    saved = {name: os.environ.get(name) for name in changes}
    try:
        for name, value in changes.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
        yield
    finally:
        for name, value in saved.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


def _send_receipt(ui_shim=None, mtime=None, target_id='peer-sid') -> dict:
    """What send_message hands back for a target it has already resolved to."""
    originals = (bridge.caller.detect_caller, bridge._own_session_id, bridge._resolve_target,
                 outbox.OUTBOX.submit, outbox.OUTBOX.await_outcome)
    bridge.caller.detect_caller = lambda: {'agent': config.AGENT_CLAUDE, 'chain': []}
    bridge._own_session_id = lambda agent: 'sender-sid'
    bridge._resolve_target = lambda *a, **kw: {
        'agent': config.AGENT_CODEX, 'session_id': target_id, 'cwd': None, 'source': 'stub',
        'ui_shim': ui_shim, 'selected_by': bridge.SELECTED_CALLER, 'mtime': mtime}
    outbox.OUTBOX.submit = lambda job: 'dlv_receipt'
    outbox.OUTBOX.await_outcome = lambda job: None
    try:
        return bridge.send_message(config.AGENT_CODEX, 'hello')
    finally:
        (bridge.caller.detect_caller, bridge._own_session_id, bridge._resolve_target,
         outbox.OUTBOX.submit, outbox.OUTBOX.await_outcome) = originals


def test_a_bridge_started_turn_is_told_its_delivery_ends_with_it() -> None:
    """The receipt told every sender to finish and report - the one thing that loses the delivery
    for a turn the bridge started, whose server carries it and exits when the turn ends. An
    incident: a request sent from such a turn was accepted, the turn ended fourteen seconds
    later, and the peer's turn was stopped mid-work with nobody told."""
    panel_shim = {'socket': '/s', 'agent': 'codex', 'pid': 1}
    with _environ(**{config.ENV_CONVERSATION_ID: 'conv_bridge_turn'}):
        by_cli = _send_receipt()
        by_panel = _send_receipt(ui_shim=panel_shim)
    editor = _send_receipt()

    warning = by_cli.get('warning') or ''
    check('a bridge-started turn delivering by CLI resume is warned the peer stops with it',
          'child of it' in warning and 'stopped mid-work' in warning, warning[:90])
    check('and is no longer told to finish what it is doing and report',
          'finish what you are doing' not in by_cli['note']
          and 'Read the warning' in by_cli['note'], by_cli['note'][:90])
    panel_warning = by_panel.get('warning') or ''
    check('delivering into a panel, it is warned the answer cannot be pushed back instead',
          'cannot be pushed back' in panel_warning and 'child of it' not in panel_warning,
          panel_warning[:90])
    check('a session in an editor panel gets neither the warning nor the changed note',
          'turn the bridge started' not in (editor.get('warning') or '')
          and 'finish what you are doing' in editor['note'], str(editor.get('warning')))


def test_the_receipt_says_how_long_the_target_has_been_idle() -> None:
    """A CLI resume of a session nobody has touched for days starts a turn nobody is watching."""
    now = time.time()
    panel_shim = {'socket': '/s', 'agent': 'codex', 'pid': 1}
    stale_cli = _send_receipt(mtime=now - 3 * 86400)
    stale_panel = _send_receipt(ui_shim=panel_shim, mtime=now - 3 * 86400)
    recent = _send_receipt(mtime=now - 60)
    unknown = _send_receipt(mtime=None)

    idle = stale_cli.get('target_last_activity_seconds_ago')
    check('the receipt reports how long the target has been idle',
          idle is not None and abs(idle - 3 * 86400) < 30, str(idle))
    check('a session idle for days, resumed over the CLI, is flagged',
          'last active 72h ago' in (stale_cli.get('warning') or ''), str(stale_cli.get('warning')))
    check('one open in a panel is not: it is plainly alive',
          'last active' not in (stale_panel.get('warning') or '')
          and stale_panel.get('target_last_activity_seconds_ago') is not None)
    check('and a recently active one is reported without a warning',
          55 <= (recent.get('target_last_activity_seconds_ago') or 0) <= 90
          and 'last active' not in (recent.get('warning') or ''),
          str(recent.get('target_last_activity_seconds_ago')))
    check('an unknown time is reported as unknown, not as zero',
          unknown.get('target_last_activity_seconds_ago') is None)


def test_a_bridge_started_turn_knows_which_session_it_is() -> None:
    """Sender identity came from the most recently active session in the server's directory when
    no panel hosted it - so a turn the bridge started was reported as whoever else was busy
    there, and the peer's answer was addressed to that session instead."""
    import subprocess
    from cross_agent_mcp import uihook

    resumed = '11111111-2222-4333-8444-555555555555'
    captured = {}

    class _Captured(Exception):
        pass

    def fake_run_cli(command, cwd, env, timeout):
        captured['command'], captured['env'] = command, env
        result = {'type': 'result', 'result': 'ok', 'session_id': resumed}
        return subprocess.CompletedProcess(command, 0, json.dumps(result) + '\n', '')

    def capture_only(command, cwd, env, timeout):
        captured['env'] = env
        raise _Captured()

    originals = (bridge._run_cli, uihook.is_enabled, uihook.find_own_session,
                 discovery.find_active_session)
    try:
        bridge._run_cli = fake_run_cli
        bridge._call_claude('hi', resumed, '/w', {'KEEP': '1'}, 30)
        told_id = captured['env'].get(config.ENV_SELF_SESSION)
        check('a resumed Claude turn is told which session it is running as',
              told_id == f'claude:{resumed}' and captured['env'].get('KEEP') == '1', str(told_id))

        bridge._call_claude('hi', None, '/w', {}, 30)
        created = captured['command'][captured['command'].index('--session-id') + 1]
        check('and so is one the bridge creates, under the id it chose for it',
              captured['env'].get(config.ENV_SELF_SESSION) == f'claude:{created}', created)

        bridge._run_cli = capture_only
        for session_id, expected in ((resumed, f'codex:{resumed}'), (None, None)):
            try:
                bridge._call_codex('hi', session_id, '/w', {}, 30)
            except _Captured:
                pass
            check(f'a Codex turn resuming {session_id!r} is told '
                  f'{expected!r}', captured['env'].get(config.ENV_SELF_SESSION) == expected,
                  str(captured['env'].get(config.ENV_SELF_SESSION)))

        with _environ(**{config.ENV_SELF_SESSION: f'claude:{resumed}'}):
            inherited = bridge._child_env('conv_x', 1, 'claude', [])
        check('a child never inherits its parent\'s identity',
              config.ENV_SELF_SESSION not in inherited)

        uihook.is_enabled = lambda: True
        uihook.find_own_session = lambda agent: {'session_id': 'panel-sid'}
        discovery.find_active_session = lambda *a, **kw: {'session_id': 'guessed-sid'}
        with _environ(**{config.ENV_SELF_SESSION: f'claude:{resumed}'}):
            declared = bridge._own_session_id('claude')
        with _environ(**{config.ENV_SELF_SESSION: f'codex:{resumed}'}):
            other_agent = bridge._own_session_id('claude')
        with _environ(**{config.ENV_SELF_SESSION: 'claude:*'}):
            wildcard = bridge._own_session_id('claude')
        with _environ(**{config.ENV_SELF_SESSION: None}):
            from_panel = bridge._own_session_id('claude')
            uihook.is_enabled = lambda: False
            guessed = bridge._own_session_id('claude')
    finally:
        (bridge._run_cli, uihook.is_enabled, uihook.find_own_session,
         discovery.find_active_session) = originals

    check('the session the bridge started it as wins over the panel and over a guess',
          declared == resumed, str(declared))
    check('a codex started inside a Claude turn is not that Claude session',
          other_agent == 'panel-sid', str(other_agent))
    check('a value that is not a session id is ignored', wildcard == 'panel-sid', str(wildcard))
    check('with nothing declared, the panel still answers, then the guess',
          from_panel == 'panel-sid' and guessed == 'guessed-sid', f'{from_panel} {guessed}')


def _stop_a_carried_delivery(kind_wants_reply: bool, sender_panel):
    """Start a real child under a delivery, stop it the way a server exiting does, and report
    what happened to the delivery and who was told."""
    import subprocess
    from cross_agent_mcp import uihook

    job = outbox.Job(
        target_agent=config.AGENT_CLAUDE, target_session_id='66666666-7777-4888-8999-000000000000',
        payload='p', run_cwd='/w', pin_cwd='/w', env={}, timeout=60, ui_shim=None, title=None,
        conversation_id='conv_stopped', hop=2, sender_agent=config.AGENT_CLAUDE,
        sender_session_id='sender-sid', wants_reply=kind_wants_reply, summary='dispatch the work')
    job.state = outbox.STATE_DELIVERING
    job.started_at = time.time()
    outbox.persist(job)

    told = []
    originals = (bridge._panel_session, uihook.send, uihook.is_enabled)
    bridge._panel_session = lambda agent, session_id, exclude: sender_panel
    uihook.send = lambda text, shim, session_id, timeout, **kw: told.append(
        (session_id, text)) or {'ok': True}
    uihook.is_enabled = lambda: True

    def carry() -> None:
        outbox._current.job = job
        try:
            bridge._run_cli(['sleep', '60'], '/tmp', dict(os.environ), 60)
        finally:
            outbox._current.job = None

    worker = threading.Thread(target=carry, daemon=True)
    try:
        worker.start()
        deadline = time.time() + 10
        while time.time() < deadline and not bridge._LIVE_CHILDREN:
            time.sleep(0.02)
        child = bridge._LIVE_CHILDREN[0] if bridge._LIVE_CHILDREN else None
        bridge.terminate_live_children()
        worker.join(timeout=10)
    finally:
        (bridge._panel_session, uihook.send, uihook.is_enabled) = originals
    return job, child, told


def test_a_delivery_stopped_with_its_carrier_is_closed_on_the_record() -> None:
    """Stopping the peer on the way out is deliberate, and used to leave the record exactly as it
    was last written - delivering, no error - so it read as still in progress, to nobody's
    knowledge, for as long as nobody asked."""
    panel = {'ui_shim': {'socket': '/s', 'agent': 'claude', 'pid': 1}}
    job, child, told = _stop_a_carried_delivery(True, panel)
    record = outbox.read_record(job.delivery_id) or {}

    check('the peer\'s turn was stopped', child is not None and child.poll() is not None)
    check('the delivery is closed as failed, with the reason on it',
          job.state == outbox.STATE_FAILED and 'exited' in (job.error or '')
          and job.finished_at is not None, f'{job.state} {job.error}')
    check('and the record on disk says so, and says the peer was stopped',
          record.get('state') == outbox.STATE_FAILED
          and record.get('is_stopped_with_carrier') is True
          and 'exited' in (record.get('error') or ''), str(record)[:160])
    check('it no longer counts as in flight',
          not os.path.exists(outbox._record_path(job.delivery_id, is_finished=False))
          and outbox.describe_origin(record).get('is_orphaned') is False)
    check('a sender with a live panel is told, once, with the delivery id and the word stopped',
          len(told) == 1 and told[0][0] == 'sender-sid' and job.delivery_id in told[0][1]
          and 'STOPPED' in told[0][1], str([(sid, text[:60]) for sid, text in told]))

    _, _, told_nobody = _stop_a_carried_delivery(True, None)[0:3]
    check('a sender with no live panel is not told: reaching it would resume a session',
          told_nobody == [])
    reply_job, _, told_reply = _stop_a_carried_delivery(False, panel)
    check('a reply that was stopped is closed too, but announces nothing',
          reply_job.state == outbox.STATE_FAILED and told_reply == [])


def test_a_worker_does_not_overwrite_why_a_delivery_was_stopped() -> None:
    """The worker waiting on the child sees only that it exited. Recording that would replace the
    reason with less, and its recovery and notice would start work in a process on its way out."""
    box = outbox.Outbox()
    notices = []
    replies = []
    box.build_notice = lambda job: notices.append(job) or None
    box.build_reply = lambda job, reply: replies.append(job) or None
    reason = 'the server carrying this delivery (pid 1) exited, so the bridge stopped the peer'

    def deliver(job):
        outbox.settle_stopped(job, reason)
        raise RuntimeError('claude CLI returned no result (exit=-15)')

    box.deliver = deliver
    job = outbox.Job(
        target_agent=config.AGENT_CLAUDE, target_session_id=None, payload='p', run_cwd='/w',
        pin_cwd='/w', env={}, timeout=60, ui_shim=None, title=None,
        conversation_id='conv_worker', hop=1, sender_agent=config.AGENT_CLAUDE,
        sender_session_id='sender-sid', wants_reply=True, summary='work')
    box._run(job)

    check('the reason the delivery was stopped survives the worker', job.error == reason,
          str(job.error))
    check('the record on disk still carries it',
          (outbox.read_record(job.delivery_id) or {}).get('error') == reason)
    check('and no notice or reply is started from a process that is exiting',
          notices == [] and replies == [], f'{len(notices)} {len(replies)}')


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
    originals = (discovery.find_session, bridge._panel_session)
    discovery.find_session = lambda agent, session_id: None
    bridge._panel_session = _fake_panel('sender-sid')
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
        (discovery.find_session, bridge._panel_session) = originals


def test_a_reply_runs_where_the_senders_session_lives() -> None:
    """A Claude transcript is filed under its own project dir; resuming elsewhere fails."""
    with tempfile.TemporaryDirectory(prefix='sender-home-') as sender_home:
        originals = (discovery.find_session, bridge._panel_session)
        discovery.find_session = lambda agent, session_id: (
            {'session_id': session_id, 'cwd': sender_home} if session_id == 'sender-sid' else None)
        bridge._panel_session = _fake_panel('sender-sid')
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
            (discovery.find_session, bridge._panel_session) = originals

    orphan = outbox.Job(
        target_agent=config.AGENT_CODEX, target_session_id='peer-sid', payload='x',
        run_cwd='/elsewhere', pin_cwd='/elsewhere', env={}, timeout=5, ui_shim=None, title=None,
        conversation_id='conv_reply', hop=1, sender_agent=config.AGENT_CLAUDE,
        sender_session_id=None, wants_reply=True, summary='req')
    check('a sender with no session produces no undeliverable reply job',
          bridge._build_reply_job(orphan, 'the answer') is None)


def _request_job(conversation_id: str, sender_session_id: str = 'sender-sid') -> 'outbox.Job':
    return outbox.Job(
        target_agent=config.AGENT_CODEX, target_session_id='peer-sid', payload='x',
        run_cwd='/w', pin_cwd='/w', env={}, timeout=5, ui_shim=None, title=None,
        conversation_id=conversation_id, hop=1, sender_agent=config.AGENT_CLAUDE,
        sender_session_id=sender_session_id, wants_reply=True, summary='req')


def test_an_answer_with_no_panel_to_land_in_is_kept_rather_than_resumed() -> None:
    """The incident: a reply into a panel-less session started a second agent in that session.

    The sender was an ordinary live Claude session with no IDE panel. Its own question came
    back as an answer, the bridge found no panel to put the answer in, and fell back to
    `claude -p --resume` - a second headless agent in the same session, with the same tools
    and permissions, which ran Bash, Grep and AskUserQuestion and answered as that session.
    Nobody asked for it and nobody could see it.

    A reply is not addressed traffic. It goes where the exchange started, and the session that
    started it is by definition one that was live a moment ago - so a resume there is a rival
    agent, not a delivery. Without a panel no answer goes back: a preview of it stays on the
    record, and the answer itself stays where the peer wrote it.
    """
    originals = (discovery.find_session, bridge._panel_session)
    discovery.find_session = lambda agent, session_id: None
    try:
        bridge._panel_session = lambda agent, wanted, exclude: None
        request = _request_job('conv_held')
        request.reply = 'the peer said this'
        request.reply_length = len(request.reply)

        check('no reply job is built when the sender has no panel',
              bridge._build_reply_job(request, request.reply) is None)
        check('and the request records that the answer was not put into the sender session',
              request.is_return_status_only is True)
        record = request.describe()
        check('so bridge_status says so',
              record.get('is_return_status_only') is True, str(record.get(
                  'is_return_status_only')))
        check('while still previewing the answer, so the record is not silent about it',
              record.get('reply_preview') == 'the peer said this',
              str(record.get('reply_preview')))
        check('and the preview is a bounded summary, not the answer itself',
              outbox.REPLY_PREVIEW_LIMIT == 2000
              and record.get('reply_length') == len('the peer said this'),
              f'limit {outbox.REPLY_PREVIEW_LIMIT}, length {record.get("reply_length")}')

        # The inverse. Same request, same answer, one difference: a panel exists.
        bridge._panel_session = _fake_panel('sender-sid')
        with_panel = _request_job('conv_held')
        reply = bridge._build_reply_job(with_panel, 'the peer said this')
        check('the same answer is delivered when there is a panel to deliver it into',
              reply is not None)
        check('and goes over that panel rather than by resuming the session',
              reply is not None and (reply.ui_shim or {}).get('pid') == 4242,
              str(reply and reply.ui_shim))
        check('and nothing is marked status-only when it was in fact delivered',
              with_panel.is_return_status_only is False)
    finally:
        (discovery.find_session, bridge._panel_session) = originals


def test_a_failure_notice_with_no_panel_is_kept_rather_than_resumed() -> None:
    """A notice travels the same road as a reply, so it carries the same defect.

    `_build_notice_job` resolves the sender's panel exactly as `_build_reply_job` does and
    falls back the same way. A request that produced no answer would announce itself by
    starting an agent in the session waiting to be told.
    """
    originals = (discovery.find_session, bridge._panel_session)
    discovery.find_session = lambda agent, session_id: None
    try:
        bridge._panel_session = lambda agent, wanted, exclude: None
        failed = _request_job('conv_notice')
        failed.error = 'BridgeError: the peer never answered'
        failed.finished_at = time.time()

        check('no notice job is built when the sender has no panel',
              bridge._build_notice_job(failed) is None)
        check('and the failure is recorded as status-only rather than announced',
              failed.is_return_status_only is True)

        bridge._panel_session = _fake_panel('sender-sid')
        with_panel = _request_job('conv_notice')
        with_panel.error = 'BridgeError: the peer never answered'
        with_panel.finished_at = time.time()
        notice = bridge._build_notice_job(with_panel)
        check('the same failure is announced when there is a panel to announce it into',
              notice is not None)
        check('over that panel, not by resuming the session',
              notice is not None and (notice.ui_shim or {}).get('pid') == 4242,
              str(notice and notice.ui_shim))
    finally:
        (discovery.find_session, bridge._panel_session) = originals


def test_a_retry_that_finds_no_panel_does_not_fall_back_to_a_resume() -> None:
    """The third road back, and the one that survives a fix to the other two.

    `_reroute` runs between retries of a reply that did not land - the sender's tab closed and
    reopened behind a new socket. It re-resolved the panel and, finding none, set `ui_shim` to
    None, which is the resume. Only return traffic is ever retried (`_deliver_with_retries`
    gives a request a single attempt), so everything reaching here is a reply or a notice.
    """
    original = bridge._panel_session
    try:
        reply = outbox.Job(
            target_agent=config.AGENT_CLAUDE, target_session_id='sender-sid', payload='answer',
            run_cwd='/w', pin_cwd='/w', env={}, timeout=5,
            ui_shim={'pid': 1111, 'socket': '/tmp/gone.sock'}, title=None,
            conversation_id='conv_reroute', hop=1, sender_agent=config.AGENT_CODEX,
            sender_session_id='peer-sid', wants_reply=False, summary='answer',
            kind=outbox.KIND_REPLY)

        bridge._panel_session = _fake_panel('sender-sid')
        bridge._reroute(reply)
        check('a reroute that finds the panel again points the retry at it',
              (reply.ui_shim or {}).get('pid') == 4242, str(reply.ui_shim))

        bridge._panel_session = lambda agent, wanted, exclude: None
        bridge._reroute(reply)
        check('and one that finds no panel leaves the retry with nowhere to go',
              reply.ui_shim is None, str(reply.ui_shim))
        # The caller is spied on rather than left real: `_call_claude` raises
        # NotDeliveredError of its own for a session it cannot find, so a check that only
        # watched for the exception would pass with the guard taken out.
        reached = []
        caller = bridge.CALLERS[config.AGENT_CLAUDE]
        bridge.CALLERS[config.AGENT_CLAUDE] = lambda *a, **kw: reached.append(a) or {}
        raised = None
        try:
            bridge._deliver(reply)
        except Exception as e:
            raised = e
        finally:
            bridge.CALLERS[config.AGENT_CLAUDE] = caller
        check('so the retry is refused rather than run as a resume',
              isinstance(raised, outbox.NotDeliveredError), f'{type(raised).__name__}: {raised}')
        check('and no agent was started for it',
              reached == [], str(reached))
    finally:
        bridge._panel_session = original


def test_a_send_to_a_dormant_session_still_resumes_it() -> None:
    """The rule is about return traffic only, and this is what says so.

    Someone choosing a session and sending to it has asked for that session to run. Resuming
    it is the delivery, and nothing above should have taken that away: without this check the
    fix could be a blanket ban on the CLI path and the suite would look just as green.
    """
    called: list = []
    original = bridge.CALLERS[config.AGENT_CLAUDE]
    bridge.CALLERS[config.AGENT_CLAUDE] = lambda *a, **kw: called.append((a, kw)) or {
        'session_id': 'dormant-sid', 'reply': 'done', 'is_new_session': False}
    try:
        request = outbox.Job(
            target_agent=config.AGENT_CLAUDE, target_session_id='dormant-sid', payload='work',
            run_cwd='/w', pin_cwd='/w', env={}, timeout=5, ui_shim=None, title=None,
            conversation_id='conv_dormant', hop=1, sender_agent=config.AGENT_CODEX,
            sender_session_id='peer-sid', wants_reply=True, summary='req',
            kind=outbox.KIND_REQUEST)
        result, raised = None, None
        try:
            result = bridge._deliver(request)
        except Exception as e:
            raised = e
        check('an addressed request with no panel still reaches the caller',
              raised is None and len(called) == 1
              and (result or {}).get('session_id') == 'dormant-sid',
              f'{type(raised).__name__}: {raised}' if raised else str(called))
        check('by the resume path, which is what the sender asked for',
              bool(called) and called[0][0][5] is None,
              str(called and called[0][0][5]))
    finally:
        bridge.CALLERS[config.AGENT_CLAUDE] = original


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


def _write_long_claude_transcript(path: str, first_message: str, name: str = '') -> None:
    """A transcript long enough that its end is past the head window, named only at the end.

    This is the shape every real conversation reaches. `_write_claude_transcript` puts the
    first name ahead of the first message, so its sessions are named inside the head no
    matter how they grow - the one shape that cannot reproduce a late rename.
    """
    lines = [json.dumps({
        'type': 'user', 'isSidechain': False, 'cwd': '/w', 'entrypoint': 'claude-vscode',
        'message': {'content': first_message}})]
    lines += [json.dumps({'type': 'assistant', 'isSidechain': False, 'cwd': '/w',
                          'message': {'content': f'turn {i}'}})
              for i in range(discovery.CLAUDE_HEAD_LINES * 2)]
    if name:
        lines.append(json.dumps({'type': 'custom-title', 'customTitle': name}))
    with open(path, 'w', encoding='utf-8') as f:
        f.write('\n'.join(lines) + '\n')


def test_a_rename_after_the_head_window_is_still_the_conversations_name() -> None:
    """A conversation named once it was long could not be reached by the name on its panel.

    The name entry is appended wherever the transcript currently ends. The head was searched
    for it and the tail was only re-read when the head had already shown one, so a session
    named for the first time after CLAUDE_HEAD_LINES lines kept answering to its generated
    title - and `session_id='<the name on the panel>'` failed, which is the one address a
    human can be expected to give. Seen on a real store: 5391 lines, renamed at line 5388.

    Naming a conversation is how you address it, and conversations are named once they have
    turned out to matter - which is to say, once they are long.
    """
    with tempfile.TemporaryDirectory(prefix='claude-late-rename-') as store:
        late = store + '/44444444-4444-4444-4444-444444444444.jsonl'
        _write_long_claude_transcript(late, 'start on the store units', name='Implementation')
        parsed = discovery._parse_claude_session(late)
        check('a conversation named after the head window answers to that name',
              parsed is not None and parsed['title'] == 'Implementation',
              str(parsed and parsed['title']))
        check('and is marked as named', parsed is not None and parsed['is_named'] is True)

        # the tail is now read for every session, so a nameless one must not acquire a name
        nameless = store + '/55555555-5555-5555-5555-555555555555.jsonl'
        _write_long_claude_transcript(nameless, 'start on the store units')
        parsed = discovery._parse_claude_session(nameless)
        check('a long conversation with no name is still unnamed',
              parsed is not None and parsed['is_named'] is False,
              str(parsed and parsed['title']))

        # A real store rather than a stubbed list_sessions: the lookup is free to reach the
        # transcripts by whichever route it likes, so this keeps testing the same thing if
        # that route changes.
        with tempfile.TemporaryDirectory(prefix='claude-late-rename-store-') as root:
            project = root + '/projects/-w'
            os.makedirs(project)
            shutil.copy(late, project + '/44444444-4444-4444-4444-444444444444.jsonl')
            shutil.copy(nameless, project + '/55555555-5555-5555-5555-555555555555.jsonl')

            saved = config.CLAUDE_PROJECTS_DIR
            config.CLAUDE_PROJECTS_DIR = root + '/projects/'
            try:
                found = discovery.find_session_by_name('claude', 'Implementation')
            finally:
                config.CLAUDE_PROJECTS_DIR = saved

            check('and the late name is what the lookup finds it by',
                  (found or {}).get('session_id') == '44444444-4444-4444-4444-444444444444',
                  str(found and found.get('session_id')))


def _transcript_with_records(path: str, records: list) -> None:
    """A Claude transcript whose given records all fall past the head window.

    Without the padding the head scan finds the name and the tail reader is never reached, so
    every check here would pass with the tail reader broken - which is how these were first
    written.
    """
    with open(path, 'w', encoding='utf-8') as f:
        f.write(json.dumps({
            'type': 'user', 'isSidechain': False, 'cwd': '/w', 'entrypoint': 'claude-vscode',
            'message': {'content': 'open the store units'}}) + '\n')
        for index in range(discovery.CLAUDE_HEAD_LINES * 2):
            f.write(json.dumps({'type': 'assistant', 'isSidechain': False, 'cwd': '/w',
                                'message': {'content': f'turn {index}'}}) + '\n')
        for record in records:
            f.write(json.dumps(record) + '\n')


def _named(name: str) -> dict:
    return {'type': 'custom-title', 'customTitle': name}


def _bulk(size: int) -> dict:
    return {'type': 'assistant', 'isSidechain': False, 'cwd': '/w',
            'message': {'content': 'x' * size}}


def test_the_current_name_is_found_however_the_transcript_ends() -> None:
    """The end of a transcript is read backwards until a name is found, not in a fixed window.

    A single record here has been measured at 481,799 bytes, so a window chosen to mean "the
    last few hundred lines" can be spent entirely inside one record and miss the name written
    just before it. These are the shapes that a fixed window gets wrong.
    """
    with tempfile.TemporaryDirectory(prefix='claude-rename-shapes-') as store:
        def title_of(records):
            path = store + '/66666666-6666-4666-8666-666666666666.jsonl'
            _transcript_with_records(path, records)
            parsed = discovery._parse_claude_session(path)
            return parsed and parsed['title'], parsed and parsed['is_named']

        title, is_named = title_of([_named('Renamed late'), _bulk(70_000)])
        check('a name followed by one record larger than 64 KiB is still found',
              title == 'Renamed late' and is_named is True, str(title))

        title, _ = title_of([_named('Renamed late'), _bulk(481_799)])
        check('and one record larger than any plausible fixed window is still found',
              title == 'Renamed late', str(title))

        title, _ = title_of([_named('First name'), _bulk(2_000),
                             _named('Second name'), _bulk(2_000),
                             _named('Current name'), _bulk(2_000)])
        check('the most recent of several names is the one it answers to',
              title == 'Current name', str(title))

        quoting = {'type': 'assistant', 'isSidechain': False, 'cwd': '/w', 'message': {
            'content': 'the entry looks like {"type":"custom-title","customTitle":"Not this"}'}}
        title, is_named = title_of([_named('The real name'), quoting])
        check('a record merely quoting "custom-title" is not mistaken for one',
              title == 'The real name', str(title))

        title, is_named = title_of([quoting])
        check('and quoting it does not name an unnamed conversation',
              is_named is False, f'{title!r} named={is_named}')

        # A record that is not a rename but carries the field anyway. The type is what makes a
        # record a rename; taking any customTitle found would answer to this one.
        # The key spelled "custom-title" puts that exact text in the raw line, so this record
        # reaches the parse rather than being dropped by the cheap pre-filter. What rejects it
        # is its type.
        impostor = {'type': 'assistant', 'isSidechain': False, 'cwd': '/w',
                    'custom-title': 'a field, not a record type',
                    'customTitle': 'Not this either', 'message': {'content': 'a tool result'}}
        title, is_named = title_of([_named('The real name'), impostor])
        check('a record of another type carrying customTitle is not a rename',
              title == 'The real name', str(title))

        title, is_named = title_of([impostor])
        check('and it alone does not name the conversation',
              is_named is False, f'{title!r} named={is_named}')

        title, is_named = title_of([_bulk(200)])
        check('a conversation with no name is not given one', is_named is False, str(title))


def test_a_name_that_straddles_a_chunk_boundary_is_still_whole() -> None:
    """The chunk boundary is not a record boundary, so records are rejoined across it.

    The read works backwards in fixed chunks. A record that begins in one chunk and ends in
    the next arrives in two pieces, and a reader that judged each piece on its own would see
    two fragments of JSON and no name. These place a rename exactly across that seam, and
    make one longer than a whole chunk so it has to survive being split more than once.
    """
    with tempfile.TemporaryDirectory(prefix='claude-chunk-seam-') as store:
        path = store + '/88888888-8888-4888-8888-888888888888.jsonl'

        def title_after(trailing: int, name: str = 'Across the seam'):
            _transcript_with_records(path, [_named(name), _bulk(trailing)])
            parsed = discovery._parse_claude_session(path)
            return parsed and parsed['title']

        # walk the rename through a whole chunk's worth of offsets: wherever the boundary
        # falls inside it, it must still be read as one record
        misses = [offset for offset in range(65_400, 65_700, 17)
                  if title_after(offset) != 'Across the seam']
        check('a rename split across a chunk boundary is rejoined',
              not misses, f'missed at trailing offsets {misses[:5]}')

        long_name = 'Name ' + 'y' * 100_000
        _transcript_with_records(path, [_named(long_name), _bulk(70_000)])
        parsed = discovery._parse_claude_session(path)
        check('and a rename record longer than a chunk survives being split repeatedly',
              parsed is not None and parsed['title'] == long_name,
              str(parsed and parsed['title'])[:60])


def test_a_record_cut_off_by_the_search_bound_is_not_half_read() -> None:
    """The record the bound lands inside is incomplete by definition and is dropped.

    Everything before it is unreachable anyway, so a partial record carries no meaning - and
    feeding half a line to the parser is how a reader invents a name that was never written.
    """
    with tempfile.TemporaryDirectory(prefix='claude-cut-record-') as store:
        path = store + '/99999999-9999-4999-8999-999999999999.jsonl'
        # a rename made enormous so the cap falls inside it rather than between records
        straddling = 'Cut in half ' + 'z' * (discovery.TAIL_BYTES // 2)
        _transcript_with_records(path, [_named(straddling), _bulk(discovery.TAIL_BYTES)])

        records = list(discovery._reversed_records(path, discovery.TAIL_BYTES))
        unparsable = []
        for line in records:
            try:
                json.loads(line)
            except ValueError:
                unparsable.append(line[:40])
        check('every record handed back is whole enough to parse', not unparsable,
              str(unparsable[:2]))

        parsed = discovery._parse_claude_session(path)
        check('and a rename the bound cuts through does not name the conversation',
              parsed is not None and parsed['is_named'] is False,
              str(parsed and parsed['title'])[:60])


def test_a_name_further_back_than_the_search_bound_is_not_found() -> None:
    """The bound is a real limit, and this is what reaching it looks like.

    The read stops after TAIL_BYTES so that listing sessions cannot be made arbitrarily
    expensive by one enormous transcript. A name buried further back than that is not found,
    and the conversation falls back to its generated title - reachable by id, not by name.
    This is a deliberate trade rather than an oversight, so it is pinned here.
    """
    with tempfile.TemporaryDirectory(prefix='claude-rename-bound-') as store:
        path = store + '/77777777-7777-4777-8777-777777777777.jsonl'
        beyond = discovery.TAIL_BYTES + 100_000
        _transcript_with_records(path, [_named('Too far back'), _bulk(beyond)])

        parsed = discovery._parse_claude_session(path)
        check('a name further back than the bound is not found',
              parsed is not None and parsed['title'] != 'Too far back', str(parsed['title']))
        check('and the conversation is reported as unnamed rather than mis-named',
              parsed is not None and parsed['is_named'] is False, str(parsed['is_named']))

        read = list(discovery._reversed_records(path, discovery.TAIL_BYTES))
        check('the reader stopped at the bound rather than reading the whole file',
              sum(len(line) for line in read) <= discovery.TAIL_BYTES,
              f'{sum(len(line) for line in read)} of {os.path.getsize(path)} bytes')


def _titled(session_id, title, mtime, is_named, is_active=True, age_minutes=5):
    return {'session_id': session_id, 'title': title, 'mtime': mtime, 'is_named': is_named,
            'updated_at': '2026-09-21 10:00', 'age_minutes': age_minutes,
            'is_active': is_active, 'cwd': '/w', 'agent': 'claude'}


def test_a_name_two_conversations_answer_to_is_refused_rather_than_guessed() -> None:
    """Taking the freshest is a guess, made where the message is about to be written.

    A retired session keeps its name until somebody renames it, so "the newest" picks the
    replacement only by luck. The caller is told which conversations answer to the name, so it
    can address one exactly, and how to stop the name meaning two things.
    """
    shared = [_titled('replacement', 'billing-api', 300.0, True),
              _titled('retired', 'billing-api', 100.0, True, is_active=False, age_minutes=28800),
              _titled('unrelated', 'Studio primer', 50.0, True)]
    original = discovery.list_sessions
    discovery.list_sessions = lambda agent, scope, cwd, limit=500, **kw: list(shared)
    try:
        raised = None
        try:
            discovery.find_session_by_name('claude', 'billing-api')
        except Exception as e:
            raised = e
        check('a name two conversations answer to is refused, not resolved',
              isinstance(raised, discovery.AmbiguousSessionName),
              f'{type(raised).__name__}: {raised}')
        check('and both are listed, so the caller can address one by id',
              raised is not None and 'replacement' in str(raised) and 'retired' in str(raised),
              str(raised))
        check('and the refusal says to address one by its id',
              raised is not None and 'by its id' in str(raised), str(raised))
        check('and points at renaming the retired one, which is what fixes it for good',
              raised is not None and 'renaming' in str(raised), str(raised))
        check('a name only one conversation answers to still resolves',
              (discovery.find_session_by_name('claude', 'Studio primer') or {})
              .get('session_id') == 'unrelated')
    finally:
        discovery.list_sessions = original


def test_the_candidates_say_whether_each_name_was_assigned_or_generated() -> None:
    """Which is how a caller tells a retired conversation from its replacement.

    A generated title is not an address at all: the same opening prompt produces the same
    title, so it can be shared by conversations that were never named anything.
    """
    generated = [_titled('first-run', 'Fix the failing build', 300.0, False),
                 _titled('second-run', 'Fix the failing build', 100.0, False)]
    original = discovery.list_sessions
    discovery.list_sessions = lambda agent, scope, cwd, limit=500, **kw: list(generated)
    try:
        raised = None
        try:
            discovery.find_session_by_name('claude', 'Fix the failing build')
        except Exception as e:
            raised = e
        check('two sessions sharing a generated title are refused as well',
              isinstance(raised, discovery.AmbiguousSessionName),
              f'{type(raised).__name__}: {raised}')
        check('and the refusal says the title was generated, not chosen',
              raised is not None and 'generated from its first message' in str(raised),
              str(raised))

        discovery.list_sessions = lambda agent, scope, cwd, limit=500, **kw: [
            _titled('named-one', 'billing-api', 300.0, True),
            _titled('named-two', 'billing-api', 100.0, True)]
        raised = None
        try:
            discovery.find_session_by_name('claude', 'billing-api')
        except Exception as e:
            raised = e
        check('while a name somebody assigned is described as assigned',
              raised is not None and 'name assigned' in str(raised)
              and 'generated from its first message' not in str(raised), str(raised))
    finally:
        discovery.list_sessions = original


def test_a_name_found_only_inside_the_search_window_is_not_called_unique() -> None:
    """One match out of a capped scan is not proof of one match.

    It looks exactly like an ordinary successful lookup from the outside, which is why it is
    refused rather than logged: the alternative is delivering on the first match found under a
    cap, which is the guess AmbiguousSessionName exists to refuse.
    """
    many = [_titled('needle', 'billing-api', 300.0, True)] + [
        _titled(f'hay-{i}', f'something else {i}', 200.0 - i, True) for i in range(3)]
    original = discovery.list_sessions
    discovery.list_sessions = lambda agent, scope, cwd, limit=500, **kw: list(many)[:limit]
    try:
        raised = None
        try:
            discovery.find_session_by_name('claude', 'billing-api', limit=3)
        except Exception as e:
            raised = e
        check('a single match from a scan that hit its cap is refused',
              isinstance(raised, discovery.UnprovenSessionName),
              f'{type(raised).__name__}: {raised}')
        check('and the refusal says how far the search actually got',
              isinstance(raised, discovery.UnprovenSessionName) and raised.scanned == 3
              and ' 3 session(s)' in str(raised),
              str(raised))
        check('and says the id addresses the conversation whatever else carries the name',
              raised is not None and 'id addresses it exactly' in str(raised), str(raised))

        check('the same name resolves once the scan can see every session',
              (discovery.find_session_by_name('claude', 'billing-api', limit=10) or {})
              .get('session_id') == 'needle')
    finally:
        discovery.list_sessions = original


def test_a_codex_name_is_read_from_the_store_and_the_scan_bound_is_respected() -> None:
    """Two things a stubbed listing cannot show, so this builds a real Codex store.

    The Codex scan gives up after CODEX_SCAN_LIMIT rollout files whatever the name cap is, so
    a short result can be a scan that ran out rather than a store that did - and the count the
    caller is told has to be what was actually reached, not the cap that was asked for. The
    same store is the only place the name in session_index.jsonl is read, which is what makes
    a Codex thread's name an assigned one rather than something generated.
    """
    saved = (config.CODEX_SESSIONS_DIR, config.CODEX_SCAN_LIMIT,
             config.CODEX_SESSION_INDEX_PATH)
    with tempfile.TemporaryDirectory(prefix='codex-truncated-') as store:
        now = time.time()
        wanted = 'aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee'
        twin = 'bbbbbbbb-cccc-dddd-eeee-ffffffffffff'
        _write_rollout(store, wanted, store + '/w', now)
        _write_rollout(store, twin, store + '/w', now - 5)
        for i in range(12):
            _write_rollout(store, f'0000000{i:04d}-0000-0000-0000-00000000000{i % 10}',
                           store + '/w', now - 100 - i)

        index = store + '/session_index.jsonl'
        with open(index, 'w', encoding='utf-8') as f:
            f.write(json.dumps({'id': wanted, 'thread_name': 'billing-api'}) + '\n')
            f.write(json.dumps({'id': twin, 'thread_name': 'billing-api'}) + '\n')

        config.CODEX_SESSIONS_DIR = store + '/'
        config.CODEX_SESSION_INDEX_PATH = index
        try:
            config.CODEX_SCAN_LIMIT = 500
            raised = None
            try:
                discovery.find_session_by_name('codex', 'billing-api', limit=100)
            except Exception as e:
                raised = e
            check('two Codex threads sharing an indexed name are refused',
                  isinstance(raised, discovery.AmbiguousSessionName),
                  f'{type(raised).__name__}: {raised}')
            check('and a name that came from session_index.jsonl is described as assigned',
                  raised is not None and 'name assigned' in str(raised)
                  and 'generated from its first message' not in str(raised), str(raised))

            with open(index, 'w', encoding='utf-8') as f:
                f.write(json.dumps({'id': wanted, 'thread_name': 'billing-api'}) + '\n')

            resolved = discovery.find_session_by_name('codex', 'billing-api', limit=100)
            check('one thread carrying the name resolves when the scan reaches the whole store',
                  (resolved or {}).get('session_id') == wanted, str(resolved))

            config.CODEX_SCAN_LIMIT = 4
            raised = None
            try:
                discovery.find_session_by_name('codex', 'billing-api', limit=100)
            except Exception as e:
                raised = e
            check('a name found inside a truncated Codex scan is not called unique',
                  isinstance(raised, discovery.UnprovenSessionName),
                  f'{type(raised).__name__}: {raised}')
            check('and the refusal counts what the search reached, not the cap it was given',
                  isinstance(raised, discovery.UnprovenSessionName)
                  and raised.scanned == 4 and ' 4 session(s)' in str(raised),
                  f'scanned={raised and getattr(raised, "scanned", None)}: {raised}')
        finally:
            (config.CODEX_SESSIONS_DIR, config.CODEX_SCAN_LIMIT,
             config.CODEX_SESSION_INDEX_PATH) = saved


def test_an_ambiguous_name_stops_the_send_rather_than_reaching_a_conversation() -> None:
    """The refusal has to reach the caller as a failed tool call, with nothing sent."""
    shared = [_titled('one', 'billing-api', 300.0, True),
              _titled('two', 'billing-api', 100.0, True)]
    originals = (discovery.list_sessions, discovery.find_session)
    discovery.list_sessions = lambda agent, scope, cwd, limit=500, **kw: list(shared)
    discovery.find_session = lambda agent, sid: None
    try:
        raised = None
        try:
            bridge._requested_session_id('claude', 'billing-api', '/w')
        except Exception as e:
            raised = e
        check('the bridge refuses the send outright, as a bridge failure',
              isinstance(raised, bridge.BridgeError),
              f'{type(raised).__name__}: {raised}')
        check('and says nothing was sent and nothing was created',
              raised is not None and 'Nothing was sent' in str(raised)
              and 'no session was created' in str(raised), str(raised))
        check('and carries the candidates through to the caller',
              raised is not None and 'one' in str(raised) and 'two' in str(raised), str(raised))
    finally:
        (discovery.list_sessions, discovery.find_session) = originals


def test_a_store_entry_answers_only_for_the_session_it_actually_holds() -> None:
    """Two ways a lookup by id can answer for a conversation nobody asked about.

    A name is not a location: a transcript in the store can be a symlink to a file outside it,
    and reading it answers with whatever that file contains. And the name of a Codex rollout is
    not what makes it that session's - the record inside says whose it is - so a file whose two
    disagree answers for the session named in its payload rather than the one that was asked
    for. Both are the mistake the id-shape check fixed, one layer further in: trusting a name
    to say what something is.
    """
    saved = (config.CLAUDE_PROJECTS_DIR, config.CODEX_SESSIONS_DIR)
    asked = 'aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee'
    other = 'ffffffff-9999-8888-7777-666666666666'
    try:
        with tempfile.TemporaryDirectory(prefix='codex-mismatch-') as codex_store:
            _write_rollout(codex_store, other, '/w', time.time())
            day = codex_store + '/2026/08/08'
            # a copy carrying the asked-for id in its name while still declaring `other`
            shutil.copyfile(f'{day}/rollout-2026-08-08T00-00-00-{other}.jsonl',
                            f'{day}/rollout-2026-08-08T00-00-00-{asked}.jsonl')
            config.CODEX_SESSIONS_DIR = codex_store + '/'

            check('a rollout named for one session but declaring another is not an answer',
                  discovery.find_session('codex', asked) is None,
                  str(discovery.find_session('codex', asked)))
            check('while the session it really holds is still reachable by its own id',
                  (discovery.find_session('codex', other) or {}).get('session_id') == other)

        with tempfile.TemporaryDirectory(prefix='outside-store-') as outside:
            real = outside + '/secret.jsonl'
            with open(real, 'w', encoding='utf-8') as f:
                f.write(json.dumps({'sessionId': asked, 'cwd': '/elsewhere', 'type': 'user',
                                    'message': {'role': 'user', 'content': 'outside'}}) + '\n')
            with tempfile.TemporaryDirectory(prefix='claude-store-') as claude_store:
                os.makedirs(claude_store + '/-w')
                os.symlink(real, f'{claude_store}/-w/{asked}.jsonl')
                config.CLAUDE_PROJECTS_DIR = claude_store + '/'

                check('a transcript that resolves outside the store is not read from it',
                      discovery.find_session('claude', asked) is None,
                      str(discovery.find_session('claude', asked)))
                check('and the containment test agrees about where that file really is',
                      not discovery.is_inside(f'{claude_store}/-w/{asked}.jsonl', claude_store)
                      and discovery.is_inside(real, outside))

            _write_rollout(outside, asked, '/elsewhere', time.time())
            with tempfile.TemporaryDirectory(prefix='codex-store-') as codex_store:
                day = codex_store + '/2026/08/08'
                os.makedirs(day)
                os.symlink(f'{outside}/2026/08/08/rollout-2026-08-08T00-00-00-{asked}.jsonl',
                           f'{day}/rollout-2026-08-08T00-00-00-{asked}.jsonl')
                config.CODEX_SESSIONS_DIR = codex_store + '/'

                check('nor is a rollout that resolves outside the Codex store',
                      discovery.find_session('codex', asked) is None,
                      str(discovery.find_session('codex', asked)))
    finally:
        (config.CLAUDE_PROJECTS_DIR, config.CODEX_SESSIONS_DIR) = saved


def test_a_session_id_cannot_name_a_lock_outside_the_lock_directory() -> None:
    """The id a reply is addressed with is whatever the calling client declared about itself.

    `caller_session_from_meta` takes the thread id out of the turn metadata and nothing checks
    its shape, so it reaches the lock name as it arrived. Interpolated into that name, a `/`
    puts the claim somewhere other than where every other claim is looked for: the bridge would
    believe it held a lock that nothing else consults, which is exclusion that silently is not.
    """
    ordinary = '01a0b4b3-c75a-77d2-95b6-be4299edf5f9'
    check('an ordinary id still names a lock, in the lock directory',
          os.path.dirname(os.path.realpath(registry._lock_path(config.AGENT_CODEX, ordinary)))
          == os.path.realpath(config.LOCK_DIR))

    for crafted in ('../../../../tmp/escaped', 'a/b', '../sibling'):
        raised = None
        try:
            registry._lock_path(config.AGENT_CODEX, crafted)
        except Exception as e:
            raised = e
        check(f'{crafted!r} is refused as a lock name',
              isinstance(raised, registry.UnusableSessionId),
              f'{type(raised).__name__}: {raised}')

    check('asking whether such an id is busy is answered, not raised',
          registry.read_busy_lock(config.AGENT_CODEX, '../../../../tmp/escaped') is None)


def test_a_session_named_in_capitals_still_reaches_its_own_panel() -> None:
    """A uuid is case-insensitive; the panel registry is not, and the two disagreed.

    `find_session` lowercases before it looks, so an id given in capitals resolves perfectly
    well - but the caller's own spelling was what got carried forward, and `find_live_session`
    compares ids as plain strings. So the panel showing that very conversation was not
    recognised as its panel, and the message was delivered by resuming the session in a new
    process instead: a second agent started for a conversation open in the editor.

    Nothing rejects a capitalised id, and nothing warns - the send succeeds and reports a
    `cli-resume`, which is exactly what it would say for a session that really was dormant.
    """
    from cross_agent_mcp import uihook

    canonical = 'a1b2c3d4-0000-4000-8000-0123456789ab'
    shouted = canonical.upper()
    panel = {'session_id': canonical, 'cwd': '/w', 'shim': {'pid': 7, 'socket': '/tmp/p.sock'},
             'last_seen': 1.0, 'is_foreign_window': False}

    originals = (uihook.is_enabled, uihook.find_live_sessions, uihook.find_foreign_sessions,
                 discovery.find_session)
    uihook.is_enabled = lambda: True
    uihook.find_live_sessions = lambda agent: [panel]
    uihook.find_foreign_sessions = lambda agent: []
    # the store answers for either spelling, because find_session lowercases before looking
    discovery.find_session = lambda agent, sid: (
        {'session_id': canonical, 'cwd': '/w', 'mtime': 1.0, 'is_active': True,
         'agent': agent, 'source': 'disk'} if str(sid).lower() == canonical else None)
    try:
        check('the lowercase spelling reaches the panel, as it always did',
              (bridge._resolve_target(config.AGENT_CLAUDE, canonical, 'cwd', '/w', False, [])
               or {}).get('ui_shim') is not None)

        wanted, requested = bridge._requested_session_id(config.AGENT_CLAUDE, shouted, '/w')
        check('a capitalised id resolves to the one the store knows it by',
              wanted == canonical, str(wanted))
        check("while the caller's own spelling is kept for reporting back to them",
              requested == shouted, str(requested))

        target = bridge._resolve_target(config.AGENT_CLAUDE, shouted, 'cwd', '/w', False, [])
        check('so the same session reaches the same panel when it is named in capitals',
              (target or {}).get('ui_shim') is not None,
              f'routed to {"panel" if (target or {}).get("ui_shim") else "cli resume"}')
        check('and it is the panel for that conversation, not some other one',
              (target or {}).get('session_id') == canonical, str((target or {}).get('session_id')))
    finally:
        (uihook.is_enabled, uihook.find_live_sessions, uihook.find_foreign_sessions,
         discovery.find_session) = originals


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


def test_a_session_id_is_recognised_by_its_shape_and_never_used_as_a_pattern() -> None:
    """`find_session` spliced whatever it was given into a glob, so `*` found some other session.

    A session is looked up by file name, and a name, a wildcard or a path is not an id. Answering
    a wildcard with whichever session the glob listed first sent a relay to a conversation nobody
    named, and the receipt reported it as the caller's own address.
    """
    from cross_agent_mcp import uihook

    real_id = '6c09c546-1111-4222-8333-444455556666'
    other_id = '01a0c298-aaaa-4bbb-8ccc-ddddeeeeffff'
    not_ids = ['*', '????????-*', '[0-9a-f]*', real_id[:8] + '*', '*' + real_id[-12:],
               '', ' ' + real_id, real_id + '\n', real_id[:-1],
               '../projects/-w/' + real_id, None]

    saved = (config.CLAUDE_PROJECTS_DIR, config.CODEX_SESSIONS_DIR, uihook.is_enabled)
    with tempfile.TemporaryDirectory(prefix='claude-id-shape-') as root, \
            tempfile.TemporaryDirectory(prefix='codex-id-shape-') as codex_store:
        project = root + '/projects/-w'
        os.makedirs(project)
        _write_claude_transcript(project + f'/{real_id}.jsonl', ['The named one'], 'hello')
        _write_claude_transcript(project + f'/{other_id}.jsonl', [], 'something else')
        now = time.time()
        _write_rollout(codex_store, real_id, '/w', now)
        _write_rollout(codex_store, other_id, '/w', now - 5)

        config.CLAUDE_PROJECTS_DIR = root + '/projects/'
        config.CODEX_SESSIONS_DIR = codex_store + '/'
        uihook.is_enabled = lambda: False
        try:
            for agent in (config.AGENT_CLAUDE, config.AGENT_CODEX):
                found = discovery.find_session(agent, real_id)
                check(f'{agent}: an id is looked up exactly',
                      (found or {}).get('session_id') == real_id,
                      str(found and found.get('session_id')))

                capitals = discovery.find_session(agent, real_id.upper())
                check(f'{agent}: the same id in capitals finds the same session',
                      (capitals or {}).get('session_id') == real_id,
                      str(capitals and capitals.get('session_id')))

                leaked = [(value, discovery.find_session(agent, value)['session_id'])
                          for value in not_ids if discovery.find_session(agent, value)]
                check(f'{agent}: a wildcard, a path, or anything else that is not an id finds '
                      'nothing', not leaked, str(leaked[:3]))

                for value in ('*', '????????-*'):
                    try:
                        target = bridge._resolve_target(agent, value, 'cwd', '/w', False, [])
                        check(f'{agent}: session_id={value!r} is refused, not resolved to '
                              'another session', False, str(target and target.get('session_id')))
                    except bridge.BridgeError:
                        check(f'{agent}: session_id={value!r} is refused, not resolved to '
                              'another session', True)

            by_id = bridge._requested_session_id(config.AGENT_CLAUDE, real_id, '/w')
            check('an exact id still resolves to itself', by_id == (real_id, real_id), str(by_id))
            by_name = bridge._requested_session_id(config.AGENT_CLAUDE, 'The named one', '/w')
            check('and a conversation name still resolves to its session',
                  by_name == (real_id, 'The named one'), str(by_name))
        finally:
            (config.CLAUDE_PROJECTS_DIR, config.CODEX_SESSIONS_DIR, uihook.is_enabled) = saved


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


def test_the_receipt_says_up_front_when_no_answer_will_arrive_here() -> None:
    """The other half of the rule: a sender that will never be written to has to be told now.

    Being quietly given no answer is the failure this replaces, not an improvement on it. The
    sender is told at send time, in the receipt it already reads, rather than finding out by
    waiting - or by never finding out.
    """
    originals = (bridge.caller.detect_caller, bridge._own_session_id, bridge._resolve_target,
                 bridge._panel_session, outbox.OUTBOX.submit, outbox.OUTBOX.await_outcome,
                 bridge.registry.touch_pin)
    bridge.caller.detect_caller = lambda: {'agent': config.AGENT_CODEX, 'chain': []}
    bridge._own_session_id = lambda agent: 'sender-sid'
    bridge._resolve_target = lambda *a, **kw: {
        'agent': config.AGENT_CLAUDE, 'session_id': 'peer-sid', 'cwd': None,
        'source': 'ide-panel', 'ui_shim': {'socket': '/s', 'pid': 1}}
    outbox.OUTBOX.submit = lambda job: job.delivery_id
    outbox.OUTBOX.await_outcome = lambda job: None
    bridge.registry.touch_pin = lambda agent, cwd: None
    try:
        bridge._panel_session = lambda agent, wanted, exclude: None
        receipt = bridge.send_message(config.AGENT_CLAUDE, 'question')
        check('a sender with no panel is told there is none to answer into',
              receipt.get('return_panel_available_now') is False,
              str(receipt.get('return_panel_available_now')))
        check('and the warning says where to read the answer instead',
              'bridge_status' in (receipt.get('warning') or ''), str(receipt.get('warning')))
        check('and says why, so it does not read as a fault to work around',
              'second agent' in (receipt.get('warning') or ''), str(receipt.get('warning')))
        check('while claiming only what a probe can prove - no panel now, not no answer ever',
              'Unless one is open when the peer answers' in (receipt.get('warning') or ''),
              str(receipt.get('warning')))
        check('and the note is conditional in the same way',
              'most likely no answer will arrive' in receipt.get('note', ''),
              receipt.get('note', '')[:180])

        # The inverse: the same send from a session that does have a panel.
        bridge._panel_session = _fake_panel('sender-sid')
        receipt = bridge.send_message(config.AGENT_CLAUDE, 'question')
        check('a sender with a panel is told there is one',
              receipt.get('return_panel_available_now') is True,
              str(receipt.get('return_panel_available_now')))
        check('with no warning about it',
              'bridge_status(delivery_id=' not in (receipt.get('warning') or ''),
              str(receipt.get('warning')))
        check('and the note that has always said so',
              'arrives later as a separate message in this session' in receipt.get('note', ''),
              receipt.get('note', '')[:160])
    finally:
        (bridge.caller.detect_caller, bridge._own_session_id, bridge._resolve_target,
         bridge._panel_session, outbox.OUTBOX.submit, outbox.OUTBOX.await_outcome,
         bridge.registry.touch_pin) = originals


def test_the_route_back_is_resolved_when_the_answer_exists_not_when_it_was_asked_for() -> None:
    """The receipt reports a probe, and a probe is a moment, not a promise.

    Minutes pass between a send and its answer. A panel that was closed at send time can be
    open by then, and one that was open can be gone. So the receipt says what is true now and
    the route is resolved again when there is something to deliver - these are the two
    transitions that would make a receipt read as a guarantee into a lie.
    """
    originals = (discovery.find_session, bridge._panel_session)
    discovery.find_session = lambda agent, session_id: None
    try:
        # absent at send time, present when the answer comes: the answer is delivered
        bridge._panel_session = lambda agent, wanted, exclude: None
        opened = _request_job('conv_opened')
        bridge._panel_session = _fake_panel('sender-sid')
        reply = bridge._build_reply_job(opened, 'the answer')
        check('a panel opened during the turn is used, whatever the receipt said',
              reply is not None and (reply.ui_shim or {}).get('pid') == 4242,
              str(reply and reply.ui_shim))
        check('and nothing is marked status-only, because the answer did go back',
              opened.is_return_status_only is False)

        # present at send time, gone when the answer comes: nothing is resumed
        bridge._panel_session = _fake_panel('sender-sid')
        closed = _request_job('conv_closed')
        closed.reply = 'the answer'
        closed.reply_length = len(closed.reply)
        bridge._panel_session = lambda agent, wanted, exclude: None
        check('a panel closed during the turn is not replaced by a resume',
              bridge._build_reply_job(closed, closed.reply) is None)
        check('and the request says the answer never went back',
              closed.is_return_status_only is True)
    finally:
        (discovery.find_session, bridge._panel_session) = originals


def test_a_request_records_where_its_answer_went() -> None:
    """A request is finalised before its answer is delivered, so it cannot report the outcome.

    It can report *where the outcome is*. Without the link a reader holding the request id has
    no way to reach the delivery that carries the answer, and the request on its own looks
    like an exchange that completed - which is exactly the case where the answer was built,
    queued, and then failed because the panel closed.
    """
    box = outbox.Outbox()
    submitted = []
    box.submit = lambda job: submitted.append(job) or job.delivery_id

    request = _request_job('conv_linked')
    request.reply = 'the answer'
    answer = outbox.Job(
        target_agent=config.AGENT_CLAUDE, target_session_id='sender-sid', payload='answer',
        run_cwd='/w', pin_cwd='/w', env={}, timeout=5, ui_shim={'pid': 4242}, title=None,
        conversation_id='conv_linked', hop=1, sender_agent=config.AGENT_CODEX,
        sender_session_id='peer-sid', wants_reply=False, summary='answer',
        kind=outbox.KIND_REPLY)
    box.build_reply = lambda job, reply: answer
    box._send_reply(request, 'the answer')

    check('the request names the delivery carrying its answer',
          request.return_delivery_id == answer.delivery_id, str(request.return_delivery_id))
    check('and the answer names the request it belongs to',
          answer.parent_delivery_id == request.delivery_id, str(answer.parent_delivery_id))
    check('both ends are on the records bridge_status reads',
          request.describe().get('return_delivery_id') == answer.delivery_id
          and answer.describe().get('parent_delivery_id') == request.delivery_id)
    check('and the link is set before the delivery is queued, since the record closes after',
          submitted == [answer]
          and submitted[0].parent_delivery_id == request.delivery_id, str(submitted))

    # a notice travels the same road and is linked the same way
    failed = _request_job('conv_linked_notice')
    failed.error = 'BridgeError: no answer'
    notice = outbox.Job(
        target_agent=config.AGENT_CLAUDE, target_session_id='sender-sid', payload='notice',
        run_cwd='/w', pin_cwd='/w', env={}, timeout=5, ui_shim={'pid': 4242}, title=None,
        conversation_id='conv_linked_notice', hop=1, sender_agent=config.AGENT_CODEX,
        sender_session_id='peer-sid', wants_reply=False, summary='notice',
        kind=outbox.KIND_NOTICE)
    box.build_notice = lambda job: notice
    box._send_notice(failed)
    check('a failure notice is linked to its request in the same way',
          failed.return_delivery_id == notice.delivery_id
          and notice.parent_delivery_id == failed.delivery_id,
          f'{failed.return_delivery_id} / {notice.parent_delivery_id}')


def test_a_request_report_carries_the_fate_of_its_answer() -> None:
    """The reader holds one id - the request's - and that record cannot answer the question.

    It is written final before the answer is delivered, so a request whose answer was built,
    queued, and then failed because the panel closed still reads as an exchange that finished.
    `bridge_status` on the request follows the link and reports what actually became of it.
    """
    request = _request_job('conv_report')
    request.reply = 'the answer'
    request.reply_length = len(request.reply)
    request.state = outbox.STATE_DELIVERED
    request.started_at = time.time() - 5
    request.finished_at = time.time()

    answer = outbox.Job(
        target_agent=config.AGENT_CLAUDE, target_session_id='sender-sid', payload='answer',
        run_cwd='/w', pin_cwd='/w', env={}, timeout=5, ui_shim=None, title=None,
        conversation_id='conv_report', hop=1, sender_agent=config.AGENT_CODEX,
        sender_session_id='peer-sid', wants_reply=False, summary='answer',
        kind=outbox.KIND_REPLY)
    answer.parent_delivery_id = request.delivery_id
    answer.state = outbox.STATE_FAILED
    answer.is_undelivered = True
    answer.error = 'NotDeliveredError: no live panel to deliver into'
    answer.started_at = time.time() - 2
    answer.finished_at = time.time()
    request.return_delivery_id = answer.delivery_id

    outbox.persist(answer)
    outbox.persist(request)

    report = bridge.delivery_report(request.delivery_id)
    check('the request is reported as before',
          report.get('ok') and report['delivery']['delivery_id'] == request.delivery_id,
          str(report)[:200])
    returned = report.get('return_delivery') or {}
    check('and the delivery carrying its answer is reported with it',
          returned.get('delivery_id') == answer.delivery_id, str(returned)[:200])
    check('so a reader holding only the request id learns the answer never landed',
          returned.get('state') == outbox.STATE_FAILED
          and returned.get('is_undelivered') is True, str(returned)[:200])

    # and when no answer was ever sent back, the report says that instead of staying silent
    quiet = _request_job('conv_report_none')
    quiet.reply = 'the answer'
    quiet.reply_length = len(quiet.reply)
    quiet.is_return_status_only = True
    quiet.state = outbox.STATE_DELIVERED
    quiet.started_at = time.time() - 5
    quiet.finished_at = time.time()
    outbox.persist(quiet)

    report = bridge.delivery_report(quiet.delivery_id)
    note = (report.get('return_delivery') or {}).get('note', '')
    check('a request whose answer was never sent back says so in the same place',
          'The answer was not sent back' in note, str(report.get('return_delivery'))[:200])
    check('and points at the preview and the transcript rather than claiming to hold it all',
          'reply_preview' in note and 'peer_transcript' in note, note[:200])

    # `is_return_status_only` also covers a request that produced NO answer, where the notice
    # was the thing not sent. There is no preview to point at, so pointing at one would be
    # pointing at nothing.
    silent = _request_job('conv_report_notice')
    silent.is_return_status_only = True
    silent.state = outbox.STATE_FAILED
    silent.error = 'BridgeError: the peer never answered'
    silent.started_at = time.time() - 5
    silent.finished_at = time.time()
    outbox.persist(silent)

    note = (bridge.delivery_report(silent.delivery_id).get('return_delivery') or {}).get('note', '')
    check('a request that produced no answer says the notice was not sent, not the answer',
          'No notice was sent back' in note, note[:200])
    check('and sends the reader to the error rather than to a preview that does not exist',
          'delivery.error' in note and 'reply_preview' not in note, note[:200])


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

    originals = (discovery.find_session, bridge._panel_session)
    discovery.find_session = lambda agent, session_id: None
    bridge._panel_session = _fake_panel('sender-sid')
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
        (discovery.find_session, bridge._panel_session) = originals


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
    """Eight threads reach for one session at once, and exactly one may hold it.

    Every racer is held at a rendezvous until all of them have tried, so the winner still owns
    the claim when the last one arrives. Holding it for a fixed time instead made the verdict a
    property of the machine rather than of the lock: a racer scheduled after the winner had
    already released acquired it legitimately, and was counted as a second winner. Sweeping
    that hold shows it plainly - 157 bad rounds in 240 with no hold at all, 54 at 2ms, none at
    the 10ms this used to use - which is why the rounds here are few and decisive instead of
    many and probabilistic.

    A rendezvous that times out, or a worker that fails some other way, is recorded apart from
    the outcomes: it says the round never became a test of exclusivity, which is not the same
    as the lock letting two claimers through.
    """
    rounds, racers = 12, 8
    bad_rounds = []

    for round_number in range(rounds):
        session_id = f'unit-race-{round_number}-' + os.urandom(3).hex()
        outcomes = []
        mishaps = []
        guard = threading.Lock()
        ready = threading.Barrier(racers)
        attempted = threading.Barrier(racers)

        def rendezvous() -> None:
            try:
                attempted.wait(timeout=30)
            except threading.BrokenBarrierError:
                with guard:
                    mishaps.append('rendezvous never completed')

        def worker() -> None:
            try:
                ready.wait(timeout=30)
                try:
                    with registry.busy_lock(config.AGENT_CODEX, session_id, 'conv_race'):
                        with guard:
                            outcomes.append('won')
                        rendezvous()
                except registry.SessionBusyError:
                    with guard:
                        outcomes.append('refused')
                    rendezvous()
            except Exception as e:
                with guard:
                    mishaps.append(f'{type(e).__name__}: {e}')

        threads = [threading.Thread(target=worker) for _ in range(racers)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)

        still_running = [t for t in threads if t.is_alive()]
        if still_running:
            bad_rounds.append((round_number, f'{len(still_running)} worker(s) never finished'))
        if mishaps:
            bad_rounds.append((round_number, f'round did not test exclusivity: {mishaps[:2]}'))
        elif outcomes.count('won') != 1 or len(outcomes) != racers:
            bad_rounds.append((round_number, list(outcomes)))
        if registry.read_busy_lock(config.AGENT_CODEX, session_id) is not None:
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


def test_a_claim_is_linked_into_place_already_complete() -> None:
    """The claim reaches `os.link`, and what it links is already the whole record.

    The racing-reader test above can only catch a torn read if the reader thread happens to be
    scheduled inside the window between the create and the write. That is a property of the
    machine, not of the code: reverting the atomic claim fails it on one host and passes ten
    runs on another. This observes the claim directly instead, so it fails on any host.
    """
    session_id = 'unit-link-' + os.urandom(4).hex()
    path = registry._lock_path(config.AGENT_CODEX, session_id)
    config.ensure_dirs()

    observed = []
    real_link = os.link

    def watched_link(src, dst, *args, **kwargs):
        with open(src, 'r', encoding='utf-8') as f:
            payload = f.read()
        observed.append({'payload': payload, 'target_existed': os.path.exists(dst)})
        return real_link(src, dst, *args, **kwargs)

    os.link = watched_link
    try:
        with registry.busy_lock(config.AGENT_CODEX, session_id, 'conv_link'):
            pass
    finally:
        os.link = real_link

    check('the claim goes through os.link at all', len(observed) == 1, str(len(observed)))
    if observed:
        try:
            record = json.loads(observed[0]['payload'])
        except ValueError as e:
            record = None
            check('what is linked parses as JSON', False, f'{type(e).__name__}: {e}')
        check('the record is already whole when it is linked',
              bool(record) and record.get('conversation_id') == 'conv_link',
              str(observed[0]['payload'])[:120])
        check('and the lock name is still free at that instant',
              observed[0]['target_existed'] is False, str(observed[0]['target_existed']))

    check('no temporary is left behind',
          not [n for n in os.listdir(config.LOCK_DIR) if n.endswith('.tmp')],
          str([n for n in os.listdir(config.LOCK_DIR) if n.endswith('.tmp')]))
    with contextlib.suppress(OSError):
        os.remove(path)


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


def test_the_transition_guard_cannot_be_held_hostage_by_another_account() -> None:
    """flock is granted on any open descriptor, whatever its access mode. A world-readable
    guard is one any account on the machine can hold exclusively and never release, which
    stops every delivery without touching anything else."""
    import stat as stat_module
    previous_umask = os.umask(0o022)
    try:
        config.ensure_dirs()
        guard = registry._guard_path()
        with contextlib.suppress(OSError):
            os.remove(guard)

        with registry.busy_lock(config.AGENT_CODEX, 'unit-guardmode-' + os.urandom(3).hex(),
                                'conv_guard'):
            pass

        mode = stat_module.S_IMODE(os.lstat(guard).st_mode)
        check('the guard is created owner-only under a permissive umask', mode == 0o600,
              oct(mode))

        # a guard left behind by a build that created it with the umask
        os.chmod(guard, 0o644)
        with registry.busy_lock(config.AGENT_CODEX, 'unit-guardrepair-' + os.urandom(3).hex(),
                                'conv_guard'):
            pass
        mode = stat_module.S_IMODE(os.lstat(guard).st_mode)
        check('and one left open by an earlier build is repaired on the next use',
              mode == 0o600, oct(mode))

        # a filesystem that will not tighten it must stop the bridge, not be worked around
        original_fchmod = os.fchmod

        def refuse_fchmod(fd, mode_):
            raise OSError(1, 'operation not permitted')

        os.fchmod = refuse_fchmod
        try:
            with registry.busy_lock(config.AGENT_CODEX, 'unit-guardfail-' + os.urandom(3).hex(),
                                    'conv_guard'):
                pass
            check('a guard that cannot be made owner-only refuses to be used', False,
                  'the lock was taken anyway')
        except OSError as e:
            check('a guard that cannot be made owner-only refuses to be used',
                  'not permitted' in str(e) or 'owner-only' in str(e), str(e))
        finally:
            os.fchmod = original_fchmod

        # and one that silently ignores the chmod is caught by reading the mode back
        def lying_fchmod(fd, mode_):
            return None

        os.chmod(guard, 0o644)
        os.fchmod = lying_fchmod
        try:
            with registry.busy_lock(config.AGENT_CODEX, 'unit-guardlie-' + os.urandom(3).hex(),
                                    'conv_guard'):
                pass
            check('a filesystem that ignores the chmod is caught by reading it back', False,
                  'the lock was taken anyway')
        except OSError as e:
            check('a filesystem that ignores the chmod is caught by reading it back',
                  'after being set to 0600' in str(e), str(e))
        finally:
            os.fchmod = original_fchmod
            with contextlib.suppress(OSError):
                os.chmod(guard, 0o600)
    finally:
        os.umask(previous_umask)
# ------------------------------------------- state on disk is readable only by its owner

STATE_PATHS = ('HOME_DIR', 'REGISTRY_PATH', 'LOCK_DIR', 'LOG_DIR', 'LOG_PATH', 'DELIVERY_DIR')


@contextlib.contextmanager
def _throwaway_state_home(umask: int = 0o022):
    """Point every state path at a temporary tree, under a deliberately permissive umask.

    0022 is the default on every distribution this runs on, so it is the umask these modes
    have to hold under: a test that inherits a strict one would pass without proving anything.
    """
    saved = {name: getattr(config, name) for name in STATE_PATHS}
    saved_repaired = config._is_repaired
    previous_umask = os.umask(umask)
    try:
        with tempfile.TemporaryDirectory(prefix='cross-agent-test-modes-') as home:
            config.HOME_DIR = home + '/'
            config.REGISTRY_PATH = config.HOME_DIR + 'registry.json'
            config.LOCK_DIR = config.HOME_DIR + 'locks/'
            config.LOG_DIR = config.HOME_DIR + 'logs/'
            config.LOG_PATH = config.LOG_DIR + 'bridge.log'
            config.DELIVERY_DIR = config.HOME_DIR + 'deliveries/'
            config._is_repaired = False
            yield config.HOME_DIR
    finally:
        os.umask(previous_umask)
        for name, value in saved.items():
            setattr(config, name, value)
        config._is_repaired = saved_repaired


def _mode(path: str) -> int:
    return stat.S_IMODE(os.lstat(path).st_mode)


def _too_open(root: str):
    """Every path under `root` that grants anything to group or other."""
    loose = []
    for current, _, files in os.walk(root):
        for path in [current] + [os.path.join(current, name) for name in files]:
            if _mode(path) & 0o077:
                loose.append(f'{path} {oct(_mode(path))}')
    return loose


def test_the_state_tree_is_owner_only() -> None:
    with _throwaway_state_home() as home:
        config.ensure_dirs()
        registry.set_pin(config.AGENT_CODEX, home, 'sid-modes', home)
        with registry.busy_lock(config.AGENT_CODEX, 'sid-modes', 'conv_modes'):
            lock_mode = _mode(registry._lock_path(config.AGENT_CODEX, 'sid-modes'))
        outbox._write_record({'delivery_id': 'req_modes_000000', 'state': 'queued'},
                             is_finished=False)
        log = panel.SecureRotatingFileHandler(config.LOG_PATH, encoding='utf-8')
        log.emit(__import__('logging').LogRecord('t', 20, __file__, 1, 'hello', None, None))
        log.close()

        check('every state directory is 0700',
              _mode(home) == 0o700 and _mode(config.LOCK_DIR) == 0o700
              and _mode(config.LOG_DIR) == 0o700 and _mode(config.DELIVERY_DIR) == 0o700,
              f'{oct(_mode(home))} {oct(_mode(config.LOCK_DIR))} '
              f'{oct(_mode(config.LOG_DIR))} {oct(_mode(config.DELIVERY_DIR))}')
        check('the registry, its lock, a delivery record and the log are 0600',
              _mode(config.REGISTRY_PATH) == 0o600 and lock_mode == 0o600
              and _mode(config.LOG_PATH) == 0o600, str(_too_open(home)))
        check('nothing under the state tree is readable by anyone else',
              not _too_open(home), str(_too_open(home)))


def test_a_panel_registration_is_owner_only() -> None:
    with _throwaway_state_home() as home:
        config.ensure_dirs()
        shim = panel.PanelShim.__new__(panel.PanelShim)
        shim.agent = config.AGENT_CLAUDE
        shim.argv = ['--resume=sid-modes']
        shim.registry_path = home + 'panels/claude-test.json'
        shim.socket_path = home + 'panels/claude-test.sock'
        panel.REGISTRY_DIR = home + 'panels/'
        try:
            shim.register()
        finally:
            panel.REGISTRY_DIR = config.HOME_DIR + 'panels/'

        check('the panel registry directory is 0700', _mode(home + 'panels') == 0o700,
              oct(_mode(home + 'panels')))
        check('a panel registration file is 0600', _mode(shim.registry_path) == 0o600,
              oct(_mode(shim.registry_path)))


def test_a_delivery_record_is_owner_only_before_it_is_renamed_into_place() -> None:
    """The temporary must never be the world-readable copy the rename then publishes."""
    seen = {}
    original_replace = outbox.os.replace

    def watched_replace(src, dst):
        seen['mode'] = _mode(src)
        return original_replace(src, dst)

    with _throwaway_state_home():
        config.ensure_dirs()
        outbox.os.replace = watched_replace
        try:
            outbox._write_record({'delivery_id': 'req_modes_111111', 'state': 'delivered'},
                                 is_finished=True)
        finally:
            outbox.os.replace = original_replace
        final = _mode(config.DELIVERY_DIR + 'req_modes_111111.json')

    check('the temporary a delivery record is written to is already 0600',
          seen.get('mode') == 0o600, oct(seen.get('mode', 0)))
    check('and the record it is renamed into place as stays 0600', final == 0o600, oct(final))


def test_a_rotated_log_generation_is_owner_only() -> None:
    """Rotation opens the next file itself, so the mode has to survive a rollover."""
    with _throwaway_state_home():
        config.ensure_dirs()
        handler = panel.SecureRotatingFileHandler(config.LOG_PATH, maxBytes=200, backupCount=1,
                                                  encoding='utf-8')
        logging = __import__('logging')
        for index in range(20):
            handler.emit(logging.LogRecord('t', 20, __file__, index, 'x' * 60, None, None))
        handler.close()

        rotated = config.LOG_PATH + '.1'
        check('the log rotated during the test', os.path.exists(rotated))
        check('both the current and the rotated log are 0600',
              _mode(config.LOG_PATH) == 0o600 and os.path.exists(rotated)
              and _mode(rotated) == 0o600,
              f'{oct(_mode(config.LOG_PATH))} '
              f'{oct(_mode(rotated)) if os.path.exists(rotated) else "missing"}')


def test_an_installation_from_before_this_is_repaired_on_startup() -> None:
    """New files are created owner-only; an existing tree has to be tightened, not left."""
    with _throwaway_state_home() as home:
        for directory in (home, home + 'locks/', home + 'logs/', home + 'deliveries/'):
            os.makedirs(directory, exist_ok=True)
            os.chmod(directory, 0o755)
        legacy = home + 'deliveries/req_legacy_000000.json'
        with open(legacy, 'w', encoding='utf-8') as f:
            f.write('{}')
        os.chmod(legacy, 0o644)

        check('the tree starts out readable by everyone', bool(_too_open(home)))
        config.ensure_dirs()
        check('startup tightened every path that was left open', not _too_open(home),
              str(_too_open(home)))
        check('and the repaired file is still readable by its owner',
              _mode(legacy) == 0o600 and open(legacy, encoding='utf-8').read() == '{}')


def test_the_repair_never_touches_the_agents_own_transcript_stores() -> None:
    """CROSS_AGENT_HOME is user-supplied; it must not become a licence to re-mode a store."""
    with tempfile.TemporaryDirectory(prefix='cross-agent-test-protected-') as store:
        victim = store + '/notes.txt'
        with open(victim, 'w', encoding='utf-8') as f:
            f.write('mine')
        os.chmod(victim, 0o644)

        saved = config._PROTECTED_TREES
        config._PROTECTED_TREES = saved | {os.path.realpath(store)}
        try:
            repaired = config.repair_state_permissions(store)
        finally:
            config._PROTECTED_TREES = saved

        check('a protected root is refused outright', repaired == 0, str(repaired))
        check('and nothing under it was re-moded', _mode(victim) == 0o644, oct(_mode(victim)))
        check('the real protected set covers home, both agent directories and both stores',
              os.path.realpath(os.path.expanduser('~')) in config._PROTECTED_EXACTLY
              and os.path.realpath(config.CLAUDE_HOME_DIR) in config._PROTECTED_TREES
              and os.path.realpath(config.CODEX_HOME_DIR) in config._PROTECTED_TREES
              and os.path.realpath(config.CLAUDE_PROJECTS_DIR) in config._PROTECTED_TREES
              and os.path.realpath(config.CODEX_SESSIONS_DIR) in config._PROTECTED_TREES)
        check('so a path inside an agent directory is refused even where no transcript lives',
              config.is_protected_path(config.CLAUDE_HOME_DIR + 'bridge')
              and config.is_protected_path(config.CODEX_HOME_DIR + 'bridge'))
        check('while the ordinary state root is not protected, or nothing would be repaired',
              not config.is_protected_path(os.path.expanduser('~/.cross-agent')))


def test_a_store_nested_under_the_state_root_is_walked_past_not_into() -> None:
    """Refusing only at the starting point is not enough: CLAUDE_CONFIG_DIR set inside
    CROSS_AGENT_HOME is all it takes for the walk to march straight into a transcript store."""
    with _throwaway_state_home() as home:
        # Inside a managed subdirectory, not merely beside one: the walk only ever enters
        # MANAGED_SUBDIRS, so a store placed elsewhere under the root is never reached and
        # would be left alone whether the walk prunes or not.
        nested = home + 'deliveries/claude-store/'
        os.makedirs(nested + 'projects/-w', exist_ok=True)
        transcript = nested + 'projects/-w/session.jsonl'
        with open(transcript, 'w', encoding='utf-8') as f:
            f.write('{}')
        for path in (nested, nested + 'projects', nested + 'projects/-w'):
            os.chmod(path, 0o755)
        os.chmod(transcript, 0o644)

        ours = home + 'deliveries/req_ours_000000.json'
        with open(ours, 'w', encoding='utf-8') as f:
            f.write('{}')
        os.chmod(ours, 0o644)

        saved = config._PROTECTED_TREES
        config._PROTECTED_TREES = saved | {os.path.realpath(nested)}
        try:
            config.repair_state_permissions(home)
        finally:
            config._PROTECTED_TREES = saved

        check('a store nested under the state root keeps its own permissions',
              _mode(transcript) == 0o644 and _mode(nested + 'projects/-w') == 0o755,
              f'{oct(_mode(transcript))} {oct(_mode(nested + "projects/-w"))}')
        check('while the bridge\'s own files beside it are still repaired',
              _mode(ours) == 0o600, oct(_mode(ours)))


def test_a_state_root_that_points_into_a_store_is_refused_before_it_is_followed() -> None:
    """chmod follows symlinks, so a state root linked into a store would re-mode the store."""
    with tempfile.TemporaryDirectory(prefix='cross-agent-test-symlink-') as outer:
        real_store = outer + '/claude-store'
        os.makedirs(real_store + '/projects', exist_ok=True)
        os.chmod(real_store, 0o755)
        os.chmod(real_store + '/projects', 0o755)
        link = outer + '/state-root'
        os.symlink(real_store, link)

        saved = config._PROTECTED_TREES
        config._PROTECTED_TREES = saved | {os.path.realpath(real_store)}
        try:
            check('a symlinked state root is seen for what it resolves to',
                  config.is_protected_path(link))
            try:
                config.secure_makedirs(link + '/locks')
                check('creating state under it is refused', False, 'no error raised')
            except ValueError as e:
                check('creating state under it is refused',
                      'own directories' in str(e) or 'home directory' in str(e), str(e))
            check('and repairing through it does nothing',
                  config.repair_state_permissions(link) == 0)
        finally:
            config._PROTECTED_TREES = saved

        check('the store it pointed at is untouched',
              _mode(real_store) == 0o755 and _mode(real_store + '/projects') == 0o755,
              f'{oct(_mode(real_store))} {oct(_mode(real_store + "/projects"))}')


def test_the_repair_only_touches_what_the_bridge_owns() -> None:
    """CROSS_AGENT_HOME can be pointed at a directory that already has things in it. Tightening
    everything found there would re-mode the user's own files for being in the wrong place."""
    with _throwaway_state_home() as home:
        config.ensure_dirs()

        theirs_dir = home + 'my-important-stuff'
        os.makedirs(theirs_dir, exist_ok=True)
        theirs_file = theirs_dir + '/notes.txt'
        with open(theirs_file, 'w', encoding='utf-8') as f:
            f.write('mine')
        loose_file = home + 'README.md'
        with open(loose_file, 'w', encoding='utf-8') as f:
            f.write('mine too')
        os.chmod(theirs_dir, 0o755)
        os.chmod(theirs_file, 0o644)
        os.chmod(loose_file, 0o644)

        ours = home + 'deliveries/req_ours_000000.json'
        with open(ours, 'w', encoding='utf-8') as f:
            f.write('{}')
        os.chmod(ours, 0o644)
        os.chmod(home + 'locks', 0o755)

        config.repair_state_permissions(home)

        check('an unrelated directory in the state root keeps its permissions',
              _mode(theirs_dir) == 0o755 and _mode(theirs_file) == 0o644,
              f'{oct(_mode(theirs_dir))} {oct(_mode(theirs_file))}')
        check('and so does an unrelated file beside the registry',
              _mode(loose_file) == 0o644, oct(_mode(loose_file)))
        check('while the directories and files the bridge owns are repaired',
              _mode(ours) == 0o600 and _mode(home + 'locks') == 0o700,
              f'{oct(_mode(ours))} {oct(_mode(home + "locks"))}')


def test_a_shim_log_written_before_this_is_tightened_when_it_is_next_opened() -> None:
    """open(2)'s mode applies only when it creates the file, so an existing log kept whatever
    it had while being appended to through a handler that looks secure."""
    with _throwaway_state_home():
        config.ensure_dirs()
        path = config.LOG_DIR + 'shim-claude.log'
        with open(path, 'w', encoding='utf-8') as f:
            f.write('written by an older build\n')
        os.chmod(path, 0o644)
        check('the log starts out readable by everyone', _mode(path) == 0o644)

        handler = panel.SecureRotatingFileHandler(path, encoding='utf-8')
        logging = __import__('logging')
        handler.emit(logging.LogRecord('t', 20, __file__, 1, 'new line', None, None))
        handler.close()

        check('opening it through the handler tightens it', _mode(path) == 0o600,
              oct(_mode(path)))
        with open(path, encoding='utf-8') as f:
            kept = f.read()
        check('and nothing that was already in it is lost',
              'written by an older build' in kept and 'new line' in kept, kept[:80])


def test_state_that_cannot_be_made_private_is_refused_rather_than_used() -> None:
    """Carrying on would mean writing sessions and messages into a directory the bridge has
    just failed to make private, while everything else assumes it succeeded."""
    with _throwaway_state_home() as home:
        target = home + 'unchmodable'
        original_chmod = os.chmod

        def refuse_chmod(path, mode, *args, **kwargs):
            if os.path.realpath(str(path)) == os.path.realpath(target):
                raise OSError(errno.EPERM, 'operation not permitted')
            return original_chmod(path, mode, *args, **kwargs)

        os.chmod = refuse_chmod
        try:
            config.secure_makedirs(target)
            check('a directory that cannot be made owner-only is refused', False,
                  'no error raised')
        except OSError as e:
            check('a directory that cannot be made owner-only is refused',
                  'owner-only' in str(e), str(e))
            check('and the error says what to do about it',
                  'CROSS_AGENT_HOME' in str(e), str(e))
        finally:
            os.chmod = original_chmod

        check('while the migration walk stays best-effort and never raises',
              config.repair_state_permissions(home) >= 0)
# --------------------------------- a spawned agent inherits a baseline, not our environment

SENTINEL = 'FAKE_VENDOR_API_TOKEN'
SENTINEL_VALUE = 'sk-live-this-must-not-reach-the-peer'


@contextlib.contextmanager
def _parent_environment(**variables):
    saved = {name: os.environ.get(name) for name in variables}
    try:
        for name, value in variables.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
        yield
    finally:
        for name, value in saved.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


def test_a_secret_in_this_process_does_not_reach_the_peer() -> None:
    """The whole point: an editor hands its MCP servers everything the desktop session has."""
    with _parent_environment(**{SENTINEL: SENTINEL_VALUE, 'AWS_SECRET_ACCESS_KEY': 'wCe/xyz',
                                'GITHUB_TOKEN': 'ghp_xyz'}):
        env = bridge._child_env('conv_env', 1, 'claude', [])

        leaked = [name for name in (SENTINEL, 'AWS_SECRET_ACCESS_KEY', 'GITHUB_TOKEN')
                  if name in env]
        check('no unnamed variable of this process reaches the child', not leaked, str(leaked))
        check('and its value appears nowhere in the child environment',
              SENTINEL_VALUE not in ' '.join(env.values()))
        named = (set(config.CHILD_ENV_BASELINE) | set(config.CHILD_ENV_BRIDGE)
                 | {config.ENV_CONVERSATION_ID, config.ENV_HOP, config.ENV_SENDER,
                    config.ENV_BUSY})
        check('every variable the child has was named somewhere, not swept up',
              set(env) <= named, str(sorted(set(env) - named)))


def test_the_baseline_a_cli_needs_is_still_passed() -> None:
    with _parent_environment(PATH='/usr/bin:/bin', HOME='/home/someone', LANG='en_GB.UTF-8',
                             TERM='xterm-256color', HTTPS_PROXY='http://proxy:3128',
                             NODE_EXTRA_CA_CERTS='/etc/ssl/corp.pem',
                             CLAUDE_CONFIG_DIR='/home/someone/.claude',
                             CODEX_HOME='/home/someone/.codex'):
        env = bridge._child_env('conv_env', 1, 'claude', [])
        wanted = {'PATH': '/usr/bin:/bin', 'HOME': '/home/someone', 'LANG': 'en_GB.UTF-8',
                  'TERM': 'xterm-256color', 'HTTPS_PROXY': 'http://proxy:3128',
                  'NODE_EXTRA_CA_CERTS': '/etc/ssl/corp.pem',
                  'CLAUDE_CONFIG_DIR': '/home/someone/.claude',
                  'CODEX_HOME': '/home/someone/.codex'}
        missing = {k: v for k, v in wanted.items() if env.get(k) != v}
        check('the CLI still gets its path, home, locale, terminal, proxy and CA bundle',
              not missing, str(missing))
        check('and the session stores the bridge itself resolves against',
              env.get('CLAUDE_CONFIG_DIR') and env.get('CODEX_HOME'))


def test_the_bridges_own_settings_are_passed_on_whole() -> None:
    """A child of this bridge runs a bridge configured like it - same state, same budgets."""
    with _parent_environment(CROSS_AGENT_HOME='/tmp/state', CROSS_AGENT_MAX_HOPS='9'):
        env = bridge._child_env('conv_env', 3, 'codex', ['claude:sid-a'])
        check('the bridge settings it names are inherited',
              env.get('CROSS_AGENT_HOME') == '/tmp/state'
              and env.get('CROSS_AGENT_MAX_HOPS') == '9', str(env.get('CROSS_AGENT_HOME')))
        check('and the chain state is set on top',
              env[config.ENV_CONVERSATION_ID] == 'conv_env' and env[config.ENV_HOP] == '3'
              and env[config.ENV_SENDER] == 'codex'
              and json.loads(env[config.ENV_BUSY]) == ['claude:sid-a'])


def test_an_extra_variable_can_be_opted_into_by_name() -> None:
    with _parent_environment(**{SENTINEL: SENTINEL_VALUE,
                                config.ENV_CHILD_PASSTHROUGH: f' {SENTINEL} , '}):
        env = bridge._child_env('conv_env', 1, 'claude', [])
        check('a variable named in the opt-in is passed through',
              env.get(SENTINEL) == SENTINEL_VALUE, str(env.get(SENTINEL)))
        check('and a blank entry in the list is ignored', '' not in env)

    with _parent_environment(**{SENTINEL: SENTINEL_VALUE,
                                config.ENV_CHILD_PASSTHROUGH: 'SOMETHING_ELSE'}):
        check('opting one variable in does not let another through',
              SENTINEL not in bridge._child_env('conv_env', 1, 'claude', []))

    with _parent_environment(**{'CROSS_AGENT_NOT_A_SETTING': 'x'}):
        check('and the bridge namespace is not a way in either',
              'CROSS_AGENT_NOT_A_SETTING' not in bridge._child_env('conv_env', 1, 'claude', []))


def test_a_cli_that_cannot_authenticate_is_told_where_the_opt_in_is() -> None:
    """A silently unauthenticated peer would look like a broken bridge."""
    with _parent_environment(ANTHROPIC_API_KEY='sk-ant-xyz'):
        hint = bridge._auth_hint(bridge._child_env('conv_env', 1, 'claude', []))
        check('the failure names the variable that was withheld',
              'ANTHROPIC_API_KEY' in hint, hint)
        check('and how to pass it', config.ENV_CHILD_PASSTHROUGH in hint, hint)
    with _parent_environment(ANTHROPIC_API_KEY=None, OPENAI_API_KEY=None,
                             ANTHROPIC_AUTH_TOKEN=None, ANTHROPIC_BASE_URL=None,
                             CLAUDE_CODE_OAUTH_TOKEN=None, OPENAI_BASE_URL=None,
                             CODEX_API_KEY=None):
        check('and nothing is said when there was nothing to withhold',
              bridge._auth_hint({}) == '')


def test_the_child_environment_is_never_written_down() -> None:
    with _parent_environment(**{SENTINEL: SENTINEL_VALUE,
                                config.ENV_CHILD_PASSTHROUGH: SENTINEL}):
        job = outbox.Job(
            target_agent='codex', target_session_id='sid-env', payload='hello',
            run_cwd='/w', pin_cwd='/w', env=bridge._child_env('conv_env', 1, 'claude', []),
            timeout=600, ui_shim=None, title=None, conversation_id='conv_env', hop=1,
            sender_agent='claude', sender_session_id='sid-me', wants_reply=True,
            summary='hello')
        check('the delivery record carries no environment at all',
              not any('env' in key for key in job.describe()), str(sorted(job.describe())))

        with tempfile.TemporaryDirectory(prefix='cross-agent-test-env-') as store:
            original = outbox.config.DELIVERY_DIR
            outbox.config.DELIVERY_DIR = store + '/'
            try:
                outbox.persist(job)
                written = ''
                for current, _, files in os.walk(store):
                    for name in files:
                        with open(os.path.join(current, name), encoding='utf-8') as f:
                            written += f.read()
            finally:
                outbox.config.DELIVERY_DIR = original
        check('and the record on disk does not contain the opted-in secret either',
              SENTINEL_VALUE not in written and SENTINEL not in written)


PROXY_WITH_CREDENTIALS = (
    'https://alice:super-secret@proxy.corp:3128',
    'http://token:abc123@proxy.corp:3128',
    'user:pass@proxy.corp:8080',                       # no scheme at all
    '//alice:secret@proxy.corp:3128',                  # scheme-relative
    'http://user%40corp:p%40ssw0rd@proxy.corp:3128',   # percent-encoded userinfo
    'HTTP://ALICE:SECRET@PROXY.CORP:3128',
    'http://token@proxy.corp:3128',                    # username, no password
    'http://proxy.corp:3128,https://bob:hunter2@other.corp:3128',
)

PROXY_WITHOUT_CREDENTIALS = (
    'http://proxy.corp:3128',
    'https://proxy.corp:3128',
    'proxy.corp:3128',
    '//proxy.corp:3128',
    'http://[::1]:3128',
    'localhost,127.0.0.1,.corp.example',
    'http://proxy.corp:3128?notify=a@b',               # an @ in the query is not userinfo
    'http://proxy.corp:3128/pac@file',                 # nor is one in the path
)


def test_a_proxy_url_carrying_a_password_is_withheld() -> None:
    """A proxy variable is on the baseline because a CLI behind one needs it - and a proxy URL
    is also a perfectly ordinary place to keep a username and password."""
    caught = [v for v in PROXY_WITH_CREDENTIALS if not config.has_embedded_credentials(v)]
    check('every shape of embedded credential is recognised', not caught, str(caught))
    missed = [v for v in PROXY_WITHOUT_CREDENTIALS if config.has_embedded_credentials(v)]
    check('and an ordinary proxy setting is not mistaken for one', not missed, str(missed))
    check('a value too malformed to parse is treated as one rather than waved through',
          config.has_embedded_credentials('http://[::1'))

    with _parent_environment(HTTPS_PROXY='https://alice:super-secret@proxy.corp:3128',
                             HTTP_PROXY='http://proxy.corp:3128',
                             all_proxy='user:pass@proxy.corp:8080',
                             no_proxy='localhost,.corp.example'):
        env = bridge._child_env('conv_proxy', 1, 'claude', [])
        check('the proxy that carries a password is not passed to the child',
              'HTTPS_PROXY' not in env and 'all_proxy' not in env, str(sorted(env)))
        check('and its password appears nowhere in the child environment',
              'super-secret' not in ' '.join(env.values()))
        check('while a credential-free proxy is still passed automatically',
              env.get('HTTP_PROXY') == 'http://proxy.corp:3128'
              and env.get('no_proxy') == 'localhost,.corp.example')

        hint = bridge._auth_hint(env)
        check('the failure hint names the withheld proxy variables',
              'HTTPS_PROXY' in hint and 'all_proxy' in hint, hint)
        check('says why, and does not print the credential',
              'username and password' in hint and 'super-secret' not in hint, hint)
        check('and names the opt-in that would pass them',
              f'{config.ENV_CHILD_PASSTHROUGH}=HTTPS_PROXY,all_proxy' in hint, hint)


def test_a_proxy_password_is_never_stripped_out_and_forwarded() -> None:
    """Half a proxy URL is worse than none: it fails at the proxy, not at the bridge."""
    with _parent_environment(HTTPS_PROXY='https://alice:super-secret@proxy.corp:3128'):
        env = bridge._child_env('conv_proxy', 1, 'claude', [])
        check('no rewritten, credential-free copy is passed instead',
              not any('proxy.corp' in value for value in env.values()), str(env))


def test_a_credential_bearing_proxy_can_still_be_opted_into() -> None:
    with _parent_environment(HTTPS_PROXY='https://alice:super-secret@proxy.corp:3128',
                             **{config.ENV_CHILD_PASSTHROUGH: 'HTTPS_PROXY'}):
        env = bridge._child_env('conv_proxy', 1, 'claude', [])
        check('naming it passes it whole',
              env.get('HTTPS_PROXY') == 'https://alice:super-secret@proxy.corp:3128')
        check('and it is no longer reported as withheld',
              'HTTPS_PROXY' not in bridge._auth_hint(env))

    with _parent_environment(HTTPS_PROXY='https://alice:super-secret@proxy.corp:3128',
                             **{config.ENV_CHILD_PASSTHROUGH: 'http_proxy'}):
        check('naming a different proxy variable does not pass this one',
              'HTTPS_PROXY' not in bridge._child_env('conv_proxy', 1, 'claude', []))


def test_a_claude_error_result_carries_the_hint_too() -> None:
    """An authentication failure usually arrives as is_error, not as a missing result."""
    original = bridge._run_cli
    result = json.dumps({'type': 'result', 'is_error': True,
                         'result': 'Invalid API key - please run /login'})
    bridge._run_cli = lambda command, cwd, env, timeout: __import__('subprocess').CompletedProcess(
        command, 1, result + '\n', '')
    try:
        with _parent_environment(ANTHROPIC_API_KEY='sk-ant-xyz'):
            env = bridge._child_env('conv_err', 1, 'codex', [])
            try:
                bridge._call_claude('hello', 'sid-err', '/w', env, 600)
                check('a claude error result is raised', False, 'no error raised')
            except bridge.BridgeError as e:
                message = str(e)
                check('a claude error result is raised', 'Invalid API key' in message, message)
                check('and it says which auth variable was withheld',
                      'ANTHROPIC_API_KEY' in message
                      and config.ENV_CHILD_PASSTHROUGH in message, message)
                check('without printing its value', 'sk-ant-xyz' not in message, message)
    finally:
        bridge._run_cli = original
# ------------------------------------ new_session=true means a new conversation, or nothing

def _claude_panel_shim(session_id):
    """A Claude stream shim driving `session_id`, or none at all when it is None."""
    from cross_agent_mcp.claude_shim import ClaudeStreamShim
    shim = ClaudeStreamShim.__new__(ClaudeStreamShim)
    shim.agent = config.AGENT_CLAUDE
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
    shim.argv = ['--resume=' + (session_id or 'none')]
    shim.real_binary = '/usr/bin/claude'
    shim.log = _quiet_log()
    shim.writes = []
    shim.write_to_child = lambda payload: shim.writes.append(json.loads(payload))
    return shim


def test_a_busy_claude_panel_refuses_to_host_a_new_conversation() -> None:
    """The reported bug: a null session id skipped the guard and the message went to whatever
    conversation that panel was already driving."""
    shim = _claude_panel_shim('9a2c4cd1-0000-4000-8000-00000000cafe')

    refusal = shim.inject('fresh reviewer please', None, timeout=600, accept_timeout=2,
                          create_new=True)
    check('a fresh conversation is refused by a panel that already has one',
          refusal.get('ok') is False and refusal.get('accepted') is False, str(refusal)[:200])
    check('the refusal names the session it would otherwise have interrupted',
          '9a2c4cd1-0000-4000-8000-00000000cafe' in str(refusal.get('error')),
          str(refusal.get('error')))
    check('and nothing at all was written into that conversation', shim.writes == [],
          str(shim.writes))
    check('while the session stays free for its own user',
          shim.is_turn_active is False and shim.injection is None)

    check('a panel driving a session does not offer to host a new one',
          shim.status().get('can_create_session') is False)


def test_an_idle_claude_panel_still_opens_one() -> None:
    shim = _claude_panel_shim(None)
    check('a panel with no conversation offers to host one',
          shim.status().get('can_create_session') is True)

    receipt = shim.inject('fresh reviewer please', None, timeout=600, accept_timeout=2,
                          create_new=True)
    check('and accepts a fresh conversation',
          receipt.get('accepted') is True and receipt.get('wasCreated') is True,
          str(receipt)[:200])
    check('writing the message that opens it', len(shim.writes) == 1, str(shim.writes))


def test_a_panel_that_cannot_create_is_not_chosen_to_create() -> None:
    from cross_agent_mcp import uihook
    busy_shim = {'agent': 'claude', 'pid': 101, 'socket': '/tmp/busy.sock', 'started_at': 200.0}
    free_shim = {'agent': 'claude', 'pid': 102, 'socket': '/tmp/free.sock', 'started_at': 100.0}
    statuses = {
        '/tmp/busy.sock': {'ok': True, 'can_create_session': False,
                           'sessions': [{'session_id': 'sid-busy'}]},
        '/tmp/free.sock': {'ok': True, 'can_create_session': True, 'sessions': []},
        # a shim from before this field existed cannot answer the question
        '/tmp/old.sock': {'ok': True, 'sessions': [{'session_id': 'sid-old'}]},
    }
    old_shim = {'agent': 'claude', 'pid': 103, 'socket': '/tmp/old.sock', 'started_at': 300.0}

    originals = (uihook.find_local_shims, uihook.read_status)
    uihook.read_status = lambda shim: statuses[shim['socket']]
    try:
        uihook.find_local_shims = lambda agent: [busy_shim, free_shim]
        host = uihook.find_panel_host('claude')
        check('the newest panel is skipped when it cannot open a conversation',
              (host or {}).get('shim', {}).get('pid') == 102, str(host))

        uihook.find_local_shims = lambda agent: [busy_shim]
        check('and when none can, no panel host is offered at all',
              uihook.find_panel_host('claude') is None)

        uihook.find_local_shims = lambda agent: [old_shim]
        check('a shim too old to answer is not chosen either',
              uihook.find_panel_host('claude') is None)
    finally:
        (uihook.find_local_shims, uihook.read_status) = originals


def test_a_forced_new_session_never_resolves_to_an_existing_one() -> None:
    from cross_agent_mcp import uihook
    existing = {'agent': 'claude', 'session_id': 'sid-in-use', 'cwd': '/w',
                'shim': {'pid': 1, 'socket': '/tmp/x.sock'}, 'is_active': True, 'mtime': 1.0}
    originals = (uihook.is_enabled, uihook.find_live_session, uihook.find_panel_host,
                 discovery.find_active_session)
    uihook.is_enabled = lambda: True
    uihook.find_live_session = lambda agent, session_id=None: dict(existing)
    discovery.find_active_session = lambda *a, **kw: dict(existing)
    try:
        uihook.find_panel_host = lambda agent: None
        target = bridge._resolve_target('claude', None, 'cwd', '/w', True, [])
        check('with no panel able to create one, no session is resolved at all',
              target is None, str(target))

        uihook.find_panel_host = lambda agent: {'session_id': None, 'cwd': None,
                                                'shim': {'pid': 9}, 'opens_new_session': True}
        target = bridge._resolve_target('claude', None, 'cwd', '/w', True, [])
        check('and a panel that can create one is targeted with no session id',
              (target or {}).get('session_id') is None
              and (target or {}).get('source') == 'ide-panel-new', str(target))
    finally:
        (uihook.is_enabled, uihook.find_live_session, uihook.find_panel_host,
         discovery.find_active_session) = originals


def test_a_reused_session_is_caught_even_if_a_shim_claims_otherwise() -> None:
    """Belt and braces: the shim refuses what it cannot do, this catches it saying it did."""
    before = {'sid-already-running', 'sid-other'}

    def outcome(response):
        try:
            bridge._raise_unless_really_new(response, before)
            return 'accepted'
        except bridge.BridgeError as e:
            return str(e)

    check('a session that existed before the request is not a new one',
          'was already running' in outcome({'wasCreated': True,
                                            'sessionId': 'sid-already-running'}))
    check('nor is one the shim did not report as created',
          'did not report as new' in outcome({'wasCreated': False, 'sessionId': 'sid-fresh'}))
    check('nor is a delivery that named no session at all',
          'an unnamed session' in outcome({'wasCreated': True, 'sessionId': None}))
    check('a genuinely new session passes',
          outcome({'wasCreated': True, 'sessionId': 'sid-fresh'}) == 'accepted')
    check('and the refusal says nothing further was sent',
          'Nothing further was sent' in outcome({'wasCreated': False, 'sessionId': 'x'}))


def test_the_codex_shim_opens_a_thread_rather_than_reusing_one() -> None:
    from cross_agent_mcp.appserver_shim import CodexAppServerShim
    shim = CodexAppServerShim.__new__(CodexAppServerShim)
    shim.agent = config.AGENT_CODEX
    shim.threads = {'thread-existing': {'thread_id': 'thread-existing', 'last_seen': 10.0}}
    shim.state_lock = threading.Lock()
    check('an existing thread is what it would normally pick',
          shim._pick_thread(None) == 'thread-existing')

    asked = []
    shim._open_thread = lambda cwd, timeout: asked.append(('open', cwd)) or _ThreadOpened('new-1')
    shim._note_thread = lambda *a, **kw: None
    shim._name_thread = lambda *a, **kw: None
    shim._is_loaded = lambda thread_id: True
    shim._start_turn = lambda thread_id, text, is_created, timeout, accept: asked.append(
        ('turn', thread_id, is_created)) or _AcceptedTurn(thread_id, is_created)
    shim.settle = lambda turn: {'ok': True, 'accepted': True, 'sessionId': turn.session_id,
                                'wasCreated': turn.is_created}

    result = shim.inject('hello', None, timeout=60, create_new=True)
    check('asked for a fresh thread, it opens one instead of picking that thread',
          ('open', None) in asked and ('turn', 'new-1', True) in asked, str(asked))
    check('and reports the new thread as created',
          result.get('sessionId') == 'new-1' and result.get('wasCreated') is True, str(result))

    check('asking for a new thread and naming one at once is refused',
          shim.inject('hello', 'thread-existing', timeout=60,
                      create_new=True).get('accepted') is False)


class _ThreadOpened:
    def __init__(self, thread_id):
        self.thread_id = thread_id
        self.error = None


class _AcceptedTurn:
    def __init__(self, session_id, is_created):
        self.session_id = session_id
        self.is_created = is_created
        self.error = None


def run_all() -> None:
    test_the_suite_writes_nowhere_near_the_real_bridge()
    test_busy_lock_is_exclusive()
    test_busy_lock_release_respects_owner()
    test_prune_spares_sticky_pins()
    test_prune_keeps_fresh_auto_pins()
    test_cwd_relations()
    test_codex_scan_filters_before_limit()
    test_timeout_kills_descendants()
    test_subagent_threads_are_rejected()
    test_new_session_is_the_last_resort()
    test_the_receipt_says_who_chose_the_conversation()
    test_an_unaddressed_relay_says_it_was_aimed_by_the_human()
    test_the_resolver_labels_a_pin_a_disk_find_and_a_fresh_start()
    test_a_bridge_started_turn_is_told_its_delivery_ends_with_it()
    test_the_receipt_says_how_long_the_target_has_been_idle()
    test_a_bridge_started_turn_knows_which_session_it_is()
    test_a_delivery_stopped_with_its_carrier_is_closed_on_the_record()
    test_a_worker_does_not_overwrite_why_a_delivery_was_stopped()
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
    test_an_answer_with_no_panel_to_land_in_is_kept_rather_than_resumed()
    test_a_failure_notice_with_no_panel_is_kept_rather_than_resumed()
    test_a_retry_that_finds_no_panel_does_not_fall_back_to_a_resume()
    test_a_send_to_a_dormant_session_still_resumes_it()
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
    test_a_rename_after_the_head_window_is_still_the_conversations_name()
    test_the_current_name_is_found_however_the_transcript_ends()
    test_a_name_that_straddles_a_chunk_boundary_is_still_whole()
    test_a_record_cut_off_by_the_search_bound_is_not_half_read()
    test_a_name_further_back_than_the_search_bound_is_not_found()
    test_a_store_entry_answers_only_for_the_session_it_actually_holds()
    test_a_session_id_cannot_name_a_lock_outside_the_lock_directory()
    test_a_session_named_in_capitals_still_reaches_its_own_panel()
    test_a_session_name_matches_exactly_or_not_at_all()
    test_a_name_two_conversations_answer_to_is_refused_rather_than_guessed()
    test_the_candidates_say_whether_each_name_was_assigned_or_generated()
    test_a_name_found_only_inside_the_search_window_is_not_called_unique()
    test_a_codex_name_is_read_from_the_store_and_the_scan_bound_is_respected()
    test_an_ambiguous_name_stops_the_send_rather_than_reaching_a_conversation()
    test_a_session_id_is_recognised_by_its_shape_and_never_used_as_a_pattern()
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
    test_the_receipt_says_up_front_when_no_answer_will_arrive_here()
    test_the_route_back_is_resolved_when_the_answer_exists_not_when_it_was_asked_for()
    test_a_request_records_where_its_answer_went()
    test_a_request_report_carries_the_fate_of_its_answer()
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
    test_a_claim_is_linked_into_place_already_complete()
    test_a_stale_clear_cannot_delete_the_lock_that_replaced_it()
    test_the_no_hard_link_fallback_is_still_exclusive_under_contention()
    test_the_transition_guard_cannot_be_held_hostage_by_another_account()
    test_the_state_tree_is_owner_only()
    test_a_panel_registration_is_owner_only()
    test_a_delivery_record_is_owner_only_before_it_is_renamed_into_place()
    test_a_rotated_log_generation_is_owner_only()
    test_an_installation_from_before_this_is_repaired_on_startup()
    test_the_repair_never_touches_the_agents_own_transcript_stores()
    test_a_store_nested_under_the_state_root_is_walked_past_not_into()
    test_a_state_root_that_points_into_a_store_is_refused_before_it_is_followed()
    test_the_repair_only_touches_what_the_bridge_owns()
    test_a_shim_log_written_before_this_is_tightened_when_it_is_next_opened()
    test_state_that_cannot_be_made_private_is_refused_rather_than_used()
    test_a_secret_in_this_process_does_not_reach_the_peer()
    test_the_baseline_a_cli_needs_is_still_passed()
    test_the_bridges_own_settings_are_passed_on_whole()
    test_an_extra_variable_can_be_opted_into_by_name()
    test_a_cli_that_cannot_authenticate_is_told_where_the_opt_in_is()
    test_the_child_environment_is_never_written_down()
    test_a_proxy_url_carrying_a_password_is_withheld()
    test_a_proxy_password_is_never_stripped_out_and_forwarded()
    test_a_credential_bearing_proxy_can_still_be_opted_into()
    test_a_claude_error_result_carries_the_hint_too()
    test_a_busy_claude_panel_refuses_to_host_a_new_conversation()
    test_an_idle_claude_panel_still_opens_one()
    test_a_panel_that_cannot_create_is_not_chosen_to_create()
    test_a_forced_new_session_never_resolves_to_an_existing_one()
    test_a_reused_session_is_caught_even_if_a_shim_claims_otherwise()
    test_the_codex_shim_opens_a_thread_rather_than_reusing_one()

if __name__ == '__main__':
    # The delivery directory used to be redirected here on its own, because finished jobs left
    # rows like "sid-normal" among the genuine ones in the real store. The whole state root is
    # temporary now, so that redirection is gone: deliveries, the registry, the locks and the
    # logs are all already inside STATE_ROOT, and a test that starts writing something new is
    # covered without anyone remembering to add it.
    try:
        run_all()
    finally:
        shutil.rmtree(STATE_ROOT, ignore_errors=True)

    print(f'\n{"ALL UNIT CHECKS PASSED" if not FAILURES else str(len(FAILURES)) + " CHECK(S) FAILED"}')
    sys.exit(1 if FAILURES else 0)
