import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from tempo_update_storage import collaboration_base, prepare_packaged_storage
import tempo_update_storage as storage


class StorageTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        self.bundle = self.root/'current'
        self.profile = self.root/'profile'
        self.old = self.root/'previous install'
        self.bundle.mkdir(); self.old.mkdir()
        self.pointer = self.profile/'config/collaboration-location.json'

    def put(self, root, name, data):
        path = root/name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return path

    def test_old_selection_then_copy_and_pointer_with_multiple_workspaces(self):
        a = self.put(self.old, 'datasets/workspaces/a/draft.json', b'draft')
        b = self.put(self.old, 'datasets/workspaces/b/image.png', b'image')
        model = self.put(self.old, 'yolo26n-scout.pt', b'model')
        self.assertEqual(collaboration_base(self.bundle, self.profile), self.bundle/'datasets')
        result = prepare_packaged_storage(self.bundle, self.profile, [self.old])
        self.assertEqual(result['status'], 'complete')
        self.assertIn(str(model), result['inventories'][1]['models'])
        self.assertEqual(collaboration_base(self.bundle, self.profile), self.profile/'collaboration')
        for source in (a, b):
            self.assertEqual((self.profile/'collaboration'/source.relative_to(self.old/'datasets')).read_bytes(), source.read_bytes())
        self.assertEqual(json.loads(self.pointer.read_text())['sources'], [str(self.old/'datasets')])
        self.assertTrue(prepare_packaged_storage(self.bundle, self.profile)['already_published'])

    def test_differing_stores_and_addons_block_publication(self):
        self.put(self.bundle, 'datasets/workspaces/a/draft', b'one')
        self.put(self.old, 'datasets/workspaces/a/draft', b'two')
        result = prepare_packaged_storage(self.bundle, self.profile, [self.old])
        self.assertEqual(result['status'], 'conflict')
        self.assertEqual(result['conflicts'][0]['kind'], 'different_stores')
        self.assertFalse(self.pointer.exists())
        self.assertEqual(collaboration_base(self.bundle, self.profile), self.bundle/'datasets')
        self.put(self.old, '.tempo-packages/experimental/receipt.json', b'{}')
        result = prepare_packaged_storage(self.bundle, self.profile, [self.old])
        self.assertEqual(result['conflicts'][0]['kind'], 'addon_requires_review')
        self.assertFalse(self.pointer.exists())

    def test_identical_stores_are_reused_with_distinct_receipts(self):
        for root in (self.bundle, self.old):
            self.put(root, 'datasets/workspaces/a/draft', b'one')
        result = prepare_packaged_storage(self.bundle, self.profile, [self.old])
        self.assertEqual(result['status'], 'complete')
        self.assertEqual(len(result['migrations']), 2)
        self.assertEqual(len(list((self.profile/'updates/migration-receipts').glob('*.json'))), 2)

    def test_corrupt_pointer_fails_closed(self):
        self.pointer.parent.mkdir(parents=True)
        for value in ('invalid', '{"format":"tempo-collaboration-pointer-v1","target":"../other","sources":[]}',
                      '{"format":"tempo-collaboration-pointer-v1","target":"collaboration","sources":[]}'):
            self.pointer.write_text(value)
            with self.assertRaises(ValueError):
                collaboration_base(self.bundle, self.profile)
            with self.assertRaises(ValueError):
                prepare_packaged_storage(self.bundle, self.profile)

    def test_cancel_then_resume_without_publishing_partial_copy(self):
        self.put(self.old, 'datasets/workspaces/a/draft', b'one')
        self.put(self.old, 'datasets/workspaces/b/draft', b'two')
        real = storage.migrate_collaboration
        def interrupted(*args, **kwargs):
            calls = iter([False, False, True])
            kwargs['cancel'] = lambda: next(calls, True)
            return real(*args, **kwargs)
        with patch.object(storage, 'migrate_collaboration', side_effect=interrupted):
            result = prepare_packaged_storage(self.bundle, self.profile, [self.old])
        self.assertEqual(result['status'], 'cancelled')
        self.assertFalse(self.pointer.exists())
        self.assertEqual(prepare_packaged_storage(self.bundle, self.profile, [self.old])['status'], 'complete')

    def test_existing_unrelated_destination_blocks_publication(self):
        self.put(self.old, 'datasets/workspaces/a/draft', b'one')
        self.put(self.profile, 'collaboration/workspaces/b/draft', b'unrelated')
        result = prepare_packaged_storage(self.bundle, self.profile, [self.old])
        self.assertEqual(result['status'], 'conflict')
        self.assertEqual(result['conflicts'][0]['kind'], 'destination_differs')
        self.assertFalse(self.pointer.exists())
        self.assertEqual((self.profile/'collaboration/workspaces/b/draft').read_bytes(), b'unrelated')


if __name__ == '__main__':
    unittest.main()
