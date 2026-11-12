"""Storage publication requires exclusion of uncooperative legacy installations."""
from contextlib import nullcontext
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tempo_update_bootstrap import (StorageBootstrapError, bootstrap_packaged_storage,
                                    find_registered_legacy)
from tempo_legacy_migration import legacy_writer_guard
from tempo_update_storage import collaboration_base


class Registry:
    HKEY_CURRENT_USER = 'user'
    HKEY_LOCAL_MACHINE = 'machine'
    KEY_WOW64_32KEY = 1
    KEY_WOW64_64KEY = 2
    KEY_READ = 4

    def __init__(self, entries=None, denied=()):
        self.entries = entries or {}
        self.denied = set(denied)
        self.visited = []

    def OpenKey(self, hive, key, reserved, access):
        identity = hive, access & 3
        self.visited.append((identity, key))
        if identity in self.denied:
            raise PermissionError('denied')
        if identity not in self.entries:
            raise FileNotFoundError()
        return Key(self.entries[identity])

    def QueryValueEx(self, key, name):
        if key.location is None:
            raise FileNotFoundError()
        return key.location, 1


class Key:
    def __init__(self, location):
        self.location = location
    def __enter__(self):
        return self
    def __exit__(self, *args):
        pass


class BootstrapTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir='/private/tmp' if Path('/private/tmp').exists() else None)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.bundle = self.root / 'current'
        self.profile = self.root / 'TEMPO'
        self.legacy = self.root / 'Programs' / 'TEMPO'
        self.bundle.mkdir()

    def run_bootstrap(self, registry=None):
        return bootstrap_packaged_storage(self.bundle, self.profile,
            registry=registry or Registry(), legacy_default_root=self.legacy,
            model_root=self.profile / 'models' / 'repository')

    def test_fresh_and_existing_bundle_store_publish_stably_and_repeat(self):
        source = self.bundle / 'datasets' / 'workspace'
        source.mkdir(parents=True)
        (source / 'draft.json').write_text('original')
        result = self.run_bootstrap()
        self.assertEqual(result['status'], 'complete')
        self.assertEqual((self.profile / 'collaboration/workspace/draft.json').read_text(), 'original')
        self.assertEqual((source / 'draft.json').read_text(), 'original')
        self.assertEqual(collaboration_base(self.bundle, self.profile), self.profile / 'collaboration')
        self.assertTrue(self.run_bootstrap()['already_published'])

    def test_every_registry_view_and_custom_install_blocks_even_after_publication(self):
        registry = Registry()
        self.run_bootstrap(registry)
        self.assertEqual(len(registry.visited), 4)
        for hive in ('user', 'machine'):
            for view in (1, 2):
                with self.subTest(hive=hive, view=view):
                    registry = Registry({(hive, view): r'D:\Custom TEMPO'})
                    with self.assertRaisesRegex(StorageBootstrapError, 'older TEMPO installation'):
                        self.run_bootstrap(registry)
        self.assertEqual(len(find_registered_legacy(Registry({('user', 1): None}))), 1)

    def test_inaccessible_registry_fails_closed(self):
        with self.assertRaisesRegex(StorageBootstrapError, 'registry access'):
            self.run_bootstrap(Registry(denied={('machine', 2)}))
        self.assertFalse((self.profile / 'config/collaboration-location.json').exists())

    def test_unregistered_data_is_copied_and_published_in_both_legacy_layouts(self):
        for prefix in ('', '_internal'):
            with self.subTest(prefix=prefix), tempfile.TemporaryDirectory(dir=self.root) as folder:
                profile = Path(folder) / 'TEMPO'
                legacy = Path(folder) / 'Programs' / 'TEMPO'
                data = legacy / prefix / 'datasets/workspace/draft.json'
                data.parent.mkdir(parents=True)
                data.write_text('unsynced legacy draft')
                model = legacy / prefix / 'cellpose4_cardio.pt'
                model.write_bytes(b'unknown model bytes')
                model_root = profile / 'models'
                guard_impl = legacy_writer_guard if os.name == 'nt' else lambda _: nullcontext()
                with patch('tempo_legacy_migration.legacy_writer_guard', side_effect=guard_impl) as guard:
                    result = bootstrap_packaged_storage(self.bundle, profile, registry=Registry(),
                        legacy_default_root=legacy, model_root=model_root)
                    self.assertEqual(result['pointer']['sources'], [str(data.parent.parent)])
                    again = bootstrap_packaged_storage(self.bundle, profile, registry=Registry(),
                        legacy_default_root=legacy, model_root=model_root)
                    self.assertTrue(again['already_published'])
                    guard.assert_called_once_with(legacy)
                copy = profile / 'collaboration/workspace/draft.json'
                self.assertEqual(copy.read_text(), 'unsynced legacy draft')
                self.assertEqual(data.read_text(), copy.read_text())
                self.assertEqual(model.read_bytes(), b'unknown model bytes')
                self.assertFalse((model_root / model.name).exists())

    def test_leftover_model_alone_does_not_block_startup_or_become_active(self):
        model = self.legacy / '_internal' / 'cellpose4_cardio.pt'
        model.parent.mkdir(parents=True)
        model.write_bytes(b'unknown model bytes')
        self.assertEqual(self.run_bootstrap()['status'], 'complete')
        self.assertEqual(model.read_bytes(), b'unknown model bytes')
        self.assertFalse((self.profile / 'models/repository' / model.name).exists())

    def test_leftover_executable_or_active_writer_still_blocks(self):
        data = self.legacy / 'datasets/draft.json'
        data.parent.mkdir(parents=True)
        data.write_text('draft')
        executable = self.legacy / 'TEMPO.exe'
        executable.write_bytes(b'orphaned application')
        with self.assertRaisesRegex(StorageBootstrapError, 'unregistered installation'):
            self.run_bootstrap()
        self.assertEqual(executable.read_bytes(), b'orphaned application')
        executable.unlink()
        with patch('tempo_legacy_migration.legacy_writer_guard', side_effect=BlockingIOError('Old app is running')):
            with self.assertRaisesRegex(StorageBootstrapError, 'Old app is running'):
                self.run_bootstrap()
        self.assertEqual(data.read_text(), 'draft')
        self.assertFalse((self.profile / 'config/collaboration-location.json').exists())

    def test_leftover_data_does_not_replace_an_already_published_store(self):
        self.run_bootstrap()
        current = self.profile / 'collaboration/draft.json'
        current.write_text('current draft')
        leftover = self.legacy / 'datasets/draft.json'
        leftover.parent.mkdir(parents=True)
        leftover.write_text('older draft')
        with patch('tempo_legacy_migration.legacy_writer_guard', return_value=nullcontext()):
            with self.assertRaisesRegex(StorageBootstrapError, 'additional older datasets'):
                self.run_bootstrap()
        self.assertEqual(current.read_text(), 'current draft')
        self.assertEqual(leftover.read_text(), 'older draft')

    def test_models_are_verified_and_preserved_without_overwriting(self):
        source = self.bundle / 'yolo26n-scout.pt'
        source.write_bytes(b'local model')
        result = self.run_bootstrap()
        import hashlib
        self.assertEqual(result['models']['yolo26n-scout.pt'], hashlib.sha256(b'local model').hexdigest())
        target = self.profile / 'models/repository/yolo26n-scout.pt'
        self.assertEqual(target.read_bytes(), b'local model')
        self.assertEqual(source.read_bytes(), b'local model')
        target.write_bytes(b'different installed model')
        result = self.run_bootstrap()
        self.assertEqual(result['models']['yolo26n-scout.pt'], hashlib.sha256(b'different installed model').hexdigest())
        self.assertEqual(target.read_bytes(), b'different installed model')
        source.unlink()
        result = self.run_bootstrap()
        self.assertIn('yolo26n-scout.pt', result['models'])

    def test_linked_stable_model_is_rejected(self):
        target = self.profile / 'models/repository/yolo26n-scout.pt'
        target.parent.mkdir(parents=True)
        real = self.root / 'external-model'
        real.write_bytes(b'model')
        try:
            target.symlink_to(real)
        except (OSError, NotImplementedError):
            self.skipTest('Symbolic links are unavailable')
        with self.assertRaisesRegex(StorageBootstrapError, 'Link or reparse point'):
            self.run_bootstrap()
        self.assertEqual(real.read_bytes(), b'model')

    def test_destination_conflict_does_not_publish_or_overwrite(self):
        source = self.bundle / 'datasets'
        source.mkdir()
        (source / 'draft').write_text('source')
        destination = self.profile / 'collaboration'
        destination.mkdir(parents=True)
        (destination / 'draft').write_text('destination')
        with self.assertRaisesRegex(StorageBootstrapError, 'storage migration'):
            self.run_bootstrap()
        self.assertEqual((source / 'draft').read_text(), 'source')
        self.assertEqual((destination / 'draft').read_text(), 'destination')
        self.assertFalse((self.profile / 'config/collaboration-location.json').exists())


if __name__ == '__main__':
    unittest.main()
