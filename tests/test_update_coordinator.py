"""Coordinator tests for guarded handoff and preflight requirements."""
import base64
import hashlib
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from tempo_update_coordinator import PackagedUpdateCoordinator


def sha(data):
    return hashlib.sha256(data).hexdigest()


class CoordinatorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.local = Path(self.temp.name).resolve()
        self.root = self.local / 'TEMPO.Bio.Desktop'
        (self.root / 'current').mkdir(parents=True)
        self.updater = self.root / 'Update.exe'
        self.repair = self.root / 'current' / 'TEMPORepair.exe'
        self.updater.write_bytes(b'certified updater')
        self.repair.write_bytes(b'certified repair')
        self.service = Mock()
        self.service.root = self.local / 'TEMPO' / 'updates'
        self.service.root.mkdir(parents=True)
        self.service.keys = {'key': b'k' * 32}
        self.installation = SimpleNamespace(root=self.root, uninstaller=self.updater)
        self.config = {'trusted_keys': {'key': base64.b64encode(b'k' * 32).decode()},
                       'channel': 'stable', 'updater_sha256': sha(self.updater.read_bytes()),
                       'repair_sha256': sha(self.repair.read_bytes())}
        self.coordinator = PackagedUpdateCoordinator(self.installation, self.config,
                            self.local / 'TEMPO', '1.0.1', 'a' * 40, service=self.service)
        self.base = SimpleNamespace(version='1.0.1', commit='a' * 40,
                                    full=SimpleNamespace(name='base.nupkg'))
        self.target = SimpleNamespace(version='1.0.2', commit='b' * 40, nvidia_descriptor=None)
        self.package = self.local / 'target.nupkg'
        self.base_package = self.local / 'base.nupkg'
        self.coordinator.base_release = self.base
        self.coordinator.base_package = self.base_package
        self.service.verified_prepared.return_value = (self.target, self.package)
        self.env = patch.dict(os.environ, {'LOCALAPPDATA': str(self.local)})
        self.env.start()
        self.addCleanup(self.env.stop)

    def preflight(self):
        with patch('tempo_update_migration.discover_addons', return_value=[]), \
             patch('tempo_update_recovery.arm_recovery', return_value=self.local/'recovery.json') as arm:
            result = self.coordinator.installation_consequences()
        return result, arm

    def test_preflight_checks_location_copies_pinned_tool_and_arms_recovery(self):
        result, arm = self.preflight()
        self.assertEqual(result, (self.target, self.package))
        self.assertEqual(self.coordinator.repair_executable.read_bytes(), self.repair.read_bytes())
        args, kwargs = arm.call_args
        self.assertEqual(args[:6], (self.root, self.service.root/'recovery', self.base,
                                   self.base_package, self.target, self.package))
        self.assertEqual(kwargs['expected_root'], self.root)
        self.assertEqual(kwargs['packages_dir'], self.service.root/'packages')
        self.assertIs(kwargs['trusted_keys'], self.service.keys)
        self.assertEqual(self.coordinator.recovery_record, self.local/'recovery.json')

    def test_invalid_root_or_tampered_native_tools_block_before_arming(self):
        with patch('tempo_update_migration.discover_addons', return_value=[]), \
             patch('tempo_update_recovery.arm_recovery') as arm:
            self.installation.root = self.local/'unsupported'
            with self.assertRaisesRegex(ValueError, 'per-user'):
                self.coordinator.installation_consequences()
            self.installation.root = self.root
            for tool, original in ((self.updater, b'certified updater'),
                                   (self.repair, b'certified repair')):
                tool.write_bytes(b'tampered')
                with self.subTest(tool=tool.name), self.assertRaisesRegex(ValueError, 'certification'):
                    self.coordinator.installation_consequences()
                tool.write_bytes(original)
            arm.assert_not_called()

    def test_handoff_waits_for_close_guard_and_preserves_pending_action(self):
        self.preflight()
        parent = Mock()
        parent._close_action = None
        parent.isVisible.return_value = True
        def queue(action):
            parent._close_action = action
        parent.request_close_action.side_effect = queue
        with patch('tempo_update_coordinator.subprocess.Popen') as popen:
            self.assertTrue(self.coordinator.request_install(parent))
            popen.assert_not_called()
            pending = parent._close_action
            with self.assertRaisesRegex(RuntimeError, 'already waiting'):
                self.coordinator.request_install(parent)
            self.assertIs(parent._close_action, pending)
            self.assertTrue(pending())
            popen.assert_called_once()
            self.assertEqual(popen.call_args.args[0], [str(self.coordinator.repair_executable), '--mode', 'apply', '--show-errors'])

    def test_handoff_rechecks_tool_before_spawning(self):
        self.preflight()
        parent = Mock()
        parent._close_action = None
        parent.isVisible.return_value = True
        parent.request_close_action.side_effect = lambda action: setattr(parent, '_close_action', action)
        with patch('tempo_update_coordinator.subprocess.Popen') as popen:
            self.coordinator.request_install(parent)
            self.repair.write_bytes(b'tampered')
            self.assertFalse(parent._close_action())
            popen.assert_not_called()
            parent.statusBar().showMessage.assert_called_once()

    def test_confirmation_noop_without_pending_transaction(self):
        with patch('tempo_update_coordinator.subprocess.run') as run:
            self.assertFalse(self.coordinator.confirm_startup())
            recovery = self.service.root / 'recovery'
            recovery.mkdir()
            record_path = recovery / 'recovery.json'
            record_path.write_text('{}')
            with patch('tempo_update_recovery._read_record',
                       return_value=(record_path, {'state': 'confirmed'}, self.root, recovery, None)):
                self.assertFalse(self.coordinator.confirm_startup())
            run.assert_not_called()

    def test_confirmation_requires_active_verified_target_worker(self):
        recovery = self.service.root / 'recovery'
        recovery.mkdir()
        record_path = recovery / 'recovery.json'
        record_path.write_text('{}')
        self.target.version, self.target.commit = self.coordinator.version, self.coordinator.commit
        self.target.nvidia_descriptor = b'{"app_version":"1.0.1"}'
        marker = {'version': 'previous'}
        (self.service.root / 'prepared-nvidia.json').write_text(json.dumps({'previous_marker': marker}))
        record = {'state': 'awaiting_confirmation', 'nvidia_marker': marker}
        with patch('tempo_update_recovery._read_record', return_value=(record_path, record, self.root, recovery, None)), \
             patch('tempo_update_recovery._load_pair', return_value=((self.base, None), (self.target, None))), \
             patch('tempo_nvidia_runtime.active_executable', return_value=None), \
             patch('tempo_nvidia_setup.activate_nvidia_runtime', return_value=True), \
             patch('tempo_update_coordinator.subprocess.run') as run:
            with self.assertRaises(ValueError):
                self.coordinator.confirm_startup()
            run.assert_not_called()

    def test_full_fallback_uses_new_plan_without_losing_recovery_base(self):
        full = SimpleNamespace(kind='full')
        self.service.plan.return_value = full
        self.assertIs(self.coordinator.use_full_download(), full)
        self.coordinator.download()
        kwargs = self.service.prepare.call_args.kwargs
        self.assertIsNone(kwargs['base_release'])
        self.assertIsNone(kwargs['base_package'])
        self.assertIs(self.coordinator.base_release, self.base)
        self.assertEqual(self.coordinator.base_package, self.base_package)

    def test_cpu_and_gpu_check_require_matching_nvidia_descriptor(self):
        self.service.check.return_value = [self.base]
        self.service.plan.return_value = SimpleNamespace(target=self.target)
        with patch('tempo_nvidia_runtime.active_executable', return_value=None):
            self.coordinator.check()
            self.assertFalse(self.coordinator.component_required)
            self.assertEqual(self.coordinator.component_download_size, 0)
        with patch('tempo_nvidia_runtime.active_executable', return_value='nvidia-worker'):
            with self.assertRaisesRegex(ValueError, 'no matching NVIDIA'):
                self.coordinator.check()
            self.target.nvidia_descriptor = b'{"archive_size":12345}'
            self.coordinator.check()
            self.assertTrue(self.coordinator.component_required)
            self.assertEqual(self.coordinator.component_download_size, 12345)


if __name__ == '__main__':
    unittest.main()
