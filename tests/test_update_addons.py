"""Local receipts cannot authorize mutation or erase experimental recovery data."""
import copy
import hashlib
import json
from pathlib import Path
import tempfile
import shutil
import unittest
from unittest.mock import patch

import tempo_update_addons as addons
from tempo_update_migration import discover_addons


def sha(value):
    return hashlib.sha256(value).hexdigest()


class AddonCompatibilityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.install = self.root / 'old installation'
        self.profile = self.root / 'profile'
        self.package = self.install / '.tempo-packages/stimulation-investigation-1'
        self.review = copy.deepcopy(addons.REVIEWED_OVERLAY)
        self.review.update(source_sha256=sha(b'reviewed source'), provenance_sha256=sha(b'reviewed provenance'))
        self.review['files'] = [dict(path='TEMPO.exe', size=len(b'overlay'), before_sha256=sha(b'original'), after_sha256=sha(b'overlay')),
                                dict(path='_internal/firmware/investigation.h', size=len(b'header'), before_sha256=None, after_sha256=sha(b'header'))]
        self.put(self.install, 'TEMPO.exe', b'overlay')
        self.put(self.install, '_internal/firmware/investigation.h', b'header')
        self.put(self.install, '_internal/assets/build-provenance.json', b'reviewed provenance')
        self.put(self.package, 'backup/TEMPO.exe', b'original')
        self.receipt = dict(schema=1, package=self.review['package'], version=self.review['version'],
                            source_sha256=self.review['source_sha256'], state='installed', files=copy.deepcopy(self.review['files']))
        self.write_receipt()
        change = patch.object(addons, 'REVIEWED_OVERLAY', self.review)
        change.start(); self.addCleanup(change.stop)

    def put(self, root, name, data):
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return path

    def write_receipt(self):
        self.put(self.package, 'receipt.json', json.dumps(self.receipt).encode())

    def assess(self):
        return addons.assess_addons(self.install)[0]

    def test_reviewed_overlay_requires_removal_and_partial_states_are_explicit(self):
        self.assertEqual(self.assess()['status'], 'removal_required')
        self.assertFalse(self.assess()['release_compatible'])
        self.assertTrue(self.assess()['removal_available'])
        for state in ('installing', 'rolled-back', 'removing'):
            self.receipt['state'] = state
            self.write_receipt()
            self.put(self.install, 'TEMPO.exe', b'original')
            self.assertEqual(self.assess()['status'], 'removal_required')
        self.receipt['state'] = 'installed'
        self.write_receipt()
        self.assertEqual(self.assess()['status'], 'requires_review')

    def test_forged_receipt_missing_backup_or_modified_app_does_not_authorize_removal(self):
        path = self.package / 'backup/TEMPO.exe'
        path.write_bytes(b'changed backup')
        self.assertFalse(self.assess()['removal_available'])
        path.write_bytes(b'original')
        (self.install / 'TEMPO.exe').write_bytes(b'changed app')
        self.assertFalse(self.assess()['removal_available'])
        (self.install / 'TEMPO.exe').write_bytes(b'overlay')
        self.receipt['files'].append(dict(path='../outside', before_sha256=None, after_sha256='a'*64, size=1))
        self.write_receipt()
        self.assertFalse(self.assess()['removal_available'])

    def test_snapshot_retains_raw_backup_receipt_app_and_research_without_removal(self):
        research = self.put(self.root, 'experiment results/data.csv', b'unchanged experiment')
        result = addons.snapshot_addons(self.install, self.profile)
        snapshot = Path(result['snapshot'])
        self.assertEqual((snapshot / 'flat/stimulation-investigation-1/backup/TEMPO.exe').read_bytes(), b'original')
        self.assertEqual((snapshot / 'flat/stimulation-investigation-1/receipt.json').read_bytes(), (self.package / 'receipt.json').read_bytes())
        self.assertEqual((snapshot / 'application/TEMPO.exe').read_bytes(), b'overlay')
        self.assertEqual((self.install / 'TEMPO.exe').read_bytes(), b'overlay')
        self.assertEqual(research.read_bytes(), b'unchanged experiment')
        self.assertTrue((snapshot / 'package-snapshot.json').is_file())
        second = addons.snapshot_addons(self.install, self.profile)
        self.assertNotEqual(second['snapshot'], str(snapshot))
        self.assertTrue((snapshot / 'package-snapshot.json').is_file())

    def test_unknown_and_empty_receipts_are_preserved_with_review_status(self):
        self.put(self.install, '_internal/.tempo-packages/unknown/receipt.json', b'not valid json')
        result = addons.snapshot_addons(self.install, self.profile)
        snapshot = Path(result['snapshot'])
        self.assertEqual((snapshot / 'internal/unknown/receipt.json').read_bytes(), b'not valid json')
        self.assertTrue(any(item['status'] == 'requires_review' for item in result['assessment']))

    def test_verified_removal_empty_parent_does_not_block_a_restored_base(self):
        shutil.rmtree(self.package)  # Simulate the original remover's child cleanup.
        self.assertTrue(discover_addons(self.install))
        self.put(self.install, 'TEMPO.exe', b'original')
        (self.install / '_internal/firmware/investigation.h').unlink()
        self.assertEqual(discover_addons(self.install), [])
        self.assertEqual(self.assess()['status'], 'base_restored')
        self.put(self.install, '_internal/firmware/investigation.h', b'left behind')
        self.assertTrue(discover_addons(self.install))
        self.assertEqual(self.assess()['status'], 'requires_review')

    def test_links_and_overlapping_snapshot_locations_cannot_read_or_write_outside_scope(self):
        with self.assertRaisesRegex(ValueError, 'outside the installation'):
            addons.snapshot_addons(self.install, self.install / 'profile')
        outside = self.put(self.root, 'private external file', b'outside')
        try:
            (self.package / 'linked-file').symlink_to(outside)
        except (OSError, NotImplementedError):
            self.skipTest('Symbolic links are unavailable')
        with self.assertRaisesRegex(ValueError, 'Link or reparse point'):
            addons.snapshot_addons(self.install, self.profile)
        self.assertEqual(outside.read_bytes(), b'outside')

    def test_changed_recovery_material_leaves_inspectable_copies_without_completion_marker(self):
        original = addons.migrate_collaboration
        def copy_then_edit(source, destination):
            result = original(source, destination)
            (self.package / 'receipt.json').write_text('changed during snapshot')
            return result
        with patch.object(addons, 'migrate_collaboration', side_effect=copy_then_edit):
            with self.assertRaisesRegex(ValueError, 'changed during copying'):
                addons.snapshot_addons(self.install, self.profile)
        self.assertFalse(list(self.profile.rglob('package-snapshot.json')))
        self.assertTrue(list(self.profile.rglob('backup/TEMPO.exe')))
        self.assertEqual((self.install / 'TEMPO.exe').read_bytes(), b'overlay')
