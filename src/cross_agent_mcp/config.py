"""Configuration for the cross-agent MCP bridge.

Every knob is overridable through an environment variable so that the bridge can be
tuned from the MCP client configuration (Claude Code `.mcp.json`, Codex `config.toml`)
without touching the code.
"""

import contextlib
import os
import stat
import urllib.parse
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
# The session a bridge-started CLI turn is running as, `<agent>:<session id>`. The bridge chose
# that session when it started the turn, so the server inside can say who it is exactly
# instead of guessing from whatever was last active in its directory.
ENV_SELF_SESSION: str = 'CROSS_AGENT_SELF_SESSION'

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


# What the bridge owns inside its state root, by name. The repair walk visits only these,
# because CROSS_AGENT_HOME can be pointed at a directory that already has things in it -
# `~/git` would do - and "tighten everything I find" would then re-mode the user's own files
# for the crime of being in the wrong directory. Anything the bridge writes is either one of
# these files or inside one of these directories.
MANAGED_FILES: tuple = ('registry.json', 'registry.json.lock', 'registry.json.tmp')
MANAGED_SUBDIRS: tuple = ('locks', 'logs', 'deliveries', 'panels')


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
            'of the agents\' own directories. The bridge will not keep its state there or '
            'change its permissions; point CROSS_AGENT_HOME somewhere of its own.')

    os.makedirs(path, mode=DIR_MODE, exist_ok=True)
    # makedirs applies the umask to `mode`, and says nothing at all about a directory that
    # already existed; chmod is what actually settles both cases. A failure here is raised
    # rather than swallowed: this is state being created to be used, and carrying on would
    # mean the bridge writing sessions and messages into a directory it has just failed to
    # make private, while every other part of it assumes otherwise. Best-effort belongs in
    # the migration walk, where the alternative to skipping a path is not starting at all.
    try:
        os.chmod(path, DIR_MODE)
    except OSError as e:
        raise OSError(
            f'cannot make {path} owner-only ({e}). The bridge keeps session ids, working '
            'directories and message summaries there, so it will not use a directory whose '
            'permissions it could not set. Point CROSS_AGENT_HOME at a filesystem that '
            'supports it.') from e


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

    repaired = _repair_path(base, DIR_MODE)
    for name in MANAGED_FILES:
        repaired += _repair_path(os.path.join(base, name), FILE_MODE)

    for name in MANAGED_SUBDIRS:
        directory = os.path.join(base, name)
        if not os.path.isdir(directory) or is_protected_path(directory):
            continue
        for current, dirs, files in os.walk(directory, followlinks=False):
            # Prune rather than only refuse at the top: a store can sit *beneath* a state
            # directory - nothing stops CLAUDE_CONFIG_DIR being set there - and a walk that
            # only checked where it started would march straight into it.
            for protected in [d for d in dirs if is_protected_path(os.path.join(current, d))]:
                dirs.remove(protected)

            repaired += _repair_path(current, DIR_MODE)
            for filename in files:
                repaired += _repair_path(os.path.join(current, filename), FILE_MODE)
    return repaired


def _repair_path(path: str, wanted: int) -> int:
    """Tighten one path if it is open to anyone else. Never follows a symlink, never raises."""
    try:
        info = os.lstat(path)
        if stat.S_ISLNK(info.st_mode) or not stat.S_IMODE(info.st_mode) & 0o077:
            return 0
        os.chmod(path, wanted)
        return 1
    except OSError:
        return 0
# ------------------------------------------------ what a spawned agent process inherits

# An MCP server is started by an editor, and an editor is started from a desktop session that
# has collected API keys, tokens and whatever else the user exports from a shell profile.
# Handing all of it to a resumed peer agent hands it to every command that peer then runs, for
# no benefit: the CLIs need an environment to work in, not this one.
#
# So the child gets a named baseline instead. Everything here is something a CLI cannot be
# expected to run without - where its binary is, whose home directory to read config from,
# what the terminal and locale are, how to reach the network and which certificates to trust.
# Adding a name to this list makes it visible to the peer agent, so it wants a reason.
CHILD_ENV_BASELINE: tuple = (
    # finding and running the binary
    'PATH', 'HOME', 'SHELL', 'USER', 'LOGNAME',
    # locale and terminal: without these the CLIs mangle non-ASCII output
    'LANG', 'LANGUAGE', 'LC_ALL', 'LC_CTYPE', 'LC_MESSAGES', 'TERM', 'COLORTERM', 'TZ',
    # scratch space and the XDG config/cache/data roots the CLIs and node store state under
    'TMPDIR', 'TEMP', 'TMP',
    'XDG_RUNTIME_DIR', 'XDG_CONFIG_HOME', 'XDG_CACHE_HOME', 'XDG_DATA_HOME', 'XDG_STATE_HOME',
    # reaching the network through a corporate proxy, and trusting its certificates
    'HTTP_PROXY', 'HTTPS_PROXY', 'NO_PROXY', 'ALL_PROXY',
    'http_proxy', 'https_proxy', 'no_proxy', 'all_proxy',
    'SSL_CERT_FILE', 'SSL_CERT_DIR', 'REQUESTS_CA_BUNDLE', 'NODE_EXTRA_CA_CERTS',
    # macOS: the Security framework reads the login keychain through HOME, and Core Foundation
    # warns on every process start without this one
    '__CF_USER_TEXT_ENCODING',
    # which session store each CLI reads - the bridge resolves sessions in these same two, so
    # a child pointed at a different one would resume a session nobody here can see
    'CLAUDE_CONFIG_DIR', 'CODEX_HOME',
)

# The bridge's own settings, so an agent it spawned runs a bridge configured exactly like
# this one - same state directory, same timeouts, same hop budget. Named one by one rather
# than swept up by prefix: a prefix would also forward anything a user happens to name
# CROSS_AGENT_something, which is no better than forwarding the whole environment. Each of
# these is a path, a number, a mode or a binary name, and none is a credential.
CHILD_ENV_BRIDGE: tuple = (
    'CROSS_AGENT_HOME', 'CROSS_AGENT_DEBUG', 'CROSS_AGENT_DELIVERY_TTL',
    'CROSS_AGENT_CLAUDE_BIN', 'CROSS_AGENT_CODEX_BIN',
    'CROSS_AGENT_REAL_CLAUDE', 'CROSS_AGENT_REAL_CODEX',
    'CROSS_AGENT_ACTIVE_WINDOW_MIN', 'CROSS_AGENT_MAX_HOPS', 'CROSS_AGENT_TIMEOUT',
    'CROSS_AGENT_PANEL_PATIENCE', 'CROSS_AGENT_SCOPE', 'CROSS_AGENT_UI_HOOK',
    'CROSS_AGENT_CODEX_SANDBOX', 'CROSS_AGENT_CODEX_MODEL', 'CROSS_AGENT_CODEX_SCAN_LIMIT',
    'CROSS_AGENT_CLAUDE_PERMISSION_MODE', 'CROSS_AGENT_CLAUDE_MODEL',
    # so the opt-in survives another hop, rather than a grandchild losing it silently
    'CROSS_AGENT_CHILD_ENV',
    # CROSS_AGENT_SELF is deliberately absent: it forces which agent the caller is taken to
    # be, and a child that adopted its parent's answer would misidentify itself.
)

# Escape hatch: a comma-separated list of extra variable names to pass through, for an
# authentication setup that genuinely needs one (a self-hosted gateway's token, a proxy's
# credential helper). Each name listed is visible to the peer agent and to every command it
# runs, so list the one variable, never a prefix of many.
ENV_CHILD_PASSTHROUGH: str = 'CROSS_AGENT_CHILD_ENV'


def child_env_passthrough() -> tuple:
    raw = os.environ.get(ENV_CHILD_PASSTHROUGH) or ''
    return tuple(name.strip() for name in raw.split(',') if name.strip())


# A proxy setting is on the baseline because a CLI behind one cannot reach anything without
# it. But the variable is a URL, and a URL has a place to put a username and password -
# `https://alice:s3cret@proxy.corp:3128` is an ordinary way to configure an authenticating
# proxy, and it is a credential sitting inside an allowlisted variable.
PROXY_ENV_NAMES: frozenset = frozenset({
    'HTTP_PROXY', 'HTTPS_PROXY', 'NO_PROXY', 'ALL_PROXY',
    'http_proxy', 'https_proxy', 'no_proxy', 'all_proxy',
})


def has_embedded_credentials(value: str) -> bool:
    """Whether a proxy setting carries userinfo in any of its entries.

    Parsed rather than pattern-matched. Hand-splitting on `://` and `/` got both ends of this
    wrong: it missed the scheme-relative `//alice:secret@proxy:3128`, and it read the `@` in
    `http://proxy:3128?notify=a@b` as a credential. urlsplit knows where the authority ends,
    and its `username`/`password` handle percent-encoded userinfo without any help.

    A scheme is optional - `user:pass@host:8080` is accepted by the CLIs - so an entry without
    one is prefixed with `//`, or `user:` would be read as the scheme. A value too malformed
    to parse is treated as carrying credentials: it is a proxy setting we cannot vouch for,
    and the failure hint says how to pass it deliberately.
    """
    for raw in value.split(','):
        entry = raw.strip()
        if not entry:
            continue
        if '://' not in entry and not entry.startswith('//'):
            entry = '//' + entry
        try:
            parts = urllib.parse.urlsplit(entry)
            if parts.username or parts.password:
                return True
        except ValueError:
            return True
    return False


def withheld_proxy_vars() -> list:
    """Proxy variables this process has that a child will not be given, and why.

    Withheld whole rather than rewritten. Stripping the credential out would hand the child a
    proxy URL that cannot authenticate, so it would fail at the first request with an error
    about the proxy rather than about the bridge - and the user would be debugging a proxy
    that works perfectly well everywhere else.
    """
    opted_in = set(child_env_passthrough())
    return [name for name in sorted(PROXY_ENV_NAMES)
            if name in os.environ and name not in opted_in
            and has_embedded_credentials(os.environ[name])]


def child_env() -> dict:
    """The environment an agent process spawned by this bridge starts with.

    Built up from nothing rather than filtered down from `os.environ`: a deny-list is only as
    good as its author's imagination, and the thing being kept out is whatever secret this
    particular user happens to export.
    """
    names = list(CHILD_ENV_BASELINE) + list(CHILD_ENV_BRIDGE) + list(child_env_passthrough())
    withheld = set(withheld_proxy_vars())
    return {name: os.environ[name] for name in names
            if name in os.environ and name not in withheld}


def ensure_dirs() -> None:
    global _is_repaired
    for path in (HOME_DIR, LOCK_DIR, LOG_DIR, DELIVERY_DIR):
        secure_makedirs(path)

    if not _is_repaired:
        # once per process, on the first thing that needs the state tree - so every entry
        # point repairs an old installation without each one having to remember to
        _is_repaired = True
        repair_state_permissions()
