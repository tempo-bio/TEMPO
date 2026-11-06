"""Legacy preparation and retirement must preserve research work and identities."""
from contextlib import nullcontext
from dataclasses import replace
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

import tempo_legacy_migration as migration
from tempo_update_bootstrap import bootstrap_packaged_storage, StorageBootstrapError
from tests.test_update_bootstrap import Registry
from tests import test_update_recovery as recovery_fixtures


def sha(value):
    return hashlib.sha256(value).hexdigest()


class LegacyMigrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir='/private/tmp' if Path('/private/tmp').exists() else None)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.profile = self.root / 'TEMPO'
        self.legacy = self.root / 'old custom installation'
        self.target = self.root / 'TEMPO.Bio.Desktop'
        self.bundle = self.target / 'current' / '_internal'
        self.bundle.mkdir(parents=True)
        (self.target / 'current' / 'TEMPO.exe').write_bytes(b'target')
        self.old_exe = self.put(self.legacy, 'TEMPO.exe', b'reviewed legacy exe')
        provenance = json.dumps({'commit': 'a' * 40}).encode()
        self.put(self.legacy, '_internal/assets/build-provenance.json', provenance)
        self.installer = self.put(self.root, 'old installer.exe', b'reviewed installer')
        self.supported = {'1.0.1': dict(commit='a' * 40, executable_sha256=sha(b'reviewed legacy exe'),
                          installer_sha256=sha(b'reviewed installer'), provenance_sha256=sha(provenance))}
        self.old_registry = Registry({('user', 1): str(self.legacy)})
        self.empty_registry = Registry()
        self.key = Ed25519PrivateKey.generate()
        self.keys = {'test': self.key.public_key().public_bytes(serialization.Encoding.Raw,
                                                               serialization.PublicFormat.Raw)}
        self.release, self.package = recovery_fixtures.RecoveryTests.release(self, '1.0.2', b'target', 'b')
        self.guard = patch.object(migration, 'legacy_writer_guard', side_effect=lambda *args: nullcontext())
        self.guard.start(); self.addCleanup(self.guard.stop)
        model_patch = patch('tempo_model_catalog.installed_model_dir', return_value=self.profile / 'models' / 'repository')
        model_patch.start(); self.addCleanup(model_patch.stop)
        preference_error = patch('tempo_config.tempo_preferences.load_error', '')
        preference_error.start(); self.addCleanup(preference_error.stop)
        self.draft = self.put(self.legacy, '_internal/datasets/workspaces/one/draft.json', b'{"sync_pending":true}')
        self.put(self.legacy, '_internal/datasets/workspaces/two/image.png', b'image bytes')
        self.put(self.legacy, '_internal/yolo26n-scout.pt', b'legacy model')

    def put(self, root, name, value):
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(value)
        return path

    def prepare(self, **kwargs):
        options = dict(trusted_keys=self.keys, registry=self.old_registry, supported=self.supported)
        options.update(kwargs)
        return migration.prepare_legacy_migration(self.legacy, self.profile, self.installer,
            self.release, self.package, self.target, **options)

    def finish(self, **kwargs):
        options = dict(trusted_keys=self.keys, registry=self.empty_registry, supported=self.supported)
        options.update(kwargs)
        return migration.finish_legacy_migration(self.profile, **options)

    def retire(self):
        self.old_exe.unlink()
        (self.legacy / '_internal/assets/build-provenance.json').unlink()

    def test_preparation_retirement_and_publication_preserve_all_work(self):
        settings = self.put(self.profile, 'config/setup_state.json', b'{"stage":"complete"}')
        external = self.put(self.root, 'recordings/scientific.tif', b'research outside the app')
        first = self.prepare()
        self.assertEqual(first['status'], 'prepared')
        self.assertTrue(self.old_exe.exists())
        self.assertFalse((self.profile / 'config/collaboration-location.json').exists())
        self.assertEqual(self.prepare(), first)
        with self.assertRaisesRegex(ValueError, 'still registered'):
            self.finish(registry=self.old_registry)
        self.retire()
        self.assertEqual(self.finish()['status'], 'complete')
        self.assertEqual((self.profile / 'collaboration/workspaces/one/draft.json').read_bytes(), self.draft.read_bytes())
        self.assertEqual((self.profile / 'models/repository/yolo26n-scout.pt').read_bytes(), b'legacy model')
        self.assertEqual(settings.read_bytes(), b'{"stage":"complete"}')
        self.assertEqual(external.read_bytes(), b'research outside the app')
        folder = migration._transaction(self.profile)
        self.assertEqual((folder / 'legacy-installer.exe').read_bytes(), b'reviewed installer')
        self.assertTrue((folder / 'target.nupkg').is_file())
        # Normal edits after successful migration do not make completion non-idempotent.
        (self.profile / 'collaboration/workspaces/one/draft.json').write_bytes(b'new work')
        self.assertTrue(self.finish()['already_published'])
        result = bootstrap_packaged_storage(self.bundle, self.profile, registry=self.empty_registry,
            legacy_default_root=self.legacy, model_root=self.profile / 'models/repository')
        self.assertTrue(result['already_published'])

    def test_new_executable_or_registration_still_blocks_finish(self):
        self.prepare()
        with self.assertRaisesRegex(ValueError, 'executable is still present'):
            self.finish()
        self.retire()
        with self.assertRaisesRegex(ValueError, 'still registered'):
            self.finish(registry=self.old_registry)

    def test_source_edits_after_preparation_are_not_silently_lost(self):
        self.prepare(); self.retire()
        self.draft.write_bytes(b'new unsynced work')
        with self.assertRaisesRegex(ValueError, 'changed after preparation'):
            self.finish()
        self.assertEqual(self.draft.read_bytes(), b'new unsynced work')
        self.assertFalse((self.profile / 'config/collaboration-location.json').exists())

    def test_snapshot_corruption_and_incomplete_prepare_block_startup(self):
        self.prepare(); self.retire()
        folder = migration._transaction(self.profile)
        snapshot = folder / 'internal/datasets/workspaces/one/draft.json'
        snapshot.write_bytes(b'corrupt')
        with self.assertRaisesRegex(ValueError, 'snapshot changed'):
            self.finish()
        with self.assertRaisesRegex(StorageBootstrapError, 'migration is pending'):
            bootstrap_packaged_storage(self.bundle, self.profile, registry=self.empty_registry,
                legacy_default_root=self.legacy, model_root=self.profile / 'models/repository')

    def test_existing_destination_conflicts_before_uninstall(self):
        existing = self.put(self.profile, 'collaboration/workspaces/one/draft.json', b'unrelated work')
        with self.assertRaisesRegex(ValueError, 'Existing collaboration work differs'):
            self.prepare()
        self.assertTrue(self.old_exe.exists())
        self.assertEqual(existing.read_bytes(), b'unrelated work')
        self.assertFalse((self.profile / 'config/collaboration-location.json').exists())

    def test_conflicting_flat_and_internal_stores_or_models_require_review(self):
        conflicting = self.put(self.legacy, 'datasets/different.json', b'other drafts')
        with self.assertRaisesRegex(ValueError, 'stores differ'):
            self.prepare()
        conflicting.unlink(); conflicting.parent.rmdir()
        self.put(self.legacy, 'yolo26n-scout.pt', b'different model')
        with self.assertRaisesRegex(ValueError, 'different models'):
            self.prepare()

    def test_identity_installer_and_signed_target_are_verified(self):
        original = self.old_exe.read_bytes()
        self.old_exe.write_bytes(b'unreviewed executable')
        with self.assertRaisesRegex(ValueError, 'not a reviewed'):
            self.prepare()
        self.old_exe.write_bytes(original)
        self.installer.write_bytes(b'wrong installer')
        with self.assertRaisesRegex(ValueError, 'exact reviewed old installer'):
            self.prepare()
        self.installer.write_bytes(b'reviewed installer')
        self.release = replace(self.release, commit='c' * 40)
        with self.assertRaisesRegex(ValueError, 'signed envelope'):
            self.prepare()

    def test_optional_packages_nvidia_and_other_registrations_are_not_bypassed(self):
        addon = self.put(self.legacy, '_internal/.tempo-packages/test/receipt.json', b'{}')
        with self.assertRaisesRegex(ValueError, 'optional package'):
            self.prepare()
        addon.unlink(); addon.parent.rmdir(); addon.parent.parent.rmdir()
        marker = self.put(self.profile, 'nvidia-runtime/active.json', b'unchanged selection')
        with self.assertRaisesRegex(ValueError, 'NVIDIA'):
            self.prepare()
        self.assertEqual(marker.read_bytes(), b'unchanged selection')
        marker.unlink()
        with self.assertRaisesRegex(ValueError, 'Multiple or unresolved'):
            self.prepare(registry=Registry({('user', 1): str(self.legacy), ('machine', 2): str(self.root / 'other install')}))

    def test_interrupted_preparation_can_resume_without_replacing_source(self):
        original = migration.migrate_collaboration
        calls = []
        def interrupt(*args, **kwargs):
            result = original(*args, **kwargs)
            if not calls:
                calls.append(True)
                raise OSError('interrupted copy')
            return result
        with patch.object(migration, 'migrate_collaboration', side_effect=interrupt), self.assertRaises(OSError):
            self.prepare()
        folder = migration._transaction(self.profile)
        self.assertTrue((folder / 'legacy-installer.exe').exists())
        self.assertTrue(self.old_exe.exists())
        self.assertFalse((folder / 'prepared.json').exists())
        self.assertEqual(self.prepare()['status'], 'prepared')
        self.retire()
        self.assertEqual(self.finish()['status'], 'complete')

    def test_disappearing_original_files_are_safe_only_with_verified_snapshot(self):
        self.prepare(); self.retire()
        self.draft.unlink()
        self.assertEqual(self.finish()['status'], 'complete')
        self.assertEqual((self.profile / 'collaboration/workspaces/one/draft.json').read_bytes(), b'{"sync_pending":true}')

    def test_verified_absolute_dataset_paths_remap_and_external_paths_stay(self):
        source = self.legacy / '_internal/datasets'
        image = source / 'workspaces/two/image.png'
        external = str(self.root / 'recordings' / 'external.tif')
        value = {'image_path': str(image), 'external_path': external, 'score': float('nan')}
        metadata = self.put(source, 'manifest.json', json.dumps(value).encode())
        raw = metadata.read_bytes()
        compiled = source / 'compiled/cells'
        yaml = self.put(compiled, 'data.yaml', ('path: ' + json.dumps(str(compiled)) + '\ntrain: images/train\n').encode())
        self.prepare(); self.retire(); self.finish()
        published = json.loads((self.profile / 'collaboration/manifest.json').read_bytes())
        self.assertEqual(published['image_path'], str(self.profile / 'collaboration/workspaces/two/image.png'))
        self.assertEqual(published['external_path'], external)
        self.assertTrue(str(published['score']) == 'nan')
        self.assertEqual(metadata.read_bytes(), raw)
        self.assertIn(str(compiled), yaml.read_text())
        relocated_yaml = (self.profile / 'collaboration/compiled/cells/data.yaml').read_text()
        self.assertEqual(json.loads(relocated_yaml.splitlines()[0][5:].strip()),
                         str(self.profile / 'collaboration/compiled/cells'))

    def test_unverified_or_ambiguous_absolute_references_block_before_uninstall(self):
        data = self.legacy / '_internal/datasets'
        metadata = self.put(data, 'manifest.json', json.dumps({'path': str(data / 'missing.png')}).encode())
        with self.assertRaisesRegex(ValueError, 'no verified migrated file'):
            self.prepare()
        self.assertTrue(self.old_exe.exists())

    def test_absolute_json_identifiers_require_explicit_review(self):
        self.put(self.legacy, '_internal/datasets/manifest.json',
                 json.dumps({str(self.draft): {'id': 'absolute-key'}}).encode())
        with self.assertRaisesRegex(ValueError, 'absolute dataset paths as identifiers'):
            self.prepare()
        self.assertTrue(self.old_exe.exists())

    def test_interrupted_publication_finishes_without_losing_verified_data(self):
        self.prepare(); self.retire()
        original = migration._atomic_json
        def fail_completion(path, payload):
            if path.name == 'completed.json':
                raise OSError('completion write interrupted')
            return original(path, payload)
        with patch.object(migration, '_atomic_json', side_effect=fail_completion), self.assertRaises(OSError):
            self.finish()
        self.assertTrue((self.profile / 'config/collaboration-location.json').exists())
        with self.assertRaisesRegex(StorageBootstrapError, 'migration is pending'):
            bootstrap_packaged_storage(self.bundle, self.profile, registry=self.empty_registry,
                legacy_default_root=self.legacy, model_root=self.profile / 'models/repository')
        self.assertEqual(self.finish()['status'], 'complete')

    def test_published_pointer_loss_does_not_silently_create_empty_storage(self):
        self.prepare(); self.retire(); self.finish()
        (self.profile / 'config/collaboration-location.json').unlink()
        with self.assertRaisesRegex(ValueError, 'unavailable'):
            migration.completed_migration(self.profile)
        self.assertEqual((self.profile / 'collaboration/workspaces/one/draft.json').read_bytes(), b'{"sync_pending":true}')

    def test_explicit_old_model_references_keep_their_verified_snapshot(self):
        model = self.legacy / '_internal/yolo26n-scout.pt'
        self.put(self.legacy, '_internal/datasets/job.json', json.dumps({'model_path': str(model)}).encode())
        self.prepare(); self.retire(); self.finish()
        value = json.loads((self.profile / 'collaboration/job.json').read_bytes())
        retained = Path(value['model_path'])
        self.assertEqual(retained.read_bytes(), b'legacy model')
        self.assertTrue(migration._transaction(self.profile) in retained.parents)

    def test_model_repository_changes_and_orphaned_installations_need_review(self):
        with self.assertRaisesRegex(ValueError, 'not registered'):
            self.prepare(registry=self.empty_registry)
        self.prepare(); self.retire()
        with patch('tempo_model_catalog.installed_model_dir', return_value=self.profile / 'models' / 'other-repo'):
            with self.assertRaisesRegex(ValueError, 'repository changed'):
                self.finish()
        self.assertFalse((self.profile / 'config/collaboration-location.json').exists())

    def test_busy_legacy_process_prevents_any_copy(self):
        with patch.object(migration, 'legacy_writer_guard', side_effect=BlockingIOError('still running')):
            with self.assertRaises(BlockingIOError):
                self.prepare()
        self.assertFalse((migration._transaction(self.profile) / 'migration.json').exists())
        self.assertEqual(self.draft.read_bytes(), b'{"sync_pending":true}')

    def test_prepared_offline_finish_survives_feed_expiry_but_new_prepare_does_not(self):
        with patch('tempo_update_recovery.datetime') as clock:
            clock.now.return_value = datetime(2100, 1, 1, tzinfo=timezone.utc)
            with self.assertRaisesRegex(ValueError, 'Expired'):
                self.prepare()
        self.assertTrue(self.old_exe.exists())
        self.prepare(); self.retire()
        with patch('tempo_update_recovery.datetime') as clock:
            clock.now.return_value = datetime(2100, 1, 1, tzinfo=timezone.utc)
            clock.side_effect = lambda *args, **kwargs: datetime(*args, **kwargs)
            self.assertEqual(self.finish()['status'], 'complete')


if __name__ == '__main__':
    unittest.main()
