import hashlib
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from scripts import prepare_velopack_updater as preparation


class PrepareUpdaterTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.source = self.root / 'upstream.rs'
        self.source.write_bytes(b'pinned upstream\n')
        self.patchfile = self.root / 'change.patch'
        self.patchfile.write_bytes(b'candidate patch\n')
        self.hashes = {'upstream.rs': hashlib.sha256(self.source.read_bytes()).hexdigest()}
        self.patch_hash = hashlib.sha256(self.patchfile.read_bytes()).hexdigest()
        self.calls = []

    def fake_git(self, root, *args):
        self.calls.append(args)
        if args[:2] == ('rev-parse', 'HEAD'):
            return preparation.COMMIT
        if args[0] == 'status':
            return ''
        if args[:2] == ('diff', '--name-only'):
            return 'upstream.rs'
        return ''

    def run_prepare(self):
        with patch.object(preparation, 'SOURCE_SHA256', self.hashes), \
             patch.object(preparation, 'PATCH_SHA256', self.patch_hash), \
             patch.object(preparation, 'git', side_effect=self.fake_git):
            return preparation.prepare(self.root, self.patchfile)

    def test_checks_before_applying_patch(self):
        self.assertEqual(self.run_prepare(), self.root.resolve())
        commands = [call[:2] for call in self.calls]
        self.assertLess(commands.index(('apply', '--check')), commands.index(('apply', '--whitespace=error')))

    def test_modified_source_rejected_before_apply(self):
        self.source.write_bytes(b'changed')
        with self.assertRaisesRegex(ValueError, 'source SHA-256'):
            self.run_prepare()
        self.assertFalse(any(call[0] == 'apply' for call in self.calls))

    def test_modified_patch_rejected_before_apply(self):
        self.patchfile.write_bytes(b'tampered')
        with self.assertRaisesRegex(ValueError, 'patch SHA-256'):
            self.run_prepare()
        self.assertFalse(any(call[0] == 'apply' for call in self.calls))

    def test_wrong_commit_or_dirty_checkout_rejected(self):
        for response, expected in [('wrong', 'pinned commit'), (preparation.COMMIT, 'clean')]:
            with self.subTest(expected=expected), \
                 patch.object(preparation, 'git', side_effect=[response, ' M file']):
                with self.assertRaisesRegex(ValueError, expected):
                    preparation.prepare(self.root, self.patchfile)


if __name__ == '__main__':
    unittest.main()
