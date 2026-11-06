import base64
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock, patch
from types import SimpleNamespace
import zipfile

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
import tempo_update_manifest as manifest
import tempo_update_recovery as recovery


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.install = self.root / 'app'
        self.current = self.install / 'current'
        self.current.mkdir(parents=True)
        (self.current / 'TEMPO.exe').write_bytes(b'base')
        self.cache = self.root / 'profile' / 'recovery'
        self.packages = self.root / 'profile' / 'packages'
        self.key = Ed25519PrivateKey.generate()
        self.keys = {'test': self.key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)}
        self.base, self.base_zip = self.release('1.0.1', b'base', 'a')
        self.target, self.target_zip = self.release('1.0.2', b'target', 'b')

    def release(self, version, body, commit, updater=None):
        archive = self.root / (version + '.zip')
        with zipfile.ZipFile(archive, 'w') as z:
            z.writestr('lib/app/TEMPO.exe', body)
            z.writestr('metadata.xml', b'meta')
            if updater is not None:
                z.writestr('lib/app/Squirrel.exe', updater)
                z.writestr('lib/app/TEMPO_ExecutionStub.exe', b'installer stub')
        inv = [{'path': p, 'size': len(b), 'sha256': hashlib.sha256(b).hexdigest()}
               for p, b in [('lib/app/TEMPO.exe', body), ('metadata.xml', b'meta')]]
        if updater is not None:
            inv.append({'path': 'lib/app/Squirrel.exe', 'size': len(updater),
                        'sha256': hashlib.sha256(updater).hexdigest()})
            inv.append({'path': 'lib/app/TEMPO_ExecutionStub.exe', 'size': len(b'installer stub'),
                        'sha256': hashlib.sha256(b'installer stub').hexdigest()})
        content = archive.read_bytes()
        obj = {'schema_version': 1, 'package_id': manifest.PACKAGE_ID, 'version': version,
               'commit': commit * 40, 'architecture': 'win-x64', 'channel': 'stable',
               'sequence': 1, 'expires_at': '2099-01-01T00:00:00Z',
               'full': {'name': 'full.zip', 'url': f'https://github.com/tempo-bio/TEMPO/releases/download/v{version}/full.zip',
                        'size': len(content), 'sha256': hashlib.sha256(content).hexdigest()},
               'inventory': inv, 'patches': [], 'minimum_updater_version': '1.2.161',
               'storage_schema_version': 1}
        signed = json.dumps(obj, sort_keys=True, ensure_ascii=False, separators=(',', ':')).encode()
        envelope = json.dumps({'signed': obj, 'signatures': [{'key_id': 'test', 'signature': base64.b64encode(self.key.sign(signed)).decode()}]}).encode()
        release = manifest.load_verified_manifest(envelope, self.keys, now=datetime.now(timezone.utc), min_sequence=0, expected_channel='stable')
        return release, archive

    def arm(self):
        return recovery.arm_recovery(self.install, self.cache, self.base, self.base_zip, self.target, self.target_zip,
                                     trusted_keys=self.keys, expected_root=self.install, packages_dir=self.packages)

    def recover(self, path, mode='forward'):
        return recovery.recover(path, self.keys, mode=mode, expected_root=self.install, packages_dir=self.packages)

    def test_forward_preserves_old_current_and_is_repeatable(self):
        path = self.arm()
        self.assertEqual(self.recover(path), 'recovered_forward')
        self.assertEqual((self.current / 'TEMPO.exe').read_bytes(), b'target')
        self.assertEqual((self.install / 'tempo-repair-quarantine' / 'TEMPO.exe').read_bytes(), b'base')
        self.assertEqual(self.recover(path), 'recovered_forward')

    def test_missing_current_and_interrupted_swap(self):
        path = self.arm()
        self.current.rename(self.install / 'tempo-repair-quarantine')
        self.assertEqual(self.recover(path), 'recovered_forward')
        self.assertEqual((self.current / 'TEMPO.exe').read_bytes(), b'target')
        self.assertEqual((self.install / 'tempo-repair-quarantine' / 'TEMPO.exe').read_bytes(), b'base')

    def test_explicit_previous_then_forward_preserves_both_quarantines(self):
        path = self.arm()
        self.assertEqual(self.recover(path, mode='previous'), 'recovered_previous')
        self.assertEqual(self.recover(path), 'recovered_forward')
        self.assertEqual((self.current / 'TEMPO.exe').read_bytes(), b'target')
        retained = list(self.install.glob('tempo-repair-quarantine*'))
        self.assertEqual(len(retained), 2)
        self.assertTrue(all((folder / 'TEMPO.exe').read_bytes() == b'base' for folder in retained))

    def test_corruption_or_wrong_trust_does_not_replace_application(self):
        path = self.arm()
        (self.cache / 'target.nupkg').write_bytes(b'broken')
        with self.assertRaises(ValueError):
            self.recover(path)
        self.assertEqual((self.current / 'TEMPO.exe').read_bytes(), b'base')
        with self.assertRaises(ValueError):
            recovery.recover(path, {'other': b'x'*32}, mode='forward', expected_root=self.install, packages_dir=self.packages)
        self.assertEqual((self.current / 'TEMPO.exe').read_bytes(), b'base')

    def test_rename_failure_restores_current(self):
        path = self.arm()
        original = recovery.os.rename
        def rename(src, dst):
            if Path(src).name == 'tempo-repair-stage':
                raise OSError('injected rename failure')
            return original(src, dst)
        with patch.object(recovery.os, 'rename', side_effect=rename), self.assertRaises(OSError):
            self.recover(path)
        self.assertEqual((self.current / 'TEMPO.exe').read_bytes(), b'base')
        self.assertEqual(self.recover(path), 'recovered_forward')

    def test_confirm_verifies_target_and_preserves_incompatible_previous(self):
        path = self.arm()
        previous = self.install / 'tempo-previous'
        self.current.rename(previous)
        self.current.mkdir()
        (self.current / 'TEMPO.exe').write_bytes(b'target')
        (self.install / 'tempo-update-pending.txt').write_text('TEMPO update pending\nprevious=1.0.1\ntarget=1.0.2\n')
        with self.assertRaises(ValueError):
            recovery.confirm(path, self.keys, running_version='1.0.1', running_commit='a'*40,
                             expected_root=self.install, packages_dir=self.packages)
        self.assertTrue(previous.exists())
        result = recovery.confirm(path, self.keys, running_version='1.0.2', running_commit='b'*40,
                                  expected_root=self.install, packages_dir=self.packages)
        self.assertEqual(result, 'confirmed')
        self.assertFalse(previous.exists())
        self.assertEqual((self.current / 'TEMPO.exe').read_bytes(), b'target')


    def test_interrupted_backup_cleanup_can_resume_without_deleting_unknown_data(self):
        path = self.arm()
        previous = self.install / 'tempo-previous'
        self.current.rename(previous)
        self.current.mkdir()
        (self.current / 'TEMPO.exe').write_bytes(b'target')
        def interrupt(folder):
            (Path(folder) / 'TEMPO.exe').unlink()
            raise OSError('interrupted backup cleanup')
        options = dict(running_version='1.0.2', running_commit='b'*40,
                       expected_root=self.install, packages_dir=self.packages)
        with patch.object(recovery.shutil, 'rmtree', side_effect=interrupt), self.assertRaises(OSError):
            recovery.confirm(path, self.keys, **options)
        unexpected = previous / 'unsynced.json'
        unexpected.write_text('valuable draft')
        with self.assertRaises(ValueError):
            recovery.confirm(path, self.keys, **options)
        self.assertEqual(unexpected.read_text(), 'valuable draft')
        unexpected.unlink()
        self.assertEqual(recovery.confirm(path, self.keys, **options), 'confirmed')
        self.assertEqual((self.current / 'TEMPO.exe').read_bytes(), b'target')

    def prepare_apply(self):
        updater = b'verified native updater'
        (self.install / 'Update.exe').write_bytes(updater)
        self.base, self.base_zip = self.release('1.0.1', b'base', 'a', updater)
        self.target, self.target_zip = self.release('1.0.2', b'target', 'b', updater)
        return self.arm()

    def test_native_package_control_files_are_not_installed_or_staged(self):
        path = self.prepare_apply()
        with zipfile.ZipFile(self.base_zip) as archive:
            self.assertIn('lib/app/Squirrel.exe', archive.namelist())
            self.assertIn('lib/app/TEMPO_ExecutionStub.exe', archive.namelist())
        self.assertFalse((self.current / 'Squirrel.exe').exists())
        self.assertFalse((self.current / 'TEMPO_ExecutionStub.exe').exists())
        stage = recovery._stage(self.install, self.target, self.target_zip)
        self.assertEqual((stage / 'TEMPO.exe').read_bytes(), b'target')
        self.assertFalse((stage / 'Squirrel.exe').exists())
        self.assertFalse((stage / 'TEMPO_ExecutionStub.exe').exists())
        self.assertEqual(self.recover(path), 'recovered_forward')
        self.assertFalse((self.current / 'Squirrel.exe').exists())
        self.assertFalse((self.current / 'TEMPO_ExecutionStub.exe').exists())

    def test_arm_rejects_missing_or_corrupt_installed_payload(self):
        updater = b'verified native updater'
        self.base, self.base_zip = self.release('1.0.1', b'base', 'a', updater)
        self.target, self.target_zip = self.release('1.0.2', b'target', 'b', updater)
        executable = self.current / 'TEMPO.exe'
        for content in (None, b'tampered'):
            with self.subTest(content=content):
                if content is None:
                    executable.unlink()
                else:
                    executable.write_bytes(content)
                with self.assertRaises(ValueError):
                    self.arm()
        executable.write_bytes(b'base')
        self.assertTrue(self.arm().exists())

    def test_arm_still_verifies_packaged_control_files(self):
        updater = b'verified native updater'
        self.base, self.base_zip = self.release('1.0.1', b'base', 'a', updater)
        self.target, self.target_zip = self.release('1.0.2', b'target', 'b', updater)
        with zipfile.ZipFile(self.target_zip, 'w') as archive:
            archive.writestr('lib/app/TEMPO.exe', b'target')
            archive.writestr('metadata.xml', b'meta')
            archive.writestr('lib/app/Squirrel.exe', updater)
            archive.writestr('lib/app/TEMPO_ExecutionStub.exe', b'tampered stub')
        with self.assertRaises(ValueError):
            self.arm()

    def apply(self, path):
        return recovery.apply_pending(path, self.keys, expected_root=self.install,
                                      packages_dir=self.packages)

    def test_apply_rejects_unarmed_or_modified_base_and_updater(self):
        path = self.prepare_apply()
        with patch.object(recovery.subprocess, 'run') as run:
            record = json.loads(path.read_text())
            record['state'] = 'recovery_required'
            path.write_text(json.dumps(record))
            with self.assertRaises(ValueError):
                self.apply(path)
            record['state'] = 'armed'
            path.write_text(json.dumps(record))
            (self.current / 'TEMPO.exe').write_bytes(b'tampered')
            with self.assertRaises(ValueError):
                self.apply(path)
            (self.current / 'TEMPO.exe').write_bytes(b'base')
            (self.install / 'Update.exe').write_bytes(b'tampered')
            with self.assertRaises(ValueError):
                self.apply(path)
            run.assert_not_called()
        self.assertEqual(json.loads(path.read_text())['state'], 'armed')

    def test_busy_application_lease_times_out_without_starting_updater(self):
        path = self.prepare_apply()
        lease = MagicMock()
        lease.__enter__.side_effect = BlockingIOError()
        with patch.object(recovery, 'application_lock', return_value=lease), \
             patch.object(recovery.time, 'monotonic', side_effect=[0, 61]), \
             patch.object(recovery.subprocess, 'run') as run:
            with self.assertRaises(TimeoutError):
                self.apply(path)
            run.assert_not_called()
        self.assertEqual(json.loads(path.read_text())['state'], 'armed')

    def test_failed_native_apply_preserves_offline_cache_and_requires_repair(self):
        path = self.prepare_apply()
        original = {p.name: p.read_bytes() for p in self.cache.iterdir() if p.is_file()}
        with patch.object(recovery.subprocess, 'run', return_value=SimpleNamespace(returncode=1)), \
             patch.object(recovery.subprocess, 'Popen') as launch:
            with self.assertRaises(RuntimeError):
                self.apply(path)
            launch.assert_not_called()
        self.assertEqual(json.loads(path.read_text())['state'], 'recovery_required')
        for name in ('base.nupkg', 'target.nupkg', 'base.json', 'target.json'):
            self.assertEqual((self.cache / name).read_bytes(), original[name])
        self.assertEqual((self.current / 'TEMPO.exe').read_bytes(), b'base')

    def test_success_requires_verified_target_before_launch(self):
        path = self.prepare_apply()
        with patch.object(recovery.subprocess, 'run', return_value=SimpleNamespace(returncode=0)), \
             patch.object(recovery.subprocess, 'Popen') as launch:
            with self.assertRaises(ValueError):
                self.apply(path)
            launch.assert_not_called()
        self.assertEqual(json.loads(path.read_text())['state'], 'installing')

    def test_success_launches_only_verified_target(self):
        path = self.prepare_apply()
        def native_apply(*args, **kwargs):
            (self.current / 'TEMPO.exe').write_bytes(b'target')
            return SimpleNamespace(returncode=0)
        with patch.object(recovery.subprocess, 'run', side_effect=native_apply), \
             patch.object(recovery.subprocess, 'Popen') as launch:
            self.assertEqual(self.apply(path), 'awaiting_confirmation')
            launch.assert_called_once_with([str(self.current / 'TEMPO.exe')],
                                           cwd=str(self.current), close_fds=True)
        self.assertEqual(json.loads(path.read_text())['state'], 'awaiting_confirmation')

    def test_confirmed_transaction_archived_before_next_arm(self):
        path = self.prepare_apply()
        def native_apply(*args, **kwargs):
            (self.current / 'TEMPO.exe').write_bytes(b'target')
            return SimpleNamespace(returncode=0)
        with patch.object(recovery.subprocess, 'run', side_effect=native_apply), \
             patch.object(recovery.subprocess, 'Popen'):
            self.apply(path)
        recovery.confirm(path, self.keys, running_version='1.0.2', running_commit='b'*40,
                         expected_root=self.install, packages_dir=self.packages)
        third, third_zip = self.release('1.0.3', b'third', 'c', b'verified native updater')
        recovery.arm_recovery(self.install, self.cache, self.target, self.target_zip, third, third_zip,
                              trusted_keys=self.keys, expected_root=self.install, packages_dir=self.packages)
        history = self.cache.with_name('recovery-history') / ('1.0.2-' + 'b'*40)
        self.assertEqual(json.loads((history / 'recovery.json').read_text())['state'], 'confirmed')
        self.assertEqual((history / 'target.nupkg').read_bytes(), self.target_zip.read_bytes())
        record = json.loads(path.read_text())
        self.assertEqual(record['state'], 'armed')
        self.assertEqual(record['target'][0], '1.0.3')


    def prepare_uninstall(self):
        updater = self.install / 'Update.exe'
        updater.write_bytes(b'certified owner')
        config = self.current / '_internal' / 'assets' / 'tempo-updates.json'
        config.parent.mkdir(parents=True)
        data = {'schema_version': 1, 'enabled': True, 'package_id': 'TEMPO.Bio.Desktop',
                'channel': 'stable', 'trusted_keys': {name: base64.b64encode(key).decode()
                                                       for name, key in self.keys.items()},
                'native_recovery_certified': True, 'updater_sha256': recovery._digest(updater),
                'repair_sha256': 'c'*64}
        config.write_text(json.dumps(data), encoding='utf-8')
        return updater, config, data

    def test_uninstall_waits_for_busy_application_without_launch(self):
        updater, _, _ = self.prepare_uninstall()
        profile = self.root / 'profile'
        with recovery.application_lock(profile, shared=True), \
             patch.object(recovery, 'EMBEDDED_UPDATER_SHA256', recovery._digest(updater)), \
             patch.object(recovery.time, 'monotonic', side_effect=[0, 61]), \
             patch.object(recovery.subprocess, 'run') as run:
            with self.assertRaises(TimeoutError):
                recovery.uninstall_application(self.install, profile, self.keys)
            run.assert_not_called()

    def test_uninstall_requires_matching_trust_and_embedded_updater(self):
        updater, config, data = self.prepare_uninstall()
        expected = recovery._digest(updater)
        with patch.object(recovery, 'EMBEDDED_UPDATER_SHA256', expected), \
             patch.object(recovery.subprocess, 'run') as run:
            data['trusted_keys'] = {'other': base64.b64encode(bytes(32)).decode()}
            config.write_text(json.dumps(data), encoding='utf-8')
            with self.assertRaises(ValueError):
                recovery.uninstall_application(self.install, self.root / 'profile', self.keys)
            data['trusted_keys'] = {name: base64.b64encode(key).decode() for name, key in self.keys.items()}
            updater.write_bytes(b'alternate owner')
            data['updater_sha256'] = recovery._digest(updater)
            config.write_text(json.dumps(data), encoding='utf-8')
            with self.assertRaises(ValueError):
                recovery.uninstall_application(self.install, self.root / 'profile', self.keys)
            run.assert_not_called()

    def test_native_uninstall_failure_preserves_user_data(self):
        updater, _, _ = self.prepare_uninstall()
        profile = self.root / 'profile'
        research = profile / 'research' / 'recording.dat'
        research.parent.mkdir(parents=True)
        research.write_bytes(b'research data')
        with patch.object(recovery, 'EMBEDDED_UPDATER_SHA256', recovery._digest(updater)), \
             patch.object(recovery.subprocess, 'run', return_value=SimpleNamespace(returncode=1)) as run:
            with self.assertRaises(RuntimeError):
                recovery.uninstall_application(self.install, profile, self.keys)
            run.assert_called_once_with([str(updater), 'uninstall', '--rootDir', str(self.install)], check=False)
        self.assertEqual(research.read_bytes(), b'research data')

    def test_uninstall_launches_verified_native_owner(self):
        updater, _, _ = self.prepare_uninstall()
        with patch.object(recovery, 'EMBEDDED_UPDATER_SHA256', recovery._digest(updater)), \
             patch.object(recovery.subprocess, 'run', return_value=SimpleNamespace(returncode=0)) as run:
            self.assertEqual(recovery.uninstall_application(self.install, self.root / 'profile', self.keys),
                             'uninstalled')
            run.assert_called_once_with([str(updater), 'uninstall', '--rootDir', str(self.install)], check=False)

    def test_unexpected_root_rejected(self):
        path = self.arm()
        with self.assertRaises(ValueError):
            recovery.recover(path, self.keys, mode='forward', expected_root=self.root / 'other', packages_dir=self.packages)


if __name__ == '__main__':
    unittest.main()
