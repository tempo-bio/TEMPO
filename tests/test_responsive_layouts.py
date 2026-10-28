"""Geometry, scientific presentation, safe queue edits and exact metadata attribution."""
import copy
import json
import os
import time
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
from pathlib import Path
import tempfile
import unittest
from tests.qt_lifecycle import close_and_dispose, wait_for_qt
from unittest.mock import patch, Mock
import pandas as pd
from PyQt6.QtCore import Qt, QPoint, QRect
from PyQt6.QtGui import QFont, QImage, QPainter
from PyQt6.QtTest import QTest
from PyQt6.QtWidgets import QApplication, QWidget, QHBoxLayout, QFormLayout, QStyleFactory, QStyleOptionGroupBox, QStyleOptionComboBox, QStyleOptionButton, QStyle, QLabel, QPushButton
from tests.presentation_fixtures import (write_dataset, write_historical_dataset, processing_view, populated_review,
                                         application_style_name, set_application_style)
from tempo_spatial import validate_registration, registration_for_row, transform_points
from tempo_processing import ProcessingResult, edit_queue, read_queue
from tempo_ui import install_ui, SettingRow, InformationButton, UI_REVISION
APP = QApplication.instance() or QApplication([])
with patch('platform.machine', return_value='x86_64'):
    import tempo_gui as gui
install_ui(APP)


def settle(view, width=1100, height=760):
    view.resize(width, height); view.show()
    for _ in range(8):
        APP.processEvents()
    QTest.qWait(20)


class ResponsiveLayoutTests(unittest.TestCase):

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(); self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)

    def analyzer(self, registered=False, traces=False):
        view = gui.AnalyzerTab(); self.addCleanup(close_and_dispose, view)
        view.load_csv(str(write_dataset(self.root, registered, traces)))
        view.metric_combo.setCurrentIndex(view.metric_combo.findData('Peak_Amplitude_au'))
        return view

    def assert_button_text_fits(self, button):
        option = QStyleOptionButton(); option.initFrom(button)
        contents = button.style().subElementRect(QStyle.SubElement.SE_PushButtonContents, option, button)
        text = button.text().replace('&&', '\0').replace('&', '').replace('\0', '&')
        bounds = button.fontMetrics().boundingRect(text)
        self.assertGreaterEqual(contents.width(), bounds.width(), button.text())
        self.assertGreaterEqual(contents.height(), bounds.height(), button.text())

    def test_large_font_queue_action_text_fits_enabled_and_disabled_controls(self):
        old_font = APP.font()
        try:
            for size in (12, 18, 24):
                APP.setFont(QFont('Arial', size))
                with patch.object(gui.tempo_backend, 'QUEUE_FILE', str(self.root/'queue.json')):
                    view = processing_view(gui)
                self.addCleanup(close_and_dispose, view)
                for width in (960, 1100, 1500):
                    settle(view, width, 620)
                    for enabled in (False, True):
                        for label in ('Add Recordings', 'Locate Recording…', 'Remove from Queue'):
                            button = view.queue_buttons[label]; button.setEnabled(enabled)
                            APP.processEvents()
                            with self.subTest(size=size, width=width, enabled=enabled, label=label):
                                self.assert_button_text_fits(button)
                                self.assertEqual(view.width(), width)
                view.close()
        finally: APP.setFont(old_font)

    def test_annotation_navigation_and_submission_text_fit_without_losing_image_view(self):
        old_font = APP.font()
        try:
            for size in (12, 18, 24):
                APP.setFont(QFont('Arial', size))
                with patch.object(gui, 'run_background', return_value=None), \
                     patch.object(gui.tempo_backend, 'UNIVERSAL_DATASET_DIR', str(self.root)), \
                     patch.object(gui.tempo_hub, 'cached_lock_manifest', return_value={'images': {}}), \
                     patch.object(gui.tempo_hub, 'get_collaborator_id', return_value='Fixture'):
                    view = gui.AnnotatorTab()
                self.addCleanup(close_and_dispose, view)
                view.lease_timer.stop(); view._sync_timer.stop()
                for width in (960, 1100, 1500):
                    for mode in (0, 1):
                        view.mode_toggle.setCurrentIndex(mode); settle(view, width, 620)
                        buttons = (view.btn_import, view.btn_guidelines, view.btn_publish) if mode else (view.btn_load, view.btn_export)
                        for button in buttons:
                            with self.subTest(size=size, width=width, mode=mode, button=button.text()):
                                self.assert_button_text_fits(button)
                                scroll = (view.submission_scroll if button is view.btn_publish else
                                          view.annotation_inspector_scroll if button in (view.btn_export, view.btn_guidelines) else
                                          view.navigator_scroll)
                                scroll.ensureWidgetVisible(button, 0, 0)
                                for _ in range(4): APP.processEvents()
                                bounds = QRect(button.mapTo(scroll.viewport(), QPoint(0, 0)), button.size())
                                self.assertTrue(scroll.viewport().rect().contains(bounds), button.text())
                        self.assertEqual(view.width(), width)
                        self.assertGreater(view.center_stack.width(), 200)
                        self.assertGreater(view.center_stack.height(), 60)
                        self.assertEqual(view.navigator_scroll.horizontalScrollBar().maximum(), 0)
                view.close()
        finally: APP.setFont(old_font)

    def test_full_well_default_keeps_all_cells_without_inventing_positions(self):
        view = self.analyzer()
        self.assertIsNone(view.field_selector.currentData())
        self.assertEqual(view.cell_selector.count(), 13)
        self.assertEqual(view.well_cells.rowCount(), 12)
        self.assertEqual(len(view.current_x_vals), 0)
        self.assertFalse(view.well_cells.isHidden())
        self.assertTrue(view.glw.isHidden())
        self.assertIn('Full-well map unavailable', view.map_notice.text())
        self.assertFalse(hasattr(view, 'spatial_info'))
        self.assertEqual(view.lbl_amp.text(), '43.50')
        view.field_selector.setCurrentIndex(2)
        self.assertEqual(len(view.current_x_vals), 6)
        self.assertEqual(view.lbl_amp.text(), '43.50')
        view.cell_selector.setCurrentIndex(1)
        self.assertIn('recording-2', view.cell_context.text())
        view.return_to_well()
        self.assertIsNone(view.field_selector.currentData())
        self.assertEqual(view.cell_selector.currentIndex(), 0)

    def test_historical_schema_chooses_available_metric_and_every_field_has_points(self):
        view = gui.AnalyzerTab(); self.addCleanup(close_and_dispose, view)
        view.metric_combo.setCurrentIndex(view.metric_combo.findData('Peak_Amplitude_AreaNorm_au'))
        view.load_csv(str(write_historical_dataset(self.root)))
        self.assertEqual(view.metric_combo.currentData(), 'Peak_Amplitude_au')
        self.assertEqual(view.well_cells.rowCount(), 80)
        self.assertTrue(view.glw.isHidden())
        for field in range(1, view.field_selector.count()):
            view.field_selector.setCurrentIndex(field)
            self.assertEqual(len(view.scatter.points()), 16)
            self.assertFalse(view.glw.isHidden())
            self.assertEqual(view.map_notice.text(), '')
        self.assertFalse(hasattr(view, 'measurement_info'))
        for form in view.metric_forms:
            for row in range(form.rowCount()):
                label = form.itemAt(row,QFormLayout.ItemRole.LabelRole).widget()
                self.assertTrue(label.accessibleDescription())
                self.assertFalse(label.toolTip())
        self.assertFalse(any(label.text().startswith('CT / RT:') for label in view.findChildren(QLabel)))

    def test_populated_review_class_selector_applies_edit_and_undo_keeps_approval_separate(self):
        view = populated_review(self.root); self.addCleanup(close_and_dispose, view)
        view.complete.setChecked(True)
        view.canvas.polygons[0].setSelected(True)
        self.assertTrue(view.classes.isEnabled())
        self.assertEqual(view.classes.currentIndex(), 0)
        view.classes.activated.emit(1)
        self.assertEqual(view.canvas.boxes[0]['class_id'], 1)
        self.assertFalse(view.approve_button.isEnabled())
        self.assertEqual(view.classes.currentIndex(), 1)
        view.canvas.undo()
        self.assertEqual(view.canvas.boxes[0]['class_id'], 0)
        self.assertFalse(view.approve_button.isEnabled())
        for size in (12,18):
            view.setFont(QFont('Arial',size))
            settle(view,960,620)
            self.assertEqual(view.classes.height(),view.delete_button.height())
            self.assertGreaterEqual(view.classes.width(),view.classes.fontMetrics().horizontalAdvance('Select cell outlines first'))
        view.reject()

    def test_populated_queue_grows_before_scrolling_and_empty_queue_stays_bounded(self):
        path = self.root/'queue.json'
        # Even compact Windows item fonts must overflow the tall viewport.
        files = [str(self.root/f'recording-{i}.avi') for i in range(100)]
        for file in files: Path(file).write_bytes(b'synthetic placeholder')
        with patch.object(gui.tempo_backend,'QUEUE_FILE',str(path)):
            path.write_text(json.dumps(files))
            view = processing_view(gui); self.addCleanup(close_and_dispose, view)
            settle(view,1100,950)
            tall_height = view.queue_list.height()
            self.assertGreater(tall_height,500)
            self.assertGreater(view.queue_list.verticalScrollBar().maximum(),0)
            settle(view,960,620)
            self.assertLess(view.queue_list.height(),tall_height)
            self.assertEqual(view.processing_scroll.verticalScrollBar().maximum(),0)
            path.write_text(json.dumps(files[:8])); view.refresh_processing_queue(); settle(view,1100,950)
            self.assertEqual(view.queue_list.verticalScrollBar().maximum(),0)
            path.write_text('[]'); view.refresh_processing_queue(); settle(view,1100,950)
            self.assertLessEqual(view.queue_list.height(),90)

    def test_model_buttons_follow_font_and_fit_labels_in_annotation_sidebar(self):
        old_font=APP.font()
        try:
            for size in (12,18,24):
                APP.setFont(QFont('Arial',size))
                with patch.object(gui,'run_background',return_value=None), \
                     patch.object(gui.tempo_backend,'UNIVERSAL_DATASET_DIR',str(self.root)), \
                     patch.object(gui.tempo_hub,'cached_lock_manifest',return_value={'images':{}}), \
                     patch.object(gui.tempo_hub,'get_collaborator_id',return_value='Fixture'):
                    view=gui.AnnotatorTab()
                self.addCleanup(close_and_dispose, view)
                view.lease_timer.stop(); view._sync_timer.stop(); settle(view,1100,760)
                buttons=(view.btn_auto_nano,view.btn_auto_stardist,view.btn_auto_cellpose)
                for enabled in (False,True):
                    view.set_auto_buttons_enabled(enabled); APP.processEvents()
                    for button in buttons:
                        with self.subTest(size=size,enabled=enabled,model=button.text()):
                            self.assertTrue(button.font().bold())
                            self.assertGreaterEqual(button.fontMetrics().height(),view.fontMetrics().height())
                            self.assertGreaterEqual(button.width(),button.fontMetrics().horizontalAdvance(button.text())+12)
                            self.assertGreaterEqual(button.height(),button.fontMetrics().height()+8)
                view.close()
        finally:
            APP.setFont(old_font)

    def test_detection_availability_is_visible_and_notebook_controls_align(self):
        import maintainer_tab
        with patch.object(gui,'run_background',return_value=None), \
             patch.object(gui.tempo_backend,'UNIVERSAL_DATASET_DIR',str(self.root)), \
             patch.object(gui.tempo_hub,'cached_lock_manifest',return_value={'images':{}}), \
             patch.object(gui.tempo_hub,'get_collaborator_id',return_value='Fixture'):
            annotator=gui.AnnotatorTab()
        self.addCleanup(close_and_dispose, annotator)
        annotator.lease_timer.stop(); annotator._sync_timer.stop()
        settle(annotator)
        self.assertTrue(annotator.lbl_auto_status.isHidden())
        self.assertFalse(annotator.auto_availability.isHidden())
        self.assertIn('Select an image',annotator.auto_availability.text())
        self.assertLess(annotator.auto_availability.y(),annotator.btn_auto_nano.y())
        annotator.lbl_auto_status.setText('Detecting cells…')
        self.assertFalse(annotator.lbl_auto_status.isHidden())
        with patch.object(maintainer_tab,'run_background',return_value=None):
            maintainer=maintainer_tab.MaintainerTab(Mock())
        self.addCleanup(close_and_dispose, maintainer); maintainer.timer.stop()
        maintainer.pages.setEnabled(True)
        maintainer.pages.setTabEnabled(maintainer.pages.indexOf(maintainer.cloud_scroll),True)
        maintainer.pages.setCurrentWidget(maintainer.cloud_scroll)
        for size in (12,18):
            maintainer.setFont(QFont('Arial',size)); settle(maintainer)
            edge=maintainer.reopen.mapTo(maintainer.cloud_scroll,QPoint(0,0)).x()
            self.assertEqual(maintainer.url.mapTo(maintainer.cloud_scroll,QPoint(0,0)).x(),edge)
            self.assertEqual(maintainer.training_workflow.mapTo(maintainer.cloud_scroll,QPoint(0,0)).x(),edge)

    def test_full_well_pixel_axis_labels_do_not_overlap_at_small_and_large_fonts(self):
        old_font = APP.font()
        try:
            for size in (12,18):
                APP.setFont(QFont('Arial',size))
                view=gui.AnalyzerTab(); self.addCleanup(close_and_dispose, view)
                view.load_csv(str(write_historical_dataset(self.root)))
                for width,height in ((960,620),(1100,760),(1700,950)):
                    settle(view,width,height)
                    view.field_selector.setCurrentIndex(1)
                    for side in ('bottom', 'left'):
                        axis=view.plot_item.getAxis(side)
                        canvas=QImage(400,200,QImage.Format.Format_ARGB32)
                        painter=QPainter(canvas); painter.setFont(APP.font())
                        try: specs=axis.generateDrawSpecs(painter)
                        finally: painter.end()
                        if specs:
                            rectangles=[rect for rect,flags,text in specs[2]]
                            for first,second in zip(rectangles,rectangles[1:]):
                                self.assertFalse(first.intersects(second))
                view.close()
        finally: APP.setFont(old_font)

    def test_registered_fields_transform_and_navigator_selects_exact_identity(self):
        view = self.analyzer(registered=True)
        self.assertEqual(len(view.current_x_vals), 12)
        self.assertAlmostEqual(view.current_x_vals[0], -176)
        self.assertAlmostEqual(view.current_x_vals[6], 64)
        self.assertFalse(view.glw.isHidden())
        self.assertEqual(len(view.field_curves), 2)
        identity = ('recording-2', 'FOV002')
        view.field_curves[identity].sigClicked.emit(view.field_curves[identity], None)
        self.assertEqual(view.field_selector.currentData(), identity)
        self.assertEqual(list(view.current_x_vals), [8,16,24,32,40,48])
        self.assertEqual(len(view.current_well_data), 12)

    def test_bad_or_partial_registration_retains_measurements_and_identifies_unplaced_cells(self):
        path = write_dataset(self.root, registered=True)
        (self.root/'recording-2.spatial.json').unlink()
        view = gui.AnalyzerTab(); self.addCleanup(close_and_dispose, view); view.load_csv(str(path))
        view.metric_combo.setCurrentIndex(view.metric_combo.findData('Peak_Amplitude_au'))
        self.assertEqual(len(view.current_x_vals), 0)
        self.assertEqual(view.cell_selector.count(), 13)
        self.assertIn('recording-2', view.map_notice.text())
        self.assertEqual(view.well_cells.rowCount(), 12)
        self.assertTrue(view.spatial_warnings)
        empty = self.root/'empty.csv'; empty.write_text('Well_ID,Cell_ID\n')
        view.load_csv(str(empty))
        self.assertEqual(view.field_curves, {})
        self.assertEqual(view.current_row_ids, [])
        self.assertIsNone(view.summary_curve.getData()[0])

    def test_summary_waveform_works_for_legacy_well_and_cell_and_missing_data_clears_it(self):
        view = self.analyzer()
        self.assertEqual(view.wave_tabs.tabText(0), 'Illustrative Beat')
        self.assertEqual(view.wave_tabs.tabText(1), 'Recorded Signal')
        self.assertEqual(list(view.summary_curve.getData()[1]), [0,43.5,0,0])
        self.assertTrue(view.summary_note.isHidden())
        self.assertEqual(view.summary_info.toolTip(), 'Generated from summary measurements; not a recorded signal.')
        view.cell_selector.setCurrentIndex(1)
        self.assertEqual(list(view.summary_curve.getData()[1]), [0,41,0,0])
        self.assertTrue(view.mini_plot.isHidden())
        self.assertIn('unavailable', view.wave_note.text())
        self.assertEqual(view.grp_wave.toolTip(), '')
        view.wave_tabs.setCurrentIndex(1); settle(view)
        self.assertLess(view.wave_note.y(), 20)
        self.assertGreater(view.wave_note.height(), 0)
        view.draw_summary_wave({'Peak_Amplitude_au':float('nan')})
        self.assertIsNone(view.summary_curve.getData()[0])
        self.assertTrue(view.summary_plot.isHidden())

    def test_recorded_units_and_transition_clear_stale_axes_and_data(self):
        view = self.analyzer(traces=True)
        view.cell_selector.setCurrentIndex(1)
        self.assertEqual(view.wave_curve.getData()[1][3], 41)
        self.assertFalse(view.mini_plot.isHidden())
        view.trace_signal.setCurrentText('Speed (a.u.)')
        self.assertEqual(view.mini_plot.getPlotItem().getAxis('left').labelText, 'Speed')
        view.field_selector.setCurrentIndex(2)
        self.assertIsNone(view.wave_curve.getData()[0])
        self.assertTrue(view.mini_plot.isHidden())
        self.assertFalse(view.trace_signal.isEnabled())

    def test_information_is_optional_while_unavailable_states_remain_visible(self):
        view=self.analyzer(traces=True)
        settle(view)
        self.assertTrue(view.summary_info.isVisible())
        self.assertFalse(hasattr(view, 'measurement_info'))
        self.assertTrue(view.summary_note.isHidden())
        self.assertEqual(view.summary_info.accessibleDescription(), 'Generated from summary measurements; not a recorded signal.')
        view.cell_selector.setCurrentIndex(1)
        view.wave_tabs.setCurrentIndex(1); APP.processEvents()
        self.assertFalse(hasattr(view, 'recorded_info'))
        self.assertTrue(view.wave_note.isHidden())
        self.assertTrue(view.mini_plot.isVisible())
        view.draw_recorded_wave(None)
        self.assertTrue(view.wave_note.isVisible())
        self.assertIn('Select a cell',view.wave_note.text())
        view.draw_summary_wave({})
        view.wave_tabs.setCurrentIndex(0); APP.processEvents()
        self.assertTrue(view.summary_note.isVisible())
        self.assertIn('unavailable',view.summary_note.text())
        self.assertNotIn('not a recorded trace',view.summary_note.text())
        view.load_csv(str(write_dataset(self.root)))
        self.assertTrue(view.summary_note.isHidden())

    def test_information_button_supports_hover_keyboard_and_large_fonts(self):
        from tempo_ui import help_controller
        host=QWidget(); row=QHBoxLayout(host)
        button=InformationButton('Illustrative beat','An illustration <of> summary measurements.')
        row.addWidget(button); row.addStretch()
        self.addCleanup(close_and_dispose, host); self.addCleanup(help_controller().dismiss)
        for size in (12,18,24):
            host.setFont(QFont('Arial',size)); settle(host,420,180)
            QTest.mouseMove(host,QPoint(400,160)); APP.processEvents()
            self.assertEqual(button.width(),button.height())
            self.assertIn('<of>',button.toolTip())
            button.setFocus(); QTest.keyClick(button,Qt.Key.Key_Space)
            popup=help_controller().bubble
            self.assertIsNotNone(popup)
            self.assertLessEqual(popup.width(),360)
            self.assertTrue(host.rect().contains(popup.geometry()))
            self.assertEqual(popup.findChild(QLabel).font().pointSize(),size)
            QTest.keyClick(button,Qt.Key.Key_Escape)
            self.assertIsNone(help_controller().bubble)
            QTest.mouseMove(button,button.rect().center()); APP.processEvents()
            self.assertTrue(wait_for_qt(lambda: help_controller().bubble is not None))
            self.assertIsNotNone(help_controller().bubble)
            QTest.mouseMove(host,QPoint(400,160)); APP.processEvents()
            self.assertTrue(wait_for_qt(lambda: help_controller().bubble is None))
            self.assertIsNone(help_controller().bubble)

    def test_information_icons_fit_empty_and_populated_analysis(self):
        old_font=APP.font(); old_style=application_style_name(APP)
        try:
            for style in ('Fusion','Windows'):
                set_application_style(APP, style)
                for size in (12,18,24):
                    APP.setFont(QFont('Arial',size))
                    view=gui.AnalyzerTab(); self.addCleanup(close_and_dispose, view)
                    for populated in (False,True):
                        if populated: view.load_csv(str(write_dataset(self.root)))
                        settle(view,1100,760)
                        for tab_index, icon in ((0,view.summary_info),):
                            if tab_index is not None:
                                view.wave_tabs.setCurrentIndex(tab_index)
                                APP.processEvents()
                            if view.map_scroll.isAncestorOf(icon):
                                started = time.monotonic()
                                deadline = started + 2
                                waited = False
                                while True:
                                    APP.processEvents()
                                    view.map_scroll.ensureWidgetVisible(icon,0,0)
                                    ancestor = icon.parentWidget()
                                    fits = True
                                    while ancestor is not None and ancestor is not view:
                                        bounds = QRect(icon.mapTo(ancestor,QPoint(0,0)),icon.size())
                                        fits = fits and ancestor.rect().contains(bounds)
                                        ancestor = ancestor.parentWidget()
                                    if fits or time.monotonic() >= deadline:
                                        break
                                    waited = True
                                    QTest.qWait(10)
                                if waited:
                                    print(f'{style}/{size}: {icon.accessibleName()} layout fits={fits} after {time.monotonic()-started:.3f}s')
                            self.assertTrue(icon.isVisible())
                            self.assertGreaterEqual(icon.width(),icon.minimumSizeHint().width())
                            ancestor=icon.parentWidget()
                            while ancestor is not None and ancestor is not view:
                                bounds=QRect(icon.mapTo(ancestor,QPoint(0,0)),icon.size())
                                self.assertTrue(ancestor.rect().contains(bounds),f'{style}/{size}: {icon.accessibleName()} bounds {bounds} outside {type(ancestor).__name__} {ancestor.rect()}')
                                ancestor=ancestor.parentWidget()
                        self.assertIsNone(view.stats_tabs.cornerWidget(Qt.Corner.TopRightCorner))
                    view.close()
        finally:
            APP.setFont(old_font); set_application_style(APP, old_style)

    def test_browse_and_file_field_have_equal_height_and_alignment(self):
        old_font=APP.font()
        try:
            for size in (12,18,24):
                APP.setFont(QFont('Arial',size))
                view=gui.CaptureTab(); self.addCleanup(close_and_dispose, view); view.timer.stop()
                settle(view,1700 if size==24 else 1100,760)
                self.assertEqual(view.cfg_path.height(),view.btn_load_cfg.height())
                self.assertEqual(view.cfg_path.y(),view.btn_load_cfg.y())
                self.assertEqual(view.btn_load_cfg.width(),view.btn_load_cfg.fontMetrics().horizontalAdvance('Browse')+22)
                self.assertGreaterEqual(view.cfg_path.height(),view.cfg_path.fontMetrics().height()+10)
                bounds=QRect(view.btn_load_cfg.mapTo(view.cfg_row,QPoint(0,0)),view.btn_load_cfg.size())
                self.assertTrue(view.cfg_row.rect().contains(bounds))
                view.close()
        finally: APP.setFont(old_font)

    def test_settings_keep_descriptions_and_guidance_without_information_icons(self):
        view=gui.SettingsTab(); self.addCleanup(close_and_dispose, view)
        self.assertFalse(view.findChildren(InformationButton))
        labels=[label.text() for label in view.findChildren(QLabel)]
        self.assertIn('Requested frames per second.',labels)
        self.assertIn('Minimum peak prominence as a percentage of the signal range. Higher values exclude smaller peaks.', labels)
        self.assertTrue(any('Start with 100 FPS' in text for text in labels))
        self.assertFalse(any('detected peaks still require interpretation' in text.lower() for text in labels))

    def test_cloud_training_guidance_opens_in_workflow(self):
        import maintainer_tab
        with patch.object(maintainer_tab,'run_background',return_value=None), \
             patch.object(maintainer_tab.QTimer,'singleShot',return_value=None):
            maintainer=maintainer_tab.MaintainerTab(Mock())
        self.addCleanup(close_and_dispose, maintainer); maintainer.timer.stop()
        maintainer.pages.setEnabled(True)
        maintainer.pages.setCurrentWidget(maintainer.cloud_scroll)

        opened = []
        def inspect_workflow(dialog):
            opened.append(dialog.reader.toPlainText())

        with patch.object(maintainer_tab.TrainingWorkflowDialog, 'exec', inspect_workflow):
            maintainer.training_workflow.click()
        self.assertEqual(len(opened), 1)
        self.assertIn('Select a GPU runtime in Colab.', opened[0])
        self.assertFalse(maintainer.pending_work())

    def test_waveform_axis_titles_fit_the_plot_at_larger_fonts(self):
        old_font=APP.font()
        try:
            for size in (12,18):
                APP.setFont(QFont('Arial',size))
                view=self.analyzer(registered=True,traces=True)
                view.cell_selector.setCurrentIndex(1)
                view.wave_tabs.setCurrentIndex(1)
                for signal in range(view.trace_signal.count()):
                    view.trace_signal.setCurrentIndex(signal); settle(view)
                    self.assertFalse(view.mini_plot.isHidden())
                    bounds=view.mini_plot.getPlotItem().getAxis('left').label.sceneBoundingRect()
                    self.assertGreaterEqual(bounds.top(),0)
                    self.assertLessEqual(bounds.bottom(),view.mini_plot.height())
                    canvas=QImage(200,200,QImage.Format.Format_ARGB32)
                    painter=QPainter(canvas); painter.setFont(APP.font())
                    try:
                        specifications=[axis.generateDrawSpecs(painter) for axis in
                                        (view.mini_plot.getPlotItem().getAxis('left'),view.plot_item.getAxis('left'))]
                    finally: painter.end()
                    for specs in specifications:
                        labels=[rect for rect,flags,text in specs[2]]
                        for first,second in zip(labels,labels[1:]):
                            self.assertFalse(first.intersects(second))
                view.close()
        finally: APP.setFont(old_font)

    def test_metric_labels_visible_and_compact_across_sizes_fonts_and_styles(self):
        old_font, old_style = APP.font(), application_style_name(APP)
        try:
            for style in ('Fusion', 'Windows'):
                if style not in QStyleFactory.keys(): continue
                set_application_style(APP, style)
                for size in (12,18):
                    APP.setFont(QFont('Arial',size))
                    view = self.analyzer()
                    for width,height in ((1100,760),(960,620),(1700,950)):
                        settle(view,width,height)
                        for tab, forms in ((0,(view.form_core,view.form_kinetics,view.form_vel,view.form_anisotropy)),
                                           (1,(view.form_core_std,view.form_kinetics_std,view.form_vel_std,view.form_anisotropy_std))):
                            view.stats_tabs.setCurrentIndex(tab)
                            for _ in range(4): APP.processEvents()
                            for form in forms:
                                group = form.parentWidget(); option = QStyleOptionGroupBox(); group.initStyleOption(option)
                                title = group.style().subControlRect(QStyle.ComplexControl.CC_GroupBox, option, QStyle.SubControl.SC_GroupBoxLabel, group)
                                first = form.itemAt(0,QFormLayout.ItemRole.LabelRole).widget()
                                self.assertGreater(first.y(),title.bottom())
                                previous_bottom = None
                                for row in range(form.rowCount()):
                                    label = form.itemAt(row,QFormLayout.ItemRole.LabelRole).widget()
                                    value = form.itemAt(row,QFormLayout.ItemRole.FieldRole).widget()
                                    self.assertGreaterEqual(label.width(),120)
                                    self.assertGreaterEqual(label.height(),label.heightForWidth(label.width()))
                                    self.assertGreaterEqual(value.width(),value.fontMetrics().horizontalAdvance(value.text()))
                                    self.assertLessEqual(label.geometry().right(), value.geometry().left())
                                    if previous_bottom is not None:
                                        self.assertLess(label.y()-previous_bottom,15)
                                    previous_bottom=label.geometry().bottom()
                            self.assertEqual(view.inspector_scroll.horizontalScrollBar().maximum(),0)
                    view.close()
        finally:
            APP.setFont(old_font); set_application_style(APP, old_style)

    def test_settings_precision_round_trip_and_adaptive_rows(self):
        preferences = copy.deepcopy(gui.tempo_preferences.get_all())
        with patch.object(gui.tempo_preferences,'_preferences',preferences), patch.object(gui.tempo_preferences,'save_to_disk',return_value=True):
            view = gui.SettingsTab(); self.addCleanup(close_and_dispose, view)
            self.assertNotIn(('cruncher','in_memory_streaming'),view.widgets)
            p = view.widgets[('kinetics','asls_p')]
            self.assertEqual(p.value(),.001)
            self.assertEqual(p.decimals(),4)
            before = {(cat,key):spec['value'] for cat,values in preferences.items() for key,spec in values.items() if isinstance(spec['value'],float)}
            view.save_preferences()
            for key in before:
                if key in view.widgets:
                    self.assertEqual(gui.tempo_preferences.get(*key),before[key])
            settle(view,1700,950)
            for row in view.findChildren(SettingRow):
                self.assertFalse(row.stacked)
                self.assertGreaterEqual(row.label.width(), row.label.fontMetrics().horizontalAdvance(row.label.text()))
            settle(view,500,700)
            self.assertTrue(any(row.stacked for row in view.findChildren(SettingRow)))

    def test_settings_columns_and_wrapped_guidance_at_large_fonts(self):
        old_font = APP.font()
        try:
            for font in (12,18):
                APP.setFont(QFont('Arial',font))
                view=gui.SettingsTab(); self.addCleanup(close_and_dispose, view)
                for width in (960,1100,1700):
                    settle(view,width,760)
                    self.assertEqual(view.width(),width)
                    starts = set()
                    for row in view.findChildren(SettingRow):
                        if not row.stacked:
                            starts.add(row.control.mapTo(view,QPoint(0,0)).x())
                        self.assertLessEqual(row.control.geometry().right(),row.width())
                        for info in row.findChildren(InformationButton):
                            control_right=row.control.mapTo(row,QPoint(row.control.width(),0)).x()
                            info_left=info.mapTo(row,QPoint(0,0)).x()
                            self.assertGreaterEqual(info_left-control_right,0)
                            self.assertLessEqual(info_left-control_right,8)
                        for label in row.findChildren(QLabel):
                            if label.wordWrap():
                                self.assertGreaterEqual(label.height(),label.heightForWidth(label.width()))
                    self.assertLessEqual(len(starts),1)
                    self.assertTrue(starts or all(row.stacked for row in view.findChildren(SettingRow)))
                view.close()
        finally: APP.setFont(old_font)

    def test_analysis_toolbar_and_waveform_use_visible_space(self):
        old_font=APP.font()
        try:
            for size in (12,18):
                APP.setFont(QFont('Arial',size))
                view=self.analyzer(registered=True,traces=True)
                view.cell_selector.setCurrentIndex(1)
                view.wave_tabs.setCurrentIndex(1)
                for width,height in ((1100,760),(1700,950),(960,620),(1100,760)):
                    settle(view,width,height)
                    self.assertEqual(view.width(),width)
                    self.assertEqual(view.height(),height)
                    self.assertLessEqual(view.analysis_toolbar.height(), view.map_controls.heightForWidth(view.map_controls.width()) + view.analysis_navigation.heightForWidth(view.analysis_toolbar.width()) + 20)
                    for selector in (view.metric_combo,view.field_selector):
                        self.assertTrue(selector.isVisible())
                        self.assertGreaterEqual(selector.height(),selector.minimumSizeHint().height())
                        bounds=QRect(selector.mapTo(view.analysis_toolbar,QPoint(0,0)),selector.size())
                        self.assertTrue(view.analysis_toolbar.rect().contains(bounds),f'Control outside toolbar: {bounds}, toolbar={view.analysis_toolbar.rect()}')
                        option=QStyleOptionComboBox(); selector.initStyleOption(option)
                        text_rect=selector.style().subControlRect(QStyle.ComplexControl.CC_ComboBox,option,QStyle.SubControl.SC_ComboBoxEditField,selector)
                        self.assertGreaterEqual(text_rect.width(),selector.fontMetrics().horizontalAdvance(selector.currentText()))
                    self.assertEqual(view.map_scroll.horizontalScrollBar().maximum(),0)
                    if view.fontMetrics().height() <= 20 and height >= 760:
                        self.assertLessEqual(view.grp_wave.mapTo(view,QPoint(0,view.grp_wave.height())).y(),height)
                    else:
                        view.map_scroll.ensureWidgetVisible(view.grp_wave,0,0)
                        if view.grp_wave.height() > view.map_scroll.viewport().height():
                            view.map_scroll.ensureVisible(0, view.grp_wave.y() + view.grp_wave.height(), 0, 0)
                        for _ in range(3): APP.processEvents()
                        self.assertLessEqual(view.grp_wave.mapTo(view,QPoint(0,view.grp_wave.height())).y(),height)
                measured_height=view.grp_wave.height()
                view.draw_recorded_wave(None)
                view.wave_tabs.setCurrentIndex(1); settle(view)
                self.assertLess(view.grp_wave.height(),measured_height)
                self.assertLessEqual(view.wave_tabs.height(),view.wave_tabs.heightForWidth(view.wave_tabs.width()) + 4)
                self.assertFalse(any(b.text()=='Full Well' for b in view.findChildren(QPushButton)))
                view.close()
        finally: APP.setFont(old_font)

    def test_one_processing_action_preserves_cooperative_cancellation_state(self):
        view=self.processing()
        path=self.root/'recording.avi'; path.write_bytes(b'placeholder')
        edit_queue(self.queue,lambda _: [str(path)])
        view.refresh_processing_queue(); settle(view)
        self.assertIs(view.btn_cancel_processing,view.btn_process_batch)
        self.assertTrue(view.processing_results.isHidden())
        self.assertTrue(view.queue_buttons['Undo Location Change'].isHidden())
        view.worker=Mock(); view.worker.isRunning.return_value=True; view.worker.wait.return_value=False
        view.lock_buttons()
        self.assertEqual(view.btn_process_batch.text(),'Cancel Processing')
        self.assertTrue(view.btn_process_batch.isEnabled())
        view.btn_process_batch.click()
        view.worker.cancel.assert_called_once()
        self.assertTrue(view._processing_busy)
        self.assertEqual(view.btn_process_batch.text(),'Cancelling…')
        self.assertFalse(view.btn_process_batch.isEnabled())
        view.refresh_processing_queue()
        self.assertFalse(view.btn_process_batch.isEnabled())
        view.worker.wait.return_value=True
        view.unlock_buttons()
        self.assertEqual(view.btn_process_batch.text(),'Process Queue')
        self.assertTrue(view.btn_process_batch.isEnabled())

    def test_acquisition_fields_share_alignment_and_configuration_copy_fits(self):
        old_font=APP.font()
        try:
            for size in (12,18):
                APP.setFont(QFont('Arial',size))
                view=gui.CaptureTab(); self.addCleanup(close_and_dispose, view); view.timer.stop()
                settle(view)
                starts={w.mapTo(view,QPoint(0,0)).x() for w in (view.spin_exposure,view.exp_id,view.well_id,view.fov_id)}
                self.assertEqual(len(starts),1)
                view.btn_connection_settings.click(); APP.processEvents()
                self.assertGreaterEqual(view.cfg_path.width(),view.cfg_path.fontMetrics().horizontalAdvance(view.cfg_path.placeholderText()) + 12)
                self.assertTrue(view.camera_settings_dialog.isAncestorOf(view.btn_load_cfg))
                self.assertTrue(view.btn_load_cfg.isVisible())
                view.camera_settings_dialog.close()
                for control in (view.btn_connection_settings,view.btn_flir_preset):
                    view.acquisition_scroll.ensureWidgetVisible(control,0,0)
                    for _ in range(3): APP.processEvents()
                    bounds=QRect(control.mapTo(view.acquisition_scroll.viewport(),QPoint(0,0)),control.size())
                    self.assertTrue(view.acquisition_scroll.viewport().rect().contains(bounds))
                self.assertEqual(view.width(),1100)
                view.close()
        finally: APP.setFont(old_font)

    def test_plate_scout_has_one_action_and_waits_for_actual_finish(self):
        view=gui.CaptureTab(); self.addCleanup(close_and_dispose, view); view.timer.stop()
        tools=view.capture_controls
        view.core=Mock(); tools._stage_present=True
        worker=Mock(); worker.isRunning.return_value=True
        view.scout_worker=worker
        tools.refresh_controls()
        tools.auto_button.click()
        worker.stop.assert_called_once()
        self.assertEqual(tools.auto_button.text(),'Stopping…')
        self.assertFalse(tools.auto_button.isEnabled())
        worker.isRunning.return_value=False
        tools.refresh_controls()
        self.assertEqual(tools.auto_button.text(),'Automatic Collection…')
        self.assertTrue(tools.auto_button.isEnabled())

    def test_saved_high_precision_or_invalid_float_is_not_silently_changed(self):
        for saved in (.001234567,0.0):
            preferences=copy.deepcopy(gui.tempo_preferences.get_all())
            preferences['kinetics']['asls_p']['value']=saved
            with patch.object(gui.tempo_preferences,'_preferences',preferences), patch.object(gui.tempo_preferences,'save_to_disk',return_value=True) as save:
                view=gui.SettingsTab(); self.addCleanup(close_and_dispose, view)
                self.assertEqual(view.widgets[('kinetics','asls_p')].value(),saved)
                view.save_preferences()
                self.assertEqual(gui.tempo_preferences.get('kinetics','asls_p'),saved)
                if saved == 0.0:
                    save.assert_not_called()
                    self.assertIn('were not saved',view.lbl_save_status.text())
                else:save.assert_called_once()

    def test_application_details_identify_loaded_checkout_without_contacting_services(self):
        view=gui.SettingsTab(); self.addCleanup(close_and_dispose, view)
        with patch('tempo_logo.AboutTempoDialog') as dialog:
            view.show_application_details()
        dialog.assert_called_once_with(view, application_path=gui.__file__)
        dialog.return_value.exec.assert_called_once_with()
        self.assertEqual(APP.property('tempoUiRevision'),UI_REVISION)

    def processing(self):
        self.queue = self.root/'queue.json'
        self.patcher = patch.object(gui.tempo_backend,'QUEUE_FILE',str(self.queue))
        self.patcher.start(); self.addCleanup(self.patcher.stop)
        view=processing_view(gui); self.addCleanup(close_and_dispose, view)
        return view

    def test_queue_actions_without_selection_do_not_change_job_status_or_open_picker(self):
        view=self.processing()
        self.assertFalse(view.queue_buttons['Locate Recording…'].isEnabled())
        with patch.object(gui.QFileDialog,'getOpenFileName') as picker:
            view.relink_recording(); view.remove_recording()
        picker.assert_not_called()
        self.assertEqual(view.job_status.text(),'No active processing job.')
        self.assertLessEqual(view.queue_list.maximumHeight(),90)
        self.assertTrue(view.console_output.isHidden())
        self.assertEqual(view.btn_process_auto.text(),'Detect Cells…')

    def test_location_cancel_confirm_and_undo_preserve_concurrent_additions(self):
        view=self.processing()
        old=str(self.root/'missing.avi'); new=self.root/'moved.avi'; new.write_bytes(b'fixture')
        edit_queue(self.queue, lambda _: [old]); view.refresh_processing_queue(); view.queue_list.setCurrentRow(0)
        with patch.object(gui.QFileDialog,'getOpenFileName',return_value=('', '')):
            view.relink_recording()
        self.assertEqual(read_queue(self.queue),[old]); self.assertIsNone(view._queue_undo)
        with patch.object(gui.QFileDialog,'getOpenFileName',return_value=(str(new), '')), patch.object(gui,'confirm_action',return_value=True) as confirm:
            view.relink_recording()
        self.assertIn(old,confirm.call_args.args[2]); self.assertIn(str(new),confirm.call_args.args[2])
        edit_queue(self.queue,lambda q:q+['concurrent.avi'])
        view.undo_location_change()
        self.assertEqual(read_queue(self.queue),[old,'concurrent.avi'])
        self.assertTrue(new.exists())
        self.assertEqual(view.job_status.text(),'No active processing job.')

    def test_undo_rejects_completed_entry_and_existing_destination_without_mutating_queue(self):
        view=self.processing(); old=str(self.root/'old.avi'); new=self.root/'new.avi'; new.write_bytes(b'fixture')
        edit_queue(self.queue,lambda _:[str(new),'arrival.avi']); view._queue_undo=(old,str(new))
        gui.tempo_backend.remove_from_queue([str(new)])
        view.undo_location_change()
        self.assertEqual(read_queue(self.queue),['arrival.avi'])
        self.assertIn('cannot be undone',view.queue_error.details.toPlainText())
        edit_queue(self.queue,lambda _:[old,str(new)])
        with self.assertRaises(ValueError):gui.tempo_backend.relink_queue(old,str(new))
        self.assertEqual(read_queue(self.queue),[old,str(new)])

    def test_active_entry_cannot_be_located_removed_or_undone(self):
        view=self.processing(); old=str(self.root/'old.avi')
        edit_queue(self.queue,lambda _:[old]); view.refresh_processing_queue(); view.queue_list.setCurrentRow(0)
        with patch.object(gui.tempo_backend,'queue_entry_running',return_value=True), patch.object(gui.QFileDialog,'getOpenFileName') as picker:
            view.update_queue_actions(); view.relink_recording(); view.remove_recording()
        picker.assert_not_called()
        self.assertFalse(view.queue_buttons['Locate Recording…'].isEnabled())
        self.assertEqual(read_queue(self.queue),[old])

    def test_malformed_queue_is_not_changed_by_location_action_or_undo(self):
        view=self.processing(); self.queue.write_text('{broken')
        view._queue_undo=('old.avi','new.avi')
        view.refresh_processing_queue(); view.undo_location_change()
        self.assertEqual(self.queue.read_text(),'{broken')
        self.assertFalse(view.btn_process_batch.isEnabled())
        self.assertFalse(view.queue_buttons['Undo Location Change'].isEnabled())
        self.assertEqual(view.job_status.text(),'No active processing job.')

    def test_short_navigation_controls_fit_their_labels_and_reflow_at_large_font(self):
        old_font=APP.font()
        try:
            for size in (12,18,24):
                APP.setFont(QFont('Arial',size)); view=self.analyzer()
                for width in (960,1100,1700):
                    settle(view,width,760)
                    self.assertEqual(view.width(),width)
                    self.assertGreaterEqual(view.field_selector.width(),view.field_selector.minimumSizeHint().width())
                    self.assertGreaterEqual(view.plate_toggle.width(),view.plate_toggle.minimumSizeHint().width())
                    self.assertGreaterEqual(view.cell_selector.width(),view.cell_selector.minimumSizeHint().width())
                    self.assertGreaterEqual(view.well_selector.width(),view.well_selector.minimumSizeHint().width())
                view.close()
        finally:APP.setFont(old_font)

    def test_long_recording_context_does_not_force_the_window_wider(self):
        path=write_dataset(self.root)
        data=pd.read_csv(path)
        data['FOV_ID']='FOV001'
        data['Recording_ID']=data['Recording_ID'].map(lambda value: value + '-recording-context-' + 'x'*100)
        data.to_csv(path,index=False)
        view=gui.AnalyzerTab(); self.addCleanup(close_and_dispose, view); view.load_csv(str(path))
        view.field_selector.setCurrentIndex(2); settle(view,960,620)
        self.assertEqual(view.width(),960)
        self.assertIn('x'*100,view.field_selector.itemData(2,Qt.ItemDataRole.ToolTipRole))
        view.cell_selector.setCurrentIndex(1)
        self.assertIn('x'*100,view.cell_context.text())

    def test_expanded_processing_and_undo_remain_accessible_in_a_small_window(self):
        old_font=APP.font()
        try:
            APP.setFont(QFont('Arial',18))
            view=self.processing()
            old=self.root/'old.avi'; new=self.root/'new.avi'; new.write_bytes(b'placeholder')
            edit_queue(self.queue,lambda _: [str(new)])
            view._queue_undo=(str(old),str(new)); view.refresh_processing_queue()
            view.queue_feedback.setText('Recording location updated. Undo is available until processing starts.')
            view.testing_toggle.setChecked(True)
            view.record_processing_result(ProcessingResult(str(new),str(self.root),'no_beats'),persist=False)
            settle(view,960,620)
            self.assertEqual(view.size().width(),960); self.assertEqual(view.size().height(),620)
            self.assertTrue(view.queue_buttons['Undo Location Change'].isVisible())
            undo=view.queue_buttons['Undo Location Change']
            self.assertLessEqual(undo.width(),undo.sizeHint().width()+4)
            self.assertEqual(view.processing_scroll.horizontalScrollBar().maximum(),0)
            view.processing_scroll.ensureWidgetVisible(view.btn_process_now,0,0)
            for _ in range(4): APP.processEvents()
            bounds=QRect(view.btn_process_now.mapTo(view.processing_scroll.viewport(),QPoint(0,0)),view.btn_process_now.size())
            self.assertTrue(view.processing_scroll.viewport().rect().contains(bounds))
        finally: APP.setFont(old_font)



class SpatialContractTests(unittest.TestCase):

    def document(self):
        return dict(version=1,recording_id='r',well_id='A01',field_id='FOV001',units='um',
                    coordinate_system='well-A01',image_size_px=[32,24],pixel_to_well=[[2,0,10],[0,-2,20]])

    def test_validation_rejects_unknown_units_nonfinite_singular_and_missing_identity(self):
        for key,value in [('units','px'),('image_size_px',[0,24]),('pixel_to_well',[[1,0,0],[2,0,0]]),
                          ('pixel_to_well',[[1,0,float('nan')],[0,1,0]]),('recording_id',''),('version',2)]:
            document=self.document(); document[key]=value
            with self.subTest(key=key),self.assertRaises(ValueError):validate_registration(document)
        self.assertEqual(transform_points(self.document(),[1],[2]).tolist(),[[12,16]])

    def test_registration_path_and_recording_identity_cannot_be_misattributed(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory); (root/'spatial.json').write_text(json.dumps(self.document()))
            row={'Recording_ID':'r','Well_ID':'A01','FOV_ID':'FOV001','Spatial_Metadata_File':'spatial.json'}
            self.assertEqual(registration_for_row(root/'results.csv',row)['recording_id'],'r')
            for change in ({'Recording_ID':'other'},{'Spatial_Metadata_File':'../spatial.json'}):
                with self.assertRaises(ValueError):registration_for_row(root/'results.csv',dict(row,**change))
