"""Read VS Code Agent Host Copilot sessions without resuming or changing them.

The editor's session UUID identifies its panel. Copilot's SDK uses a separate UUID,
recorded in session metadata, for the event stream that contains completed answers.
"""

import datetime
import json
import logging
import os
import pathlib
import re
import sqlite3
import time
import urllib.parse
from typing import Any, Dict, Iterator, List, Optional, Tuple

from . import config


logger = logging.getLogger('cross_agent_mcp.copilot')
SESSION_ID_PATTERN = re.compile(
    r'[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}',
    re.IGNORECASE | re.ASCII)
METADATA_KEYS = ('customTitle', 'copilot.project.uri', 'copilot.workingDirectory',
                 'copilot.workingDirectories', 'defaultChatProviderData', 'copilot.model',
                 'isArchived')
EVENT_TAIL_BYTES = 8_000_000
MAX_JSON_BYTES = 2_000_000


class AmbiguousCopilotSession(LookupError):
    """An editor UUID occurs in distinct configured stores, so it is not an address."""

    def __init__(self, session_id: str, sessions: List[Dict[str, Any]]) -> None:
        self.session_id = session_id
        self.sessions = sessions
        stores = ', '.join(str(session['host_db_path']) for session in sessions)
        super().__init__(f'Copilot session {session_id} occurs in multiple stores: {stores}. '
                         'Configure one intended Agent Host store before delivering.')


class AmbiguousCopilotSdkSession(AmbiguousCopilotSession):
    """An SDK UUID does not identify exactly one registered default chat."""

    def __init__(self, sdk_session_id: str, sessions: List[Dict[str, Any]]) -> None:
        self.sdk_session_id = sdk_session_id
        self.session_id = sdk_session_id
        self.sessions = sessions
        addresses = ', '.join(f'{session["session_id"]} ({session["host_db_path"]})'
                              for session in sessions)
        LookupError.__init__(
            self, f'Copilot SDK session {sdk_session_id} maps to multiple Agent Host '
            f'default chats: {addresses}. Configure one intended Agent Host store '
            'before delivering.')


def normalize_session_id(value: Any) -> Optional[str]:
    """Accept an exact UUID or either editor URI spelling; reject all other URI parts."""
    if not isinstance(value, str):
        return None
    for prefix in ('copilotcli:/', 'agent-host-copilotcli:/'):
        if value.startswith(prefix):
            value = value[len(prefix):]
            break
    return value.lower() if SESSION_ID_PATTERN.fullmatch(value) else None


def _is_inside(path: str, root: str) -> bool:
    real_root = os.path.realpath(root).rstrip(os.sep)
    return os.path.realpath(path).startswith(real_root + os.sep)


def _safe_mtime(path: str) -> float:
    try:
        return os.path.getmtime(path)
    except OSError:
        return 0.0


def _json_value(value: Any) -> Any:
    if not isinstance(value, str) or len(value) > MAX_JSON_BYTES:
        return None
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return value


def _text_value(value: Any) -> str:
    parsed = _json_value(value)
    return parsed if isinstance(parsed, str) else value if isinstance(value, str) else ''


def _open_database(path: str) -> sqlite3.Connection:
    connection = sqlite3.connect(pathlib.Path(os.path.abspath(path)).as_uri() + '?mode=ro',
                                 uri=True, timeout=0.2)
    connection.row_factory = sqlite3.Row
    connection.execute('PRAGMA query_only = ON')
    return connection


def _store_pairs() -> Iterator[Tuple[str, str]]:
    """Pair profile roots, or explicit overrides in their configured order.

    Never cross-product independent profiles: an SDK id or title from another profile
    cannot establish the identity of the session registered in this host database.
    """
    databases = tuple(dict.fromkeys(config.COPILOT_HOST_DBS))
    roots = tuple(dict.fromkeys(config.COPILOT_SESSION_DIRS))
    seen = set()
    for index, database in enumerate(databases):
        suffix = '/User/globalStorage/agent-host.db'
        profile_root = database[:-len(suffix)] if database.endswith(suffix) else None
        expected = profile_root + '/agentSessionData' if profile_root else None
        matched = [root for root in roots
                   if expected and os.path.realpath(root) == os.path.realpath(expected)]
        if not matched and index < len(roots):
            matched = [roots[index]]
        for root in matched:
            key = (os.path.realpath(database), os.path.realpath(root))
            if key in seen or not _is_inside(database, os.path.dirname(database)):
                continue
            seen.add(key)
            yield database, root


def _session_database(root: str, session_id: str) -> Optional[str]:
    path = root.rstrip(os.sep) + f'/{session_id}/session.db'
    expected = os.path.realpath(root).rstrip(os.sep) + f'/{session_id}/session.db'
    if not os.path.isfile(path) or os.path.realpath(path) != expected:
        return None
    return path


def _load_metadata(path: Optional[str]) -> Dict[str, Any]:
    if not path:
        return {}
    connection = None
    try:
        connection = _open_database(path)
        placeholders = ','.join('?' for _ in METADATA_KEYS)
        rows = connection.execute(
            f'SELECT key, value FROM session_metadata WHERE key IN ({placeholders})',
            METADATA_KEYS).fetchall()
        return {row['key']: row['value'] for row in rows}
    except (OSError, sqlite3.Error) as error:
        logger.debug(f'_load_metadata [exception]: {type(error).__name__}')
        return {}
    finally:
        if connection:
            connection.close()


def _file_uri_path(value: Any) -> Optional[str]:
    if not isinstance(value, str):
        return None
    try:
        parsed = urllib.parse.urlsplit(value)
    except ValueError:
        return None
    if parsed.scheme != 'file' or parsed.netloc not in ('', 'localhost'):
        return None
    if parsed.query or parsed.fragment:
        return None
    path = urllib.parse.unquote(parsed.path)
    return path if os.path.isabs(path) and '\x00' not in path else None


def _session_cwd(metadata: Dict[str, Any], data: Dict[str, Any]) -> Optional[str]:
    project = data.get('project') if isinstance(data.get('project'), dict) else {}
    cwd = _file_uri_path(_text_value(metadata.get('copilot.project.uri')))
    if cwd:
        return cwd
    working = _text_value(metadata.get('copilot.workingDirectory'))
    if working and os.path.isabs(working) and '\x00' not in working:
        return working
    cwd = _file_uri_path(project.get('uri'))
    if cwd:
        return cwd
    directories = _json_value(metadata.get('copilot.workingDirectories'))
    directories = directories if isinstance(directories, list) else data.get('workingDirectories')
    if isinstance(directories, list) and len(directories) == 1:
        candidate = directories[0]
        if isinstance(candidate, str):
            return _file_uri_path(candidate) or (
                candidate if os.path.isabs(candidate) and '\x00' not in candidate else None)
    return None


def _sdk_event_path(session_id: Optional[str]) -> Optional[str]:
    if not session_id:
        return None
    paths = []
    for root in config.COPILOT_SDK_SESSION_DIRS:
        path = root.rstrip(os.sep) + f'/{session_id}/events.jsonl'
        expected = os.path.realpath(root).rstrip(os.sep) + f'/{session_id}/events.jsonl'
        if os.path.isfile(path) and os.path.realpath(path) == expected:
            paths.append(path)
    unique = {os.path.realpath(path): path for path in paths}
    return next(iter(unique.values())) if len(unique) == 1 else None


def _parse_session(row: sqlite3.Row, database: str, root: str) -> Optional[Dict[str, Any]]:
    session_uri = row['session_uri']
    session_id = normalize_session_id(session_uri)
    if not session_id or session_uri != f'copilotcli:/{session_id}':
        return None
    if row['provider'] != 'copilotcli':
        return None
    payload = _json_value(row['payload'])
    payload = payload if isinstance(payload, dict) else {}
    data = payload.get('data') if isinstance(payload.get('data'), dict) else {}
    path = _session_database(root, session_id)
    # A present entry escaping the store is not a session with missing metadata.
    candidate = root.rstrip(os.sep) + f'/{session_id}/session.db'
    if os.path.lexists(candidate) and not path:
        return None
    metadata = _load_metadata(path)
    custom_title = _text_value(metadata.get('customTitle'))
    provider = _json_value(metadata.get('defaultChatProviderData'))
    provider = provider if isinstance(provider, dict) else {}
    sdk_id = provider.get('sdkSessionId')
    sdk_session_id = (sdk_id.lower() if isinstance(sdk_id, str)
                      and SESSION_ID_PATTERN.fullmatch(sdk_id) else None)
    model = _json_value(metadata.get('copilot.model'))
    model = model if isinstance(model, dict) else provider.get('model')
    model = model if isinstance(model, dict) else {}
    sdk_path = _sdk_event_path(sdk_session_id)
    modified = row['modified_time']
    modified = float(modified) / 1000.0 if isinstance(modified, (int, float)) else 0.0
    local_mtime = _safe_mtime(path) if path else (_safe_mtime(database) if not modified else 0.0)
    mtime = max(modified, local_mtime,
                _safe_mtime(path + '-wal') if path else 0.0,
                _safe_mtime(sdk_path) if sdk_path else 0.0)
    age_minutes = (time.time() - mtime) / 60.0
    is_archived = (_text_value(metadata.get('isArchived')).casefold() == 'true'
                   if 'isArchived' in metadata else data.get('isArchived') is True)
    summary = data.get('summary') if isinstance(data.get('summary'), str) else ''
    return {
        'agent': config.AGENT_COPILOT, 'session_id': session_id,
        'session_uri': session_uri, 'ui_session_uri': f'agent-host-copilotcli:/{session_id}',
        'cwd': _session_cwd(metadata, data), 'path': path or database,
        'host_db_path': database, 'session_db_path': path, 'sdk_session_id': sdk_session_id,
        'sdk_path': sdk_path, 'model_id': model.get('id') if isinstance(model.get('id'), str) else None,
        'origin': 'vscode-agent-host', 'title': custom_title or summary,
        'is_named': bool(custom_title), 'is_archived': is_archived,
        'mtime': mtime, 'updated_at': time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(mtime)),
        'age_minutes': round(age_minutes, 1),
        'is_active': not is_archived and age_minutes <= config.ACTIVE_WINDOW_MINUTES,
    }


def _all_sessions(session_id: Optional[str] = None) -> Iterator[Dict[str, Any]]:
    for database, root in _store_pairs():
        if not os.path.isfile(database):
            continue
        connection = None
        try:
            connection = _open_database(database)
            query = ('SELECT session_uri, provider, modified_time, payload FROM sessions_v2 '
                     'WHERE provider = ?')
            parameters = ['copilotcli']
            if session_id:
                query += ' AND session_uri = ?'
                parameters.append(f'copilotcli:/{session_id}')
            for row in connection.execute(query, parameters):
                session = _parse_session(row, database, root)
                if session:
                    yield session
        except (OSError, sqlite3.Error, ValueError) as error:
            logger.debug(f'_all_sessions [exception]: {type(error).__name__}')
        finally:
            if connection:
                connection.close()


def list_copilot_sessions(scope: str, cwd: str, limit: int = 20,
                          since_mtime: Optional[float] = None) -> List[Dict[str, Any]]:
    from . import discovery

    if scope not in discovery.SCOPES:
        raise ValueError(f'unknown scope: {scope}')
    sessions = []
    for session in _all_sessions():
        relation = discovery._cwd_relation(session.get('cwd'), cwd)
        if not discovery._is_in_scope(relation, scope):
            continue
        if since_mtime is not None and session['mtime'] < since_mtime:
            continue
        session['cwd_relation'] = relation
        sessions.append(session)
    return sorted(sessions, key=discovery._sort_key)[:max(0, limit)]


def find_copilot_session(session_id: str) -> Optional[Dict[str, Any]]:
    normalized = normalize_session_id(session_id)
    if not normalized:
        return None
    sessions = list(_all_sessions(normalized))
    if len(sessions) > 1:
        raise AmbiguousCopilotSession(normalized, sessions)
    return sessions[0] if sessions else None


def find_copilot_session_by_sdk_id(sdk_session_id: str) -> Optional[Dict[str, Any]]:
    """Resolve a runtime-declared UUID through registered default-chat metadata only.

    Recent activity, pins, process ancestry and another chat's SDK UUID cannot
    establish this caller's address. Count archived matches too: filtering one out
    before proving uniqueness would hide an ambiguous identity.
    """
    if not isinstance(sdk_session_id, str) or not SESSION_ID_PATTERN.fullmatch(sdk_session_id):
        return None
    sdk_session_id = sdk_session_id.lower()
    sessions = [session for session in _all_sessions()
                if session.get('sdk_session_id') == sdk_session_id]
    if len(sessions) > 1:
        raise AmbiguousCopilotSdkSession(sdk_session_id, sessions)
    if not sessions or sessions[0].get('is_archived'):
        return None
    return sessions[0]


def _event_epoch(event: Dict[str, Any]) -> Optional[float]:
    stamp = event.get('timestamp')
    if not isinstance(stamp, str):
        return None
    try:
        return datetime.datetime.fromisoformat(stamp.replace('Z', '+00:00')).timestamp()
    except ValueError:
        return None


def _has_token(text: str, token: str) -> bool:
    return re.search(r'(?<![A-Za-z0-9_])' + re.escape(token) + r'(?![A-Za-z0-9_])',
                     text) is not None


def _read_events(path: str, sdk_session_id: str) -> Optional[List[Dict[str, Any]]]:
    """The stream must declare the UUID metadata points to, independent of its filename."""
    try:
        with open(path, 'rb') as stream:
            head = stream.readline(MAX_JSON_BYTES + 1)
            start = json.loads(head)
            data = start.get('data') if isinstance(start, dict) else None
            if (not isinstance(data, dict) or start.get('type') != 'session.start'
                    or normalize_session_id(data.get('sessionId')) != sdk_session_id):
                return None
            size = os.fstat(stream.fileno()).st_size
            if size > EVENT_TAIL_BYTES:
                stream.seek(size - EVENT_TAIL_BYTES)
                stream.readline()
            else:
                stream.seek(0)
            lines = stream.read(EVENT_TAIL_BYTES).splitlines()
        events = []
        for line in lines:
            try:
                event = json.loads(line)
            except (ValueError, UnicodeError):
                continue
            if isinstance(event, dict):
                events.append(event)
        return events
    except (OSError, ValueError, UnicodeError) as error:
        logger.debug(f'_read_events [exception]: {type(error).__name__}')
        return None


def _completed_turns(events: List[Dict[str, Any]]) -> Tuple[List[Dict[str, Any]], bool]:
    """One interaction may contain several model rounds separated by tool execution.

    A round ending with tool requests is still work in progress. Only its final
    round's speech is an answer, and the originating prompt belongs to the exact
    interaction rather than to the SDK's round-local turn id.
    """
    turns = []
    current = None
    prompts: Dict[str, str] = {}
    prompt = ''
    prompt_turn_id = None
    prompt_interaction_id = None
    pending_user = False
    continuation_pending = False
    for event in events:
        data = event.get('data') if isinstance(event.get('data'), dict) else {}
        kind = event.get('type')
        turn_id = data.get('turnId')
        interaction_id = data.get('interactionId')
        interaction_id = interaction_id if isinstance(interaction_id, str) and interaction_id else None
        if kind == 'user.message':
            prompt = data.get('content') if isinstance(data.get('content'), str) else ''
            prompt_turn_id = turn_id
            prompt_interaction_id = interaction_id
            if interaction_id:
                prompts[interaction_id] = prompt
                turns = [turn for turn in turns if turn['interaction_id'] != interaction_id]
            pending_user = True
        elif kind == 'assistant.turn_start' and isinstance(turn_id, str):
            if interaction_id:
                matched_prompt = prompts.get(interaction_id, '')
                turns = [turn for turn in turns if turn['interaction_id'] != interaction_id]
            else:
                matched_prompt = (prompt if prompt_interaction_id is None
                                  and prompt_turn_id in (None, turn_id) else '')
            current = {'turn_id': turn_id, 'interaction_id': interaction_id, 'texts': [],
                       'prompt': matched_prompt, 'needs_continuation': False}
            prompt = ''
            prompt_turn_id = None
            prompt_interaction_id = None
            pending_user = False
            continuation_pending = False
        elif kind == 'assistant.message' and current and turn_id == current['turn_id']:
            if interaction_id is not None and interaction_id != current['interaction_id']:
                continue
            text = data.get('content')
            current['needs_continuation'] = bool(data.get('toolRequests'))
            if data.get('toolRequests'):
                current['texts'] = []
            elif isinstance(text, str) and text:
                current['texts'].append(text)
        elif kind == 'assistant.turn_end' and current and turn_id == current['turn_id']:
            if interaction_id is not None and interaction_id != current['interaction_id']:
                continue
            continuation_pending = current['needs_continuation']
            if not continuation_pending:
                turns.append({'text': '\n'.join(current['texts']).strip(),
                              'prompt': current['prompt'], 'written_at': _event_epoch(event),
                              'interaction_id': current['interaction_id']})
            current = None
    turns.reverse()
    return turns, current is not None or pending_user or continuation_pending


def peer_progress(session_id: str, after: Optional[float] = None,
                  token: Optional[str] = None) -> Optional[Dict[str, Any]]:
    session = find_copilot_session(session_id)
    if not session:
        return None
    path = session['sdk_path']
    events = _read_events(path, session['sdk_session_id']) if path else None
    turns, is_working = _completed_turns(events) if events is not None else ([], False)
    fresh = [turn for turn in turns if turn['text'] and (after is None or (
        turn['written_at'] is not None and turn['written_at'] > after))]
    answer = None
    if token:
        answer = next((turn for turn in fresh if _has_token(turn['text'], token)
                       and _has_token(turn['prompt'], token)), None)
    else:
        answer = fresh[0] if fresh else None
    unmatched = fresh[0] if token and not answer and fresh else None
    return {
        'answer': answer['text'] if answer else None,
        'answered_at': answer['written_at'] if answer else None,
        'unmatched_turn': unmatched['text'] if unmatched else None,
        'unmatched_turn_at': unmatched['written_at'] if unmatched else None,
        'is_working': is_working,
        'last_turn_finished_at': turns[0]['written_at'] if turns else None,
        'transcript_mtime': _safe_mtime(path) if path else session['mtime'],
        'recovery_available': events is not None,
    }
