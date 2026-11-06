"""Storage publication requires exclusion of uncooperative legacy installations."""
import tempfile
import unittest
from pathlib import Path

from tempo_update_bootstrap import (StorageBootstrapError, bootstrap_packaged_storage,
                                    find_registered_legacy)
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

    def test_unregistered_default_data_blocks_without_changes(self):
        data = self.legacy / 'datasets' / 'draft.json'
        data.parent.mkdir(parents=True)
        data.write_text('legacy')
        with self.assertRaisesRegex(StorageBootstrapError, 'origin and active writers'):
            self.run_bootstrap()
        self.assertEqual(data.read_text(), 'legacy')
        self.assertFalse((self.profile / 'config/collaboration-location.json').exists())

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
