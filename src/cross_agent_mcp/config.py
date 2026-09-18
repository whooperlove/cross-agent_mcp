"""Configuration for the cross-agent MCP bridge.

Every knob is overridable through an environment variable so that the bridge can be
tuned from the MCP client configuration (Claude Code `.mcp.json`, Codex `config.toml`)
without touching the code.
"""

import contextlib
import os
import stat
from typing import IO, Optional


def get_env_str(name: str, default: str) -> str:
    value = os.environ.get(name)
    return value if value else default


def get_env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def get_env_optional(name: str) -> Optional[str]:
    value = os.environ.get(name)
    return value if value else None


# bridge state directories
HOME_DIR: str = os.path.expanduser(get_env_str('CROSS_AGENT_HOME', '~/.cross-agent')) + '/'
REGISTRY_PATH: str = HOME_DIR + 'registry.json'
LOCK_DIR: str = HOME_DIR + 'locks/'
LOG_DIR: str = HOME_DIR + 'logs/'
LOG_PATH: str = LOG_DIR + 'bridge.log'

# Deliveries, kept so they outlive the server process that carried them: recorded from the
# moment one is queued (under in-flight/ until it ends), finished ones directly here. Records
# are for reporting only. Nothing reads them back to resume or resend a delivery - a restarted
# server re-sending its queue would ask the peer to do the same work twice.
DELIVERY_DIR: str = HOME_DIR + 'deliveries/'

# delivery records older than this are pruned when the directory is read
DELIVERY_TTL_SECONDS: int = get_env_int('CROSS_AGENT_DELIVERY_TTL', 7 * 24 * 3600)

# agent session stores
CLAUDE_HOME_DIR: str = os.path.expanduser(get_env_str('CLAUDE_CONFIG_DIR', '~/.claude')) + '/'
CLAUDE_PROJECTS_DIR: str = CLAUDE_HOME_DIR + 'projects/'
CODEX_HOME_DIR: str = os.path.expanduser(get_env_str('CODEX_HOME', '~/.codex')) + '/'
CODEX_SESSIONS_DIR: str = CODEX_HOME_DIR + 'sessions/'
CODEX_SESSION_INDEX_PATH: str = CODEX_HOME_DIR + 'session_index.jsonl'

# CLI entry points
CLAUDE_BIN: str = get_env_str('CROSS_AGENT_CLAUDE_BIN', 'claude')
CODEX_BIN: str = get_env_str('CROSS_AGENT_CODEX_BIN', 'codex')

# a session whose transcript has not been touched for longer than this is not "active"
ACTIVE_WINDOW_MINUTES: int = get_env_int('CROSS_AGENT_ACTIVE_WINDOW_MIN', 240)

# ping-pong guard: how many bridge hops a single conversation may take
MAX_HOPS: int = get_env_int('CROSS_AGENT_MAX_HOPS', 4)

# how long a single relayed turn may take before it is aborted - on the CLI path, where the
# turn is our own subprocess. A panel turn cannot be aborted from here at all; see below.
SEND_TIMEOUT_SECONDS: int = get_env_int('CROSS_AGENT_TIMEOUT', 600)

# How long a panel delivery keeps listening for the peer's turn to end. The panel path can only
# watch: the turn belongs to the editor's own session and goes on whether we listen or not, so
# giving up early never saved any work - it only turned a finished answer into a guess read out
# of the transcript. Peer turns of 500..820s were routine on the day this was measured; the
# default leaves room above that. After this the bridge still watches the transcript for a
# while (outbox.RECOVERY_WINDOW_SECONDS) before closing the delivery.
PANEL_PATIENCE_SECONDS: int = get_env_int('CROSS_AGENT_PANEL_PATIENCE', 3600)

# 'cwd' = only sessions rooted at the same directory, 'any' = every recorded session
DEFAULT_SCOPE: str = get_env_str('CROSS_AGENT_SCOPE', 'cwd')

# Deliver into the Codex thread open in this editor window through the app-server shim.
# 'auto' uses it when a shim is running, 'off' always relays over the CLI, 'require' fails
# rather than silently falling back to a headless resume the panel will not show.
UI_HOOK_MODE: str = get_env_str('CROSS_AGENT_UI_HOOK', 'auto')

# applied only when the bridge has to spawn a brand new session
CODEX_SANDBOX: str = get_env_str('CROSS_AGENT_CODEX_SANDBOX', 'read-only')
CODEX_MODEL: Optional[str] = get_env_optional('CROSS_AGENT_CODEX_MODEL')
CLAUDE_PERMISSION_MODE: Optional[str] = get_env_optional('CROSS_AGENT_CLAUDE_PERMISSION_MODE')
CLAUDE_MODEL: Optional[str] = get_env_optional('CROSS_AGENT_CLAUDE_MODEL')

# safety valve on how many rollout files a Codex scan opens; only the first line of each is
# read, and the scan stops early once enough matching sessions are found
CODEX_SCAN_LIMIT: int = get_env_int('CROSS_AGENT_CODEX_SCAN_LIMIT', 2000)

# chain state handed down to the agent process spawned by this bridge
ENV_CONVERSATION_ID: str = 'CROSS_AGENT_CONVERSATION_ID'
ENV_HOP: str = 'CROSS_AGENT_HOP'
ENV_BUSY: str = 'CROSS_AGENT_BUSY'
ENV_SENDER: str = 'CROSS_AGENT_SENDER'

AGENT_CLAUDE: str = 'claude'
AGENT_CODEX: str = 'codex'


# Everything the bridge writes is owner-only. The state tree names the sessions being
# bridged, the directories they run in, and a one-line summary of every message relayed; the
# logs add the routing around them. None of that is another account's business, and the
# user's umask is not a safe place to decide it - a default 0022 leaves it all world-readable.
DIR_MODE: int = 0o700
FILE_MODE: int = 0o600

# whether this process has already repaired a state tree created before those modes
_is_repaired: bool = False

# Places the bridge must never re-mode, however CROSS_AGENT_HOME is set. Two different rules,
# because the home directory is both the thing to protect and the thing the state tree
# normally lives inside:
#
#   equality  - the state root may not BE one of these. `/` and `~` are here only: the normal
#               `~/.cross-agent` is inside the home directory and must keep working.
#   inside    - the state root may not be, or contain, one of these. Both agents' stores are
#               here, so a state root pointed at one, symlinked to one, or merely sitting
#               above one is refused, and a store nested under a legitimate root is walked
#               past rather than into.
_PROTECTED_EXACTLY = {os.path.realpath(p) for p in ('/', os.path.expanduser('~'))}
_PROTECTED_TREES = {os.path.realpath(p) for p in (
    CLAUDE_HOME_DIR, CLAUDE_PROJECTS_DIR, CODEX_HOME_DIR, CODEX_SESSIONS_DIR)}
_PROTECTED_ROOTS = _PROTECTED_EXACTLY | _PROTECTED_TREES


def _within(path: str, root: str) -> bool:
    return path == root or path.startswith(root.rstrip(os.sep) + os.sep)


def is_protected_path(path: str) -> bool:
    """Whether this path is one the bridge must not create, follow into, or re-mode.

    Answered on the *resolved* path, so a symlink pointing into a protected store is refused
    as firmly as the store itself - `chmod` follows symlinks, so a state root that is a link
    to `~/.claude` would otherwise re-mode the real thing.
    """
    real = os.path.realpath(path)
    if real in _PROTECTED_EXACTLY:
        return True
    # Only "is, or is inside, a store". A directory that merely *contains* one is a legitimate
    # state root - the walk prunes the store out of it rather than refusing the whole tree.
    return any(_within(real, root) for root in _PROTECTED_TREES)


def secure_makedirs(path: str) -> None:
    """Create a state directory nobody else can read, whatever the umask says.

    The resolved path is checked first. `chmod` follows symlinks, so a state directory that is
    a link into one of the agents' stores would otherwise tighten the real store - and it is
    the user's own configuration doing it, which makes it neither an attack nor a reason to
    let it happen.
    """
    if is_protected_path(path):
        raise ValueError(
            f'{path} resolves to {os.path.realpath(path)}, which is the home directory or one '
            'of the agents\' session stores. The bridge will not keep its state there or '
            'change its permissions; point CROSS_AGENT_HOME somewhere of its own.')

    os.makedirs(path, mode=DIR_MODE, exist_ok=True)
    # makedirs applies the umask to `mode`, and says nothing at all about a directory that
    # already existed; chmod is what actually settles both cases.
    with contextlib.suppress(OSError):
        os.chmod(path, DIR_MODE)


def secure_open(path: str, mode: str = 'w') -> IO[str]:
    """Open a state file for writing, created owner-only from the first byte.

    The mode is given to `open(2)` rather than applied afterwards, so the file is never
    briefly readable by anyone else - which matters most for the temporary a delivery record
    is written to before it is renamed into place.
    """
    flags = os.O_WRONLY | os.O_CREAT
    if 'x' in mode:
        flags |= os.O_EXCL
    elif 'a' in mode:
        flags |= os.O_APPEND
    else:
        flags |= os.O_TRUNC
    if '+' in mode:
        flags = (flags & ~os.O_WRONLY) | os.O_RDWR
    return os.fdopen(os.open(path, flags, FILE_MODE), mode, encoding='utf-8')


def repair_state_permissions(root: Optional[str] = None) -> int:
    """Tighten a state tree written before these modes were enforced. Returns what it changed.

    New files are created owner-only, but an installation that predates that keeps whatever
    the umask gave it - so the modes have to be repaired, not merely applied from here on.
    Only paths that grant something to group or other are touched, symlinks are never
    followed, and a failure on one path never stops the walk: this runs at startup and must
    not be able to stop the bridge from working.
    """
    base = root if root is not None else HOME_DIR
    # CROSS_AGENT_HOME is user-supplied. Pointed at a home directory or a transcript store it
    # would walk the user's own files and tighten them, so a root that is not a directory of
    # the bridge's own is left alone.
    if is_protected_path(base):
        return 0

    repaired = 0
    for current, dirs, files in os.walk(base, followlinks=False):
        # Prune rather than only refuse at the top: a store can sit *beneath* a legitimate
        # state root - CLAUDE_CONFIG_DIR inside CROSS_AGENT_HOME is all it takes - and a walk
        # that only checked its starting point would march straight into it.
        pruned = [name for name in dirs if is_protected_path(os.path.join(current, name))]
        for name in pruned:
            dirs.remove(name)

        for path, wanted in ([(current, DIR_MODE)]
                             + [(os.path.join(current, name), FILE_MODE) for name in files]):
            try:
                info = os.lstat(path)
                if stat.S_ISLNK(info.st_mode) or not stat.S_IMODE(info.st_mode) & 0o077:
                    continue
                os.chmod(path, wanted)
                repaired += 1
            except OSError:
                continue
    return repaired


def ensure_dirs() -> None:
    global _is_repaired
    for path in (HOME_DIR, LOCK_DIR, LOG_DIR, DELIVERY_DIR):
        secure_makedirs(path)

    if not _is_repaired:
        # once per process, on the first thing that needs the state tree - so every entry
        # point repairs an old installation without each one having to remember to
        _is_repaired = True
        repair_state_permissions()
