"""Discover the currently active Claude Code sessions and Codex threads.

Both products persist every session as a JSONL transcript, so "which session is the
user talking to right now" reduces to "which transcript was written to most recently".

  Claude Code : ~/.claude/projects/<slugified-cwd>/<session-uuid>.jsonl
  Codex       : ~/.codex/sessions/<YYYY>/<MM>/<DD>/rollout-<ts>-<uuid>.jsonl
"""

import datetime
import glob
import json
import logging
import os
import re
import time
from typing import Any, Dict, Iterator, List, Optional, Tuple

from . import config, registry


logger = logging.getLogger('cross_agent_mcp.discovery')

# how many leading transcript lines are inspected to recover session metadata
CLAUDE_HEAD_LINES = 120

SCOPE_CWD = 'cwd'
SCOPE_TREE = 'tree'
SCOPE_ANY = 'any'

# how a session's working directory relates to the one being searched; lower sorts first
CWD_EXACT = 0
CWD_DESCENDANT = 1
CWD_ANCESTOR = 2
CWD_UNRELATED = 3

# Which relations each scope accepts. Ancestors are opt-in: every directory sits under the
# home directory, so accepting them by default would let a session opened at ~ answer for
# every project on the machine.
SCOPE_RELATIONS: Dict[str, Any] = {
    SCOPE_CWD: (CWD_EXACT, CWD_DESCENDANT),
    SCOPE_TREE: (CWD_EXACT, CWD_DESCENDANT, CWD_ANCESTOR),
}
SCOPES = (SCOPE_CWD, SCOPE_TREE, SCOPE_ANY)


def slugify_project_dir(cwd: str) -> str:
    """Reproduce Claude Code's project directory name for a working directory."""
    return re.sub(r'[^a-zA-Z0-9]', '-', os.path.realpath(os.path.expanduser(cwd)))


def _safe_mtime(path: str) -> float:
    """A transcript can be rotated away mid-scan; treat that as infinitely old."""
    try:
        return os.path.getmtime(path)
    except OSError:
        return 0.0


def _cwd_relation(session_cwd: Optional[str], target_cwd: str) -> Optional[int]:
    """Classify a session's directory against the directory being searched."""
    if not session_cwd:
        return None

    left = os.path.realpath(session_cwd)
    right = os.path.realpath(target_cwd)
    if left == right:
        return CWD_EXACT
    if left.startswith(right + '/'):
        return CWD_DESCENDANT
    if right.startswith(left + '/'):
        return CWD_ANCESTOR
    return None


def _is_in_scope(relation: Optional[int], scope: str) -> bool:
    if scope == SCOPE_ANY:
        return True
    return relation is not None and relation in SCOPE_RELATIONS[scope]


def _sort_key(session: Dict[str, Any]):
    """Closest directory relation first, then most recently written."""
    relation = session.get('cwd_relation')
    return (CWD_UNRELATED if relation is None else relation, -session['mtime'])


def _describe_age(mtime: float) -> Dict[str, Any]:
    age_minutes = (time.time() - mtime) / 60.0
    return {
        'mtime': mtime,
        'updated_at': time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(mtime)),
        'age_minutes': round(age_minutes, 1),
        'is_active': age_minutes <= config.ACTIVE_WINDOW_MINUTES,
    }


def _shorten(text: str, limit: int = 90) -> str:
    flat = ' '.join(str(text).split())
    return flat[:limit] + ('…' if len(flat) > limit else '')


# ------------------------------------------------------------- Claude Code

def _extract_text(message: Any) -> str:
    if isinstance(message, str):
        return message
    if isinstance(message, dict):
        content = message.get('content')
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            parts = [b.get('text', '') for b in content
                     if isinstance(b, dict) and b.get('type') == 'text']
            return ' '.join(p for p in parts if p)
    return ''


def _latest_custom_title(path: str) -> Optional[str]:
    """The name a conversation carries *now*.

    A rename is appended wherever the transcript currently ends, so a session named after its
    first CLAUDE_HEAD_LINES lines has nothing in its head to find: a 5391-line transcript
    renamed at line 5388 kept answering to its generated title and could not be reached by the
    name on its own panel. Every session therefore pays a read of the end, not only the ones
    that already showed a name.

    The end is read backwards a chunk at a time and stopped at the first name found, so a
    session renamed recently - the ordinary case - costs one chunk rather than the whole bound.
    A fixed byte window cannot do this: a single record here has been measured at 481,799
    bytes, so a window sized for "the last few hundred lines" can be spent entirely on one of
    them and miss a name written just before it.
    """
    for line in _reversed_records(path, TAIL_BYTES):
        if '"custom-title"' not in line:
            continue
        try:
            entry = json.loads(line)
        except Exception:
            continue
        if entry.get('type') == 'custom-title' and entry.get('customTitle'):
            return str(entry['customTitle'])
    return None


def _parse_claude_session(path: str) -> Optional[Dict[str, Any]]:
    session_id = os.path.basename(path)[:-len('.jsonl')]
    session_cwd: Optional[str] = None
    origin: Optional[str] = None
    title = ''
    custom_title = ''
    has_user_message = False
    is_sidechain_only = True

    try:
        with open(path, 'r', encoding='utf-8', errors='replace') as f:
            for index, line in enumerate(f):
                if index >= CLAUDE_HEAD_LINES:
                    break
                try:
                    entry = json.loads(line)
                except Exception:
                    continue

                if entry.get('type') == 'custom-title' and entry.get('customTitle'):
                    custom_title = str(entry['customTitle'])

                if entry.get('type') == 'ai-title' and entry.get('aiTitle'):
                    title = str(entry['aiTitle'])

                if entry.get('type') not in ('user', 'assistant'):
                    continue

                if not entry.get('isSidechain'):
                    is_sidechain_only = False
                if session_cwd is None:
                    session_cwd = entry.get('cwd')
                    origin = entry.get('entrypoint')
                if entry.get('type') == 'user' and not has_user_message:
                    has_user_message = True
                    if not title:
                        title = _shorten(_extract_text(entry.get('message')))
    except Exception as e:
        logger.debug(f'_parse_claude_session [exception]: {path} {e}')
        return None

    if not has_user_message or is_sidechain_only:
        return None

    # A name the human gave the conversation, shown at the top of its panel. It outranks the
    # generated title and the first message, because it is the only one they can be expected
    # to say back to us - "the koppa_studio session" means this, not the words it opened with.
    custom_title = _latest_custom_title(path) or custom_title

    info: Dict[str, Any] = {
        'agent': config.AGENT_CLAUDE,
        'session_id': session_id,
        'cwd': session_cwd,
        'path': path,
        'origin': origin,
        'title': custom_title or title,
        'is_named': bool(custom_title),
    }
    info.update(_describe_age(_safe_mtime(path)))
    return info


def _claude_project_dirs(scope: str, cwd: str) -> List[str]:
    """Project directories worth opening for this search.

    The directory name is a pure character substitution of the path, so containment survives
    as a prefix relation on the slug. That narrows the scan cheaply; the per-session `cwd`
    recorded in the transcript then rejects the false positives the lossy slug lets through.
    """
    entries = [p for p in glob.glob(config.CLAUDE_PROJECTS_DIR + '*') if os.path.isdir(p)]
    if scope == SCOPE_ANY:
        return entries

    target = slugify_project_dir(cwd)
    accepts_ancestor = CWD_ANCESTOR in SCOPE_RELATIONS[scope]
    keep: List[str] = []
    for path in entries:
        name = os.path.basename(path)
        if name == target or name.startswith(target + '-'):
            keep.append(path)
        elif accepts_ancestor and target.startswith(name + '-'):
            keep.append(path)
    return keep


def list_claude_sessions(scope: str, cwd: str, limit: int = 20) -> List[Dict[str, Any]]:
    paths: List[str] = []
    for project_dir in _claude_project_dirs(scope, cwd):
        paths.extend(glob.glob(project_dir + '/*.jsonl'))

    paths = sorted(paths, key=_safe_mtime, reverse=True)

    sessions: List[Dict[str, Any]] = []
    for path in paths:
        if len(sessions) >= limit:
            break

        info = _parse_claude_session(path)
        if not info:
            continue

        relation = _cwd_relation(info.get('cwd'), cwd)
        if not _is_in_scope(relation, scope):
            continue
        info['cwd_relation'] = relation
        sessions.append(info)

    return sorted(sessions, key=_sort_key)


# ------------------------------------------------------------------- Codex

def _load_codex_thread_names() -> Dict[str, str]:
    names: Dict[str, str] = {}
    try:
        with open(config.CODEX_SESSION_INDEX_PATH, 'r', encoding='utf-8', errors='replace') as f:
            for line in f:
                try:
                    entry = json.loads(line)
                except Exception:
                    continue
                if entry.get('id'):
                    names[entry['id']] = entry.get('thread_name') or ''
    except FileNotFoundError:
        pass
    except Exception as e:
        logger.debug(f'_load_codex_thread_names [exception]: {e}')
    return names


def _parse_codex_session(path: str) -> Optional[Dict[str, Any]]:
    try:
        with open(path, 'r', encoding='utf-8', errors='replace') as f:
            entry = json.loads(f.readline())
    except Exception:
        return None

    if entry.get('type') != 'session_meta':
        return None

    payload = entry.get('payload', {})
    session_id = payload.get('session_id') or payload.get('id')
    if not session_id:
        return None

    # sub-thread rollouts belong to a Codex subagent, never to the user's chat
    if payload.get('thread_source') != 'user':
        return None

    info: Dict[str, Any] = {
        'agent': config.AGENT_CODEX,
        'session_id': session_id,
        'cwd': payload.get('cwd'),
        'path': path,
        'origin': payload.get('originator'),
        'title': '',
    }
    info.update(_describe_age(_safe_mtime(path)))
    return info


def list_codex_sessions(scope: str, cwd: str, limit: int = 20,
                        since_mtime: Optional[float] = None,
                        stats: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
    """Scan the Codex rollout store, newest first.

    Codex keeps every project's rollouts in one date-partitioned store, so a directory filter
    can only be applied by opening each file. Two bounds keep that honest:

      - `since_mtime` stops the walk at a point in time. Session discovery uses the active
        window, because a transcript older than that cannot be the live session anyway.
      - the count cap only applies when nothing is being filtered out. Truncating a filtered
        scan by file count would silently discard the one session being looked for.
    """
    paths = glob.glob(config.CODEX_SESSIONS_DIR + '**/rollout-*.jsonl', recursive=True)
    paths = sorted(paths, key=_safe_mtime, reverse=True)

    thread_names = _load_codex_thread_names()
    by_session: Dict[str, Dict[str, Any]] = {}
    is_filtering = scope != SCOPE_ANY
    examined = 0

    for path in paths:
        if len(by_session) >= limit:
            break
        if since_mtime is not None and _safe_mtime(path) < since_mtime:
            break
        if not is_filtering and examined >= config.CODEX_SCAN_LIMIT:
            logger.info(f'list_codex_sessions [truncated]: stopped after {examined} of '
                        f'{len(paths)} rollout files')
            if stats is not None:
                stats['is_truncated'] = True
            break
        examined += 1

        info = _parse_codex_session(path)
        if not info:
            continue

        relation = _cwd_relation(info.get('cwd'), cwd)
        if not _is_in_scope(relation, scope):
            continue

        # a resumed thread produces several rollout files: keep the freshest one
        previous = by_session.get(info['session_id'])
        if previous and previous['mtime'] >= info['mtime']:
            continue
        info['title'] = thread_names.get(info['session_id'], '')
        info['is_named'] = bool(info['title'])
        info['cwd_relation'] = relation
        by_session[info['session_id']] = info

    return sorted(by_session.values(), key=_sort_key)[:limit]


# ------------------------------------------------------------------ shared

def list_sessions(agent: str, scope: str, cwd: str, limit: int = 20,
                  since_mtime: Optional[float] = None,
                  stats: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
    """`stats` collects what the listing itself knows and the returned list cannot show -
    at present only whether the scan stopped at a cap rather than at the end of the store."""
    if agent == config.AGENT_CLAUDE:
        return list_claude_sessions(scope, cwd, limit)
    if agent == config.AGENT_CODEX:
        return list_codex_sessions(scope, cwd, limit, since_mtime, stats=stats)
    raise ValueError(f'unknown agent: {agent}')


# Both agents name a session with a uuid, in the file name and everywhere else the id appears.
# The id is spliced into a glob below, so what counts as one is decided by its shape rather than
# by whether a lookup happens to succeed: a `*` would otherwise match some other session's file
# and be answered with that session, reported to the caller as the one they named.
SESSION_ID_PATTERN = re.compile(
    r'[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}', re.IGNORECASE | re.ASCII)


def is_session_id(value: Any) -> bool:
    """Whether `value` is shaped like a session id: a uuid, and nothing a glob would read."""
    return isinstance(value, str) and SESSION_ID_PATTERN.fullmatch(value) is not None


def is_inside(path: str, root: str) -> bool:
    """Whether a path really sits under a root once every link in it is resolved.

    A store entry is found by its name, and a name is not a location: a transcript in the
    store can be a symlink to a file outside it, and reading it would answer for a
    conversation the store does not hold, with whatever that file contains.
    """
    try:
        real_root = os.path.realpath(root)
        real_path = os.path.realpath(path)
    except OSError:
        return False
    return real_path == real_root or real_path.startswith(real_root.rstrip(os.sep) + os.sep)


def find_session(agent: str, session_id: str) -> Optional[Dict[str, Any]]:
    """Look up one session by id. Both stores encode the id in the file name.

    Only something shaped like an id is looked up. Anything else - a conversation name, a
    wildcard, a path - is not an id and finds nothing here; naming a session is
    `find_session_by_name`'s job.
    """
    if agent not in (config.AGENT_CLAUDE, config.AGENT_CODEX):
        raise ValueError(f'unknown agent: {agent}')
    if not is_session_id(session_id):
        return None
    session_id = session_id.lower()

    if agent == config.AGENT_CLAUDE:
        for path in glob.glob(config.CLAUDE_PROJECTS_DIR + f'*/{session_id}.jsonl'):
            if not is_inside(path, config.CLAUDE_PROJECTS_DIR):
                continue
            info = _parse_claude_session(path)
            if info:
                return info
        return None

    paths = glob.glob(config.CODEX_SESSIONS_DIR + f'**/rollout-*-{session_id}.jsonl',
                      recursive=True)
    for path in sorted(paths, key=_safe_mtime, reverse=True):
        if not is_inside(path, config.CODEX_SESSIONS_DIR):
            continue
        info = _parse_codex_session(path)
        # The name of a rollout is not what makes it that session's: the record inside says
        # whose it is, and a file whose two disagree answers for a conversation nobody asked
        # about. Same mistake as reading an id out of a glob, one layer further in.
        if info and str(info.get('session_id', '')).lower() == session_id:
            info['title'] = _load_codex_thread_names().get(session_id, '')
            info['is_named'] = bool(info['title'])
            return info
    return None


# how many sessions a lookup by name will look through before it gives up on proving the
# name unique
TITLE_SEARCH_LIMIT = 500

# how many candidates an error spells out before it starts counting instead
DESCRIBED_SESSION_LIMIT = 5


def _normalised(value: Any) -> str:
    return ' '.join(str(value or '').split()).casefold()


def find_exact_title_matches(
        agent: str, name: str,
        limit: Optional[int] = None) -> Tuple[List[Dict[str, Any]], bool, int]:
    """Every session answering exactly to a name, whether the search saw all there are, and
    how many it actually looked at.

    The second value is the one that matters. A scan that stopped at its cap cannot say a
    name is unique - only that it did not meet a second holder before it ran out - and a
    caller told "found it" on that basis has been guessed at. The third is what the caller
    was really told about, which is not the cap it asked for: Codex stops at its own rollout
    bound, so a search asked for 500 can have looked at four.
    """
    wanted = _normalised(name)
    if not wanted:
        return [], True, 0

    cap = TITLE_SEARCH_LIMIT if limit is None else limit
    stats: Dict[str, Any] = {}
    seen = list_sessions(agent, SCOPE_ANY, os.getcwd(), limit=cap + 1, stats=stats)
    # Fewer sessions than the cap does not settle it on its own: the Codex scan stops after
    # CODEX_SCAN_LIMIT rollout files whatever the cap is, and a short list produced that way
    # is a scan that ran out, not a store that did.
    is_exhaustive = len(seen) <= cap and not stats.get('is_truncated')
    examined = seen[:cap]
    matches = [candidate for candidate in examined
               if _normalised(candidate.get('title')) == wanted]
    return sorted(matches, key=lambda s: -s['mtime']), is_exhaustive, len(examined)


def describe_sessions(sessions: List[Dict[str, Any]],
                      limit: int = DESCRIBED_SESSION_LIMIT) -> str:
    """One line per candidate: which session, when it was last used, and where its name
    came from.

    Whether the title was assigned or generated from a first message is the fastest way to
    tell a retired conversation from its replacement - the replacement was usually named on
    purpose - and a generated title is shared by every session that opened the same way,
    which is how a name comes to mean several conversations in the first place.

    Only the first few are spelled out. A name twenty conversations answer to is a name to
    stop using, not a list to read.
    """
    shown = '; '.join(
        f'{s["session_id"]} (last active {s.get("updated_at")}, {s.get("age_minutes")} min ago'
        f'{"" if s.get("is_active") else ", idle"}, '
        f'{"name assigned" if s.get("is_named") else "title generated from its first message"}'
        f', cwd={s.get("cwd") or "unknown"})'
        for s in sessions[:limit])
    if len(sessions) > limit:
        shown += f'; and {len(sessions) - limit} more'
    return shown


RENAME_ADVICE = ('If one of them is retired or replaced, renaming it - and leaving the name '
                 'to the conversation still in use - makes the name mean one conversation '
                 'again.')


class AmbiguousSessionName(LookupError):
    """More than one conversation answers to a name, so none of them can be delivered to.

    Carries the candidates rather than only the count: the caller's way out is to name one by
    id, and it can only do that if it is told which ones there are.
    """

    def __init__(self, agent: str, name: str, matches: List[Dict[str, Any]],
                 is_count_exact: bool = True) -> None:
        self.agent = agent
        self.name = name
        self.matches = matches
        self.is_count_exact = is_count_exact
        super().__init__(
            f'{"" if is_count_exact else "at least "}{len(matches)} {agent} sessions answer to '
            f'{name!r}, so it does not name one conversation: {describe_sessions(matches)}. '
            f'Address one of them by its id. {RENAME_ADVICE}')


class UnprovenSessionName(LookupError):
    """One session carries this name, out of the ones the scan got to.

    Kept apart from "no match" and from "several matches" because it is neither: the name may
    well identify exactly this conversation, and the search simply cannot say so. Delivering
    on it would be choosing the first match found under a cap, which is the same guess
    AmbiguousSessionName exists to refuse - only harder to notice, because from the outside it
    looks like an ordinary successful lookup.
    """

    def __init__(self, agent: str, name: str, match: Dict[str, Any], scanned: int) -> None:
        self.agent = agent
        self.name = name
        self.match = match
        self.scanned = scanned
        super().__init__(
            f'one {agent} session answering to {name!r} was found among the {scanned} '
            f'session(s) the search reached, but it stopped there, so nothing rules out '
            f'another one '
            f'further back: {describe_sessions([match])}. Its id addresses it exactly, whatever '
            f'else carries the name.')


def find_session_by_name(agent: str, name: str,
                         limit: Optional[int] = None) -> Optional[Dict[str, Any]]:
    """Look one session up by the title the user sees, not by its uuid.

    A conversation the human named answers to that name; one they did not falls back to a
    title made from its first message, which is a description and not something to be called
    by. Either way the match is exact, case- and whitespace-insensitive, and nothing else.

    Substring matching used to be allowed and it picked the wrong conversation: "koppa_studio"
    found a path quoted in a months-old session's opening prompt and resumed it headlessly,
    while the session actually named koppa_studio went unseen.

    A name the caller half-remembers must fail loudly. Guessing is how a message ends up in a
    conversation nobody is watching. A name several conversations answer to is the same
    problem wearing a better disguise: taking the most recently active one is a guess about
    which conversation the human meant, made at the moment the message is about to be written
    into it, and a retired session keeps its name until somebody changes it.
    """
    matches, is_exhaustive, examined = find_exact_title_matches(agent, name, limit=limit)
    if len(matches) > 1:
        raise AmbiguousSessionName(agent, name, matches, is_count_exact=is_exhaustive)
    if not matches:
        return None
    if not is_exhaustive:
        raise UnprovenSessionName(agent, name, matches[0], examined)

    best = matches[0]
    best['source'] = 'name'
    best['matched_name'] = name
    return best


def suggest_session_names(agent: str, name: str, limit: int = 500,
                          suggestions: int = 5) -> List[str]:
    """Titles that merely contain what was asked for - shown when nothing matched exactly.

    These are suggestions for a human to read, never something to deliver into.
    """
    wanted = ' '.join(name.split()).casefold()
    if not wanted:
        return []

    seen: List[str] = []
    for candidate in list_sessions(agent, SCOPE_ANY, os.getcwd(), limit=limit):
        title = str(candidate.get('title') or '').strip()
        if title and wanted in ' '.join(title.split()).casefold():
            seen.append(_shorten(title))
        if len(seen) >= suggestions:
            break
    return seen


# how much of a transcript's tail is read when recovering an answer from it
TAIL_BYTES = 2_000_000


def _reversed_records(path: str, limit_bytes: int) -> Iterator[str]:
    """Whole records from the end of a transcript, newest first, within `limit_bytes`.

    Reading backwards lets a caller stop as soon as it has what it wants. The chunk boundary
    is not a record boundary, so the leading fragment of each chunk is carried over and joined
    to the tail of the chunk before it; a record longer than a chunk simply accumulates across
    several. Only the final, incomplete-by-definition fragment at the start of the bound is
    dropped, and only once the bound is reached.

    The caller parses what it gets: a raw substring search would match a record that merely
    quotes the text, and message and tool content routinely does.
    """
    chunk_size = 65_536
    try:
        size = os.path.getsize(path)
        with open(path, 'rb') as f:
            position = size
            carry = b''
            while position > 0 and size - position < limit_bytes:
                step = min(chunk_size, position, limit_bytes - (size - position))
                position -= step
                f.seek(position)
                block = f.read(step) + carry
                lines = block.split(b'\n')
                carry = lines[0]
                for raw in reversed(lines[1:]):
                    text = raw.decode('utf-8', 'replace').strip()
                    if text:
                        yield text
            if position == 0 and carry.strip():
                yield carry.decode('utf-8', 'replace').strip()
    except OSError:
        return


def _tail_lines(path: str, limit_bytes: int = TAIL_BYTES) -> List[str]:
    """The end of a transcript. A rollout runs to thousands of lines; the answer is at the end."""
    try:
        size = os.path.getsize(path)
        with open(path, 'rb') as f:
            if size > limit_bytes:
                f.seek(size - limit_bytes)
                f.readline()  # the seek lands mid-line; drop the fragment
            data = f.read()
    except OSError:
        return []
    return data.decode('utf-8', errors='replace').splitlines()


def _entry_epoch(entry: Dict[str, Any]) -> Optional[float]:
    """Both agents stamp every transcript entry with an ISO 8601 instant."""
    raw = entry.get('timestamp')
    if not isinstance(raw, str):
        return None
    try:
        return datetime.datetime.fromisoformat(raw.replace('Z', '+00:00')).timestamp()
    except ValueError:
        return None


REQUEST_TOKEN_PATTERN = re.compile(r'\breq_\d+_[0-9a-f]{6}\b')


def request_token_in(text: str) -> Optional[str]:
    """The FIRST request id in a message. Use `request_tokens_in` to ask whether one is there."""
    match = REQUEST_TOKEN_PATTERN.search(text)
    return match.group(0) if match else None


def request_tokens_in(text: str) -> List[str]:
    """Every request id a message mentions.

    An answer often names more than one: agents quote the request they are answering, refer
    back to an earlier one, or paste a delivery id out of a status report. Comparing only the
    first meant an answer that mentioned an older request and then ended with the right token
    - exactly the shape the envelope asks for, the id on its own final line - was read as
    answering something else.
    """
    return REQUEST_TOKEN_PATTERN.findall(text)


def _parsed(lines: List[str]) -> Iterator[Dict[str, Any]]:
    """Transcript entries newest first; lines that are not JSON objects are skipped."""
    for line in reversed(lines):
        try:
            entry = json.loads(line)
        except Exception:
            continue
        if isinstance(entry, dict):
            yield entry


def _claude_turns(lines: List[str]) -> Tuple[List[Dict[str, Any]], bool]:
    """Completed turns in a Claude transcript tail, newest first, and whether one is still open.

    Every API response is stored as one entry per content block, all sharing a requestId and a
    stop_reason. `tool_use` means the model paused to run a tool and will speak again; anything
    else ends the turn. The text a model writes between tool calls is real narration - "reverting
    provenance now" - and it is exactly what reading "the last message" used to hand back as
    the answer.
    """
    turns: List[Dict[str, Any]] = []
    is_working = False
    is_newest_group = True
    group_id: Any = None
    group_texts: List[str] = []
    group_stop: Optional[str] = None
    group_at: Optional[float] = None

    def flush() -> None:
        nonlocal is_newest_group, is_working
        if group_id is None:
            return
        if group_stop == 'tool_use':
            if is_newest_group:
                is_working = True
        else:
            text = ' '.join(t for t in reversed(group_texts) if t).strip()
            turns.append({'text': text, 'written_at': group_at})
        is_newest_group = False

    saw_speech = False
    for entry in _parsed(lines):
        kind = entry.get('type')
        if kind not in ('user', 'assistant') or entry.get('isSidechain'):
            continue

        if kind == 'user':
            # A question the human just asked, still unanswered, is also a turn in progress.
            content = (entry.get('message') or {}).get('content')
            is_tool_result = isinstance(content, list) and any(
                isinstance(b, dict) and b.get('type') == 'tool_result' for b in content)
            if not saw_speech and not is_tool_result:
                is_working = True
            continue

        saw_speech = True
        message = entry.get('message') if isinstance(entry.get('message'), dict) else {}
        this_id = entry.get('requestId') or entry.get('uuid') or id(entry)
        if this_id != group_id:
            flush()
            group_id, group_texts, group_stop, group_at = this_id, [], message.get('stop_reason'), None
        group_texts.append(_extract_text(message))
        written = _entry_epoch(entry)
        if written is not None and (group_at is None or written > group_at):
            group_at = written
    flush()
    return turns, is_working


def _codex_turns(lines: List[str]) -> Tuple[List[Dict[str, Any]], bool]:
    """Completed turns in a Codex rollout tail, newest first, and whether one is still open.

    The rollout brackets every turn with `task_started` and `task_complete` events, and the
    completion carries `last_agent_message` - the app-server's own idea of the answer. The
    assistant messages in between are narration between tool calls, the same trap as Claude's.
    A rollout old enough to have no task events falls back to its last message.
    """
    turns: List[Dict[str, Any]] = []
    is_working = False
    saw_boundary = False
    unfinished_turn_id: Optional[str] = None
    latest_messages: List[Dict[str, Any]] = []

    for entry in _parsed(lines):
        payload = entry.get('payload') if isinstance(entry.get('payload'), dict) else {}
        kind = entry.get('type')

        if kind == 'event_msg':
            event = payload.get('type')
            if event == 'task_complete':
                saw_boundary = True
                text = str(payload.get('last_agent_message') or '').strip()
                turns.append({'text': text, 'written_at': _entry_epoch(entry)})
                unfinished_turn_id = None if text else payload.get('turn_id')
            elif event == 'task_started' and not turns:
                saw_boundary = True
                is_working = True
            elif (event == 'item_completed' and unfinished_turn_id
                  and payload.get('turn_id') == unfinished_turn_id):
                # a completion that named no message: take the turn's last spoken item
                item = payload.get('item') if isinstance(payload.get('item'), dict) else {}
                if item.get('type') == 'AgentMessage' and item.get('text'):
                    turns[-1]['text'] = str(item['text']).strip()
                    unfinished_turn_id = None
            continue

        if kind != 'response_item' or payload.get('role') != 'assistant':
            continue
        if payload.get('type') not in ('message', 'agent_message'):
            continue
        text = ' '.join(block.get('text', '') for block in (payload.get('content') or [])
                        if isinstance(block, dict)).strip()
        if text and not turns:
            latest_messages.append({'text': text, 'written_at': _entry_epoch(entry)})

    if not saw_boundary and latest_messages:
        turns = latest_messages[:1]
    return turns, is_working


def _pick_answer(turns: List[Dict[str, Any]], after: Optional[float], token: Optional[str],
                 label: str) -> Tuple[Optional[Dict[str, Any]], Optional[Dict[str, Any]]]:
    """The turn that answers the request, and the one that merely came after it.

    A request carries a token and asks the peer to end its answer with it, so when there is a
    token an echo of it is the only thing that makes a turn this request's answer. Nothing
    else is evidence. A turn that finished after the message was delivered may be an answer to
    a question the human asked in the same session a minute later, or the peer's reply to
    somebody else entirely - both were written after the request, both look fresh, and neither
    answers it. That turn comes back as context, for a human reading the delivery report, and
    never as the answer.

    Without a token - a reply, which asks nothing and expects no answer - timing is all there
    is, and a turn written before the request still cannot be its answer.
    """
    spoken = [t for t in turns if t.get('text')]
    fresh = [t for t in spoken
             if after is None or (t['written_at'] is not None and t['written_at'] > after)]

    if token is not None:
        for turn in spoken:
            if token in request_tokens_in(turn['text']):
                return turn, None
        if fresh:
            echoed = sorted({e for t in fresh for e in request_tokens_in(t['text'])})
            logger.info(f'_pick_answer [unmatched]: {label} finished a turn after the request '
                        f'but it does not echo {token}'
                        + (f' (it answers {echoed})' if echoed else '')
                        + '; keeping it as context, not as the answer')
            return None, fresh[0]
        return None, None

    if not fresh:
        if spoken:
            logger.info(f'_pick_answer [stale]: {label} last finished a turn before the request '
                        'was delivered, so there is no answer to recover yet')
        return None, None
    return fresh[0], None


def peer_progress(agent: str, session_id: str, after: Optional[float] = None,
                  token: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """What a session's transcript says about a request: the answer, or that it is still working.

    The bridge normally carries an answer back from the process it started. When that process
    dies first - the editor window reloaded, the machine slept - the answer is not gone, it is
    just unread: the peer already wrote it to disk. This reads it from there, so an answer is
    lost only when the peer never produced one.

    `after` is the instant the request was delivered, and without it this reads the peer's last
    answer whether or not it has anything to do with the question. That is not a hypothetical:
    the same paragraph, written nine minutes before the request existed, came back as the
    answer to two different questions. A message that predates its own question is not an
    answer, and saying nothing is the honest result.

    Only *finished* turns count. A peer mid-task also has a last message - a line about what it
    is doing next - and a delivery that timed out at 600s used to come back with that line as
    its answer. Here that peer is reported as working, and the answer is read once it ends.

    `unmatched_turn` is the other half of that honesty. When the request carried a token and
    the peer finished a turn without it, there *is* something to show a human reading the
    delivery report - it just is not the answer, and is reported under its own name so nothing
    downstream can mistake it for one.
    """
    session = find_session(agent, session_id)
    if not session:
        return None

    lines = _tail_lines(session['path'])
    turns, is_working = (_claude_turns(lines) if agent == config.AGENT_CLAUDE
                         else _codex_turns(lines))
    answer, unmatched = _pick_answer(turns, after, token, f'{agent} {session_id}')
    return {
        'answer': answer['text'] if answer else None,
        'answered_at': answer['written_at'] if answer else None,
        'unmatched_turn': unmatched['text'] if unmatched else None,
        'unmatched_turn_at': unmatched['written_at'] if unmatched else None,
        'is_working': is_working,
        'last_turn_finished_at': turns[0]['written_at'] if turns else None,
        'transcript_mtime': _safe_mtime(session['path']),
    }


def last_agent_message(agent: str, session_id: str,
                       after: Optional[float] = None,
                       token: Optional[str] = None) -> Optional[str]:
    """The answer a session's finished turn produced, or None while there is none to read."""
    progress = peer_progress(agent, session_id, after, token)
    return progress['answer'] if progress else None


def find_active_session(agent: str, scope: str, cwd: str,
                        exclude_ids: Optional[List[str]] = None,
                        use_pin: bool = True) -> Optional[Dict[str, Any]]:
    """Resolve the session the user is currently talking to.

    Order of preference:
      1. a pin recorded in the registry (sticky pins never expire)
      2. the most recently written transcript inside the active window

    `use_pin=False` skips step 1. A pin records where to *send*, so it must not answer a
    question about who the caller itself is - see uihook.find_own_session.
    """
    blocked = set(exclude_ids or [])

    pin = registry.get_pin(agent, cwd) if use_pin else None
    if pin and pin.get('session_id') not in blocked:
        pinned = find_session(agent, pin['session_id'])
        if pinned and (pin.get('is_sticky') or pinned['is_active']):
            pinned['source'] = 'pin'
            return pinned

    # candidates are ordered by directory closeness first, so an inactive exact-cwd session
    # must be skipped rather than ending the search on a fresher, more distant one
    active_since = time.time() - config.ACTIVE_WINDOW_MINUTES * 60
    for candidate in list_sessions(agent, scope, cwd, limit=50, since_mtime=active_since):
        if candidate['session_id'] in blocked or not candidate['is_active']:
            continue
        candidate['source'] = 'discovery'
        return candidate

    return None
