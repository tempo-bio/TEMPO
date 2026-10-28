"""Review navigation, edit protection, and asynchronous response ordering."""
import copy
import os
import tempfile
import unittest
from types import SimpleNamespace
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
from PyQt6.QtWidgets import QApplication, QCheckBox
from PyQt6.QtGui import QImage, QFont
from PyQt6.QtCore import Qt, QPoint, QRect
from PyQt6 import sip
from PyQt6.QtTest import QTest
import annotation_review_dialog as review
from tests.qt_lifecycle import close_and_dispose

APP = QApplication.instance() or QApplication([])


class ReviewInteractionTests(unittest.TestCase):
    def test_populated_review_controls_fit_small_windows_at_large_fonts(self):
        from tempo_ui import install_ui
        from tempo_tasks import dispose_closed_views
        from tests.presentation_fixtures import populated_review
        from PyQt6 import sip
        install_ui(APP)
        old_font=APP.font();self.addCleanup(lambda:APP.setFont(old_font))
        def dispose(view):
            if not sip.isdeleted(view):
                view.save_current_edits();view.reviewed_snapshot=copy.deepcopy(view.current_annotations)
                view.close();dispose_closed_views(view)
        for font in (12,18,24):
            APP.setFont(QFont('Arial',font))
            view=populated_review(self.tmp.name);self.addCleanup(dispose,view)
            view.request_refresh=lambda:None
            view.canvas.polygons[1].setSelected(True);view.canvas.set_group(True)
            for width in (960,1100,1700):
                view.resize(width,620);view.show()
                for _ in range(12):APP.processEvents()
                with self.subTest(font=font,width=width):
                    self.assertEqual(view.width(),width)
                    self.assertEqual(view.height(),620)
                    self.assertGreater(view.canvas.viewport().width(),150)
                    self.assertGreater(view.canvas.viewport().height(),60)
                    controls=(*view.view_buttons,*view.action_buttons,view.classes,
                              view.undo_button,view.delete_button,*view.findChildren(QCheckBox))
                    for control in controls:
                        bounds=QRect(control.mapTo(view,QPoint()),control.size())
                        self.assertTrue(view.rect().contains(bounds),control.objectName() or control.__class__.__name__)
            dispose(view)

    def test_review_checkboxes_have_no_persistent_glow_after_click_or_keyboard_focus(self):
        from tempo_ui import install_ui
        install_ui(APP)
        self.load_submission()
        self.view.show(); APP.processEvents()
        self.pending.pop()[1]([{'submission_id':'one','collaborator_id':'Researcher','image_count':1}],None)
        for font in (12, 18, 24):
            self.view.setFont(QFont('Arial', font))
            for width in (960, 1100, 1700):
                self.view.resize(width, 760); APP.processEvents()
                for checkbox in (self.view.view_buttons[-1], self.view.complete):
                    with self.subTest(font=font, width=width, checkbox=checkbox.text()):
                        previous = checkbox.isChecked()
                        QTest.mouseClick(checkbox, Qt.MouseButton.LeftButton, pos=QPoint(8, checkbox.height() // 2))
                        QTest.mouseMove(self.view.canvas.viewport(), QPoint(2, 2)); APP.processEvents()
                        self.assertNotEqual(checkbox.isChecked(), previous)
                        self.assertTrue(checkbox.hasFocus()); self.assertFalse(checkbox.underMouse())
                        focused = checkbox.grab().toImage()
                        self.view.refresh_button.setFocus(); APP.processEvents()
                        self.assertEqual(focused, checkbox.grab().toImage())
                        checkbox.setFocus(Qt.FocusReason.TabFocusReason); APP.processEvents()
                        self.assertEqual(focused, checkbox.grab().toImage())
                        QTest.keyClick(checkbox, Qt.Key.Key_Space)
                        self.assertEqual(checkbox.isChecked(), previous)
        self.assertEqual(self.view.current_annotations, self.view.reviewed_snapshot)

    def test_editable_review_fits_first_show_and_preserves_later_zoom(self):
        self.load_submission()
        self.view.resize(944, 554); self.view.show(); APP.processEvents()
        canvas = self.view.canvas
        bounds = canvas.mapFromScene(canvas.sceneRect()).boundingRect()
        self.assertLessEqual(bounds.width(), canvas.viewport().width())
        self.assertLessEqual(bounds.height(), canvas.viewport().height())
        canvas.scale(1.2, 1.2); zoom = canvas.transform().m11()
        self.view.hide(); self.view.show(); APP.processEvents()
        self.assertAlmostEqual(canvas.transform().m11(), zoom)

    def test_closing_review_ignores_inflight_submission_response(self):
        callback = self.pending.pop()[1]
        self.view.reject()
        callback([{'submission_id': 'late', 'collaborator_id': 'Alice', 'image_count': 1}], None)
        self.assertEqual(self.view.sub_list.count(), 0)
        self.assertFalse(self.view._loading)
        self.assertTrue(self.view.refresh_button.isEnabled())

    def test_read_timeout_settles_once_and_ignores_late_response(self):
        results = []
        with patch.object(review, 'run_background') as background:
            timer = self.view.run_read(lambda: None, lambda value, error: results.append((value, error)))
        callback = background.call_args.args[1]
        timer.timeout.emit()
        callback('late image', None)
        self.assertEqual(len(results), 1)
        self.assertIsInstance(results[0][1], TimeoutError)

    def test_readonly_preview_fits_when_first_shown(self):
        canvas = review.ReviewCanvas(editable=False); self.addCleanup(close_and_dispose, canvas)
        canvas.set_image_and_masks(str(self.path), self.record)
        canvas.resize(400, 220); canvas.show(); APP.processEvents()
        bounds = canvas.mapFromScene(canvas.sceneRect()).boundingRect()
        self.assertLessEqual(bounds.width(), canvas.viewport().width())
        self.assertLessEqual(bounds.height(), canvas.viewport().height())
        canvas.resize(500, 400); APP.processEvents()
        bounds = canvas.mapFromScene(canvas.sceneRect()).boundingRect()
        self.assertGreater(bounds.height(), canvas.viewport().height() * .9)

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 'image.png'
        image = QImage(100, 100, QImage.Format.Format_RGB32); image.fill(0); image.save(str(self.path))
        self.record = {'width':100, 'height':100, 'coordinate_space':'normalized',
                       'boxes':[{'class_id':0, 'polygon':[[.1,.1],[.8,.1],[.8,.8]]}]}
        self.pending = []
        patcher = patch.object(review, 'run_background', side_effect=lambda action, done:self.pending.append((action,done)))
        patcher.start(); self.addCleanup(patcher.stop)
        self.view = review.AnnotationReviewDialog(verified=True)
        self.addCleanup(self.dispose_review)

    def dispose_review(self):
        if sip.isdeleted(self.view):
            return
        # Settle fake requests without executing their network actions. A retry
        # left pending by a test must receive an outcome before close can succeed.
        for _ in range(20):
            if not self.pending:
                break
            self.pending.pop(0)[1](None, ConnectionError('Fixture request cancelled.'))
        self.assertFalse(self.pending, 'Fixture reads kept restarting during teardown.')
        self.assertFalse(self.view._operation, 'A fixture write did not settle.')
        with patch.object(review, 'confirm_action', return_value=True):
            close_and_dispose(self.view)

    def test_presentation_snapshot_never_starts_a_remote_read_when_shown_again(self):
        from tests.presentation_fixtures import populated_review
        view = populated_review(self.tmp.name)
        self.addCleanup(close_and_dispose, view)
        with patch.object(review, 'run_background', side_effect=AssertionError('Unexpected remote read')) as read:
            view.show(); APP.processEvents()
            view.hide(); view.show(); APP.processEvents()
        read.assert_not_called()
        self.assertFalse(view._loading)
        self.assertEqual(view.current_image_filename, 'review-image.png')

    def test_fixture_disposal_removes_read_deadlines_before_the_next_test(self):
        from PyQt6.QtCore import QTimer
        timers = self.view.findChildren(QTimer)
        self.assertTrue(any(timer.isActive() for timer in timers))
        self.view.show(); APP.processEvents()
        self.dispose_review()
        self.assertTrue(sip.isdeleted(self.view))
        self.assertTrue(all(sip.isdeleted(timer) for timer in timers))
        self.assertFalse(self.pending)

    def load_submission(self):
        self.pending.pop()[1]([{'submission_id':'one','collaborator_id':'Researcher','image_count':1}],None)
        self.view.sub_list.setCurrentRow(0)
        self.pending.pop()[1]({'image.png':copy.deepcopy(self.record)},None)
        self.pending.pop()[1](str(self.path),None)

    def test_approval_requires_image_completeness_and_edit_invalidates_it(self):
        self.load_submission()
        self.assertFalse(self.view.approve_button.isEnabled())
        self.view.complete.setChecked(True)
        self.assertTrue(self.view.approve_button.isEnabled())
        self.view.canvas.polygons[0].setSelected(True)
        self.view.canvas.reclassify(1)
        self.assertFalse(self.view.approve_button.isEnabled())
        self.view.canvas.undo()
        self.assertEqual(self.view.canvas.boxes[0]['class_id'],0)

    def test_refresh_preserves_edits_when_discard_is_cancelled(self):
        self.load_submission()
        self.view.canvas.polygons[0].setSelected(True)
        self.view.canvas.reclassify(1)
        with patch.object(review,'confirm_action',return_value=False) as confirm:
            self.view.refresh_submissions()
        confirm.assert_called_once()
        self.assertEqual(self.view.current_annotations['image.png']['boxes'][0]['class_id'],1)
        self.assertFalse(self.pending)

    def test_failed_approval_preserves_edits_and_allows_retry(self):
        self.load_submission()
        self.view.canvas.polygons[0].setSelected(True)
        self.view.canvas.reclassify(1)
        self.view.complete.setChecked(True)
        with patch.object(review, 'confirm_action', return_value=True):
            self.view.approve_and_merge()
        self.pending.pop()[1](None, ConnectionError('offline'))
        self.assertEqual(self.view.current_annotations['image.png']['boxes'][0]['class_id'], 1)
        self.assertEqual(self.view.reviewed_snapshot['image.png']['boxes'][0]['class_id'], 0)
        self.assertTrue(self.view.approve_button.isEnabled())
        self.assertTrue(self.view.canvas.isEnabled())
        self.assertTrue(self.view.error_details.details.isHidden())
        self.view.error_details.toggle.click()
        self.assertIn('offline', self.view.error_details.details.toPlainText())
        with patch.object(review, 'confirm_action', return_value=True):
            self.view.approve_and_merge()
        self.assertTrue(self.view.error_details.isHidden())
        self.assertEqual(len(self.pending), 1)

    def test_viewing_pixel_record_does_not_create_unsaved_edits(self):
        self.record['coordinate_space'] = 'pixels'
        self.record['boxes'][0]['polygon'] = [[10, 10], [80, 10], [80, 80]]
        self.load_submission()
        with patch.object(review, 'confirm_action') as confirm:
            self.assertTrue(self.view.can_leave())
        confirm.assert_not_called()
        self.assertEqual(self.view.current_annotations, self.view.reviewed_snapshot)

    def test_undo_to_loaded_geometry_clears_unsaved_changes(self):
        self.load_submission()
        self.view.canvas.polygons[0].setSelected(True)
        self.view.canvas.reclassify(1)
        self.view.canvas.undo()
        with patch.object(review, 'confirm_action') as confirm:
            self.assertTrue(self.view.can_leave())
        confirm.assert_not_called()

    def test_zoom_readout_tracks_buttons_and_fit(self):
        self.load_submission()
        before = self.view.canvas.transform().m11()
        self.view.view_buttons[0].click()
        self.assertAlmostEqual(self.view.canvas.transform().m11(), before * 1.2)
        self.assertEqual(self.view.zoom_label.text(), f'{before * 120:.0f}%')
        self.view.view_buttons[2].click()
        self.assertEqual(self.view.zoom_label.text(), f'{self.view.canvas.transform().m11() * 100:.0f}%')

    def test_repeat_refresh_does_not_start_duplicate_requests(self):
        self.view.refresh_submissions()
        self.assertEqual(len(self.pending),1)

    def test_opening_review_discovers_submission_after_initial_empty_queue(self):
        self.pending.pop()[1]([], None)
        self.assertEqual(self.view.status.text(), 'No submissions awaiting review.')
        self.view.show(); APP.processEvents()
        self.assertEqual(len(self.pending), 1)
        self.pending.pop()[1]([{'submission_id':'reported','collaborator_id':'Researcher','image_count':1}], None)
        self.assertEqual(self.view.sub_list.count(), 1)
        self.assertEqual(self.view.sub_list.item(0).data(Qt.ItemDataRole.UserRole)['submission_id'], 'reported')

    def test_automatic_refresh_preserves_selected_submission_image_and_zoom(self):
        self.load_submission()
        self.view.canvas.zoom_by(1.2)
        zoom = self.view.canvas.transform().m11()
        self.view.refresh_submissions(automatic=True)
        self.pending.pop()[1]([{'submission_id':'one','collaborator_id':'Researcher','image_count':1},
                              {'submission_id':'two','collaborator_id':'Other','image_count':1}], None)
        self.assertEqual(self.view.current_submission['submission_id'], 'one')
        self.assertEqual(self.view.current_image_filename, 'image.png')
        self.assertAlmostEqual(self.view.canvas.transform().m11(), zoom)
        self.assertEqual(self.view.frame_list.count(), 1)

    def test_manual_refresh_reloads_changed_annotations_and_preserves_image_zoom(self):
        self.load_submission()
        self.view.canvas.zoom_by(1.2)
        zoom = self.view.canvas.transform().m11()
        self.view.refresh_submissions()
        self.pending.pop()[1]([{'submission_id':'one','collaborator_id':'Researcher','image_count':1}], None)
        changed = copy.deepcopy(self.record); changed['boxes'][0]['class_id'] = 1
        self.pending.pop()[1]({'image.png':changed}, None)
        self.pending.pop()[1](str(self.path), None)
        self.assertEqual(self.view.current_image_filename, 'image.png')
        self.assertEqual(self.view.current_annotations['image.png']['boxes'][0]['class_id'], 1)
        self.assertEqual(self.view.current_annotations, self.view.reviewed_snapshot)
        self.assertAlmostEqual(self.view.canvas.transform().m11(), zoom)

    def test_automatic_refresh_defers_unsaved_edits_without_discard_prompt(self):
        self.load_submission()
        self.view.canvas.polygons[0].setSelected(True)
        self.view.canvas.reclassify(1)
        self.view.show(); APP.processEvents()
        with patch.object(review, 'confirm_action') as confirm:
            self.view.request_refresh()
        confirm.assert_not_called()
        self.assertFalse(self.pending)
        self.assertTrue(self.view._refresh_pending)
        self.assertEqual(self.view.current_annotations['image.png']['boxes'][0]['class_id'], 1)

    def test_refresh_notifications_coalesce_while_loading(self):
        self.view.show(); APP.processEvents()
        self.view.request_refresh(); self.view.request_refresh()
        self.assertEqual(len(self.pending), 1)
        self.pending.pop()[1]([], None)
        self.assertEqual(len(self.pending), 1)
        self.pending.pop()[1]([{'submission_id':'new','collaborator_id':'Researcher','image_count':1}], None)
        self.assertFalse(self.pending)
        self.assertEqual(self.view.sub_list.count(), 1)

    def test_foreign_account_or_dataset_response_cannot_populate_queue(self):
        callback = self.pending.pop()[1]
        with patch.object(review.tempo_hub, 'get_dataset_repo', return_value='different/dataset'):
            callback([{'submission_id':'old','collaborator_id':'Researcher','image_count':1}], None)
            self.assertEqual(self.view.sub_list.count(), 0)
            self.assertFalse(self.view._loading)
            self.assertTrue(self.view._refresh_pending)

    def test_failed_refresh_keeps_confirmed_selection_and_reports_error(self):
        self.load_submission()
        self.view.refresh_submissions()
        self.pending.pop()[1](None, PermissionError('Review access unavailable'))
        self.assertEqual(self.view.current_image_filename, 'image.png')
        self.assertEqual(self.view.sub_list.count(), 1)
        self.assertEqual(self.view.status.text(), 'Submissions could not be loaded.')
        self.assertTrue(self.view.retry_button.isVisibleTo(self.view))

    def test_server_failure_offers_retry_without_mislabeling_access(self):
        error = RuntimeError('HTTP 500')
        error.response = SimpleNamespace(status_code=500)
        self.pending.pop()[1](None, error)
        self.assertEqual(self.view.status.text(), 'Submissions could not be loaded.')
        self.assertIn('Hugging Face returned a server error.', self.view.error_details.summary.text())
        self.assertEqual(self.view.image_title.toPlainText(), '')
        self.assertTrue(self.view.retry_button.isVisibleTo(self.view))
        self.view.retry_button.click()
        self.pending.pop()[1]([{'submission_id':'reported','collaborator_id':'Researcher','image_count':1}], None)
        self.assertEqual(self.view.sub_list.count(), 1)

    def test_edits_started_during_fetch_survive_submission_removal(self):
        self.load_submission()
        self.view.refresh_submissions()
        self.view.canvas.polygons[0].setSelected(True)
        self.view.canvas.reclassify(1)
        self.pending.pop()[1]([], None)
        self.assertEqual(self.view.current_annotations['image.png']['boxes'][0]['class_id'], 1)
        self.assertEqual(self.view.current_image_filename, 'image.png')
        self.assertTrue(self.view._refresh_pending)

    def test_late_submission_response_does_not_replace_new_selection(self):
        self.pending.pop()[1]([
            {'submission_id':'one','collaborator_id':'A','image_count':1},
            {'submission_id':'two','collaborator_id':'B','image_count':1}],None)
        self.view.sub_list.setCurrentRow(0); old=self.pending.pop()[1]
        self.view.sub_list.setCurrentRow(1); new=self.pending.pop()[1]
        new({'new.png':copy.deepcopy(self.record)},None)
        old({'old.png':copy.deepcopy(self.record)},None)
        self.assertEqual(set(self.view.current_annotations),{'new.png'})
        self.assertEqual(self.view.frame_list.item(0).data(Qt.ItemDataRole.UserRole),'new.png')

    def test_edit_preserves_zoom_and_handles_are_selection_only(self):
        self.load_submission()
        self.assertFalse(any(handle.isVisible() for handle in self.view.canvas.handles))
        self.view.canvas.scale(2,2); zoom=self.view.canvas.transform().m11()
        self.view.canvas.polygons[0].setSelected(True)
        self.assertTrue(all(handle.isVisible() for handle in self.view.canvas.handles))
        self.view.canvas.reclassify(1)
        self.assertAlmostEqual(self.view.canvas.transform().m11(),zoom)

    def test_failed_submission_can_retry_without_changing_selection(self):
        self.pending.pop()[1]([{'submission_id':'one','collaborator_id':'Researcher','image_count':1}],None)
        self.view.sub_list.setCurrentRow(0)
        self.pending.pop()[1](None,ConnectionError('offline'))
        self.assertFalse(self.view.retry_button.isHidden())
        self.view.retry_button.click()
        self.assertEqual(len(self.pending),1)
        self.pending.pop()[1]({'image.png':copy.deepcopy(self.record)},None)
        self.pending.pop()[1](str(self.path),None)
        self.assertEqual(self.view.current_image_filename,'image.png')
        self.assertTrue(self.view.retry_button.isHidden())

    def test_failed_image_can_retry_without_reloading_annotations(self):
        self.pending.pop()[1]([{'submission_id':'one','collaborator_id':'Researcher','image_count':1}],None)
        self.view.sub_list.setCurrentRow(0)
        self.pending.pop()[1]({'image.png':copy.deepcopy(self.record)},None)
        self.pending.pop()[1](None,ConnectionError('offline'))
        self.assertTrue(all(not b.isEnabled() for b in self.view.view_buttons))
        self.view.retry_button.click()
        self.pending.pop()[1](str(self.path),None)
        self.assertTrue(all(b.isEnabled() for b in self.view.view_buttons))
        self.assertEqual(set(self.view.current_annotations),{'image.png'})

    def test_empty_review_does_not_instruct_selection(self):
        self.pending.pop()[1]([],None)
        self.assertEqual(self.view.image_title.toPlainText(),'')
        self.assertTrue(all(not b.isEnabled() for b in self.view.view_buttons))
