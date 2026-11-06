import json
from pathlib import Path
import tempfile
import unittest

from tempo_update_migration import discover_addons, inventory_legacy, migrate_collaboration


class MigrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.old = self.root / 'install' / 'datasets'
        self.new = self.root / 'profile' / 'collaboration'
        self.old.mkdir(parents=True)
        self.receipt = self.root / 'profile' / 'migration.json'

    def put(self, root, name, content):
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        return path

    def test_copy_repeat_custom_workspace_and_preserve_original(self):
        original = self.put(self.old, 'workspaces/custom/universal_images/draft.json', b'important')
        self.put(self.old, 'universal_images/image.png', b'image')
        first = migrate_collaboration(self.old, self.new, receipt_path=self.receipt)
        second = migrate_collaboration(self.old, self.new, receipt_path=self.receipt)
        self.assertEqual(first['status'], 'complete')
        self.assertEqual(first['files'], second['files'])
        self.assertEqual(original.read_bytes(), b'important')
        self.assertEqual((self.new / original.relative_to(self.old)).read_bytes(), b'important')
        self.assertEqual(json.loads(self.receipt.read_text())['status'], 'complete')

    def test_conflicting_destination_never_overwritten_and_retry(self):
        original = self.put(self.old, 'workspaces/a/draft', b'old')
        destination = self.put(self.new, 'workspaces/a/draft', b'new')
        result = migrate_collaboration(self.old, self.new, receipt_path=self.receipt)
        self.assertEqual(result['status'], 'conflict')
        self.assertEqual(result['conflicts'], ['workspaces/a/draft'])
        self.assertEqual((original.read_bytes(), destination.read_bytes()), (b'old', b'new'))
        destination.write_bytes(b'old')
        self.assertEqual(migrate_collaboration(self.old, self.new, receipt_path=self.receipt)['status'], 'complete')

    def test_cancel_and_resume(self):
        self.put(self.old, 'a', b'a')
        self.put(self.old, 'b', b'b')
        calls = iter([False, False, True])
        result = migrate_collaboration(self.old, self.new, receipt_path=self.receipt, cancel=lambda: next(calls, True))
        self.assertEqual(result['status'], 'cancelled')
        self.assertEqual(migrate_collaboration(self.old, self.new, receipt_path=self.receipt)['status'], 'complete')
        self.assertEqual((self.new / 'b').read_bytes(), b'b')

    def test_reject_links_and_overlapping_roots(self):
        self.put(self.root, 'outside/secret', b'secret')
        (self.old / 'link').symlink_to(self.root / 'outside', target_is_directory=True)
        with self.assertRaises(ValueError):
            migrate_collaboration(self.old, self.new)
        self.assertFalse((self.new / 'link' / 'secret').exists())
        with self.assertRaises(ValueError):
            migrate_collaboration(self.old, self.old / 'nested')

    def test_inventory_addon_and_model(self):
        install = self.old.parent
        self.put(install, 'yolo26n-scout.pt', b'model')
        self.put(install, '.tempo-packages/overlay/receipt.json', b'{}')
        found = inventory_legacy(install)
        self.assertEqual(found['datasets'], str(self.old))
        self.assertEqual(len(found['models']), 1)
        self.assertEqual(found['addons'][0]['compatibility'], 'requires_review')
        self.assertEqual(found['addons'], discover_addons(install))

    def test_reject_unrelated_receipt(self):
        self.receipt.parent.mkdir(parents=True)
        self.receipt.write_text('{"other": true}')
        with self.assertRaises(ValueError):
            migrate_collaboration(self.old, self.new, receipt_path=self.receipt)
        self.assertEqual(json.loads(self.receipt.read_text()), {'other': True})

if __name__ == '__main__':
    unittest.main()
