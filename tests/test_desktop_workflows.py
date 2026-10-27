"""Offscreen desktop workflow checks; isolate all files and cloud operations."""
import os
os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
import json
import copy
import tempfile
from pathlib import Path
import unittest
from unittest.mock import patch, MagicMock
from PyQt6.QtWidgets import QApplication
from PyQt6.QtGui import QImage
from PyQt6.QtCore import QPointF, Qt
from PyQt6.QtTest import QTest
APP=QApplication.instance() or QApplication([])
with patch('platform.machine',return_value='x86_64'):
    import tempo_gui
import annotation_review_dialog as review
import update_wizard
from tempo_hub import TempoHub
from tempo_tasks import run_task, dispose_closed_views


def close_fixture(view):
    view.close()
    dispose_closed_views(view)


class DesktopTests(unittest.TestCase):
    def test_unreadable_image_directory_preserves_current_workspace(self):
        self.view.refresh_universal_file_list(refresh=False)
        previous_dir = self.view.current_dir
        with patch.object(tempo_gui.QFileDialog, 'getExistingDirectory', return_value='/unavailable'), \
             patch.object(tempo_gui.os, 'listdir', side_effect=PermissionError('access denied')):
            self.view.load_directory()
        self.assertEqual(self.view.current_dir, previous_dir)
        self.assertEqual(self.view.list_files.count(), 1)
        self.assertIn('access denied', self.view.file_error.details.toPlainText())

    def test_invalid_annotations_preserve_workspace_and_source(self):
        self.view.refresh_universal_file_list(refresh=False)
        previous_dir = self.view.current_dir
        previous_annotations = copy.deepcopy(self.view.annotations)
        directory = self.root / 'invalid-import'
        directory.mkdir()
        path = directory / 'annotations.json'
        for content in ('{broken', '[]', '{"a.png": [null]}'):
            path.write_text(content)
            with patch.object(tempo_gui.QFileDialog, 'getExistingDirectory', return_value=str(directory)):
                self.view.load_directory()
            self.assertEqual(self.view.current_dir, previous_dir)
            self.assertEqual(self.view.annotations, previous_annotations)
            self.assertEqual(self.view.list_files.count(), 1)
            self.assertEqual(path.read_text(), content)
            self.assertFalse(self.view.file_error.isHidden())

    def test_local_annotation_reader_retains_legacy_and_polygon_shapes(self):
        directory = self.root / 'legacy-import'
        directory.mkdir()
        polygon = {'class_id': 1, 'polygon': [[1, 2], [3, 4], [5, 2]]}
        (directory / 'annotations.json').write_text(json.dumps({
            'a.png': [[1, 2, 3, 4], polygon], 'b.png': []}))
        loaded = self.view.read_local_annotations(str(directory))
        self.assertEqual(loaded[str(directory / 'a.png')], [
            {'class_id': 0, 'box': [1, 2, 3, 4]}, polygon])
        self.assertEqual(loaded[str(directory / 'b.png')], [])

    def test_import_duplicate_basenames_preserves_both_images(self):
        sources = []
        for folder, payload in [('one', b'first'), ('two', b'second')]:
            directory = self.root / folder
            directory.mkdir()
            source = directory / 'same.png'
            source.write_bytes(payload)
            sources.append(str(source))
        dialog = MagicMock()
        dialog.exec.return_value = True
        dialog.get_data.return_value = (10, 'A1')
        with patch.object(tempo_gui.QFileDialog, 'getOpenFileNames', return_value=(sources, '')),              patch.object(tempo_gui, 'UniversalImportDialog', return_value=dialog),              patch.object(self.view, 'refresh_universal_file_list'):
            self.view.import_universal_images()
        added = {name: record for name, record in self.view.universal_annotations.items() if name != 'a.png'}
        self.assertEqual(len(added), 2)
        self.assertEqual({(self.root / name).read_bytes() for name in added}, {b'first', b'second'})
        self.assertTrue(all(record['original_filename'] == 'same.png' for record in added.values()))

    def test_import_save_failure_has_retry_without_recopying(self):
        dialog = MagicMock()
        dialog.exec.return_value = True
        dialog.get_data.return_value = (10, 'A1')
        with patch.object(tempo_gui.QFileDialog, 'getOpenFileNames', return_value=([str(self.root / 'a.png')], '')),              patch.object(tempo_gui, 'UniversalImportDialog', return_value=dialog),              patch.object(self.view, 'save_universal_annotations', side_effect=OSError('disk full')):
            self.view.import_universal_images()
        self.assertFalse(self.view.btn_retry_import_save.isHidden())
        self.assertEqual(len(self.view.universal_annotations), 2)
        with patch.object(self.view, 'refresh_universal_file_list'):
            self.view.retry_import_save()
        self.assertTrue(self.view.btn_retry_import_save.isHidden())
        self.assertEqual(len(json.loads(Path(self.view.universal_json_path).read_text())), 2)

    def test_partial_import_failure_remains_visible_after_save_retry(self):
        dialog = MagicMock()
        dialog.exec.return_value = True
        dialog.get_data.return_value = (10, 'A1')
        sources = [str(self.root / 'a.png'), str(self.root / 'missing.png')]
        with patch.object(tempo_gui.QFileDialog, 'getOpenFileNames', return_value=(sources, '')),              patch.object(tempo_gui, 'UniversalImportDialog', return_value=dialog),              patch.object(self.view, 'save_universal_annotations', side_effect=OSError('disk full')):
            self.view.import_universal_images()
        self.assertEqual(len(self.view.universal_annotations), 2)
        with patch.object(self.view, 'refresh_universal_file_list'):
            self.view.retry_import_save()
        self.assertTrue(self.view.btn_retry_import_save.isHidden())
        self.assertIn('missing.png', self.view.file_error.details.toPlainText())
        self.assertIn('could not be copied', self.view.file_error.summary.text())

    def test_shared_annotation_load_failure_preserves_files_and_recovers(self):
        original = copy.deepcopy(self.view.universal_annotations)
        path = Path(self.view.universal_json_path)
        path.write_text('{broken')
        self.assertFalse(self.view.load_universal_annotations())
        self.assertEqual(self.view.universal_annotations, original)
        with self.assertRaises(OSError):
            self.view.save_universal_annotations()
        self.assertEqual(path.read_text(), '{broken')
        self.assertFalse(self.view.btn_import.isEnabled())
        self.assertFalse(self.view.btn_retry_annotation_load.isHidden())
        path.write_text(json.dumps(original))
        with patch.object(self.view, 'refresh_universal_file_list'):
            self.view.retry_annotation_load()
        self.assertTrue(self.view.btn_import.isEnabled())
        self.assertTrue(self.view.btn_retry_annotation_load.isHidden())
        self.view.save_universal_annotations()
        self.assertEqual(json.loads(path.read_text()), original)

    def test_shared_load_failure_blocks_edits_and_remote_submission(self):
        self.view.refresh_universal_file_list(refresh=False)
        self.view.list_files.setCurrentRow(0)
        Path(self.view.universal_json_path).write_text('{broken')
        self.assertFalse(self.view.load_universal_annotations())
        self.view.on_file_selected(self.view.list_files.item(0))
        self.assertTrue(self.view._current_file_readonly)
        self.assertFalse(self.view.btn_publish.isEnabled())
        self.assertIn('Read-only', self.view.lbl_lock_status.text())
        with patch.object(tempo_gui, 'run_task') as task:
            self.view.publish_submission()
            self.assertFalse(self.view.approve_selected_images(names=['a.png']))
        task.assert_not_called()

    def test_shared_malformed_shapes_reject_without_replacing_annotations(self):
        original = copy.deepcopy(self.view.universal_annotations)
        path = Path(self.view.universal_json_path)
        cases = [
            {'class_id': 9, 'polygon': [[0, 0], [1, 0], [0, 1]]},
            {'class_id': 1, 'polygon': [[0, 0], [1, 0]]},
            {'class_id': 1, 'polygon': [[0, 0], [1, 0], [0, float('nan')]]},
            None,
        ]
        for shape in cases:
            with self.subTest(shape=shape):
                path.write_text(json.dumps({'a.png': {'boxes': [shape]}}))
                content = path.read_bytes()
                self.assertFalse(self.view.load_universal_annotations())
                self.assertEqual(self.view.universal_annotations, original)
                self.assertEqual(path.read_bytes(), content)

    def test_manual_edit_save_failure_retains_edits_for_retry(self):
        self.view.refresh_universal_file_list(refresh=False)
        self.view.on_file_selected(self.view.list_files.item(0))
        previous = copy.deepcopy(self.view.get_current_boxes())
        with patch.object(self.view, 'save_annotations_to_disk', side_effect=[OSError('disk full'), None]):
            self.view.add_polygon([[30, 30], [40, 30], [40, 40]])
        self.assertEqual(len(self.view.get_current_boxes()), len(previous)+1)
        self.assertTrue(self.view._annotation_save_unconfirmed)
        self.assertIn('edits remain', self.view.file_error.summary.text())
        self.assertIn('disk full', self.view.file_error.details.toPlainText())
        with patch.object(tempo_gui.tempo_hub,'sync_annotation_drafts') as sync:
            self.view.sync_pending_annotations()
        sync.assert_not_called()
        with patch.object(self.view,'save_annotations_to_disk'):
            self.view.retry_manual_annotation_save()
        self.assertFalse(self.view._annotation_save_unconfirmed)
        self.assertTrue(self.view.btn_retry_annotation_save.isHidden())

    def test_remote_preview_cannot_replace_private_edits_or_reopen_approved_image(self):
        self.view.refresh_universal_file_list(refresh=False)
        owned=copy.deepcopy(self.manifest['images']['a.png'])
        saved=copy.deepcopy(self.view.universal_annotations['a.png']['boxes'])
        self.view.universal_annotations['a.png']['sync_pending']=True
        self.view.universal_annotations['a.png']['draft_owner']='Alice'
        payload=dict(name='a.png',path=str(self.root/'a.png'),record={'boxes':[]},reservation=owned,revision='one')
        self.view.show_shared_preview(payload)
        self.assertEqual(self.view.get_current_boxes(),saved)
        self.assertFalse(self.view._current_file_readonly)
        self.manifest['images']['a.png']={'status':'approved'}
        self.view.show_shared_preview(payload)
        self.assertTrue(self.view._current_file_readonly)
        self.assertEqual(self.view.universal_annotations['a.png']['boxes'],saved)
        self.manifest['images']['a.png']=owned
        payload['reservation']={'status':'approved'}
        self.view.show_shared_preview(payload)
        self.assertTrue(self.view._current_file_readonly)

    def test_delete_save_failure_keeps_edit_and_current_image(self):
        self.view.refresh_universal_file_list(refresh=False)
        self.view.on_file_selected(self.view.list_files.item(0))
        previous = copy.deepcopy(self.view.get_current_boxes())
        self.view.list_boxes.item(0).setSelected(True)
        with patch.object(self.view, 'save_annotations_to_disk', side_effect=OSError('storage unavailable')) as save:
            self.view.delete_box()
        self.assertEqual(save.call_count, 1)
        self.assertEqual(len(self.view.get_current_boxes()), len(previous)-1)
        self.assertEqual(self.view.list_boxes.count(), len(previous)-1)
        self.assertTrue(self.view._annotation_save_unconfirmed)
        self.assertIn('storage unavailable', self.view.file_error.details.toPlainText())

    def test_local_delete_failed_save_leaves_original_file_intact(self):
        self.view.is_universal_mode = False
        self.view.current_dir = str(self.root)
        self.view.current_file = str(self.root / 'a.png')
        shape = {'class_id': 1, 'polygon': [[1, 1], [20, 1], [20, 20]]}
        self.view.annotations = {self.view.current_file: [shape]}
        self.view.save_annotations_to_disk()
        original = (self.root / 'annotations.json').read_bytes()
        self.view.refresh_boxes()
        self.view.list_boxes.item(0).setSelected(True)
        with patch.object(tempo_gui, 'atomic_json', side_effect=OSError('disk full')):
            self.view.delete_box()
        self.assertEqual((self.root / 'annotations.json').read_bytes(), original)
        self.assertEqual(self.view.get_current_boxes(), [])
        self.assertTrue(self.view._annotation_save_unconfirmed)

    def test_close_after_unconfirmed_save_does_not_claim_local_copies_saved(self):
        from types import SimpleNamespace
        self.view._annotation_save_unconfirmed = True
        dashboard = SimpleNamespace(tab_maintainer=SimpleNamespace(review=None, pending_work=lambda: False), tab_annotator=self.view,
                                    tab_capture=SimpleNamespace(cleanup=MagicMock()))
        event = MagicMock()
        with patch.object(tempo_gui, 'confirm_action', return_value=False) as confirm:
            tempo_gui.TempoDashboard.closeEvent(dashboard, event)
        event.ignore.assert_called_once()
        dashboard.tab_capture.cleanup.assert_not_called()
        self.assertIn('could not be saved', confirm.call_args.args[2])
        self.assertEqual(confirm.call_args.args[3], 'Close Anyway')

    def test_identity_storage_error_clears_only_after_both_records_save(self):
        self.view.identity_error.set_error('Ownership was not saved.', OSError('disk full'))
        with patch('tempo_annotation_store.atomic_bytes', side_effect=[None, OSError('session write failed')]):
            with self.assertRaises(OSError):
                self.view.save_universal_annotations()
        self.assertFalse(self.view.identity_error.isHidden())
        self.view.save_universal_annotations()
        self.assertTrue(self.view.identity_error.isHidden())

    def test_cloud_sync_local_save_failure_retains_retry_state(self):
        record = self.view.universal_annotations['a.png']
        record['sync_pending'] = True
        self.view._sync_snapshot = {'a.png': {}}
        with patch.object(self.view, 'normalized_draft', return_value={}), \
             patch.object(self.view, 'save_universal_annotations', side_effect=OSError('disk full')):
            self.view.on_annotation_sync_finished(1, None)
        self.assertTrue(record['sync_pending'])
        self.assertFalse(self.view._sync_busy)
        self.assertIn('Cloud sync succeeded', self.view.lbl_sync_status.text())
        self.assertIn('disk full', self.view.sync_error.details.toPlainText())
        self.assertTrue(self.view._sync_timer.isActive())

    def test_sync_result_does_not_replace_submission_feedback(self):
        self.view.submission_status.setText('Select completed images before submitting.')
        self.view.on_annotation_sync_finished(None, ConnectionError('offline'))
        self.assertEqual(self.view.submission_status.text(), 'Select completed images before submitting.')
        self.assertIn('Sync failed', self.view.lbl_sync_status.text())

    def test_auto_annotation_uses_configured_overlap_threshold(self):
        from types import SimpleNamespace
        worker = tempo_gui.AutoAnnotateWorker('example.png'); errors = []
        worker.error_signal.connect(errors.append)
        def preference(category, key, default=None):
            return {'confidence_threshold': 0.25, 'nms_iou_threshold': 0.65}.get(key, default)
        with patch.object(tempo_gui.tempo_preferences, 'get', side_effect=preference), \
             patch.object(tempo_gui.platform, 'system', return_value='Darwin'), \
             patch.object(tempo_gui.subprocess, 'run', return_value=SimpleNamespace(stdout='{"success": true, "boxes": []}')) as run:
            worker.run()
        command = run.call_args.args[0]
        self.assertEqual(command[command.index('--iou') + 1], '0.65')
        self.assertFalse(errors)

    def test_windows_auto_annotation_forwards_thresholds_in_process(self):
        worker = tempo_gui.AutoAnnotateWorker('example.png'); errors = []
        worker.error_signal.connect(errors.append)
        def preference(category, key, default=None):
            return {'confidence_threshold': 0.25, 'nms_iou_threshold': 0.65}.get(key, default)
        with patch.object(tempo_gui.tempo_preferences, 'get', side_effect=preference), \
             patch.object(tempo_gui.platform, 'system', return_value='Windows'), \
             patch.object(tempo_gui.sys, 'executable', 'C:/TEMPO/python.exe'), \
             patch.object(tempo_gui, 'detect_cells_via_bridge', return_value={'success': True, 'boxes': []}) as detect, \
             patch.object(tempo_gui.subprocess, 'run') as run:
            worker.run()
        detect.assert_called_once_with('example.png', 'cellpose', conf=0.25, iou=0.65)
        run.assert_not_called()
        self.assertFalse(errors)

    def test_settings_does_not_report_failed_save_as_success(self):
        from types import SimpleNamespace
        from PyQt6.QtWidgets import QLabel
        view = tempo_gui.SettingsTab(); self.addCleanup(view.close)
        with patch.object(tempo_gui.tempo_preferences,'save_to_disk',return_value=False):
            tempo_gui.SettingsTab.save_preferences(view)
        self.assertIn('could not be saved',view.lbl_save_status.text())

    def test_loading_defaults_does_not_persist_or_reset_identity(self):
        view = tempo_gui.SettingsTab(); self.addCleanup(close_fixture, view)
        identity = view.widgets[('collaboration', 'collaborator_id')]; identity.setText('Researcher')
        fps = view.widgets[('capture', 'target_fps')]; fps.setValue(150)
        view._saved_values = view.preference_values()
        with patch('tempo_ui.confirm_action',return_value=True), patch.object(tempo_gui.tempo_preferences,'save') as save:
            view.reset_defaults()
        save.assert_not_called()
        self.assertEqual(identity.text(),'Researcher')
        self.assertEqual(fps.value(),100)
        self.assertEqual(view.lbl_save_status.text(),'Unsaved changes')

    def test_annotation_has_no_administration_or_training_controls(self):
        from PyQt6.QtWidgets import QPushButton, QScrollArea
        texts = {b.text() for b in self.view.findChildren(QPushButton)}
        self.assertNotIn('Cloud Training', texts)
        self.assertNotIn('Approve Selected Images', texts)
        self.assertNotIn('Review Submissions', texts)
        self.assertTrue(self.view.findChildren(QScrollArea))

    def test_single_completed_image_can_be_submitted(self):
        self.view.refresh_universal_file_list()
        self.view.list_files.setCurrentRow(0)
        self.view.update_submission_readiness()
        self.assertTrue(self.view.btn_publish.isEnabled())
        self.assertIn('1 image selected', self.view.lbl_readiness_badge.text())
        self.view.list_files.clearSelection()
        self.view.update_submission_readiness()
        self.assertFalse(self.view.btn_publish.isEnabled())
        self.assertNotIn('No annotated frames', self.view.lbl_readiness_badge.text())

    def test_approve_selected_sends_only_selected_image(self):
        self.view.universal_annotations['unfinished.png'] = {'boxes': []}
        self.view.refresh_universal_file_list()
        self.view.list_files.setCurrentRow(0)
        with patch.object(tempo_gui.QMessageBox, 'question', return_value=tempo_gui.QMessageBox.StandardButton.Yes), \
             patch.object(tempo_gui.tempo_hub, 'approve_local_images', return_value=1) as approve:
            self.view.approve_selected_images()
        self.assertEqual(set(approve.call_args.args[0]), {'a.png'})
        self.assertEqual(self.view.universal_annotations['a.png']['status'], 'approved')
        self.assertNotIn('status', self.view.universal_annotations['unfinished.png'])

    def test_reservation_refresh_started_before_approval_cannot_restore_editing(self):
        self.view._lease_sync_busy = False
        with patch.object(tempo_gui, 'run_background') as background:
            self.view.refresh_lease_status()
            callback = background.call_args.args[1]
            self.view._reservation_revision = 1
            self.view.lock_manifest['images']['a.png']['status'] = 'approved'
            with patch.object(self.view, 'on_lease_status_finished') as apply:
                callback({'images': {'a.png': {'status': 'locked'}}}, None)
            apply.assert_not_called()
            self.assertEqual(background.call_count, 2)
        self.assertEqual(self.view.lock_manifest['images']['a.png']['status'], 'approved')

    def test_remote_approval_locks_editor_even_when_local_save_fails(self):
        self.view.current_file = str(self.root/'a.png')
        self.view._current_file_readonly = False
        with patch.object(tempo_gui.tempo_hub, 'approve_local_images', return_value=1), \
             patch.object(self.view, 'save_universal_annotations', side_effect=OSError('disk full')):
            with self.assertRaisesRegex(OSError, 'disk full'):
                self.view.approve_selected_images(names=['a.png'])
        self.assertTrue(self.view.approval_remote_succeeded)
        self.assertTrue(self.view._current_file_readonly)
        self.assertFalse(self.view.btn_delete.isEnabled())
        self.assertFalse(self.view.btn_auto_cellpose.isEnabled())
        self.assertTrue(self.view.btn_auto_nano.isEnabled())
        self.assertEqual(self.view.universal_annotations['a.png']['status'], 'approved')
    def test_empty_canvas_disables_editing_actions(self):
        self.view.on_file_selected(None)
        self.assertFalse(self.view.btn_auto_nano.isEnabled())
        self.assertFalse(self.view.btn_restore_annotations.isEnabled())
        self.assertFalse(self.view.btn_delete.isEnabled())
        self.assertIs(self.view.center_stack.currentWidget(), self.view.canvas_hint)

    def test_switching_images_cleans_unfinished_drawing_before_scene_clear(self):
        view = self.view.view
        view.set_image(str(self.root / 'a.png'))
        view.temp_polygon_item = view.scene.addPolygon(tempo_gui.QPolygonF()) if hasattr(tempo_gui, 'QPolygonF') else view.scene.addRect(0, 0, 5, 5)
        other = self.root / 'b.png'
        image = QImage(100, 100, QImage.Format.Format_RGB32); image.fill(0); image.save(str(other))
        view.set_image(str(other))
        self.assertIsNone(view.temp_polygon_item)

    def test_shared_rows_show_status_and_annotation_count(self):
        self.view.refresh_universal_file_list(refresh=False)
        self.assertIn('Reserved by you · 1 annotation', self.view.list_files.item(0).text())

    def test_submit_does_not_silently_drop_an_ineligible_selected_image(self):
        image=QImage(100,100,QImage.Format.Format_RGB32); image.fill(0)
        image.save(str(self.root/'approved.png'))
        self.view.universal_annotations['approved.png']={'status':'approved','boxes':copy.deepcopy(self.view.universal_annotations['a.png']['boxes'])}
        self.manifest['images']['approved.png']={'status':'approved'}
        self.view.list_files.setSelectionMode(tempo_gui.QAbstractItemView.SelectionMode.ExtendedSelection)
        self.view.refresh_universal_file_list(refresh=False)
        for i in range(self.view.list_files.count()): self.view.list_files.item(i).setSelected(True)
        with patch.object(tempo_gui.tempo_hub,'upload_submission') as upload, patch.object(tempo_gui.QMessageBox,'warning') as warning:
            self.view.publish_submission()
        upload.assert_not_called()
        warning.assert_not_called()
        self.assertIn('already approved: approved.png', self.view.submission_error.details.toPlainText())
        self.assertTrue(self.view.submission_error.details.isHidden())

    def test_pending_reservation_shows_review_and_editing_separately(self):
        record=dict(TempoHub._lease('Alice'),status='submitted')
        self.view.update_reservation_status(record)
        self.assertIn('Awaiting review · editable',self.view.lbl_lock_status.text())
        self.assertIn('Reservation ends',self.view.lbl_lock_status.text())
        self.view.update_reservation_status({'status':'approved'})
        self.assertNotIn('expiry',self.view.lbl_lock_status.text())

    def browser_catalog(self):
        return {'manifest':copy.deepcopy(self.manifest),
                'records':copy.deepcopy(getattr(getattr(self,'view',None),'universal_annotations',{})),
                'revision':'fixture','dataset_repo':'fixture/data'}

    def test_shared_mode_has_one_browser_and_owned_focus_opens_editor(self):
        self.view.mode_toggle.setCurrentIndex(1)
        self.assertFalse(self.view.shared_browser.isHidden())
        self.assertTrue(self.view.list_files.isHidden())
        self.assertFalse(hasattr(self.view,'btn_checkout'))
        self.assertFalse(any(button.text()=='Your Downloaded Images'
                             for button in self.view.shared_browser.findChildren(tempo_gui.QPushButton)))
        self.view.shared_browser.images.setCurrentItem(self.view.shared_browser.items['a.png'])
        self.assertEqual(self.view.current_file,str(self.root/'a.png'))
        self.assertFalse(self.view._current_file_readonly)
        self.assertTrue(self.view.btn_auto_cellpose.isEnabled())
        self.assertEqual(self.view.selected_annotation_paths(),[])
        self.view.shared_browser.items['a.png'].setCheckState(Qt.CheckState.Checked)
        self.assertEqual(set(self.view.submission_payload()),{'a.png'})
        self.assertTrue(self.view.btn_publish.isEnabled())
        self.view.mode_toggle.setCurrentIndex(0)
        self.assertTrue(self.view.shared_browser.isHidden())
        self.assertFalse(self.view.list_files.isHidden())

    def test_shared_preview_requires_reservation_and_model_clicks_work_after_reserving(self):
        from tempo_ui import install_ui
        install_ui(APP)
        self.manifest['images']['a.png']={'status':'available'}
        self.view.lock_manifest=self.manifest
        self.view.mode_toggle.setCurrentIndex(1)
        self.view.resize(1200,850);self.view.show();APP.processEvents()
        browser=self.view.shared_browser
        browser.images.setCurrentItem(browser.items['a.png']);APP.processEvents()
        self.assertTrue(self.view._current_file_readonly)
        self.assertIn('Reserve this image',self.view.auto_availability.text())
        buttons=(self.view.btn_auto_nano,self.view.btn_auto_stardist,self.view.btn_auto_cellpose)
        with patch.object(tempo_gui,'AutoAnnotateWorker') as launch:
            for button in buttons:
                self.view.annotation_inspector_scroll.ensureWidgetVisible(button);APP.processEvents()
                self.assertFalse(button.isEnabled())
                self.assertEqual(button.cursor().shape(),Qt.CursorShape.ArrowCursor)
                QTest.mouseClick(button,Qt.MouseButton.LeftButton)
            launch.assert_not_called()
        def reserve(*args):
            self.manifest['images']['a.png']=TempoHub._lease('Alice')
            return ['a.png'],[]
        with patch.object(tempo_gui.tempo_hub,'checkout_batch',side_effect=reserve), \
             patch.object(self.view,'load_universal_annotations',return_value=True):
            QTest.mouseClick(browser.reserve_image,Qt.MouseButton.LeftButton)
        self.assertEqual(self.view.current_file,str(self.root/'a.png'))
        self.assertTrue(self.view.auto_availability.isHidden())
        for button,model in zip(buttons,('scout','stardist','cellpose')):
            self.view.annotation_inspector_scroll.ensureWidgetVisible(button);APP.processEvents()
            self.assertTrue(button.isEnabled())
            self.assertEqual(button.cursor().shape(),Qt.CursorShape.PointingHandCursor)
            worker=MagicMock();worker.wait.return_value=True
            with patch.object(tempo_gui,'AutoAnnotateWorker',return_value=worker) as launch:
                QTest.mouseClick(button,Qt.MouseButton.LeftButton)
                launch.assert_called_once_with(str(self.root/'a.png'),model_type=model,parent=self.view)
                worker.start.assert_called_once()
                self.view.on_auto_annotate_error('Fixture inference stopped')

    def test_approved_image_keeps_models_disabled_even_if_enable_is_requested(self):
        self.manifest['images']['a.png']={'status':'approved'}
        self.view.lock_manifest=self.manifest
        self.view.refresh_universal_file_list(refresh=False)
        self.view.on_file_selected(self.view.list_files.item(0))
        before=copy.deepcopy(self.view.get_current_boxes())
        self.view.set_auto_buttons_enabled(True)
        self.assertFalse(self.view.btn_auto_cellpose.isEnabled())
        self.assertEqual(self.view.auto_availability.text(),'Approved annotations are locked.')
        self.assertEqual(self.view.get_current_boxes(),before)

    def test_shared_browser_keeps_unsaved_draft_and_original_storage_error(self):
        (self.root/'b.png').write_bytes((self.root/'a.png').read_bytes())
        self.manifest['images']['b.png']={'status':'available'}
        self.view.mode_toggle.setCurrentIndex(1)
        browser=self.view.shared_browser
        browser.images.setCurrentItem(browser.items['a.png'])
        with patch.object(self.view,'save_annotations_to_disk',side_effect=OSError('disk full')):
            self.view.add_polygon([[30,30],[40,30],[40,40]])
        before=copy.deepcopy(self.view.get_current_boxes())
        browser.images.setCurrentItem(browser.items['b.png'])
        self.assertEqual(browser.focused_name,'a.png')
        self.assertEqual(self.view.current_file,str(self.root/'a.png'))
        self.assertEqual(self.view.get_current_boxes(),before)
        self.assertIn('disk full',self.view.file_error.details.toPlainText())

    def test_local_imports_remain_visible_in_single_shared_browser(self):
        self.manifest['images']={}
        self.view.lock_manifest=self.manifest
        self.view.mode_toggle.setCurrentIndex(1)
        self.assertIn('a.png',self.view.shared_browser.items)
        self.assertIn('Local image',self.view.shared_browser.items['a.png'].text())
        self.view.shared_browser.images.setCurrentItem(self.view.shared_browser.items['a.png'])
        self.assertTrue(self.view._current_file_readonly)
        self.assertIn('no shared reservation',self.view.auto_availability.text())
        self.assertTrue((self.root/'a.png').is_file())

    def test_browser_has_no_implicit_selection_and_labels_adoption(self):
        self.manifest['images']={'image10.png':{'status':'available'},'image2.png':{'status':'locked','claimed_by':'Bob','expires_at':'2000-01-01T00:00:00+00:00'}}
        with patch.object(tempo_gui.tempo_hub,'fetch_image_catalog',side_effect=self.browser_catalog), patch.object(self.view.shared_browser,'pump'):
            self.view.open_checkout_dialog()
        browser=self.view.shared_browser
        self.assertEqual(browser.checked_names(),[])
        self.assertFalse(browser.reserve_selected.isEnabled())
        names=[browser.images.item(i).data(Qt.ItemDataRole.UserRole) for i in range(browser.images.count())]
        self.assertLess(names.index('image2.png'),names.index('image10.png'))
        self.assertIn('Expired',browser.items['image2.png'].text())

    def test_failed_reservation_retains_batch_selection_and_current_image(self):
        with patch.object(tempo_gui.tempo_hub,'fetch_image_catalog',side_effect=self.browser_catalog), patch.object(self.view.shared_browser,'pump'):
            self.view.open_checkout_dialog()
        browser=self.view.shared_browser
        browser.images.item(0).setCheckState(Qt.CheckState.Checked)
        with patch.object(tempo_gui.tempo_hub,'checkout_batch',side_effect=ConnectionError('download interrupted')):
            self.view.reserve_shared_images(['a.png'])
        self.assertEqual(browser.checked_names(),['a.png'])
        self.assertTrue(browser.reserve_selected.isEnabled())
        self.assertIn('download interrupted',browser.error.details.toPlainText())

    def test_partial_reservation_opens_downloaded_image_for_editing(self):
        self.manifest['images']['b.png']={'status':'available'}
        with patch.object(tempo_gui.tempo_hub,'fetch_image_catalog',side_effect=self.browser_catalog), patch.object(self.view.shared_browser,'pump'):
            self.view.open_checkout_dialog()
            browser=self.view.shared_browser
            browser.select_first_available()
            with patch.object(tempo_gui.tempo_hub,'checkout_batch',return_value=(['a.png'],['b.png'])), patch.object(self.view,'load_universal_annotations',return_value=True):
                self.view.reserve_shared_images(['a.png','b.png'])
        self.assertEqual(self.view.current_file,str(self.root/'a.png'))
        self.assertFalse(self.view._current_file_readonly)
        self.assertTrue(self.view.btn_auto_cellpose.isEnabled())
        self.assertIn('b.png',browser.checked_names())
        self.assertIn('1 unavailable',browser.reservation_status.text())

    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        self.addCleanup(patch.stopall)
        patch.object(tempo_gui.tempo_backend,'UNIVERSAL_DATASET_DIR',self.temp.name).start()
        patch.object(tempo_gui.QMessageBox,'information',return_value=None).start()
        patch.object(tempo_gui.QMessageBox,'warning',return_value=None).start()
        patch.object(tempo_gui,'confirm_action',return_value=True).start()
        self.manifest={'images':{'a.png':TempoHub._lease('Alice')}}
        patch.object(tempo_gui.tempo_hub,'fetch_lock_manifest',side_effect=lambda **kw:self.manifest).start()
        patch.object(tempo_gui.tempo_hub,'cached_lock_manifest',side_effect=lambda:self.manifest).start()
        patch.object(tempo_gui,'run_background',side_effect=lambda action,done:done(action(),None)).start()
        patch.object(tempo_gui.tempo_hub,'get_collaborator_id',return_value='Alice').start()
        patch.object(tempo_gui.tempo_hub,'fetch_image_catalog',side_effect=lambda:DesktopTests.browser_catalog(self)).start()
        patch.object(tempo_gui.tempo_hub,'fetch_image_preview',side_effect=lambda name,catalog:dict(
            name=name,path=str(self.root/name),record=catalog['records'].get(name,{'boxes':[]}),
            reservation=catalog['manifest']['images'][name],revision=catalog['revision'])).start()
        image=QImage(100,100,QImage.Format.Format_RGB32); image.fill(0); image.save(str(self.root/'a.png'))
        self.view=tempo_gui.AnnotatorTab(); self.addCleanup(close_fixture,self.view)
        self.view.is_universal_mode=True
        self.view.universal_annotations={'a.png':{'status':'locked','boxes':[{'class_id':1,'polygon':[[10,10],[20,10],[20,20],[10,20]]}]}}
    def begin_auto_annotation(self):
        self.view.refresh_universal_file_list()
        self.view.on_file_selected(self.view.list_files.item(0))
        self.view.save_annotations_to_disk()
        worker = MagicMock()
        worker.isRunning.return_value = False
        patch.object(tempo_gui, 'AutoAnnotateWorker', return_value=worker).start()
        patch.object(tempo_gui.CustomPopup, 'exec', return_value=None).start()
        self.view.start_auto_annotation()
        return worker

    def auto_result(self):
        return {'success': True, 'boxes': [
            {'class_id': 0, 'polygon': [[30,30],[40,30],[40,40]]},
            {'class_id': 1, 'polygon': [[50,50],[60,50],[60,60]]},
        ]}

    def test_native_outlines_are_neutral_until_explicitly_classified(self):
        self.begin_auto_annotation()
        result = self.auto_result()
        for box in result['boxes']:
            box['class_id'] = 0
            box['requires_viability_review'] = True
        self.view.on_auto_annotate_finished(result)
        self.view.list_files.item(0).setSelected(True)
        self.view.update_submission_readiness()
        self.assertIn('unclassified', self.view.list_boxes.item(0).text())
        self.assertEqual(self.view.count_labels['healthy'].text(), 'healthy: 0')
        self.assertEqual(self.view.unclassified_count.text(), 'unclassified: 2')
        self.assertEqual(self.view.current_rect_items[0].brush().color().getRgb()[:3], (230, 170, 30))
        self.assertFalse(self.view.btn_publish.isEnabled())
        for row in (0, 1):
            self.view.list_boxes.item(row).setSelected(True)
        self.view.btn_mark_dead.click()
        self.assertTrue(all(b['class_id'] == 1 and not b.get('requires_viability_review') for b in self.view.get_current_boxes()))
        self.assertEqual(self.view.count_labels['dead'].text(), 'dead: 2')
        self.assertTrue(self.view.btn_publish.isEnabled())

    def test_release_syncs_draft_before_unlocking_and_preserves_annotations(self):
        self.view.refresh_universal_file_list()
        self.view.on_file_selected(self.view.list_files.item(0))
        previous = copy.deepcopy(self.view.get_current_boxes())
        calls = []
        def sync(owner, records):
            calls.append('sync')
            self.assertEqual(records['a.png']['boxes'][0]['class_id'], 1)
        def release(names, owner):
            calls.append('release')
            self.assertEqual(names, ['a.png'])
            self.assertEqual(owner, 'Alice')
        with patch.object(tempo_gui.tempo_hub, 'sync_annotation_drafts', side_effect=sync), \
             patch.object(tempo_gui.tempo_hub, 'release_claim', side_effect=release), \
             patch.object(self.view.shared_browser, 'load'):
            self.view.btn_release.click()
        self.assertEqual(calls, ['sync', 'release'])
        self.assertEqual(self.view.get_current_boxes(), previous)
        self.assertTrue(self.view._current_file_readonly)
        self.assertTrue(self.view.btn_release.isHidden())
        self.assertEqual(self.view.submission_status.text(), '')
        self.assertTrue(self.view.submission_status.isHidden())

    def test_release_sync_failure_keeps_reservation_and_draft(self):
        self.view.refresh_universal_file_list()
        self.view.on_file_selected(self.view.list_files.item(0))
        with patch.object(tempo_gui.tempo_hub, 'sync_annotation_drafts', side_effect=ConnectionError('offline')), \
             patch.object(tempo_gui.tempo_hub, 'release_claim') as release:
            self.view.release_current_lease()
        release.assert_not_called()
        self.assertFalse(self.view._current_file_readonly)
        self.assertIn('offline', self.view.reservation_error.details.toPlainText())

    def test_foreign_pending_draft_displays_profile_and_clears_stale_identity_hover(self):
        actor = 'hf:foreign-draft-owner'
        self.view.current_file = str(self.root / 'a.png')
        self.view.universal_annotations['a.png'].update(sync_pending=True, draft_owner=actor)
        self.view.lock_manifest['collaborators'] = {actor: dict(profile_name='Cell Mechanics Lab')}
        self.view.update_reservation_status(self.manifest['images']['a.png'])
        self.assertIn('belong to Cell Mechanics Lab', self.view.lbl_lock_status.text())
        self.assertNotIn(actor, self.view.lbl_lock_status.text())
        self.assertEqual(self.view.lbl_lock_status.toolTip(), actor)
        self.view.universal_annotations['a.png']['sync_pending'] = False
        self.view.update_reservation_status(self.manifest['images']['a.png'])
        self.assertEqual(self.view.lbl_lock_status.toolTip(), '')

    def test_release_never_syncs_or_clears_another_accounts_pending_draft(self):
        self.view.refresh_universal_file_list()
        self.view.on_file_selected(self.view.list_files.item(0))
        self.view.universal_annotations['a.png'].update(sync_pending=True, draft_owner='Bob')
        with patch.object(tempo_gui.tempo_hub, 'sync_annotation_drafts') as sync, \
             patch.object(tempo_gui.tempo_hub, 'release_claim') as release, \
             patch.object(self.view.shared_browser, 'load'):
            self.view.release_current_lease()
        sync.assert_not_called()
        release.assert_called_once_with(['a.png'], 'Alice')
        self.assertTrue(self.view.universal_annotations['a.png']['sync_pending'])
        self.assertEqual(self.view.universal_annotations['a.png']['draft_owner'], 'Bob')

    def test_release_is_unavailable_for_submitted_and_other_owner_images(self):
        for state, owner in [('submitted', 'Alice'), ('locked', 'Bob'), ('approved', None)]:
            self.manifest['images']['a.png'] = dict(TempoHub._lease(owner), status=state)
            self.view.refresh_universal_file_list()
            self.view.on_file_selected(self.view.list_files.item(0))
            self.assertTrue(self.view.btn_release.isHidden())
            with patch.object(tempo_gui.tempo_hub, 'release_claim') as release:
                self.view.release_current_lease()
            release.assert_not_called()

    def test_auto_failure_preserves_memory_and_saved_draft(self):
        self.begin_auto_annotation()
        original = json.loads((self.root/'local_session.json').read_text())
        self.assertEqual(len(self.view.get_current_boxes()), 1)
        self.view.on_auto_annotate_error('Inference failed')
        self.assertEqual(json.loads((self.root/'local_session.json').read_text()), original)
        self.assertEqual(self.view.get_current_boxes(), original['annotations']['a.png']['boxes'])

    def test_auto_success_replaces_instead_of_appending_and_backs_up(self):
        self.begin_auto_annotation()
        original = json.loads((self.root/'local_session.json').read_text())
        self.view.on_auto_annotate_finished(self.auto_result())
        self.assertEqual(len(self.view.get_current_boxes()), 2)
        saved = json.loads((self.root/'local_session.json').read_text())
        self.assertEqual(saved['annotations']['a.png']['boxes'], self.view.get_current_boxes())
        backups = list(self.root.glob('auto_annotation_backup_*.json'))
        self.assertEqual(len(backups), 1)
        self.assertEqual(json.loads(backups[0].read_text())['a.png']['boxes'], original['annotations']['a.png']['boxes'])

    def test_auto_replacement_can_be_cancelled_before_model_launch(self):
        self.view.refresh_universal_file_list(); self.view.list_files.setCurrentRow(0)
        with patch.object(tempo_gui,'confirm_action',return_value=False), patch.object(tempo_gui,'AutoAnnotateWorker') as worker:
            self.view.start_auto_annotation()
        worker.assert_not_called()

    def test_annotation_backup_is_restorable_from_ui(self):
        self.begin_auto_annotation()
        original = copy.deepcopy(self.view.get_current_boxes())
        self.view.on_auto_annotate_finished(self.auto_result())
        backup = next(self.root.glob('auto_annotation_backup_*.json'))
        with patch.object(tempo_gui.QFileDialog,'getOpenFileName',return_value=(str(backup),'')):
            self.view.restore_annotation_backup()
        self.assertEqual(self.view.get_current_boxes(), original)
        self.assertEqual(self.view.restore_status.text(),'Annotations restored.')

    def test_export_creates_new_folder_without_overwriting_existing_dataset(self):
        self.view.is_universal_mode=False
        self.view.annotations={str(self.root/'a.png'):[{'class_id':0,'polygon':[[10,10],[20,10],[20,20]]}]}
        from PyQt6.QtWidgets import QListWidgetItem
        from PyQt6.QtCore import Qt
        item=QListWidgetItem('a.png'); item.setData(Qt.ItemDataRole.UserRole,str(self.root/'a.png'))
        self.view.list_files.addItem(item); self.view.list_files.setCurrentItem(item)
        existing=self.root/'existing.txt'; existing.write_text('keep')
        with patch.object(tempo_gui.QFileDialog,'getExistingDirectory',return_value=str(self.root)), patch.object(tempo_gui.CustomPopup,'exec',return_value=None):
            self.assertTrue(self.view.export_annotations())
        self.assertEqual(existing.read_text(),'keep')
        exported=list(self.root.glob('tempo-annotations-*'))
        self.assertEqual(len(exported),1)
        self.assertTrue((exported[0]/'a.png').exists())
        restored = self.view.read_local_annotations(str(exported[0]))
        self.assertEqual(restored[str(exported[0]/'a.png')],self.view.annotations[str(self.root/'a.png')])

    def test_failed_export_removes_only_its_incomplete_output(self):
        self.view.is_universal_mode=False
        path=str(self.root/'a.png')
        self.view.annotations={path:[{'class_id':0,'polygon':[[10,10],[20,10],[20,20]]}]}
        item=tempo_gui.QListWidgetItem('a.png'); item.setData(tempo_gui.Qt.ItemDataRole.UserRole,path)
        self.view.list_files.addItem(item); self.view.list_files.setCurrentItem(item)
        existing=self.root/'previous-dataset'; existing.mkdir(); (existing/'keep.txt').write_text('preserve')
        with patch.object(tempo_gui.QFileDialog,'getExistingDirectory',return_value=str(self.root)), patch.object(tempo_gui.shutil,'copy',side_effect=OSError('disk full')):
            self.assertFalse(self.view.export_annotations())
        self.assertFalse(list(self.root.glob('tempo-annotations-*')))
        self.assertEqual((existing/'keep.txt').read_text(),'preserve')
        self.assertIn('disk full',self.view.export_error.details.toPlainText())

    def test_auto_result_does_not_replace_intervening_manual_edits(self):
        self.begin_auto_annotation()
        self.view.add_polygon([[70,70],[80,70],[80,80]])
        saved = (self.root/'local_session.json').read_bytes()
        self.view.on_auto_annotate_finished(self.auto_result())
        self.assertEqual((self.root/'local_session.json').read_bytes(), saved)
        self.assertIn('discarded', self.view.lbl_auto_status.text())

    def test_auto_empty_invalid_or_cancelled_results_preserve_draft(self):
        for result in ({'success':True,'boxes':[]}, {'success':False,'error':'cancelled'}, {'success':True,'boxes':[{'class_id':0,'polygon':[[1,1]]}]}):
            self.begin_auto_annotation()
            saved = (self.root/'local_session.json').read_bytes()
            self.view.on_auto_annotate_finished(result)
            self.assertEqual(len(self.view.get_current_boxes()),1)
            self.assertEqual((self.root/'local_session.json').read_bytes(),saved)

    def test_auto_result_discarded_after_navigation_or_lease_change(self):
        self.begin_auto_annotation()
        saved = (self.root/'local_session.json').read_bytes()
        self.view.current_file = str(self.root/'other.png')
        self.view.on_auto_annotate_finished(self.auto_result())
        self.assertEqual((self.root/'local_session.json').read_bytes(),saved)
        self.begin_auto_annotation()
        self.manifest['images']['a.png'] = {'status':'submitted', 'claimed_by':'Alice'}
        self.view.on_auto_annotate_finished(self.auto_result())
        self.assertEqual(len(self.view.get_current_boxes()),1)
        self.assertTrue(self.view._current_file_readonly)

    def test_auto_save_failure_restores_original_and_retains_backup(self):
        self.begin_auto_annotation()
        original = json.loads((self.root/'local_session.json').read_text())
        with patch.object(self.view, 'save_annotations_to_disk', side_effect=OSError('disk full')):
            self.view.on_auto_annotate_finished(self.auto_result())
        self.assertEqual(self.view.get_current_boxes(),original['annotations']['a.png']['boxes'])
        self.assertEqual(len(list(self.root.glob('auto_annotation_backup_*.json'))),1)
        self.assertIn('disk full', self.view.auto_error.details.toPlainText())
        self.assertTrue(self.view.auto_error.details.isHidden())

    def test_native_auto_failure_preserves_saved_draft(self):
        worker = self.begin_auto_annotation()
        self.view.on_auto_annotate_error('cancelled')
        boxes = self.view.get_current_boxes()
        self.view.is_universal_mode = False
        self.view.current_dir = self.temp.name
        self.view.annotations = {self.view.current_file: boxes}
        self.view.save_annotations_to_disk()
        saved = (self.root/'annotations.json').read_bytes()
        self.view.start_auto_annotation()
        self.view.on_auto_annotate_error('failed')
        self.assertEqual((self.root/'annotations.json').read_bytes(),saved)
        self.assertEqual(self.view.get_current_boxes(),boxes)
        self.assertIn('failed', self.view.auto_error.details.toPlainText())

    def test_pending_submission_editable_and_approval_freezes(self):
        self.view.refresh_universal_file_list()
        with patch.object(tempo_gui.tempo_hub,'fetch_lock_manifest',side_effect=AssertionError('Unexpected online call')):
            self.view.on_file_selected(self.view.list_files.item(0))
            self.assertFalse(self.view._current_file_readonly)
            self.view.add_polygon([[30,30],[40,30],[40,40]])
        self.assertTrue((self.root/'local_session.json').exists())
        self.manifest['images']['a.png']=dict(TempoHub._lease('Alice'),status='submitted',submission_id='pending')
        self.view.refresh_universal_file_list(); self.view.on_file_selected(self.view.list_files.item(0))
        count=len(self.view.get_current_boxes())
        self.view.add_polygon([[50,50],[60,50],[60,60]])
        self.assertEqual(len(self.view.get_current_boxes()),count+1)
        self.assertFalse(self.view._current_file_readonly)
        self.assertFalse(self.view.btn_publish.isEnabled())
        self.manifest['images']['a.png']['status']='approved'
        self.view.refresh_universal_file_list(); self.view.on_file_selected(self.view.list_files.item(0))
        self.view.add_polygon([[70,70],[80,70],[80,80]])
        self.assertEqual(len(self.view.get_current_boxes()),count+1)
        self.assertTrue(self.view._current_file_readonly)

    def test_sync_ack_does_not_clear_newer_edits(self):
        self.view.refresh_universal_file_list()
        self.view.on_file_selected(self.view.list_files.item(0))
        self.view.save_annotations_to_disk()
        with patch.object(tempo_gui, 'run_background') as background:
            self.view.sync_pending_annotations()
        sent = self.view._sync_snapshot['a.png']
        self.view.add_polygon([[30,30],[40,30],[40,40]])
        self.view.on_annotation_sync_finished(1,None)
        self.assertTrue(self.view.universal_annotations['a.png']['sync_pending'])
        self.assertNotEqual(self.view.normalized_draft('a.png'),sent)

    def test_failed_sync_keeps_local_draft_and_pending_flag(self):
        self.view.refresh_universal_file_list()
        self.view.on_file_selected(self.view.list_files.item(0))
        self.view.save_annotations_to_disk()
        saved = (self.root/'local_session.json').read_bytes()
        self.view.on_annotation_sync_finished(None,ConnectionError('offline'))
        self.assertEqual((self.root/'local_session.json').read_bytes(),saved)
        self.assertTrue(self.view.universal_annotations['a.png']['sync_pending'])
        self.assertIn('Sync failed',self.view.lbl_sync_status.text())
        self.assertIn('offline', self.view.sync_error.details.toPlainText())
        self.assertTrue(self.view.sync_error.details.isHidden())

    def test_failed_reservation_renewal_preserves_draft(self):
        self.view.refresh_universal_file_list()
        self.view.on_file_selected(self.view.list_files.item(0))
        before = copy.deepcopy(self.view.universal_annotations)
        with patch.object(tempo_gui.tempo_hub, 'renew_claim', side_effect=ConnectionError('offline')):
            self.view.renew_current_lease()
        self.assertEqual(self.view.universal_annotations, before)
        self.assertIn('offline', self.view.reservation_error.details.toPlainText())
        self.assertTrue(self.view.reservation_error.details.isHidden())

    def test_gui_cannot_start_local_training(self):
        with patch.object(tempo_gui, 'QProcess', side_effect=AssertionError('Local training launched')):
            self.assertFalse(hasattr(self.view, 'iterate_model'))
            self.assertFalse(hasattr(self.view, 'on_export_finished_start_training'))
        self.assertEqual('', self.view.lbl_sync_status.text())

    def test_lease_refresh_does_not_block_or_clear_selection(self):
        self.view.refresh_universal_file_list()
        self.view.list_files.setCurrentRow(0)
        with patch.object(tempo_gui, 'run_background') as background, \
             patch.object(tempo_gui, 'run_task', side_effect=AssertionError('Modal lease check')):
            self.view.refresh_universal_file_list()
        self.assertTrue(background.called)
        self.assertEqual(len(self.view.list_files.selectedItems()),1)
        self.view.lbl_sync_status.setText('All changes synced')
        self.view.on_lease_status_finished(None, ConnectionError('offline'))
        self.assertEqual(self.view.list_files.count(),1)
        self.assertIn('Showing saved status', self.view.reservation_refresh_error.summary.text())
        self.assertEqual(self.view.lbl_sync_status.text(), 'All changes synced')
        self.view.reservation_error.set_error('Renewal failed.', ConnectionError('renewal offline'))
        self.view.on_lease_status_finished(self.manifest, None)
        self.assertTrue(self.view.reservation_refresh_error.isHidden())
        self.assertFalse(self.view.reservation_error.isHidden())

    def test_background_approval_turns_off_editing(self):
        self.view.refresh_universal_file_list()
        self.view.on_file_selected(self.view.list_files.item(0))
        self.assertFalse(self.view._current_file_readonly)
        self.view.on_lease_status_finished({'images':{'a.png':{'status':'approved'}}}, None)
        self.assertTrue(self.view._current_file_readonly)
        self.assertFalse(self.view.btn_delete.isEnabled())
    def test_adoption_archives_draft_once(self):
        self.manifest['images']['a.png']=TempoHub._lease('Bob')
        self.view.refresh_universal_file_list()
        backups=list(self.root.glob('orphaned_backup_*.json'))
        self.assertEqual(len(backups),1)
        self.assertIn('a.png',json.loads(backups[0].read_text()))
        self.view.refresh_universal_file_list()
        self.assertEqual(len(list(self.root.glob('orphaned_backup_*.json'))),1)
        self.view.on_file_selected(self.view.list_files.item(0))
        self.assertTrue(self.view._current_file_readonly)

    def test_failed_adoption_backup_preserves_list_and_locks_foreign_draft(self):
        self.view.refresh_universal_file_list()
        self.view.list_files.setCurrentRow(0)
        self.view.current_file = str(self.root/'a.png')
        self.manifest['images']['a.png'] = TempoHub._lease('Bob')
        with patch.object(tempo_gui, 'atomic_json', side_effect=OSError('disk full')):
            self.view.refresh_universal_file_list()
        self.assertEqual(self.view.list_files.count(), 1)
        self.assertFalse(self.view.list_files.signalsBlocked())
        self.assertTrue(self.view._current_file_readonly)
        self.assertIn('disk full', self.view.adoption_error.details.toPlainText())
        self.assertNotIn('orphan_backup', self.view.universal_annotations['a.png'])

    def test_adoption_retries_failed_record_save_without_duplicate_backup(self):
        self.manifest['images']['a.png'] = TempoHub._lease('Bob')
        with patch.object(self.view, 'save_universal_annotations', side_effect=OSError('disk full')):
            self.view.refresh_universal_file_list(refresh=False)
        self.assertEqual(len(list(self.root.glob('orphaned_backup_*.json'))), 1)
        self.assertTrue(self.view._pending_archive_paths)
        with patch.object(self.view, 'save_universal_annotations', wraps=self.view.save_universal_annotations) as save:
            self.view.refresh_universal_file_list(refresh=False)
        save.assert_called_once()
        self.assertFalse(self.view._pending_archive_paths)
        self.assertTrue(self.view.adoption_error.isHidden())
        self.assertEqual(len(list(self.root.glob('orphaned_backup_*.json'))), 1)

    def test_account_switch_preserves_previous_authors_draft_and_disables_editing(self):
        self.view.refresh_universal_file_list()
        self.view.on_file_selected(self.view.list_files.item(0))
        self.view.save_annotations_to_disk()
        boxes = copy.deepcopy(self.view.universal_annotations['a.png']['boxes'])
        patch.object(tempo_gui.tempo_hub, 'get_collaborator_id', return_value='Bob').start()
        self.view.refresh_account_identity()
        self.assertEqual(self.view.collaborator_id, 'Bob')
        self.assertTrue(self.view._current_file_readonly)
        self.assertEqual(self.view.universal_annotations['a.png']['boxes'], boxes)
        self.assertEqual(self.view.universal_annotations['a.png']['draft_owner'], 'Alice')
        self.assertFalse(list(self.root.glob('orphaned_backup_*.json')))
        saved = json.loads((self.root/'universal_annotations.json').read_text())
        self.assertEqual(saved['a.png']['draft_owner'], 'Alice')
        # A later reservation must not upload another author's pending local copy.
        self.manifest['images']['a.png'] = TempoHub._lease('Bob')
        self.view._apply_current_lease()
        self.assertTrue(self.view._current_file_readonly)
        self.assertIn('unsynced annotations belong to Alice', self.view.lbl_lock_status.text())
        self.assertFalse(self.view.submission_payload())
        with patch.object(tempo_gui.tempo_hub, 'sync_annotation_drafts') as sync:
            self.view.sync_pending_annotations()
        sync.assert_not_called()

    def test_account_setup_refreshes_settings_field_before_next_save(self):
        from types import SimpleNamespace
        from PyQt6.QtWidgets import QLineEdit
        field = QLineEdit('Alice')
        view = SimpleNamespace(widgets={('collaboration', 'collaborator_id'): field},_saved_values={},update_save_state=MagicMock())
        with patch.object(tempo_gui.tempo_preferences, 'get', return_value='Bob'):
            tempo_gui.SettingsTab.refresh_account_fields(view)
        self.assertEqual(field.text(), 'Bob')

    def test_long_original_filename_wraps_without_expanding_window(self):
        import time
        from PyQt6.QtGui import QFont, QTextCursor
        from PyQt6.QtTest import QTest
        # Stay within Windows' filename limit while forcing wrapping even with
        # compact system fonts. The previous name could fit on one line.
        name = 'Cardiomyocyte_experiment_long_sample_name_'*5 + 'plate_A01_field_3.png'
        self.view.universal_annotations['a.png']['original_filename'] = name
        self.view.refresh_universal_file_list(refresh=False)
        for size in (8,12):
            with self.subTest(font=size):
                self.view.setFont(QFont('Arial',size))
                self.view.resize(944,554); self.view.show()
                self.view.list_files.setCurrentRow(0)
                label = self.view.lbl_image_name
                deadline = time.monotonic()+2
                while time.monotonic() < deadline:
                    APP.processEvents()
                    if label.isVisible() and label.document().firstBlock().layout().lineCount() > 1:
                        break
                    QTest.qWait(5)
                self.assertEqual(self.view.width(),944)
                self.assertEqual(label.toPlainText(),name)
                lines = label.document().firstBlock().layout()
                self.assertGreater(lines.lineCount(),1)
                for index in range(lines.lineCount()):
                    self.assertLessEqual(lines.lineAt(index).naturalTextRect().right(),label.viewport().width())
                cursor = label.textCursor(); cursor.movePosition(QTextCursor.MoveOperation.End)
                label.setTextCursor(cursor); label.ensureCursorVisible(); APP.processEvents()
                self.assertTrue(label.viewport().rect().contains(label.cursorRect()))
                self.assertEqual(label.horizontalScrollBar().maximum(),0)
                self.assertEqual(self.view.view.horizontalScrollBar().maximum(),0)
        self.assertIn(name, self.view.list_files.item(0).toolTip())
        self.view.on_file_selected(None)
        self.assertTrue(self.view.lbl_image_name.isHidden())
    def test_review_vertices_delete_and_reclassify(self):
        canvas=review.ReviewCanvas(); self.addCleanup(canvas.close)
        canvas.set_image_and_masks(str(self.root/'a.png'),{'width':100,'height':100,'coordinate_space':'normalized','boxes':[{'class_id':1,'polygon':[[.1,.1],[.2,.1],[.2,.2]]}]})
        handles=[i for i in canvas.scene().items() if isinstance(i,review.VertexHandle)]
        handle=handles[0]; handle.setPos(QPointF(35,45))
        self.assertEqual(canvas.boxes[handle.pi]['polygon'][handle.vi],[35,45])
        canvas.polygons[0].setSelected(True); canvas.reclassify(0)
        self.assertEqual(canvas.boxes[0]['class_id'],0)
        canvas.polygons[0].setSelected(True); canvas.delete_selected()
        self.assertEqual(canvas.boxes,[])
    def test_nonmaintainer_cannot_construct_review(self):
        with patch.object(review.tempo_hub,'is_maintainer',return_value=False):
            with self.assertRaises(PermissionError): review.AnnotationReviewDialog()
    def test_task_error_is_returned_without_hanging_gui(self):
        with self.assertRaisesRegex(ValueError,'mock failure'):
            run_task(None,'Test',lambda:(_ for _ in ()).throw(ValueError('mock failure')))


class IdentityPresentationTests(unittest.TestCase):
    setUp = DesktopTests.setUp

    def select(self, status='locked', owner='Alice', boxes=None):
        self.view.lease_timer.stop(); self.view._sync_timer.stop()
        self.manifest['images']['a.png'] = dict(TempoHub._lease(owner or 'Alice'), status=status, claimed_by=owner)
        if boxes is not None:
            self.view.universal_annotations['a.png']['boxes'] = boxes
        self.view.refresh_universal_file_list(refresh=False)
        self.view.list_files.setCurrentRow(0)
        self.view.update_submission_readiness()

    def test_selection_explains_each_actual_ineligibility_reason(self):
        for status, owner, expected in [('approved',None,'already approved'),
                                       ('submitted','Alice','already awaiting review'),
                                       ('available',None,'not reserved by you'),
                                       ('locked','Bob','reserved by another account')]:
            with self.subTest(status=status,owner=owner):
                self.select(status,owner)
                self.assertIn(expected,self.view.lbl_readiness_badge.text())
                self.assertIn('a.png',self.view.lbl_readiness_badge.text())
                self.assertFalse(self.view.btn_publish.isEnabled())

    def test_owned_empty_image_says_it_needs_annotations(self):
        self.select(boxes=[])
        self.assertIn('no cell annotations',self.view.lbl_readiness_badge.text())
        self.assertNotIn('not reserved',self.view.lbl_readiness_badge.text())

    def test_mixed_selection_counts_ready_and_unreserved_images_separately(self):
        from PyQt6.QtWidgets import QAbstractItemView
        import shutil
        shutil.copyfile(self.root/'a.png',self.root/'b.png')
        self.view.universal_annotations['b.png']=copy.deepcopy(self.view.universal_annotations['a.png'])
        self.view.universal_annotations['b.png'].pop('status',None)
        self.manifest['images']['b.png']={'status':'available'}
        self.view.mode_toggle.setCurrentIndex(1)
        self.view.refresh_universal_file_list(refresh=False)
        self.view.shared_browser.load()
        for item in self.view.shared_browser.items.values():
            if item.flags() & Qt.ItemFlag.ItemIsUserCheckable:
                item.setCheckState(Qt.CheckState.Checked)
        self.view.update_submission_readiness()
        text=self.view.lbl_readiness_badge.text()
        self.assertIn('2 images selected',text)
        self.assertIn('1 ready to submit',text)
        self.assertIn('1 not reserved by you: b.png',text)
        self.assertFalse(self.view.btn_publish.isEnabled())
        with patch.object(tempo_gui.tempo_hub,'upload_submission') as upload:
            self.view.publish_submission()
        upload.assert_not_called()

    def test_expired_reservation_is_explicit(self):
        self.select()
        self.manifest['images']['a.png']['expires_at']='2000-01-01T00:00:00+00:00'
        self.view.update_submission_readiness()
        self.assertIn('reservation expired',self.view.lbl_readiness_badge.text())

    def test_alias_refresh_keeps_annotations_and_removes_duplicate_row(self):
        old=copy.deepcopy(self.view.universal_annotations['a.png'])
        self.view.universal_annotations['legacy.png']=old
        self.view.universal_annotations['a.png']={'boxes':[]}
        manifest={'images':{'legacy.png':TempoHub._lease('Alice')},'aliases':{'a.png':'legacy.png'}}
        import shutil
        shutil.copyfile(self.root/'a.png',self.root/'legacy.png')
        self.manifest.clear();self.manifest.update(manifest)
        self.view.on_lease_status_finished(manifest,None)
        self.assertEqual(set(self.view.universal_annotations),{'legacy.png'})
        self.assertEqual(self.view.universal_annotations['legacy.png']['boxes'],old['boxes'])
        self.assertEqual(self.view.list_files.count(),1)
        self.assertTrue(list(self.root.glob('identity_backup_*.json')))

    def test_selection_messages_fit_and_scroll_with_large_fonts_and_many_images(self):
        import shutil
        from PyQt6.QtCore import Qt, QPoint, QRect
        from PyQt6.QtGui import QFont
        from PyQt6.QtTest import QTest
        from PyQt6.QtWidgets import QAbstractItemView, QToolTip, QWidget
        self.view.lease_timer.stop(); self.view._sync_timer.stop()
        self.view.mode_toggle.setCurrentIndex(1)
        states=[('approved',None),('submitted','Alice'),('available',None),('locked','Bob'),('locked','Alice')]
        for index in range(30):
            name=f'Historical_cardiomyocyte_field_{index+1:02d}.png'
            shutil.copyfile(self.root/'a.png',self.root/name)
            status,owner=states[index%len(states)]
            self.manifest['images'][name]=dict(TempoHub._lease(owner or 'Alice'),status=status,claimed_by=owner)
            self.view.universal_annotations[name]=copy.deepcopy(self.view.universal_annotations['a.png'])
        self.view.refresh_universal_file_list(refresh=False)
        self.view.shared_browser.load()
        for item in self.view.shared_browser.items.values():
            if item.flags() & Qt.ItemFlag.ItemIsUserCheckable:
                item.setCheckState(Qt.CheckState.Checked)
        self.view.update_submission_readiness()
        evidence=os.environ.get('TEMPO_ANNOTATION_REVIEW_DIR')
        old_font=APP.font();self.addCleanup(APP.setFont,old_font)
        for size in (12,18,24):
            APP.setFont(QFont('Arial',size))
            self.view.setFont(QFont('Arial',size))
            # Previously polished style wrappers can retain their earlier font.
            # Apply and verify the fixture font on the actual controls as well.
            for control in self.view.findChildren(QWidget):
                control.setFont(QFont('Arial',size))
            for width,height in ((960,620),(1100,760),(1700,950)):
                with self.subTest(font=size,width=width):
                    self.view.resize(width,height);self.view.show()
                    for _ in range(8):APP.processEvents()
                    label=self.view.lbl_readiness_badge
                    self.assertEqual(label.font().pointSize(),size)
                    needed=label.fontMetrics().boundingRect(label.contentsRect(),
                        int(Qt.TextFlag.TextWordWrap),label.text())
                    self.assertGreaterEqual(label.contentsRect().height(),needed.height())
                    self.assertEqual(self.view.width(),width)
                    self.view.submission_scroll.ensureWidgetVisible(self.view.btn_publish,0,0)
                    for _ in range(4):APP.processEvents()
                    button=self.view.btn_publish; viewport=self.view.submission_scroll.viewport()
                    bounds=QRect(button.mapTo(viewport,QPoint(0,0)),button.size())
                    self.assertTrue(viewport.rect().contains(bounds))
                    self.assertGreater(self.view.shared_browser.images.verticalScrollBar().maximum(),0)
                    self.assertGreater(self.view.center_stack.width(),200)
                    if evidence:
                        destination=Path(evidence);destination.mkdir(parents=True,exist_ok=True)
                        self.view.grab().save(str(destination/f'mixed-{width}-{size}pt.png'))
        # Trigger ordinary pointer hover, then leave, without injecting tooltip events.
        self.view.submission_scroll.ensureWidgetVisible(label,0,0)
        APP.processEvents();QTest.mouseMove(self.view.center_stack,QPoint(20,20))
        viewport=self.view.submission_scroll.viewport()
        label_bounds=QRect(label.mapTo(viewport,QPoint(0,0)),label.size())
        hover=label_bounds.intersected(viewport.rect()).center()
        QTest.mouseMove(label,label.mapFrom(viewport,hover))
        import time
        deadline=time.monotonic()+2
        while not QToolTip.isVisible() and time.monotonic()<deadline:
            QTest.qWait(20)
        self.assertTrue(QToolTip.isVisible())
        self.assertIn('already awaiting review',QToolTip.text())
        QTest.mouseMove(self.view.center_stack,QPoint(30,30))
        deadline=time.monotonic()+1
        while QToolTip.isVisible() and time.monotonic()<deadline:
            QTest.qWait(20)
        self.assertFalse(QToolTip.isVisible())
        QToolTip.hideText()


class UpdateTests(unittest.TestCase):
    def test_failed_git_pull_stops_pip_and_restores_stash(self):
        commands=[]
        def execute(command):
            commands.append(command)
            if command==['git','branch','--show-current']: return 'main'
            if command==['git','status','--porcelain']: return ' M file.py'
            if command==['git','rev-parse','refs/stash']: return 'backup-sha'
            if command[:2]==['git','pull']: raise RuntimeError('pull failed')
            return ''
        with patch.object(update_wizard,'checked_command',side_effect=execute):
            with self.assertRaisesRegex(RuntimeError,'pull failed'): update_wizard.perform_software_update()
        self.assertIn(['git','stash','apply','backup-sha'],commands)
        self.assertFalse(any('pip' in c for c in commands))
    def test_failed_dependency_sync_never_reports_success(self):
        revisions=iter(('before','after'))
        def execute(command):
            if command==['git','branch','--show-current']: return 'main'
            if command==['git','rev-parse','HEAD']: return next(revisions)
            if 'pip' in command: raise RuntimeError('pip failed')
            return ''
        with patch.object(update_wizard,'checked_command',side_effect=execute):
            with self.assertRaisesRegex(RuntimeError,'pip failed'): update_wizard.perform_software_update()


if __name__=='__main__': unittest.main()
