"""Listener recovery without agent turns, process restarts, or real bridge state.

    PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src .venv/bin/python tests/panel_recovery.py
"""
import errno
import json
import logging
import os
from pathlib import Path
import socket
import stat
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

STATE = tempfile.TemporaryDirectory(prefix='cross-agent-recovery-')
os.environ['CROSS_AGENT_HOME'] = STATE.name + '/state'
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from cross_agent_mcp import bridge, panel, uihook  # noqa: E402


def request(path, payload):
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        client.settimeout(2)
        client.connect(path)
        client.sendall((json.dumps(payload) + '\n').encode())
        buf = b''
        while b'\n' not in buf:
            chunk = client.recv(65536)
            if not chunk:
                raise RuntimeError('connection closed without its reply')
            buf += chunk
        return json.loads(buf.split(b'\n', 1)[0])


def eventually(predicate, seconds=3):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        try:
            if predicate():
                return
        except (OSError, ValueError):
            pass
        time.sleep(.01)
    raise AssertionError('listener did not recover')


class FakeShim(panel.PanelShim):
    agent = 'codex'

    def __init__(self):
        super().__init__(['no-agent-is-started'], [])
        self.marker = object()
        self.waiting = threading.Event()
        self.release = threading.Event()
        self.log = logging.getLogger('test.panel-recovery')
        self.log.addHandler(logging.NullHandler())
        self.log.propagate = False

    def status(self):
        return {'ok': True, 'pid': os.getpid(), 'marker': id(self.marker),
                'sessions': [{'session_id': 'unchanged-conversation'}]}

    def _handle_request(self, message):
        if message.get('op') == 'hold':
            self.waiting.set()
            self.release.wait(2)
            return {'ok': True, 'reply': 'original request completed'}
        return super()._handle_request(message)


class Recovery(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=STATE.name)
        self.root = Path(self.temp.name)
        self.registry = str(self.root / 'registry') + '/'
        self.patches = [
            patch.object(panel, 'LOCAL_SOCKET_ROOT', str(self.root / 'local')),
            patch.object(panel, 'REGISTRY_DIR', self.registry),
            patch.object(uihook, 'REGISTRY_DIR', self.registry),
            patch.object(panel, 'SOCKET_HEALTH_SECONDS', .1),
            patch.object(panel, 'SOCKET_RETRY_SECONDS', .02),
        ]
        for p in self.patches:
            p.start()
        self.shim = None

    def tearDown(self):
        if self.shim:
            self.shim.release.set()
            self.shim.unregister()
            if self.shim._side_channel_thread:
                self.shim._side_channel_thread.join(2)
                self.assertFalse(self.shim._side_channel_thread.is_alive())
        for p in reversed(self.patches):
            p.stop()
        self.temp.cleanup()

    def start(self):
        self.shim = FakeShim()
        self.shim.start_side_channel()
        eventually(lambda: request(self.shim.socket_path, {'op': 'status'})['ok'])
        return self.shim

    def poison_path(self):
        # The listener is still alive, but its visible pathname reaches a dead
        # socket inode. This reproduces connect=ECONNREFUSED with exists=True.
        os.unlink(self.shim.socket_path)
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as orphan:
            orphan.bind(self.shim.socket_path)

    def test_local_socket_and_host_scoped_private_registration(self):
        shim = self.start()
        self.assertEqual(Path(shim.socket_path).parent, self.root / 'local')
        self.assertEqual(stat.S_IMODE(os.stat(shim.socket_path).st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(os.stat(self.root / 'local').st_mode), 0o700)
        record = json.loads(Path(shim.registry_path).read_text())
        self.assertEqual(record['hostname'], socket.gethostname())
        self.assertEqual(record['socket'], shim.socket_path)
        self.assertEqual(stat.S_IMODE(os.stat(shim.registry_path).st_mode), 0o600)

    def test_visible_dead_path_recovers_and_preserves_session_state(self):
        shim = self.start()
        marker = id(shim.marker)
        with patch.object(shim, '_check_socket', return_value=None):
            self.poison_path()
            self.assertTrue(os.path.exists(shim.socket_path))
            with self.assertRaises(ConnectionRefusedError):
                request(shim.socket_path, {'op': 'status'})
        eventually(lambda: request(shim.socket_path, {'op': 'status'})['marker'] == marker)
        self.assertEqual(request(shim.socket_path, {'op': 'status'})['sessions'],
                         [{'session_id': 'unchanged-conversation'}])

    def test_accepted_request_survives_listener_replacement(self):
        shim = self.start()
        results = []
        worker = threading.Thread(target=lambda: results.append(
            request(shim.socket_path, {'op': 'hold'})))
        worker.start()
        self.assertTrue(shim.waiting.wait(1))
        self.poison_path()
        eventually(lambda: request(shim.socket_path, {'op': 'status'})['ok'])
        shim.release.set()
        worker.join(2)
        self.assertEqual(results, [{'ok': True, 'reply': 'original request completed'}])

    def test_removed_path_and_registration_are_republished(self):
        shim = self.start()
        os.unlink(shim.socket_path)
        os.unlink(shim.registry_path)
        eventually(lambda: request(shim.socket_path, {'op': 'status'})['ok'])
        eventually(lambda: Path(shim.registry_path).exists())

    def test_removed_runtime_directory_recovers(self):
        shim = self.start()
        started = json.loads(Path(shim.registry_path).read_text())['started_at']
        os.unlink(shim.socket_path)
        os.rmdir(Path(shim.socket_path).parent)
        eventually(lambda: request(shim.socket_path, {'op': 'status'})['ok'])
        self.assertEqual(json.loads(Path(shim.registry_path).read_text())['started_at'], started)

    def test_initial_bind_failure_retries_without_starting_agent(self):
        self.shim = FakeShim()
        os.rmdir(Path(self.shim.socket_path).parent)
        self.shim.start_side_channel()
        eventually(lambda: request(self.shim.socket_path, {'op': 'status'})['ok'])
        self.assertIsNone(self.shim.process)

    def test_busy_backlog_is_not_treated_as_a_dead_socket(self):
        shim = FakeShim()
        with patch.object(panel.socket, 'socket') as make:
            probe = make.return_value.__enter__.return_value
            for error in [socket.timeout(), BlockingIOError(errno.EAGAIN, 'busy')]:
                probe.connect.side_effect = error
                shim._check_socket()  # Busy is not a reason to discard queued requests.
            probe.connect.side_effect = ConnectionRefusedError(errno.ECONNREFUSED, 'gone')
            with self.assertRaises(ConnectionRefusedError):
                shim._check_socket()

    def test_socket_directory_cannot_be_a_symlink(self):
        target = self.root / 'elsewhere'
        target.mkdir()
        (self.root / 'local').symlink_to(target, target_is_directory=True)
        with self.assertRaisesRegex(OSError, 'unsafe bridge socket directory'):
            FakeShim()

    def test_foreign_host_registry_is_ignored_and_not_deleted(self):
        Path(self.registry).mkdir()
        record = Path(self.registry) / 'other-host.json'
        record.write_text(json.dumps({'agent': 'codex', 'hostname': 'a-different-node',
                                      'pid': -1, 'socket': '/not-on-this-node'}))
        self.assertEqual(uihook.list_shims('codex'), [])
        self.assertTrue(record.exists())

    def test_transport_failure_is_reported_as_unreachable_not_closed(self):
        shim = self.start()
        with patch.object(shim, '_check_socket', return_value=None):
            self.poison_path()
            broken = uihook.unreachable_shims('codex')
            self.assertEqual(len(broken), 1)
            self.assertEqual(broken[0]['pid'], os.getpid())
            self.assertIn('ConnectionRefusedError', broken[0]['error'])

    def test_missing_socket_does_not_erase_a_live_shim_registration(self):
        shim = self.start()
        with patch.object(shim, '_check_socket', return_value=None):
            os.unlink(shim.socket_path)
            broken = uihook.unreachable_shims('codex')
            self.assertEqual(len(broken), 1)
            self.assertIn('FileNotFoundError', broken[0]['error'])
            self.assertTrue(Path(shim.registry_path).exists())

    def test_vscode_thread_does_not_fall_back_to_second_writer(self):
        broken = [{'pid': 123, 'error': 'ConnectionRefusedError'}]
        with patch.object(bridge, '_requested_session_id', return_value=('sid', 'sid')), \
             patch.object(bridge, '_panel_session', return_value=None), \
             patch.object(bridge.discovery, 'find_session',
                          return_value={'session_id': 'sid', 'origin': 'codex_vscode'}), \
             patch.object(uihook, 'is_enabled', return_value=True), \
             patch.object(uihook, 'unreachable_shims', return_value=broken):
            with self.assertRaisesRegex(bridge.BridgeError, 'bridge connection is unavailable'):
                bridge._resolve_target('codex', 'sid', 'any', self.temp.name, False, [])

    def test_terminal_thread_keeps_its_legitimate_cli_fallback(self):
        with patch.object(bridge, '_requested_session_id', return_value=('sid', 'sid')), \
             patch.object(bridge, '_panel_session', return_value=None), \
             patch.object(bridge.discovery, 'find_session',
                          return_value={'session_id': 'sid', 'origin': 'codex_exec'}):
            result = bridge._resolve_target('codex', 'sid', 'any', self.temp.name, False, [])
            self.assertEqual(result['session_id'], 'sid')


if __name__ == '__main__':
    try:
        unittest.main()
    finally:
        STATE.cleanup()
