"""Relay a message into the peer agent's live session.

  Claude Code : claude -p --resume <session-id> --output-format json "<message>"
  Codex       : codex exec resume <thread-id> --json "<message>"

Both commands re-enter an existing transcript, so the peer answers with its whole
conversation context intact instead of starting from a blank agent.

Handing the message over is not the same as waiting for the answer. `send_message` only
queues the delivery and returns; `outbox` runs it, and the peer's answer comes back as a
message into the sender's session rather than as this function's return value. Nothing is
locked open for the length of a peer turn, so a turn may take as long as it needs.
"""

import atexit
import contextlib
import json
import logging
import os
import signal
import subprocess
import threading
import time
import uuid
from typing import Any, Dict, List, Optional, Tuple

from . import caller, config, discovery, outbox, registry, uihook


logger = logging.getLogger('cross_agent_mcp.bridge')

# how long a killed process group gets to die before it is SIGKILLed
KILL_GRACE_SECONDS = 5

# how much of the relayed message is used to title a conversation the bridge opened
PANEL_TITLE_LIMIT = 50

# Shim answers that mean "not now" rather than "no". Matched as substrings because the shim
# builds them as prose; a phrase that stops appearing costs a retry, never a wrong delivery.
PEER_BUSY_SIGNALS = (
    'busy with another turn',
    'already in flight',
)

# Shim answers from before the shim said `accepted` explicitly, that meant the message never
# left. A current shim states it; these keep a panel process that has not been restarted since
# from turning a plain refusal into fifteen minutes of watching for an answer.
NEVER_LANDED_SIGNALS = (
    'thread not found',
    'is not open in this panel',
    'drives session',
    'failed to write to',
    'could not open a new thread',
    'failed to open a panel thread',
    'panel shim unreachable',
)

# How long a panel hand-over may take to be acknowledged. Acceptance itself is a matter of
# milliseconds; the allowance is for opening a fresh thread first, which the app server is
# given THREAD_OPEN_TIMEOUT_SECONDS (30s) for.
PANEL_ACCEPT_SECONDS = 45

# One `await` call to the shim covers this much of the peer's turn; the bridge then asks again
# until the turn ends or patience runs out. Shorter than a turn on purpose: a socket that dies
# mid-turn is noticed within this, not at the end of the whole budget - and between two calls
# the peer's transcript is read, so a turn the shim failed to see end is noticed within this
# too, instead of at the end of the whole budget. That happened: a peer answered eight seconds
# after taking the message, the shim never reported the turn over, and the delivery - and the
# lock, and five messages queued behind it - sat for the full hour.
PANEL_AWAIT_CHUNK_SECONDS = 30

AGENT_LABEL: Dict[str, str] = {
    config.AGENT_CLAUDE: 'Claude Code',
    config.AGENT_CODEX: 'Codex',
}
PEER_TOOL: Dict[str, str] = {
    config.AGENT_CLAUDE: 'send_to_codex',
    config.AGENT_CODEX: 'send_to_claude',
}


class BridgeError(Exception):
    """Raised for every condition the calling agent should see as a tool failure."""


# ------------------------------------------------------------- chain context

def _busy_from_env() -> List[str]:
    raw = os.environ.get(config.ENV_BUSY)
    if not raw:
        return []
    try:
        value = json.loads(raw)
        return [str(v) for v in value] if isinstance(value, list) else []
    except Exception:
        return []


def _resolve_conversation_id(conversation_id: Optional[str]) -> Tuple[str, bool]:
    if conversation_id:
        return conversation_id, False
    inherited = os.environ.get(config.ENV_CONVERSATION_ID)
    if inherited:
        return inherited, False
    return 'conv_' + uuid.uuid4().hex[:12], True


def _address_line(sender: str, reply_to: Optional[str]) -> str:
    """The sender's own address, written on the envelope the way an email carries From.

    The bridge already routes the answer on its own, so this is not what makes a reply work.
    It matters when the automatic path cannot: it lets the peer send a NEW request straight
    back to the exact session that wrote to it, instead of re-deriving the address from
    whatever happens to look active on this side.
    """
    if not reply_to:
        return ('reply-to: (unknown - this sender has no session of its own, so an automatic '
                'answer cannot be delivered back)\n')
    return f'reply-to: {sender} session {reply_to}\n'


def _build_envelope(sender: str, target: str, conversation_id: str, hop: int, remaining: int,
                    message: str, reply_to: Optional[str] = None,
                    request_id: Optional[str] = None) -> str:
    sender_label = AGENT_LABEL.get(sender, sender)
    reply_tool = PEER_TOOL.get(target, 'the cross-agent tool')

    if remaining > 0 and reply_to:
        follow_up = (f'- For a NEW request back to {sender_label}, call `{reply_tool}` with '
                     f'session_id="{reply_to}" ({remaining} bridge hop(s) left). Otherwise '
                     'just answer.')
    elif remaining > 0:
        follow_up = (f'- If you need to send a NEW request back to {sender_label}, call the '
                     f'`{reply_tool}` tool ({remaining} bridge hop(s) left). Otherwise just answer.')
    else:
        follow_up = ('- The hop budget for this conversation is exhausted. Do NOT call any '
                     'cross-agent tool; answer directly.')

    return (
        '=== CROSS-AGENT BRIDGE MESSAGE ===\n'
        f'from: {sender_label} (peer AI agent, not the human user)\n'
        f'{_address_line(sender, reply_to)}'
        f'conversation: {conversation_id} | hop {hop}/{config.MAX_HOPS}\n'
        + (f'request: {request_id}\n' if request_id else '')
        + '\n'
        f'{message}\n'
        '\n'
        '=== HOW TO REPLY ===\n'
        f'- Your final assistant message is relayed back to {sender_label} as a message in its\n'
        '  own session. Take the time the task needs; nothing is parked waiting on you.\n'
        + (f'- End your final message with the request id on its own line: {request_id}\n'
           '  If the relay drops, that line is how your answer is matched to this request\n'
           '  rather than mistaken for a reply to something else.\n' if request_id else '')
        +
        '- Answer the peer directly; do not wait for the human and do not ask for confirmation.\n'
        '- Keep the answer self-contained: the peer sees only your final message.\n'
        f'{follow_up}\n'
    )


def _build_reply_envelope(sender: str, target: str, conversation_id: str, hop: int,
                          remaining: int, reply: str, reply_to: Optional[str] = None,
                          is_recovered: bool = False,
                          failure_reason: Optional[str] = None) -> str:
    """Wrap a peer's answer so the original sender reads it as an answer, not a new request.

    A recovered answer says so, and says why the transport failed. Recovery reads the peer's
    last message at the moment the transport gave up, and a peer that is still working has a
    last message too - a line about what it is doing next. Delivered unmarked, that reads
    exactly like a finished answer, and the reader acts on a report that was never made. And
    delivered without the reason, the reader cannot tell a panel that refused the message from
    a socket that merely ran out of patience, which are different things to do next.
    """
    sender_label = AGENT_LABEL.get(sender, sender)
    reply_tool = PEER_TOOL.get(target, 'the cross-agent tool')

    if remaining > 0 and reply_to:
        follow_up = (f'- Only if you have a NEW request, call `{reply_tool}` with '
                     f'session_id="{reply_to}" ({remaining} bridge hop(s) left).')
    elif remaining > 0:
        follow_up = (f'- Only if you have a NEW request, call `{reply_tool}` '
                     f'({remaining} bridge hop(s) left).')
    else:
        follow_up = ('- The hop budget for this conversation is exhausted. Do NOT call any '
                     'cross-agent tool.')

    if is_recovered:
        provenance = (
            f'- RECOVERED, NOT RECEIVED. Delivery from {sender_label} failed, so this was read\n'
            f'  out of its transcript: it is whatever {sender_label} had last said at that\n'
            '  moment, which may be a note about what it was still doing rather than its\n'
            '  answer. Treat it as finished only if it reads like a finished answer, and check\n'
            f'  with {sender_label} before acting on it as a report.\n'
            f'- Why the transport failed: {failure_reason or "not recorded"}\n')
    else:
        provenance = '- This is the answer to a message you relayed earlier.\n'

    return (
        '=== CROSS-AGENT BRIDGE REPLY ===\n'
        f'from: {sender_label} (peer AI agent, not the human user)\n'
        f'{_address_line(sender, reply_to)}'
        f'conversation: {conversation_id} | answering hop {hop}/{config.MAX_HOPS}'
        f'{" | recovered from transcript" if is_recovered else ""}\n'
        + (f'transport failure: {failure_reason}\n' if is_recovered and failure_reason else '')
        + '\n'
        f'{reply}\n'
        '\n'
        '=== NOTE ===\n'
        f'{provenance}'
        '- Nothing is waiting on you.\n'
        f'{follow_up}\n'
    )


def _build_notice_envelope(job: outbox.Job, remaining: int) -> str:
    """Tell the sender that a request of theirs produced nothing.

    Two different failures, told apart by the first line. A message that never landed is safe
    to send again; the peer has no idea it existed. A message that landed and went unanswered
    is a peer still working, or a peer that stopped - and resending it would set the same work
    going twice, so the reader is pointed at the transcript instead.
    """
    target_label = AGENT_LABEL.get(job.target_agent, job.target_agent)
    reply_tool = PEER_TOOL.get(job.sender_agent, 'the cross-agent tool')
    session = job.resolved_session_id or job.target_session_id or '(none)'

    if job.is_undelivered:
        headline = f'Your message was NOT delivered to {target_label} (session {session}).'
        standing = f'- {target_label} never saw it. Nothing is waiting on you.\n'
        if 'thread not found' in (job.error or ''):
            advice = (f'- The Codex panel had unloaded that thread and the bridge could not load '
                      'it back. Ask the human to open that conversation in the Codex panel, '
                      'then send again.\n')
        else:
            advice = ('- If the request still matters, send it again; check `bridge_status` '
                      f'(delivery_id="{job.delivery_id}") first if the reason is unclear.\n')
    else:
        headline = (f'Your message reached {target_label} (session {session}), but no answer '
                    f'came back in {round((job.finished_at or time.time()) - (job.started_at or time.time()))}s.')
        standing = (f'- {target_label} may still be working on it, or may have stopped without '
                    'answering. Do NOT resend blindly: that starts the same work twice.\n')
        advice = (f'- Call `bridge_status` with delivery_id="{job.delivery_id}" to read the '
                  f'{target_label} transcript: peer_transcript.answer is filled in once its '
                  'turn ends, and is_working says whether it is still going.\n')

    if remaining > 0:
        budget = (f'- Sending again costs a hop ({remaining} left); use `{reply_tool}` with '
                  f'session_id="{session}" to reach the same conversation.\n')
    else:
        budget = ('- The hop budget for this conversation is exhausted; a new request would '
                  'need a new conversation.\n')

    return (
        '=== CROSS-AGENT BRIDGE DELIVERY FAILED ===\n'
        'from: the bridge itself (not the peer agent, not the human user)\n'
        f'conversation: {job.conversation_id} | hop {job.hop}/{config.MAX_HOPS}\n'
        f'request: {job.delivery_id}\n'
        '\n'
        f'{headline}\n'
        f'reason: {job.error or "unknown"}\n'
        f'message: {job.summary}\n'
        '\n'
        '=== NOTE ===\n'
        f'{standing}'
        f'{advice}'
        f'{budget}'
    )


def _summary(text: str) -> str:
    """A one-line trace of a message, for delivery listings."""
    first_line = next((line.strip() for line in text.splitlines()
                       if line.strip() and not line.startswith('===')), '')
    return ' '.join(first_line.split())[:PANEL_TITLE_LIMIT * 2]


def _child_env(conversation_id: str, hop: int, sender: str, busy: List[str]) -> Dict[str, str]:
    env = dict(os.environ)
    env[config.ENV_CONVERSATION_ID] = conversation_id
    env[config.ENV_HOP] = str(hop)
    env[config.ENV_SENDER] = sender
    env[config.ENV_BUSY] = json.dumps(busy)
    return env


# ----------------------------------------------------------------- CLI calls

def _terminate_group(process: subprocess.Popen) -> None:
    """Kill the CLI *and* the tool-call subprocesses it spawned.

    The agent CLIs run builds, test suites and shell commands as their own children. Killing
    only the direct child would leave those running unsupervised after a timeout.
    """
    try:
        group_id = os.getpgid(process.pid)
    except OSError:
        process.kill()
        return

    with contextlib.suppress(OSError):
        os.killpg(group_id, signal.SIGTERM)
    try:
        process.wait(timeout=KILL_GRACE_SECONDS)
    except subprocess.TimeoutExpired:
        with contextlib.suppress(OSError):
            os.killpg(group_id, signal.SIGKILL)


# Deliveries this process started and has not finished. A CLI runs in its own process group
# so a timeout can take down the whole tool tree with it - which also means it survives us.
_LIVE_CHILDREN: List[subprocess.Popen] = []
_CHILDREN_GUARD = threading.Lock()


def _track_child(process: subprocess.Popen) -> None:
    with _CHILDREN_GUARD:
        _LIVE_CHILDREN.append(process)


def _untrack_child(process: subprocess.Popen) -> None:
    with _CHILDREN_GUARD:
        with contextlib.suppress(ValueError):
            _LIVE_CHILDREN.remove(process)


def terminate_live_children() -> None:
    """Take our deliveries down with us.

    An orphaned delivery is not merely wasted work: it keeps writing to the peer's session
    and its repository with nobody watching, and the busy lock stops protecting that session
    the moment this process dies, because staleness is judged by our pid. A re-request then
    starts a second agent on the same files. That happened - two `claude -p` resumes ran
    concurrently on one session after the window that started the first was reloaded.
    """
    with _CHILDREN_GUARD:
        children = list(_LIVE_CHILDREN)
        _LIVE_CHILDREN.clear()

    for process in children:
        if process.poll() is not None:
            continue
        logger.info(f'terminate_live_children [killing]: pid={process.pid}')
        with contextlib.suppress(Exception):
            _terminate_group(process)


def install_shutdown_guard() -> None:
    """Arrange for in-flight deliveries to die with this server, however it exits."""
    atexit.register(terminate_live_children)

    for number in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        try:
            previous = signal.getsignal(number)
        except (OSError, ValueError):
            continue

        def handler(signum, frame, _previous=previous):
            terminate_live_children()
            if callable(_previous):
                _previous(signum, frame)
            else:
                raise SystemExit(128 + signum)

        with contextlib.suppress(OSError, ValueError):
            signal.signal(number, handler)


def _run_cli(command: List[str], cwd: str, env: Dict[str, str], timeout: int) -> subprocess.CompletedProcess:
    logger.debug(f'_run_cli [BEGIN]: cwd={cwd} cmd={command[:4]}')
    try:
        # start_new_session puts the CLI in its own process group so the whole tree is killable
        process = subprocess.Popen(
            command, cwd=cwd, env=env, stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            start_new_session=True,
        )
    except FileNotFoundError:
        raise outbox.NotDeliveredError(f'CLI not found: {command[0]}')

    _track_child(process)
    try:
        stdout, stderr = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        logger.error(f'_run_cli [exception]: timeout after {timeout}s, killing process group')
        _terminate_group(process)
        with contextlib.suppress(Exception):
            process.communicate(timeout=KILL_GRACE_SECONDS)
        raise BridgeError(f'peer agent did not answer within {timeout}s')
    finally:
        _untrack_child(process)
        logger.debug('_run_cli [END]')

    return subprocess.CompletedProcess(command, process.returncode, stdout, stderr)


def _panel_title(sender_agent: str, message: str) -> str:
    """A list entry the user can recognise, instead of the panel's default "New chat"."""
    first_line = next((line.strip() for line in message.splitlines() if line.strip()), '')
    label = AGENT_LABEL.get(sender_agent, sender_agent)
    return f'{label}: {" ".join(first_line.split())[:PANEL_TITLE_LIMIT]}'


def _raise_for_panel_failure(response: Dict[str, Any]) -> None:
    """Turn a shim's `ok: false` into the exception that says what it means for the message.

    Three answers, three different next steps. Busy: the peer will take it later, retry. Never
    landed: nothing to wait for, tell the sender. Anything else: the peer has the message and
    something broke on the way back, so its transcript is where the answer will be.
    """
    if response.get('ok') or response.get('pending'):
        return
    error = str(response.get('error') or '')
    # The shim waits for the peer to be free and gives up after a while. That deadline is its
    # own, not ours: the peer is still going to be free eventually.
    if any(signal in error for signal in PEER_BUSY_SIGNALS):
        raise outbox.PeerBusyError(error)

    accepted = response.get('accepted')
    is_never_landed = (accepted is False if 'accepted' in response
                       else any(signal in error for signal in NEVER_LANDED_SIGNALS))
    if is_never_landed:
        raise outbox.NotDeliveredError(f'IDE panel relay refused: {error}')
    raise BridgeError(f'IDE panel relay failed: {error}')


def _transcript_answer(target_agent: Optional[str], session_id: Optional[str],
                       after: float, token: Optional[str]) -> Optional[str]:
    """The peer's finished answer to *this* request, read from its transcript - or None.

    Only a turn that echoes the request token counts here. While the transport is still
    alive, a merely fresh finished turn is not proof: the thread may just have finished
    somebody else's turn. The token is proof, wherever the shim's own bookkeeping got to.
    """
    if not (target_agent and session_id and token):
        return None
    try:
        progress = discovery.peer_progress(target_agent, session_id, after=after, token=token)
    except Exception as e:
        logger.debug(f'_transcript_answer [exception]: {session_id} {e}')
        return None

    answer = (progress or {}).get('answer')
    if answer and discovery.request_token_in(answer) == token:
        return answer
    return None


def _call_via_panel(message: str, session_id: Optional[str], ui_shim: Dict[str, Any],
                    timeout: int, cwd: str, title: Optional[str] = None,
                    on_accepted: Optional[Any] = None, wants_result: bool = True,
                    patience: Optional[float] = None, target_agent: Optional[str] = None,
                    request_token: Optional[str] = None) -> Dict[str, Any]:
    """Deliver through the editor panel shim, so the exchange shows up in the panel.

    The hand-over and the answer are two waits, not one. The shim answers the first as soon as
    the peer has the message; the answer is then collected with as many `await` calls as the
    turn takes, up to `patience`. A single socket wait for the whole turn was how a 600s
    deadline cut off turns that ran 500..820s, and the peer kept working after each cut.

    Between two awaits the peer's transcript is read as well. The shim reports the turn over
    when it sees the app server's completion event for the turn it started; when that event
    does not match - a turn queued behind another, an id the shim never learned - the shim
    keeps saying "still running" while the answer sits finished on disk. A finished turn that
    echoes the request token is that answer, and ends the wait right there.

    A shim from before this protocol ignores the acceptance deadline and answers when the turn
    ends, exactly as before; nothing here depends on the new fields being present.
    """
    started = time.time()
    budget = patience if patience is not None else timeout
    response = uihook.send(message, ui_shim, session_id, timeout, cwd, title,
                           accept_timeout=PANEL_ACCEPT_SECONDS)
    _raise_for_panel_failure(response)

    is_accepted_reported = False
    while response.get('pending'):
        if response.get('accepted') and not is_accepted_reported:
            is_accepted_reported = True
            if on_accepted is not None:
                on_accepted(response)
            if not wants_result:
                # a reply is complete the moment it lands; nobody reads what the peer says next
                break

        if response.get('accepted'):
            confirmed = _transcript_answer(
                target_agent, response.get('sessionId') or session_id, started, request_token)
            if confirmed is not None:
                logger.info(f'_call_via_panel [confirmed by transcript]: {request_token} - the '
                            'peer finished and echoed the request while the shim still reported '
                            'the turn as running')
                return {
                    'session_id': response.get('sessionId') or session_id or '',
                    'reply': confirmed.strip(),
                    'is_new_session': bool(response.get('wasCreated')),
                    'is_reply_confirmed_by_transcript': True,
                    'usage': None,
                    'cost_usd': None,
                }

        remaining = started + budget - time.time()
        if remaining <= 0:
            raise BridgeError(
                f'IDE panel relay failed: the peer turn is still running after '
                f'{round(time.time() - started)}s; its answer will be read from the transcript '
                'when it ends')
        response = uihook.await_turn(ui_shim, str(response.get('injectionId')),
                                     int(min(PANEL_AWAIT_CHUNK_SECONDS, max(remaining, 1))))
        _raise_for_panel_failure(response)

    return {
        'session_id': response.get('sessionId') or session_id or '',
        'reply': str(response.get('reply') or '').strip(),
        'is_new_session': bool(response.get('wasCreated')),
        'is_reply_confirmed_by_transcript': False,
        'usage': None,
        'cost_usd': None,
    }


def _call_claude(message: str, session_id: Optional[str], cwd: str, env: Dict[str, str],
                 timeout: int, ui_shim: Optional[Dict[str, Any]] = None,
                 title: Optional[str] = None, **panel: Any) -> Dict[str, Any]:
    """Resume (or create) a Claude Code session and return its final message."""
    if ui_shim:
        return _call_via_panel(message, session_id, ui_shim, timeout, cwd, title, **panel)

    is_new = session_id is None
    target_id = session_id or str(uuid.uuid4())

    command = [config.CLAUDE_BIN, '-p', '--output-format', 'json']
    command += ['--session-id', target_id] if is_new else ['--resume', target_id]
    if config.CLAUDE_PERMISSION_MODE:
        command += ['--permission-mode', config.CLAUDE_PERMISSION_MODE]
    if config.CLAUDE_MODEL:
        command += ['--model', config.CLAUDE_MODEL]
    command.append(message)

    completed = _run_cli(command, cwd, env, timeout)

    payload: Optional[Dict[str, Any]] = None
    for line in completed.stdout.splitlines():
        line = line.strip()
        if not line.startswith('{'):
            continue
        try:
            candidate = json.loads(line)
        except Exception:
            continue
        if isinstance(candidate, dict) and candidate.get('type') == 'result':
            payload = candidate

    if payload is None:
        detail = (completed.stderr or completed.stdout or '').strip()[-800:]
        raise BridgeError(f'claude CLI returned no result (exit={completed.returncode}): {detail}')

    if payload.get('is_error'):
        raise BridgeError(f'claude CLI error: {str(payload.get("result"))[:800]}')

    return {
        'session_id': payload.get('session_id') or target_id,
        'reply': str(payload.get('result') or '').strip(),
        'is_new_session': is_new,
        'usage': payload.get('usage'),
        'cost_usd': payload.get('total_cost_usd'),
    }


def _call_codex(message: str, session_id: Optional[str], cwd: str, env: Dict[str, str],
                timeout: int, ui_shim: Optional[Dict[str, Any]] = None,
                title: Optional[str] = None, **panel: Any) -> Dict[str, Any]:
    """Deliver to a Codex thread and return its final message.

    With a shim available the turn is started on the app-server the editor panel is attached
    to, so the exchange shows up in the panel. Otherwise the thread is resumed over the CLI,
    which keeps the context but stays invisible to the panel until it is reopened.
    """
    if ui_shim:
        return _call_via_panel(message, session_id, ui_shim, timeout, cwd, title, **panel)

    is_new = session_id is None

    if is_new:
        command = [config.CODEX_BIN, 'exec', '--json', '--skip-git-repo-check',
                   '-s', config.CODEX_SANDBOX, '-C', cwd]
        if config.CODEX_MODEL:
            command += ['-m', config.CODEX_MODEL]
    else:
        # `exec resume` intentionally exposes no sandbox/cwd flags: it inherits the
        # settings the live session was started with.
        command = [config.CODEX_BIN, 'exec', 'resume', session_id, '--json', '--skip-git-repo-check']
    command.append(message)

    completed = _run_cli(command, cwd, env, timeout)

    thread_id: Optional[str] = None
    reply = ''
    errors: List[str] = []

    for line in completed.stdout.splitlines():
        line = line.strip()
        if not line.startswith('{'):
            continue
        try:
            event = json.loads(line)
        except Exception:
            continue

        event_type = event.get('type')
        if event_type == 'thread.started':
            thread_id = event.get('thread_id') or thread_id
        elif event_type == 'item.completed':
            item = event.get('item') or {}
            if item.get('type') == 'agent_message' and item.get('text'):
                reply = str(item['text'])
        elif event_type in ('error', 'turn.failed'):
            errors.append(json.dumps(event, ensure_ascii=False)[:400])

    if not reply:
        detail = '; '.join(errors) or (completed.stderr or completed.stdout or '').strip()[-800:]
        raise BridgeError(f'codex CLI returned no agent message (exit={completed.returncode}): {detail}')

    return {
        'session_id': thread_id or session_id or '',
        'reply': reply.strip(),
        'is_new_session': is_new,
        'usage': None,
        'cost_usd': None,
    }


CALLERS = {config.AGENT_CLAUDE: _call_claude, config.AGENT_CODEX: _call_codex}


# ------------------------------------------------------------------ dispatch

PANEL_SETTING = {
    config.AGENT_CODEX: 'chatgpt.cliExecutable -> codex-shim.sh',
    config.AGENT_CLAUDE: 'claudeCode.claudeProcessWrapper -> claude-shim.sh',
}


# How a relay's target was arrived at. Only SELECTED_CALLER and SELECTED_PIN are addresses
# the caller controls; the rest move with what the human is doing.
SELECTED_CALLER = 'caller'
SELECTED_PIN = 'pin'
SELECTED_PANEL_FOCUS = 'panel-focus'
SELECTED_DISCOVERY = 'discovery'
SELECTED_CREATED = 'created'
SELECTED_FORCED_NEW = 'forced-new'

UNADDRESSED_SELECTIONS = (SELECTED_PANEL_FOCUS, SELECTED_DISCOVERY)


def _panel_session(target_agent: str, wanted_id: Optional[str],
                   exclude_ids: List[str]) -> Optional[Dict[str, Any]]:
    """An already-open conversation in this window's panel, or None."""
    if not uihook.is_enabled():
        return None

    live = uihook.find_live_session(target_agent, wanted_id)
    if not live or live['session_id'] in exclude_ids:
        if not wanted_id and config.UI_HOOK_MODE == uihook.UI_HOOK_REQUIRE:
            raise BridgeError(
                f'CROSS_AGENT_UI_HOOK=require but no live {target_agent} panel session was found '
                f'for this editor window. Check that {PANEL_SETTING.get(target_agent)} is set and '
                'that a panel is open.')
        return None

    return {
        'agent': target_agent,
        'session_id': live['session_id'],
        'cwd': live.get('cwd'),
        'source': ('ide-panel-other-window' if live.get('is_foreign_window') else 'ide-panel'),
        'ui_shim': live['shim'],
        'is_active': True,
        'mtime': live.get('last_seen', time.time()),
    }


def _new_panel_conversation(target_agent: str) -> Optional[Dict[str, Any]]:
    """Last resort: a panel process that can host a fresh conversation."""
    if not uihook.is_enabled():
        return None

    host = uihook.find_panel_host(target_agent)
    if not host:
        return None

    return {
        'agent': target_agent,
        'session_id': None,
        'cwd': None,
        'source': 'ide-panel-new',
        'ui_shim': host['shim'],
        'is_active': True,
        'mtime': time.time(),
    }


def _requested_session_id(target_agent: str, session_id: Optional[str],
                          cwd: str) -> Tuple[Optional[str], Optional[str]]:
    """Resolve what the caller asked for into a session id.

    `session_id` may be a real id or the conversation's name, because that is what a human
    hands the agent. A sticky pin stands in when nothing was named.
    """
    if session_id:
        if discovery.find_session(target_agent, session_id):
            return session_id, session_id
        named = discovery.find_session_by_name(target_agent, session_id)
        if named:
            logger.info(f'_requested_session_id [resolved by name]: '
                        f'{session_id!r} -> {named["session_id"]}')
            return named['session_id'], session_id
        near = discovery.suggest_session_names(target_agent, session_id)
        hint = (f' Titles containing it: {", ".join(repr(t) for t in near)}. Names match '
                'exactly, so pass one of these in full or use the session id.'
                if near else '')
        raise BridgeError(
            f'no {target_agent} session is named {session_id!r}, and no session has that id. '
            f'Nothing was sent and no session was created.{hint} '
            'Use list_agent_sessions to see what exists.')

    pin = registry.get_pin(target_agent, cwd)
    if pin and pin.get('is_sticky'):
        return pin.get('session_id'), None
    return None, None


def _resolve_target(target_agent: str, session_id: Optional[str], scope: str, cwd: str,
                    is_new_forced: bool, exclude_ids: List[str]) -> Optional[Dict[str, Any]]:
    """Pick the conversation a relay lands in.

    Starting a fresh conversation is the LAST resort: it silently drops whatever context the
    caller meant to reach, which in the middle of a long task looks like the peer forgetting
    everything. Everything else is tried first.
    """
    def chosen(target: Optional[Dict[str, Any]], how: str) -> Optional[Dict[str, Any]]:
        if target is not None:
            target['selected_by'] = how
        return target

    if is_new_forced:
        return chosen(_new_panel_conversation(target_agent), SELECTED_FORCED_NEW)

    wanted_id, requested = _requested_session_id(target_agent, session_id, cwd)

    # 1. the session that was named (or pinned) - in the panel if it happens to be open there
    if wanted_id:
        how = SELECTED_CALLER if requested else SELECTED_PIN
        panel = _panel_session(target_agent, wanted_id, exclude_ids)
        if panel:
            return chosen(panel, how)

        found = discovery.find_session(target_agent, wanted_id)
        if not found:
            label = requested or wanted_id
            raise BridgeError(
                f'{target_agent} session {label!r} no longer exists. Nothing was sent and no '
                'session was created; clear the pin with pin_agent_session or name another one.')
        found['source'] = 'name' if requested else 'pin'
        return chosen(found, how)

    # 2. the conversation the user is working in, in this window's panel. Nothing the caller
    #    said chose this one: it is whichever tab the human most recently typed into, which is
    #    a convenience for an interactive ask and an address that moves under anything else.
    panel = _panel_session(target_agent, None, exclude_ids)
    if panel:
        return chosen(panel, SELECTED_PANEL_FOCUS)

    # 3. an existing session on disk. The panel will not show the exchange, but the peer keeps
    #    its context - always better than starting over
    active = discovery.find_active_session(target_agent, scope, cwd, exclude_ids)
    if active:
        return chosen(active, SELECTED_DISCOVERY)

    # 4. nothing to resume anywhere
    return chosen(_new_panel_conversation(target_agent), SELECTED_CREATED)


# --------------------------------------------------------- outbox delivery

def _deliver(job: outbox.Job) -> Dict[str, Any]:
    """Run one queued delivery. Called on an outbox worker thread, never on the caller's."""
    result = CALLERS[job.target_agent](
        job.payload, job.target_session_id, job.run_cwd, job.env, job.timeout, job.ui_shim,
        job.title,
        on_accepted=lambda response: job.mark_accepted(response.get('sessionId')),
        wants_result=job.wants_reply,
        patience=max(job.timeout, config.PANEL_PATIENCE_SECONDS),
        # a request carries a token the peer echoes; that is what lets the transcript settle a
        # turn the shim lost track of. A reply carries none, and wants no answer anyway.
        target_agent=job.target_agent,
        request_token=job.delivery_id if job.wants_reply else None,
    )

    if result['is_new_session'] and result['session_id']:
        registry.set_pin(job.target_agent, job.pin_cwd, result['session_id'], job.run_cwd,
                         is_sticky=False, is_bridge_created=True)
    elif job.target_session_id:
        registry.touch_pin(job.target_agent, job.pin_cwd)
    return result


def _reroute(job: outbox.Job) -> None:
    """Point a delivery at wherever its target session lives *now*.

    Used before retrying a reply that did not land. The route was resolved when the answer
    came in; a tab closed and reopened since is a new panel process behind a new socket, and
    the old one refuses the connection. Without a panel the CLI resume remains.
    """
    if not job.target_session_id:
        return
    panel: Optional[Dict[str, Any]] = None
    with contextlib.suppress(Exception):
        panel = _panel_session(job.target_agent, job.target_session_id, [])
    job.ui_shim = (panel or {}).get('ui_shim')
    logger.info(f'_reroute [resolved]: {job.delivery_id} -> '
                f'{"panel pid=" + str(job.ui_shim.get("pid")) if job.ui_shim else "cli resume"}')


def _build_reply_job(job: outbox.Job, reply: str) -> Optional[outbox.Job]:
    """Turn the peer's answer into a delivery aimed back at whoever started the exchange.

    The answer does not spend a hop: it closes the hop the request already paid for. Only a
    genuinely new request costs budget, which keeps MAX_HOPS meaning what it used to mean.
    """
    if not job.sender_session_id:
        logger.info(f'_build_reply_job [skipped]: {job.delivery_id} has no sender session to '
                    'answer into; the reply is only in the peer transcript')
        return None

    hop = int(registry.get_conversation(job.conversation_id).get('hops', job.hop))
    remaining = max(config.MAX_HOPS - hop, 0)
    answering_id = job.resolved_session_id or job.target_session_id
    payload = _build_reply_envelope(job.target_agent, job.sender_agent, job.conversation_id,
                                    hop, remaining, reply, answering_id,
                                    is_recovered=job.is_reply_recovered,
                                    failure_reason=job.error if job.is_reply_recovered else None)

    # resolved fresh: the sender's panel may have opened, closed or moved during the turn
    panel: Optional[Dict[str, Any]] = None
    with contextlib.suppress(Exception):
        panel = _panel_session(job.sender_agent, job.sender_session_id, [])

    # An answer runs where the sender's session lives, not where the request was aimed. A
    # Claude transcript is filed under its own project directory, so resuming it from the
    # target's directory fails with "No conversation found" even though the session is fine.
    reply_cwd = job.pin_cwd
    known = discovery.find_session(job.sender_agent, job.sender_session_id)
    if known and known.get('cwd') and os.path.isdir(known['cwd']):
        reply_cwd = known['cwd']

    child_busy = [f'{job.target_agent}:{answering_id}'] if answering_id else []

    return outbox.Job(
        target_agent=job.sender_agent,
        target_session_id=job.sender_session_id,
        payload=payload,
        run_cwd=reply_cwd,
        pin_cwd=reply_cwd,
        env=_child_env(job.conversation_id, hop, job.target_agent, child_busy),
        # Our own floor, not the requester's. job.timeout came from whoever sent the request,
        # so a peer on an older build was deciding how long we may spend delivering the answer
        # into our own sender's session - two unrelated waits. Too short and the reply fails
        # into transcript recovery, which is how a half-written turn gets read as an answer.
        timeout=max(job.timeout, config.SEND_TIMEOUT_SECONDS),
        ui_shim=(panel or {}).get('ui_shim'),
        title=_panel_title(job.target_agent, reply),
        conversation_id=job.conversation_id,
        hop=hop,
        sender_agent=job.target_agent,
        sender_session_id=answering_id,
        wants_reply=False,
        summary=_summary(reply),
        kind=outbox.KIND_REPLY,
    )


def _build_notice_job(job: outbox.Job) -> Optional[outbox.Job]:
    """Turn a request that produced no answer into a notice aimed back at its sender.

    A notice travels like a reply - into the sender's session, spending no hop, expecting no
    answer - and says what a reply would have been standing in for.
    """
    if not job.sender_session_id:
        logger.info(f'_build_notice_job [skipped]: {job.delivery_id} has no sender session to '
                    'tell')
        return None

    hop = int(registry.get_conversation(job.conversation_id).get('hops', job.hop))
    remaining = max(config.MAX_HOPS - hop, 0)
    payload = _build_notice_envelope(job, remaining)

    panel: Optional[Dict[str, Any]] = None
    with contextlib.suppress(Exception):
        panel = _panel_session(job.sender_agent, job.sender_session_id, [])

    notice_cwd = job.pin_cwd
    known = discovery.find_session(job.sender_agent, job.sender_session_id)
    if known and known.get('cwd') and os.path.isdir(known['cwd']):
        notice_cwd = known['cwd']

    return outbox.Job(
        target_agent=job.sender_agent,
        target_session_id=job.sender_session_id,
        payload=payload,
        run_cwd=notice_cwd,
        pin_cwd=notice_cwd,
        env=_child_env(job.conversation_id, hop, job.target_agent, []),
        timeout=config.SEND_TIMEOUT_SECONDS,
        ui_shim=(panel or {}).get('ui_shim'),
        title=f'bridge: delivery {job.delivery_id} failed',
        conversation_id=job.conversation_id,
        hop=hop,
        sender_agent=job.target_agent,
        sender_session_id=job.resolved_session_id or job.target_session_id,
        wants_reply=False,
        summary=f'delivery failed: {(job.error or "")[:PANEL_TITLE_LIMIT * 2]}',
        kind=outbox.KIND_NOTICE,
    )


def _recover_reply(job: outbox.Job) -> Optional[str]:
    """Read the peer's answer out of its own transcript when the transport did not bring it.

    Both agents write every turn to a JSONL transcript, so a delivery that reached the peer
    has its answer on disk even when the process carrying it died first. Recovery costs one
    file read and cannot ask the peer to redo the work, which re-sending would.
    """
    session_id = job.resolved_session_id or job.target_session_id
    if not session_id:
        return None
    # The token is proof; the timestamp is the fallback for a peer that did not echo it.
    return discovery.last_agent_message(
        job.target_agent, session_id, after=job.started_at, token=job.delivery_id)


outbox.OUTBOX.deliver = _deliver
outbox.OUTBOX.build_reply = _build_reply_job
outbox.OUTBOX.build_notice = _build_notice_job
outbox.OUTBOX.recover = _recover_reply
outbox.OUTBOX.reroute = _reroute


def _orphan_note(record: Dict[str, Any], is_never_handed_over: bool) -> str:
    why = (f'the server process that carried it (pid {record.get("origin_pid")}) has exited'
           if not record.get('is_origin_alive') else
           'it is past the longest any server could still be carrying it (expires_at)')
    if is_never_handed_over:
        return (f'ORPHANED: {why} while the delivery was still queued. The peer never received '
                'this message, so no answer to it will come. It is not resent - send it again '
                'if it still matters.')
    handed_over = ('' if record.get('accepted_at') else
                   ' The hand-over was never acknowledged, so the peer may or may not have '
                   'received it; an answer in peer_transcript that echoes the request id settles '
                   'that it did.')
    return (f'ORPHANED: {why}, so nothing will move this delivery past '
            f'state={record.get("state")}, and it is not resent.{handed_over} The peer may still '
            'have done the work: peer_transcript is read just now - `answer` is its answer to '
            'this request, `is_working` means it is still on it.')


def delivery_report(delivery_id: str) -> Dict[str, Any]:
    """One delivery in full, with a fresh look at what its target has written since.

    The delivery record is what the bridge saw. The transcript is what the peer did, and the
    two part ways exactly when it matters: a delivery closed while the peer was mid-task has a
    fragment, or nothing, where the answer belongs. Reading the transcript again later - after
    the peer has finished - is how that answer is found, and this is where to ask for it.

    Any server can answer for any delivery, including one whose server has exited: every
    delivery is on disk from the moment it is queued. One whose server is gone is reported with
    `is_orphaned`, and its answer is still read from the peer transcript. Nothing is resent.
    """
    job = outbox.OUTBOX.find(delivery_id)
    if job is not None:
        record = outbox.describe_origin(job.describe(), is_carried_here=True)
    else:
        kept = outbox.read_record(delivery_id)
        record = outbox.describe_origin(kept) if kept is not None else None
    if record is None:
        return {'ok': False,
                'error': f'no delivery {delivery_id} is known to this server or kept on disk'}

    report: Dict[str, Any] = {'ok': True, 'delivery': record}

    # Orphaned while still queued, the message never reached the peer. Its transcript holds no
    # answer to it, and reading one there - with no request time to filter by - would hand back
    # whatever the peer last said about something else.
    is_never_handed_over = (bool(record.get('is_orphaned'))
                            and record.get('state') == outbox.STATE_QUEUED)
    if record.get('is_orphaned'):
        report['note'] = _orphan_note(record, is_never_handed_over)

    target_agent = record.get('target_agent')
    target_session = record.get('target_session_id')
    if target_agent in CALLERS and target_session and is_never_handed_over:
        report['peer_transcript'] = {
            'answer': None,
            'note': ('Not read: this delivery was still queued when its server stopped, so the '
                     'peer never received it and nothing in its transcript answers it.'),
        }
        report['peer_panel'] = panel_state(target_agent, target_session)
    elif target_agent in CALLERS and target_session:
        after = record.get('started_at')
        progress = discovery.peer_progress(target_agent, target_session, after=after,
                                           token=delivery_id)
        if progress is None:
            report['peer_transcript'] = {'error': f'no {target_agent} transcript found for '
                                                  f'session {target_session}'}
        else:
            report['peer_transcript'] = {
                **progress,
                'note': ('Read from the peer transcript just now. `answer` is the text of the '
                         'last turn the peer FINISHED after this request went out (matched by '
                         'the echoed request id when there is one); null while it has not '
                         'finished one. `is_working` is true while a turn is open. '
                         + ('' if after else 'This record predates the request time being '
                                             'kept, so the answer is not filtered by time; check '
                                             'it against the request yourself.')),
            }
        report['peer_panel'] = panel_state(target_agent, target_session)
    return report


def panel_state(agent: str, session_id: str) -> Dict[str, Any]:
    """What the panel hosting a session knows that its transcript cannot say.

    A turn waiting on a human - a command to approve, a question to answer - writes nothing to
    the transcript while it waits, so from the transcript it is indistinguishable from a turn
    that is working. The shim sees the approval request go out and the answer come back, and
    that is the difference between "still going" and "stuck until somebody clicks".
    """
    if not uihook.is_enabled():
        return {'note': 'panel integration is off'}
    try:
        live = uihook.find_live_session(agent, session_id)
    except Exception as e:
        return {'error': f'{type(e).__name__}: {e}'}
    if not live:
        return {'is_open_in_a_panel': False,
                'note': 'not open in any panel this bridge can see; only the transcript speaks '
                        'for it'}

    approval = live.get('awaiting_approval')
    return {
        'is_open_in_a_panel': True,
        'shim_pid': live.get('shim_pid'),
        'is_turn_active': live.get('is_turn_active'),
        'is_awaiting_approval': bool(approval),
        'awaiting_approval': approval,
        'note': ('AWAITING APPROVAL: the peer\'s turn is paused on a prompt only the human can '
                 'answer. It is not working and will not finish until someone approves it in '
                 'the panel.' if approval else
                 'the panel reports no pending approval prompt for this session'),
    }


def _own_session_id(sender_agent: str) -> Optional[str]:
    """The caller's own session - the return address the peer's answer is delivered to.

    Asked of the panel first, which knows it exactly: the shim hosting this process is an
    ancestor of it. Only when there is no panel does this fall back to the transcript on
    disk, and even then without pins - a pin says where to send, and letting it answer this
    question addressed a reply to a session that had not existed for weeks.
    """
    if sender_agent not in CALLERS:
        return None

    if uihook.is_enabled():
        own = uihook.find_own_session(sender_agent)
        if own:
            return own['session_id']

    # rooted at the directory this server was launched from, not at the `cwd` argument,
    # which may point anywhere
    found = discovery.find_active_session(
        sender_agent, discovery.SCOPE_CWD, os.getcwd(), use_pin=False)
    return found['session_id'] if found else None


def send_message(target_agent: str, message: str, session_id: Optional[str] = None,
                 is_new_session: bool = False, scope: Optional[str] = None,
                 cwd: Optional[str] = None, timeout: Optional[int] = None,
                 conversation_id: Optional[str] = None, is_raw: bool = False,
                 allows_same_agent: bool = False,
                 caller_session_id: Optional[str] = None) -> Dict[str, Any]:
    """Queue `message` for the peer agent's active session and return once it is accepted.

    The peer's answer is not this function's return value. It arrives later as a message in
    the caller's own session, delivered by the outbox.

    `caller_session_id` is the session the calling agent *says* it is in - Codex names its
    thread on every tool call. It outranks anything inferred: every Codex thread of a window
    shares one app server and one MCP server, so "the session hosting this process" is a
    whole window's worth of threads, and picking the busiest of them addressed four replies
    to a thread that had not asked.
    """
    started_at = time.time()
    config.ensure_dirs()

    if not message or not message.strip():
        raise BridgeError('message must not be empty')

    scope = scope or config.DEFAULT_SCOPE
    if scope not in discovery.SCOPES:
        raise BridgeError(f"scope must be one of {', '.join(discovery.SCOPES)}, got: {scope}")

    cwd = os.path.realpath(os.path.expanduser(cwd)) if cwd else os.getcwd()

    # The timeout bounds the peer's turn, not the caller's wait - nobody waits any more. So a
    # value below the configured budget has no upside and one real effect: it kills work that
    # would have finished. Callers carried the habit over from when this blocked, and turns
    # were being cut off at 30s and 120s while peer turns here run 216s..660s.
    requested_timeout = timeout
    timeout = max(timeout or config.SEND_TIMEOUT_SECONDS, config.SEND_TIMEOUT_SECONDS)

    identity = caller.detect_caller()
    sender_agent = identity['agent']

    self_session_id = caller_session_id or _own_session_id(sender_agent)
    if caller_session_id:
        logger.info(f'send_message [caller named itself]: {sender_agent} {caller_session_id}')

    # Naming a session and asking for a brand new one are opposite intentions. Honouring both
    # would open a fresh conversation while the caller believes it reached the one it named.
    if session_id and is_new_session:
        raise BridgeError(
            'session_id and new_session cannot be combined: one targets an existing '
            f'conversation, the other opens a new one. Nothing was sent. Drop new_session to '
            f'reach {session_id!r}, or drop session_id to start a new conversation.')

    if sender_agent == target_agent and not allows_same_agent and not session_id:
        raise BridgeError(
            f'refusing to relay a message from {target_agent} back into {target_agent}. '
            f'Use `{PEER_TOOL.get(sender_agent, "the peer tool")}` to reach the other agent, '
            'or pass an explicit session_id together with allows_same_agent=true.')

    busy = _busy_from_env()
    exclude_ids = [s.split(':', 1)[1] for s in busy if s.startswith(target_agent + ':')]
    if sender_agent == target_agent and self_session_id:
        exclude_ids.append(self_session_id)

    conversation_id, is_new_conversation = _resolve_conversation_id(conversation_id)
    hops_used = registry.get_conversation(conversation_id).get('hops', 0)
    if hops_used >= config.MAX_HOPS:
        raise BridgeError(
            f'conversation {conversation_id} reached the hop limit ({config.MAX_HOPS}). '
            'Answer with what you already have instead of relaying again.')

    target = _resolve_target(target_agent, session_id, scope, cwd, is_new_session, exclude_ids)
    target_id = target['session_id'] if target else None

    record = registry.bump_conversation(conversation_id, sender_agent, target_agent)
    hop = int(record.get('hops', 1))
    remaining = max(config.MAX_HOPS - hop, 0)

    # Issued before the envelope, since the envelope must carry the token the peer echoes back.
    request_id = outbox.new_request_id()
    payload = message if is_raw else _build_envelope(
        sender_agent, target_agent, conversation_id, hop, remaining, message, self_session_id,
        request_id=request_id)

    run_cwd = cwd
    if target and target.get('cwd') and os.path.isdir(target['cwd']):
        run_cwd = target['cwd']

    # Only the session actually being written to is off limits. The sender is deliberately
    # left out: it is not parked waiting any more, so the peer relaying back into it is a
    # normal message rather than a deadlock.
    child_busy = list(busy)
    if target_id:
        child_busy.append(f'{target_agent}:{target_id}')

    job = outbox.Job(
        target_agent=target_agent,
        target_session_id=target_id,
        payload=payload,
        run_cwd=run_cwd,
        pin_cwd=cwd,
        env=_child_env(conversation_id, hop, sender_agent, child_busy),
        timeout=timeout,
        ui_shim=(target or {}).get('ui_shim'),
        title=_panel_title(sender_agent, message),
        conversation_id=conversation_id,
        hop=hop,
        sender_agent=sender_agent,
        sender_session_id=self_session_id,
        wants_reply=True,
        summary=_summary(message),
        delivery_id=request_id,
        kind=outbox.KIND_REQUEST,
    )
    # Stay on the line for a moment. A message the worker cannot hand over at all fails within
    # a second, and the caller who is still here is the right one to hear it - three requests
    # were queued as "accepted" today and refused a second later, and nobody was told until
    # bridge_status was asked. Set before submitting, so the worker knows the caller is here.
    job.report_failures_until = time.time() + outbox.EARLY_FAILURE_WINDOW_SECONDS
    delivery_id = outbox.OUTBOX.submit(job)
    outbox.OUTBOX.await_outcome(job)

    if job.is_failure_reportable_synchronously():
        logger.info(f'send_message [refused]: {sender_agent}->{target_agent} '
                    f'delivery={delivery_id} {job.error}')
        return {
            'ok': False,
            'accepted': False,
            'delivery_id': delivery_id,
            'error': job.error,
            'note': ('NOT delivered: the peer never received this message, so nothing is in '
                     'flight and no answer will come. Fix the cause and send again if it still '
                     'matters; this attempt spent a hop.'),
            'target_agent': target_agent,
            'target_session_id': target_id,
            'is_undelivered': job.is_undelivered,
            'conversation_id': conversation_id,
            'hop': hop,
            'hops_remaining': remaining,
            'elapsed_seconds': round(time.time() - started_at, 1),
        }

    logger.info(f'send_message [accepted]: {sender_agent}->{target_agent} '
                f'session={target_id or "NEW"} conv={conversation_id} hop={hop} '
                f'delivery={delivery_id} state={job.state}')

    is_new_target = target_id is None
    warnings: List[str] = []
    if is_new_target:
        warnings.append(
            f'No existing {target_agent} session was reachable for {run_cwd}, so a NEW '
            'conversation will be started. It has none of the earlier context. Tell the user '
            'this happened, and pass session_id (an id or the conversation name) or '
            'pin_agent_session to target a specific one.')
    selected_by = (target or {}).get('selected_by', SELECTED_CREATED)
    if selected_by in UNADDRESSED_SELECTIONS and target_id:
        where = ('the conversation tab this editor window was most recently used in'
                 if selected_by == SELECTED_PANEL_FOCUS
                 else 'the most recently active session on disk')
        warnings.append(
            f'You did not say which {target_agent} session to reach, so this went to '
            f'{where}: {target_id}. That is chosen from what the human is doing, not from '
            'anything you said, and it moves when they switch tabs - two sends in a row can '
            'land in different conversations. Check target_session_id is the one you meant. '
            'For anything automated, delayed, or part of an ongoing exchange, pass session_id '
            'explicitly every time.')
    if not self_session_id:
        warnings.append(
            'Your own session could not be identified, so the peer\'s answer cannot be '
            'delivered back here. It will exist only in the peer\'s transcript.')
    if requested_timeout is not None and requested_timeout < timeout:
        warnings.append(
            f'timeout={requested_timeout}s was raised to {timeout}s. It bounds the peer\'s '
            'turn, not your wait - this call already returned - so a shorter value only '
            'aborts work that would have finished. Pass a larger one to allow more time.')

    return {
        'ok': True,
        'accepted': True,
        'delivery_id': delivery_id,
        # whether the peer's process has already taken the message, as opposed to it waiting
        # in the queue or for the peer to finish another turn
        'is_in_peer_hands': job.accepted_at is not None,
        'state': job.state,
        'note': ('Queued, not answered. This result carries no reply: the peer\'s answer '
                 'arrives later as a separate message in this session. Do not invent, predict '
                 'or wait for it - finish what you are doing and report that the message was '
                 'sent. If the delivery fails later, a DELIVERY FAILED notice arrives here the '
                 'same way. Check bridge_status(delivery_id=...) for delivery state and the '
                 'peer\'s progress.'),
        'warning': ' '.join(warnings) or None,
        'target_agent': target_agent,
        'target_session_id': target_id,
        'session_origin': 'created' if is_new_target else (target or {}).get('source', 'unknown'),
        'target_selected_by': selected_by,
        'is_explicitly_addressed': selected_by == SELECTED_CALLER,
        'will_create_session': is_new_target,
        'delivery': 'ide-panel' if (target or {}).get('ui_shim') else 'cli-resume',
        'is_visible_in_panel': bool((target or {}).get('ui_shim')),
        'queue_depth': outbox.OUTBOX.depth(job.key()),
        'sender_agent': sender_agent,
        'reply_lands_in_session': self_session_id,
        'conversation_id': conversation_id,
        'is_new_conversation': is_new_conversation,
        'hop': hop,
        'hops_remaining': remaining,
        'scope': scope,
        'cwd': run_cwd,
        'elapsed_seconds': round(time.time() - started_at, 1),
    }
