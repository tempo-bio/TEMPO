"""Concurrent local sessions merge safe changes and preserve real conflicts."""
import copy
import json
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from tempo_annotation_store import update_session, read_session, AnnotationSessionConflict


def record(value):
    return {'status': 'locked', 'boxes': [{'class_id': 1, 'box': [value, 2, 3, 4]}]}


class SessionMergeTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name)/'local_session.json'
        self.base = update_session(self.path, lambda _: dict(
            collaborator_id='hf:alice', dataset_repo='synthetic/data',
            claimed_images=['a.png', 'b.png'], annotations={'a.png':record(1), 'b.png':record(1)}))

    def save(self, proposed, base=None):
        baseline = self.base if base is None else base
        return update_session(self.path, lambda current: dict(current, **proposed),
                              expected_revision=baseline['session_revision'], base_session=baseline)

    def test_disjoint_edits_and_independent_claims_are_retained(self):
        update_session(self.path, lambda current: dict(current,
            annotations={'a.png':record(1), 'b.png':record(9), 'c.png':record(3)},
            claimed_images=['a.png','b.png','c.png'], preserved_metadata='kept'))
        result = self.save(dict(annotations={'a.png':record(5),'b.png':record(1)}, claimed_images=['a.png']))
        self.assertEqual(result['annotations'], {'a.png':record(5),'b.png':record(9),'c.png':record(3)})
        self.assertEqual(result['claimed_images'], ['a.png','c.png'])
        self.assertEqual(result['preserved_metadata'], 'kept')

    def test_same_image_divergence_never_overwrites_either_file(self):
        update_session(self.path, lambda current: dict(current, annotations={'a.png':record(9),'b.png':record(1)}))
        before = self.path.read_bytes()
        with self.assertRaises(AnnotationSessionConflict):
            self.save(dict(annotations={'a.png':record(5),'b.png':record(1)}))
        self.assertEqual(self.path.read_bytes(), before)

    def test_convergent_edits_are_safe_and_disk_deletions_are_not_resurrected(self):
        update_session(self.path, lambda current: dict(current, annotations={'a.png':record(5)}))
        result = self.save(dict(annotations={'a.png':record(5),'b.png':record(1)}))
        self.assertEqual(result['annotations'], {'a.png':record(5)})

    def test_account_and_dataset_switches_cannot_be_silently_merged(self):
        for changed in (dict(collaborator_id='hf:bob'), dict(dataset_repo='different/data')):
            with self.subTest(changed=changed):
                update_session(self.path, lambda current: dict(self.base, **changed))
                before = self.path.read_bytes()
                with self.assertRaises(AnnotationSessionConflict):
                    self.save(dict(self.base))
                self.assertEqual(self.path.read_bytes(), before)

    def test_rejected_stale_revision_does_not_invoke_transform_without_baseline(self):
        from unittest.mock import Mock
        transform = Mock()
        with self.assertRaises(AnnotationSessionConflict):
            update_session(self.path, transform, expected_revision=0)
        transform.assert_not_called()

    def test_mutating_transform_cannot_replace_the_saved_merge_input(self):
        update_session(self.path, lambda current: dict(current, annotations={'a.png':record(9)}))
        before = self.path.read_bytes()
        def edit(current):
            current['annotations']['a.png'] = record(5)
            return current
        with self.assertRaises(AnnotationSessionConflict):
            update_session(self.path, edit, expected_revision=self.base['session_revision'], base_session=self.base)
        self.assertEqual(self.path.read_bytes(), before)


class AnnotationRecoveryUiTests(unittest.TestCase):
    def setUp(self):
        from tests.test_desktop_workflows import DesktopTests
        self.fixture = DesktopTests('test_release_syncs_draft_before_unlocking_and_preserves_annotations')
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.view, self.root = self.fixture.view, self.fixture.root
        self.view.refresh_universal_file_list()
        self.view.on_file_selected(self.view.list_files.item(0))
        self.view.save_universal_annotations()

    def change_disk(self, **changes):
        return update_session(self.view.session_file, lambda current: dict(current, **changes))

    def conflict(self):
        local = copy.deepcopy(self.view.universal_annotations)
        local['a.png']['boxes'][0]['class_id'] = 0
        self.view.universal_annotations = local
        saved = copy.deepcopy(read_session(self.view.session_file)['annotations'])
        saved['a.png']['boxes'][0]['polygon'][0] = [11, 11]
        self.change_disk(annotations=saved)
        with self.assertRaises(AnnotationSessionConflict):
            self.view.save_universal_annotations()
        return local, saved

    def test_stale_revision_with_no_conflicting_edits_can_release_directly(self):
        import tempo_gui as gui
        saved = copy.deepcopy(self.view.universal_annotations)
        saved['b.png'] = record(3)
        self.change_disk(annotations=saved, claimed_images=['a.png','b.png'])
        with patch.object(gui.tempo_hub, 'sync_annotation_drafts'), \
             patch.object(gui.tempo_hub, 'release_claim') as release, \
             patch.object(self.view.shared_browser, 'load'):
            self.view.release_current_lease()
        release.assert_called_once()
        self.assertIn('b.png', read_session(self.view.session_file)['annotations'])
        self.assertEqual(read_session(self.view.session_file)['claimed_images'], ['b.png'])
        self.assertFalse(self.view._annotation_load_failed)

    def test_conflict_reveals_real_reload_action_and_backups_are_restorable(self):
        local, saved = self.conflict()
        self.assertFalse(self.view.btn_retry_annotation_load.isHidden())
        self.assertEqual(self.view.btn_retry_annotation_load.text(), 'Reload Saved Annotations')
        self.assertIn('Reload Saved Annotations', self.view.annotation_load_error.summary.text())
        with patch.object(self.view, 'refresh_universal_file_list'):
            self.view.retry_annotation_load()
        self.assertEqual(self.view.universal_annotations, saved)
        backups = list(self.root.glob('auto_annotation_backup_*.json'))
        self.assertEqual(len(backups), 1)
        self.assertEqual(json.loads(backups[0].read_text()), local)
        self.assertIn('Restore Backup', self.view.restore_status.text())
        self.assertFalse(self.view._annotation_load_failed)
        self.assertTrue(self.view.btn_retry_annotation_load.isHidden())

    def test_backup_failure_keeps_in_memory_edits_and_saved_session_untouched(self):
        local, _ = self.conflict()
        before = Path(self.view.session_file).read_bytes()
        with patch('tempo_gui.atomic_bytes', side_effect=PermissionError('No space')):
            self.view.retry_annotation_load()
        self.assertEqual(self.view.universal_annotations, local)
        self.assertEqual(Path(self.view.session_file).read_bytes(), before)
        self.assertIn('could not be backed up', self.view.annotation_load_error.summary.text())

    def test_reload_backup_can_be_restored_through_the_existing_action(self):
        import tempo_gui as gui
        local, _ = self.conflict()
        self.view.retry_annotation_load()
        backup = next(self.root.glob('auto_annotation_backup_*.json'))
        with patch.object(gui.QFileDialog, 'getOpenFileName', return_value=(str(backup), '')):
            self.view.restore_annotation_backup()
        self.assertEqual(self.view.get_current_boxes(), local['a.png']['boxes'])
        self.assertEqual(read_session(self.view.session_file)['annotations']['a.png']['boxes'],
                         local['a.png']['boxes'])
        self.assertEqual(self.view.restore_status.text(), 'Annotations restored.')

    def test_normalized_baseline_and_pixel_gui_do_not_create_false_conflict(self):
        from tempo_annotations import normalize_record
        normalized = {'a.png': normalize_record(self.view.universal_annotations['a.png'], 100, 100)}
        self.change_disk(annotations=normalized)
        self.assertTrue(self.view.load_universal_annotations())
        self.change_disk(last_updated='another operation')
        self.view.save_universal_annotations()
        self.assertFalse(getattr(self.view, '_annotation_session_conflict', False))
        self.assertEqual(self.view.universal_annotations['a.png']['coordinate_space'], 'pixels')

    def test_safely_adopted_current_image_edits_refresh_canvas_and_list(self):
        saved = copy.deepcopy(read_session(self.view.session_file)['annotations'])
        saved['a.png']['boxes'].append({'class_id': 0, 'polygon': [[30,30],[40,30],[40,40]]})
        self.change_disk(annotations=saved)
        self.view.save_universal_annotations()
        self.assertEqual(len(self.view.get_current_boxes()), 2)
        self.assertEqual(self.view.list_boxes.count(), 2)
        self.assertEqual(len(self.view.current_rect_items), 2)
        self.assertIn('healthy', self.view.list_boxes.item(1).text())
