"""Cross-process lock behavior shared with the native updater."""
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from contextlib import contextmanager
from unittest.mock import patch

from tempo_update_lock import application_lock, update_lock, installation_lock


_CHILD = r'''
import sys, time
from pathlib import Path
from tempo_update_lock import application_lock, update_lock, installation_lock
root, kind, ready, release = map(Path, sys.argv[1:])
lock = (installation_lock(root / 'installation') if str(kind) == 'installation' else
        update_lock(root) if str(kind) == 'update' else application_lock(root, shared=str(kind) == 'shared'))
try:
    with lock:
        ready.write_text('held')
        deadline = time.monotonic() + 15
        while not release.exists():
            if time.monotonic() >= deadline:
                raise TimeoutError('parent did not release lock')
            time.sleep(.02)
except BlockingIOError:
    sys.exit(42)
'''


_WORKER_CHILD = r'''
import sys, time
from pathlib import Path
import tempo_config
import tempo_update_installation as installation
root, kind, ready, release = map(Path, sys.argv[1:])
tempo_config.get_config_dir = lambda: root / 'config'
installation.detect_installation = lambda: installation.Installation('unknown_frozen')
sys.platform = 'win32'
sys.frozen = True
installation.prepare_packaged_startup(['TEMPO.exe', '--assist-model-cuda'])
ready.write_text('held')
deadline = time.monotonic() + 15
while not release.exists():
    if time.monotonic() >= deadline:
        raise TimeoutError('parent did not release worker')
    time.sleep(.02)
'''


class UpdateLockTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.ready = self.root / 'ready'
        self.release = self.root / 'release'

    def child(self, kind, code=_CHILD):
        process = subprocess.Popen([sys.executable, '-u', '-c', code, str(self.root), kind,
                                    str(self.ready), str(self.release)],
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        def cleanup():
            if process.poll() is None:
                process.kill()
            process.communicate(timeout=5)
        self.addCleanup(cleanup)
        deadline = time.monotonic() + 5
        while not self.ready.exists() and process.poll() is None and time.monotonic() < deadline:
            time.sleep(.02)
        if not self.ready.exists():
            if process.poll() is None:
                process.kill()
            out, err = process.communicate(timeout=5)
            self.fail(f'Child failed to acquire {kind} lock: {out!r} {err!r}')
        return process

    def stop(self, process):
        self.release.touch()
        out, err = process.communicate(timeout=5)
        self.assertEqual(process.returncode, 0, (out, err))

    def test_package_lock_is_exact_native_file_and_released(self):
        process = self.child('update')
        self.assertTrue((self.root / '.velopack_lock').is_file())
        with self.assertRaises(BlockingIOError):
            with update_lock(self.root):
                pass
        self.stop(process)
        with update_lock(self.root):
            self.assertTrue((self.root / '.velopack_lock').is_file())

    def test_application_shared_and_exclusive_across_processes(self):
        process = self.child('shared')
        with application_lock(self.root, shared=True):
            pass
        with self.assertRaises(BlockingIOError):
            with application_lock(self.root):
                pass
        self.stop(process)
        with application_lock(self.root):
            pass

    def test_installation_lease_survives_retirement_and_path_reuse(self):
        root = self.root / 'installation'
        root.mkdir()
        process = self.child('installation')
        root.rename(self.root / 'retired')
        root.mkdir()
        with self.assertRaises(BlockingIOError):
            with installation_lock(root):
                pass
        self.stop(process)
        with installation_lock(root):
            pass
        self.assertTrue((self.root / '.installation.tempo-owner.lock').is_file())

    def test_exclusive_application_lock_blocks_shared_and_exclusive(self):
        process = self.child('exclusive')
        for shared in (True, False):
            with self.subTest(shared=shared), self.assertRaises(BlockingIOError):
                with application_lock(self.root, shared=shared):
                    pass
        self.stop(process)
        with application_lock(self.root, shared=True):
            pass

    def test_external_frozen_worker_holds_lease_for_its_lifetime(self):
        process = self.child('external-worker', code=_WORKER_CHILD)
        with self.assertRaises(BlockingIOError):
            with application_lock(self.root):
                pass
        self.stop(process)
        with application_lock(self.root):
            pass

    def test_other_thread_in_same_process_cannot_reenter(self):
        errors = []
        def acquire():
            try:
                with update_lock(self.root):
                    errors.append('acquired')
            except BlockingIOError:
                errors.append('blocked')
        with update_lock(self.root):
            thread = threading.Thread(target=acquire)
            thread.start()
            thread.join(timeout=5)
            self.assertFalse(thread.is_alive())
        self.assertEqual(errors, ['blocked'])
        with update_lock(self.root):
            pass

    def test_startup_guard_covers_the_bootstrap_to_shared_transition(self):
        import tempo_update_installation as installation
        probes = []
        probe_code = """
import sys
from tempo_update_lock import startup_guard
try:
    with startup_guard(sys.argv[1]):
        pass
except BlockingIOError:
    sys.exit(42)
"""
        @contextmanager
        def observed_lock(profile, *, shared=False):
            if shared:
                # This is the precise gap after exclusive bootstrap release and
                # before the long-lived shared desktop lease is acquired.
                result = subprocess.run([sys.executable, '-c', probe_code, str(profile)],
                                        capture_output=True, timeout=5)
                probes.append(result.returncode)
            with application_lock(profile, shared=shared):
                yield
        previous = installation._application_lease
        try:
            with patch.object(installation, 'detect_installation', return_value=installation.Installation('velopack')), \
                 patch('tempo_config.get_config_dir', return_value=self.root/'config'), \
                 patch('tempo_config.get_base_dir', return_value=self.root/'bundle'), \
                 patch('tempo_update_bootstrap.bootstrap_packaged_storage') as bootstrap, \
                 patch.object(installation, '_maintain_velopack_packages'), \
                 patch('tempo_update_lock.application_lock', side_effect=observed_lock), \
                 patch.object(installation.atexit, 'register'):
                installation.prepare_packaged_startup(['TEMPO.exe'])
            self.assertEqual(probes, [42])
            bootstrap.assert_called_once()
            replace_code = probe_code.replace('startup_guard', 'application_lock')
            result = subprocess.run([sys.executable, '-c', replace_code, str(self.root)],
                                    capture_output=True, timeout=5)
            self.assertEqual(result.returncode, 42, result.stderr)
        finally:
            if installation._application_lease is not previous:
                installation._application_lease.__exit__(None, None, None)
            installation._application_lease = previous
        # Once the desktop lease is released the startup guard is available.
        result = subprocess.run([sys.executable, '-c', probe_code, str(self.root)],
                                capture_output=True, timeout=5)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_linked_directory_or_lock_file_is_rejected(self):
        target = self.root / 'target'
        target.mkdir()
        link = self.root / 'linked'
        try:
            link.symlink_to(target, target_is_directory=True)
        except (OSError, NotImplementedError):
            self.skipTest('Creating symlinks is unavailable')
        with self.assertRaises(ValueError):
            with update_lock(link):
                pass
        (self.root / '.velopack_lock').symlink_to(target / 'file')
        with self.assertRaises(ValueError):
            with update_lock(self.root):
                pass


if __name__ == '__main__':
    unittest.main()
