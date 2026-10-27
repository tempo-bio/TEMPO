"""Isolated annotation feedback, category filters and populated panel geometry."""
import copy
import json
import os
from pathlib import Path
import unittest
from unittest.mock import patch

from PyQt6.QtCore import QPoint, QPointF, QRect, Qt
from PyQt6.QtTest import QTest
from PyQt6.QtWidgets import QStyle, QStyleOptionButton

from tests import test_desktop_workflows as desktop
from tempo_hub import TempoHub
from tempo_ui import InformationButton, install_ui

APP, gui = desktop.APP, desktop.tempo_gui


class AnnotationPanelTests(unittest.TestCase):
    setUp = desktop.DesktopTests.setUp
    browser_catalog = desktop.DesktopTests.browser_catalog
    begin_auto_annotation = desktop.DesktopTests.begin_auto_annotation
    auto_result = desktop.DesktopTests.auto_result

    def open_editor(self):
        self.view.refresh_universal_file_list(refresh=False)
        self.view.on_file_selected(self.view.list_files.item(0))

    def prepare_mask_selection(self):
        self.view.universal_annotations['a.png']['boxes'] = [
            {'class_id': 0, 'polygon': [[10,10],[80,10],[80,80],[10,80]]},
            {'class_id': 1, 'polygon': [[20,20],[40,20],[40,40],[20,40]]},
            {'class_id': 0, 'polygon': [[85,10],[95,10],[95,20],[85,20]]},
        ]
        self.open_editor()
        self.view.resize(1100, 760); self.view.show()
        for _ in range(12):
            APP.processEvents()

    def pick_mask(self, x, y, shift=False):
        modifiers = Qt.KeyboardModifier.ControlModifier
        if shift:
            modifiers |= Qt.KeyboardModifier.ShiftModifier
        canvas = self.view.view
        QTest.mouseClick(canvas.viewport(), Qt.MouseButton.LeftButton, modifiers,
                         canvas.mapFromScene(QPointF(x, y)))
        APP.processEvents()

    def selected_masks(self):
        return [item.data(Qt.ItemDataRole.UserRole) for item in self.view.list_boxes.selectedItems()]

    def test_canvas_selection_cycles_overlaps_and_synchronizes_list_and_actions(self):
        self.prepare_mask_selection()
        before = copy.deepcopy(self.view.get_current_boxes())
        with patch.object(self.view, 'save_manual_annotation_edit') as save:
            self.pick_mask(30, 30)
            self.assertEqual(self.selected_masks(), [1])
            self.assertEqual(self.view.list_boxes.currentRow(), 1)
            self.assertEqual(self.view.current_rect_items[1].pen().color().name(), '#00ffff')
            self.assertTrue(self.view.btn_delete.isEnabled())
            self.pick_mask(30, 30)
            self.assertEqual(self.selected_masks(), [0])
            self.pick_mask(30, 30)
            self.assertEqual(self.selected_masks(), [1])
            self.pick_mask(90, 15, shift=True)
            self.assertEqual(self.selected_masks(), [1, 2])
            self.pick_mask(90, 15, shift=True)
            self.assertEqual(self.selected_masks(), [1])
            save.assert_not_called()
        self.assertEqual(self.view.get_current_boxes(), before)
        self.view.list_boxes.setCurrentRow(0)
        self.assertEqual(self.view.current_rect_items[0].pen().color().name(), '#00ffff')

    def test_modified_click_preserves_draft_and_empty_click_never_draws(self):
        self.prepare_mask_selection()
        canvas = self.view.view
        QTest.mouseClick(canvas.viewport(), Qt.MouseButton.LeftButton,
                         pos=canvas.mapFromScene(QPointF(15, 15)))
        draft = copy.deepcopy(canvas.current_polygon_pts)
        self.assertEqual(len(draft), 1)
        self.pick_mask(30, 30)
        self.pick_mask(98, 98)
        self.assertEqual(canvas.current_polygon_pts, draft)
        self.assertEqual(self.selected_masks(), [1])
        self.assertEqual(len(self.view.get_current_boxes()), 3)
        QTest.keyClick(canvas, Qt.Key.Key_Escape)
        self.assertFalse(canvas.current_polygon_pts)
        self.assertEqual(self.selected_masks(), [1])

    def test_hidden_masks_and_polygon_bounding_box_gaps_are_not_selectable(self):
        self.prepare_mask_selection()
        self.view.show_overlays.setChecked(False)
        self.pick_mask(30, 30)
        self.assertEqual(self.selected_masks(), [])
        self.view.show_overlays.setChecked(True)
        self.view.universal_annotations['a.png']['boxes'] = [
            {'class_id': 0, 'polygon': [[10,10],[80,10],[10,80]]}]
        self.view.refresh_boxes()
        self.pick_mask(70, 70)
        self.assertEqual(self.selected_masks(), [])
        self.pick_mask(20, 20)
        self.assertEqual(self.selected_masks(), [0])

    def test_readonly_selection_and_image_clear_preserve_edit_restrictions(self):
        self.prepare_mask_selection()
        self.view._current_file_readonly = True
        self.view.view.set_draw_mode('none')
        self.pick_mask(30, 30)
        self.assertEqual(self.selected_masks(), [1])
        self.assertFalse(self.view.btn_delete.isEnabled())
        self.assertFalse(self.view.btn_mark_dead.isEnabled())
        self.view.on_file_selected(None)
        self.pick_mask(30, 30)
        self.assertEqual(self.selected_masks(), [])

    def test_canvas_selection_reveals_offscreen_list_entry_and_resets_after_refresh(self):
        self.prepare_mask_selection()
        boxes = self.view.universal_annotations['a.png']['boxes']
        boxes[2:2] = [copy.deepcopy(boxes[0]) for _ in range(40)]
        self.view.refresh_boxes(); APP.processEvents()
        self.pick_mask(90, 15)
        self.assertEqual(self.selected_masks(), [42])
        item = self.view.list_boxes.item(42)
        self.assertTrue(self.view.list_boxes.viewport().rect().contains(
            self.view.list_boxes.visualItemRect(item)))
        boxes.pop()
        self.view.refresh_boxes()
        self.pick_mask(90, 15)
        self.assertEqual(self.selected_masks(), [])

    def seed_feedback(self):
        self.view.lbl_auto_status.setText('2 annotations generated.')
        self.view.auto_error.set_error('Detection failed.', RuntimeError('fixture'))

    def assert_feedback_cleared(self):
        self.assertEqual(self.view.lbl_auto_status.text(), '')
        self.assertTrue(self.view.lbl_auto_status.isHidden())
        self.assertTrue(self.view.auto_error.isHidden())

    def pool(self):
        entries = {
            'a.png': TempoHub._lease('Alice'),
            'available.png': {'status': 'available'},
            'approved.png': {'status': 'approved'},
            'mine-submitted.png': dict(TempoHub._lease('Alice'), status='submitted'),
            'others.png': TempoHub._lease('Bob'),
            'others-submitted.png': dict(TempoHub._lease('Bob'), status='submitted'),
            'expired.png': dict(TempoHub._lease('Bob'), expires_at='2000-01-01T00:00:00+00:00'),
        }
        self.manifest['images'] = entries
        for name in (*entries, 'local.png'):
            if name != 'a.png':
                (self.root / name).write_bytes((self.root / 'a.png').read_bytes())
        self.view.universal_annotations['local.png'] = {'boxes': []}
        self.view.mode_toggle.setCurrentIndex(1)
        return self.view.shared_browser

    @staticmethod
    def visible(browser):
        return {name for name, item in browser.items.items() if not item.isHidden()}

    def test_filters_default_to_all_and_combine_categories(self):
        browser = self.pool()
        self.assertFalse(any(box.isChecked() for box in browser.filters.values()))
        self.assertEqual(self.visible(browser), set(browser.items))
        with patch.object(gui.tempo_hub, 'fetch_image_catalog') as fetch:
            browser.filters['available'].setChecked(True)
            self.assertEqual(self.visible(browser), {'available.png', 'expired.png'})
            browser.filters['approved'].setChecked(True)
            self.assertEqual(self.visible(browser), {'available.png', 'expired.png', 'approved.png'})
            browser.filters['available'].setChecked(False)
            self.assertEqual(self.visible(browser), {'approved.png'})
            browser.filters['approved'].setChecked(False)
            browser.filters['mine'].setChecked(True)
            self.assertEqual(self.visible(browser), {'a.png', 'mine-submitted.png'})
            self.assertIn('awaiting review', browser.items['mine-submitted.png'].text())
            self.assertIn('awaiting review', browser.items['others-submitted.png'].text())
            browser.filters['mine'].setChecked(False)
            self.assertEqual(self.visible(browser), set(browser.items))
            fetch.assert_not_called()

    def test_empty_filter_results_and_refresh_preserve_filter_choices(self):
        browser = self.pool()
        browser.filters['approved'].setChecked(True)
        del self.manifest['images']['approved.png']
        (self.root / 'approved.png').unlink()
        browser.load()
        self.assertTrue(browser.filters['approved'].isChecked())
        self.assertEqual(self.visible(browser), set())
        self.assertFalse(browser.select_first.isEnabled())
        browser.filters['approved'].setChecked(False)
        self.assertEqual(self.visible(browser), set(browser.items))

    def test_hidden_checks_preserve_preview_and_both_batch_actions(self):
        browser = self.pool()
        browser.images.setCurrentItem(browser.items['a.png'])
        browser.items['a.png'].setCheckState(Qt.CheckState.Checked)
        before = self.view.current_file
        browser.filters['available'].setChecked(True)
        self.assertEqual(self.view.current_file, before)
        self.assertEqual(browser.focused_name, 'a.png')
        self.assertEqual(browser.checked_names(), ['a.png'])
        self.assertEqual(self.view.selected_annotation_paths(), [str(self.root / 'a.png')])
        self.assertIn('a.png', self.view.submission_payload())
        self.assertTrue(self.view.btn_publish.isEnabled())
        self.assertEqual(browser.hidden_selection.text(), '1 selected image hidden')
        batches = []
        browser.reserve_requested.disconnect(self.view.reserve_shared_images)
        browser.reserve_requested.connect(batches.append)
        browser.reserve_selected.click()
        self.assertEqual(batches, [['a.png']])
        browser.filters['available'].setChecked(False)
        self.assertTrue(browser.hidden_selection.isHidden())
        self.assertEqual(browser.hidden_selection.text(), '')

    def test_select_first_only_changes_visible_checks(self):
        browser = self.pool()
        browser.items['a.png'].setCheckState(Qt.CheckState.Checked)
        browser.filters['available'].setChecked(True)
        browser.count.setValue(1)
        browser.select_first_available()
        self.assertEqual(set(browser.checked_names()), {'a.png', 'available.png'})
        self.assertEqual(browser.hidden_selection.text(), '1 selected image hidden')
        browser.load()
        self.assertEqual(set(browser.checked_names()), {'a.png', 'available.png'})
        self.assertTrue(browser.filters['available'].isChecked())

    def test_detection_feedback_clears_on_manual_edits_and_restore(self):
        self.open_editor()
        for action in ('draw', 'classify', 'delete', 'restore'):
            with self.subTest(action=action):
                if not self.view.get_current_boxes():
                    self.view.add_polygon([[10, 10], [20, 10], [20, 20]])
                self.seed_feedback()
                self.view.list_boxes.item(0).setSelected(True)
                if action == 'draw':
                    self.view.add_polygon([[40, 40], [50, 40], [50, 50]])
                elif action == 'classify':
                    self.view.classify_selected_shapes(0)
                elif action == 'delete':
                    self.view.delete_box()
                else:
                    backup = self.root / 'restore.json'
                    backup.write_text(json.dumps({'a.png': {'boxes': self.auto_result()['boxes']}}))
                    with patch.object(gui.QFileDialog, 'getOpenFileName', return_value=(str(backup), '')):
                        self.view.restore_annotation_backup()
                    self.assertEqual(self.view.restore_status.text(), 'Annotations restored.')
                self.assert_feedback_cleared()

    def test_detection_feedback_clears_for_preview_loading_switch_clear_and_mode(self):
        browser = self.pool()
        browser.images.setCurrentItem(browser.items['a.png'])
        self.seed_feedback()
        self.view.loading_shared_preview('available.png')
        self.assert_feedback_cleared()
        self.assertEqual(self.view.lbl_lock_status.text(), '')
        self.assertEqual(self.view.lbl_image_name.toPlainText(), '')
        self.seed_feedback()
        browser.images.setCurrentItem(browser.items['available.png'])
        self.assert_feedback_cleared()
        self.assertEqual(self.view.lbl_image_name.toPlainText(), 'available.png')
        self.seed_feedback()
        self.view.on_file_selected(None)
        self.assert_feedback_cleared()
        self.assertTrue(self.view.lbl_lock_status.isHidden())
        self.seed_feedback()
        self.view.mode_toggle.setCurrentIndex(0)
        self.assert_feedback_cleared()

    def test_local_image_switch_clears_detection_feedback(self):
        self.view.is_universal_mode = False
        self.view.current_dir = str(self.root)
        for name in ('a.png', 'b.png'):
            if name == 'b.png':
                (self.root / name).write_bytes((self.root / 'a.png').read_bytes())
            item = gui.QListWidgetItem(name)
            item.setData(Qt.ItemDataRole.UserRole, str(self.root / name))
            self.view.list_files.addItem(item)
        self.view.list_files.setCurrentRow(0)
        self.seed_feedback()
        self.view.list_files.setCurrentRow(1)
        self.assert_feedback_cleared()
        self.assertEqual(self.view.lbl_image_name.toPlainText(), 'b.png')

    def test_late_worker_signals_cannot_update_a_new_image_or_request(self):
        worker = self.begin_auto_annotation()
        request = self.view._auto_pending
        status = worker.status_signal.connect.call_args.args[0]
        finished = worker.finished_signal.connect.call_args.args[0]
        failed = worker.error_signal.connect.call_args.args[0]
        status('Loading model…')
        self.assertEqual(self.view.lbl_auto_status.text(), 'Loading model…')
        self.view.current_file = str(self.root / 'other.png')
        self.view.clear_detection_feedback()
        status('Old progress')
        failed('Old failure')
        self.assert_feedback_cleared()
        self.view._auto_pending = dict(request)
        self.view.current_file = request['file']
        new_request = self.view._auto_pending
        self.view.lbl_auto_status.setText('New detection…')
        finished(self.auto_result())
        failed('Old failure')
        status('Old progress')
        self.assertIs(self.view._auto_pending, new_request)
        self.assertEqual(self.view.lbl_auto_status.text(), 'New detection…')
        self.view._auto_pending = None

    def test_successful_sync_collapses_feedback_and_newer_edits_stay_visible(self):
        self.open_editor()
        record = self.view.universal_annotations['a.png']
        record['sync_pending'] = True
        self.view._sync_snapshot = {'a.png': self.view.normalized_draft('a.png')}
        self.view.on_annotation_sync_finished(None, None)
        self.assertTrue(self.view.lbl_sync_status.isHidden())
        record['sync_pending'] = True
        record['boxes'].append(copy.deepcopy(record['boxes'][0]))
        self.view.on_annotation_sync_finished(None, None)
        self.assertEqual(self.view.lbl_sync_status.text(), 'Annotations pending sync')

    def test_shared_images_information_icon_is_removed(self):
        self.assertFalse(self.view.shared_browser.findChildren(InformationButton))

    def test_header_uses_one_line_when_the_text_fits_and_clears_on_deselect(self):
        install_ui(APP)
        self.open_editor()
        self.view.resize(1800, 950); self.view.show()
        for _ in range(12):
            APP.processEvents()
        filename, status = self.view.lbl_image_name, self.view.lbl_lock_status
        self.assertEqual(filename.parentWidget(), status.parentWidget())
        self.assertLess(filename.geometry().right(), status.x())
        self.assertLessEqual(status.geometry().right(), self.view.preview_header.width())
        required = filename.fontMetrics().horizontalAdvance(filename.toPlainText()) + 12 + status.sizeHint().width() + 12
        self.assertGreaterEqual(self.view.preview_header.width(), required)
        self.assertLessEqual(status.height(), status.fontMetrics().lineSpacing() + 4)
        self.view.on_file_selected(None)
        self.assertEqual(filename.toPlainText(), '')
        self.assertEqual(status.text(), '')

    def test_feedback_states_render_without_moving_reservation_controls(self):
        install_ui(APP)
        browser = self.pool()
        browser.images.setCurrentItem(browser.items['a.png'])
        self.view.lease_timer.stop(); self.view._sync_timer.stop()
        self.view.resize(1100, 760); self.view.show()
        output = os.environ.get('TEMPO_ANNOTATION_SCREEN_DIR')
        if output:
            Path(output).mkdir(parents=True, exist_ok=True)

        def capture(state):
            for _ in range(12): APP.processEvents()
            if output:
                self.view.grab().save(str(Path(output) / f'state-{state}.png'))

        capture('editable')
        for state in ('detecting', 'pending-sync', 'error', 'loading', 'readonly', 'empty', 'long-filename'):
            if state == 'detecting':
                self.view.lbl_auto_status.setText('Detecting cells…')
                self.view._auto_pending = {'file': self.view.current_file, 'universal': True, 'boxes': copy.deepcopy(self.view.get_current_boxes())}
                self.view.set_auto_buttons_enabled(False)
            elif state == 'pending-sync':
                self.view._auto_pending = None
                self.view.set_auto_buttons_enabled(True)
                self.view.clear_detection_feedback()
                self.view.lbl_sync_status.setText('Annotations pending sync')
            elif state == 'error':
                self.view.sync_error.set_error('Sync failed. Check your connection.', ConnectionError('offline fixture'))
            elif state == 'loading':
                self.view.loading_shared_preview('available.png')
            elif state == 'readonly':
                browser.images.setCurrentItem(browser.items['approved.png'])
                self.assertFalse(self.view.btn_auto_cellpose.isEnabled())
                self.assertTrue(self.view.reservation_actions.isHidden())
            elif state == 'empty':
                self.view.on_file_selected(None)
                self.assertTrue(self.view.lbl_lock_status.isHidden())
            else:
                browser.images.setCurrentItem(browser.items['a.png'])
                self.view.lbl_image_name.setText('Cardiomyocyte_experiment_long_sample_name_' * 5 + 'A01.png')
            capture(state)
            if state in ('pending-sync', 'error'):
                self.assertEqual(self.view.reservation_actions.y() - self.view.btn_publish.geometry().bottom() - 1, 6)
        browser.filters['mine'].setChecked(True)
        browser.items['a.png'].setCheckState(Qt.CheckState.Checked)
        browser.filters['mine'].setChecked(False)
        browser.filters['available'].setChecked(True)
        capture('hidden-selection')
        self.assertGreaterEqual(browser.hidden_selection.height(),
                                browser.hidden_selection.heightForWidth(browser.hidden_selection.width()))
        self.assertTrue(browser.filter_row.rect().contains(browser.hidden_selection.geometry()))
        browser.filters['available'].setChecked(False)
        capture('all-images')
        self.assertTrue(browser.hidden_selection.isHidden())
        self.assertEqual(browser.filter_row.height(), browser.filter_row.flow.heightForWidth(browser.filter_row.width()))

    def test_populated_geometry_and_offscreen_renders(self):
        install_ui(APP)
        browser = self.pool()
        for index in range(30):
            name = f'sample{index}.png'
            self.manifest['images'][name] = {'status': 'available'}
            (self.root / name).write_bytes((self.root / 'a.png').read_bytes())
        browser.load()
        browser.images.setCurrentItem(browser.items['a.png'])
        self.view.lease_timer.stop(); self.view._sync_timer.stop()
        output = os.environ.get('TEMPO_ANNOTATION_SCREEN_DIR')
        if output:
            Path(output).mkdir(parents=True, exist_ok=True)
        for font_size in (12, 18, 24):
            # Qt stylesheets prevent application-font changes from cascading
            # into existing descendants. Scale the rendered controls explicitly.
            self.view.setStyleSheet(f'QWidget {{ font: {font_size}pt Arial; }}')
            for width, height in ((1100, 760), (1500, 950), (960, 620)):
                self.view.resize(width, height); self.view.show()
                for _ in range(12):
                    APP.processEvents()
                with self.subTest(font=font_size, width=width):
                    self.assertEqual(self.view.width(), width)
                    for panel in (self.view.navigator_scroll, self.view.annotation_sidebar, self.view.submission_scroll):
                        self.assertTrue(self.view.rect().contains(QRect(panel.mapTo(self.view, QPoint()), panel.size())))
                    self.assertGreater(self.view.center_stack.height(), 60)
                    row = self.view.classify_actions
                    buttons = (self.view.btn_mark_healthy, self.view.btn_mark_dead, self.view.btn_guidelines)
                    self.assertEqual(buttons[0].x(), 0)
                    self.assertEqual(buttons[-1].geometry().right() + 1, row.width())
                    self.assertEqual(row.width(), self.view.btn_delete.width())
                    for left, right in zip(buttons, buttons[1:]):
                        self.assertEqual(right.x() - left.geometry().right() - 1, 6)
                    for button in (*buttons, self.view.btn_renew, self.view.btn_release):
                        self.assertEqual(button.font().pointSize(), font_size)
                        option = QStyleOptionButton(); option.initFrom(button)
                        bounds = button.style().subElementRect(QStyle.SubElement.SE_PushButtonContents, option, button)
                        ink = button.fontMetrics().boundingRect(button.text().replace('&&', '&'))
                        self.assertGreaterEqual(bounds.width(), ink.width(), button.text())
                        self.assertGreaterEqual(bounds.height(), ink.height(), button.text())
                    renew, release = self.view.btn_renew, self.view.btn_release
                    self.assertEqual(release.x() - renew.geometry().right() - 1, 6)
                    self.assertEqual(self.view.reservation_actions.width(), self.view.btn_publish.width())
                    self.assertEqual(release.geometry().right() + 1, self.view.btn_publish.width())
                    self.assertEqual(self.view.reservation_actions.y() - self.view.btn_publish.geometry().bottom() - 1, 6)
                    self.view.submission_scroll.verticalScrollBar().setValue(0)
                    viewport = self.view.submission_scroll.viewport()
                    for button in (renew, release):
                        self.assertTrue(viewport.rect().contains(QRect(button.mapTo(viewport, QPoint()), button.size())))
                    self.assertGreater(browser.images.verticalScrollBar().maximum(), 0)
                    self.assertGreater(self.view.lbl_lock_status.x(), self.view.lbl_image_name.x())
                    for control in (self.view.btn_guidelines, self.view.btn_delete):
                        scroll = self.view.annotation_inspector_scroll
                        scroll.ensureWidgetVisible(control, 0, 0)
                        APP.processEvents()
                        self.assertTrue(scroll.viewport().rect().contains(QRect(control.mapTo(scroll.viewport(), QPoint()), control.size())))
                    if output:
                        self.view.grab().save(str(Path(output) / f'populated-{width}-{font_size}.png'))


if __name__ == '__main__':
    unittest.main()
