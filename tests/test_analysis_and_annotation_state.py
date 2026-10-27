"""Isolated UI state, cancellation, and read lifecycle regressions."""
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
from contextlib import ExitStack
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch, Mock
import pandas as pd
from PyQt6.QtWidgets import QApplication
from PyQt6.QtTest import QTest
from PyQt6.QtCore import QTimer, QCoreApplication, QEvent
APP = QApplication.instance() or QApplication([])
with patch('platform.machine', return_value='x86_64'):
    import tempo_gui as gui
from tempo_tasks import ReadTaskDialog


class AnalysisStateTests(unittest.TestCase):

    def setUp(self):
        self.view = gui.AnalyzerTab(); self.addCleanup(self.dispose_view)
        self.view.metric_combo.setCurrentIndex(self.view.metric_combo.findData('Peak_Amplitude_au'))
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name, 'results.csv')
        pd.DataFrame({'Well_ID':['A01']*3, 'Recording_ID':['r1','r1','r2'],
                      'FOV_ID':['FOV001','FOV002','FOV001'], 'Cell_ID':['1']*3,
                      'Centroid_X':[1,10,None], 'Centroid_Y':[2,20,None],
                      'Peak_Amplitude_au':[4,5,6]}).to_csv(self.path,index=False)
        self.view.load_csv(str(self.path))

    def dispose_view(self):
        self.view.close()
        self.view.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        APP.processEvents()

    def test_missing_metric_clears_cache_and_valid_metric_clears_error(self):
        view = self.view
        view.field_selector.setCurrentIndex(1)
        view.metric_combo.setCurrentIndex(view.metric_combo.findData('Peak_Amplitude_au'))
        view.update_map()
        self.assertEqual(len(view.scatter.points()), 1)
        view.metric_combo.setCurrentIndex(view.metric_combo.findData('Time_to_Peak_ms'))
        view.update_map(); view.update_scatter_colors()
        self.assertEqual(len(view.scatter.points()), 0)
        self.assertEqual(len(view.current_z_vals), 0)
        self.assertFalse(view.color_bar.isVisible())
        self.assertEqual(view.color_bar.levels(), (0, 1))
        self.assertIn('selected measurement', view.map_notice.text())
        self.assertEqual(view.analysis_status.text(), 'results.csv')
        view.metric_combo.setCurrentIndex(view.metric_combo.findData('Peak_Amplitude_au'))
        view.update_map()
        self.assertEqual(view.map_notice.text(), '')
        self.assertEqual(len(view.scatter.points()), 1)
        self.assertTrue(view.color_bar.isVisible())

    def test_fields_do_not_merge_and_nonspatial_cells_remain_selectable(self):
        view = self.view
        self.assertEqual(len(view.current_well_data), 3)
        self.assertEqual(view.field_selector.count(), 4)
        view.field_selector.setCurrentIndex(2)
        self.assertEqual(list(view.current_x_vals), [10])
        view.field_selector.setCurrentIndex(3)
        self.assertEqual(len(view.scatter.points()), 0)
        self.assertEqual(view.cell_selector.count(), 2)
        view.cell_selector.setCurrentIndex(1)
        self.assertEqual(view.lbl_amp.text(), '6.00')

    def test_invalid_replacement_preserves_dataset_and_selection(self):
        before = self.view.df.copy()
        bad = self.path.with_name('bad.csv')
        bad.write_text('Well_ID,Cell_ID,Centroid_X\nA01,1,broken\n')
        self.view.load_csv(str(bad))
        pd.testing.assert_frame_equal(self.view.df, before)
        self.assertEqual(self.view.dataset_path, str(self.path))
        self.view.metric_combo.setCurrentIndex(self.view.metric_combo.findData('Time_to_Peak_ms'))
        self.view.update_map()
        self.assertIn('invalid', self.view.analysis_error.details.toPlainText())

    def test_all_null_measurement_cannot_redraw_old_points(self):
        self.view.field_selector.setCurrentIndex(1)
        self.view.current_field_data = self.view.current_field_data.assign(Peak_Amplitude_au=float('nan'))
        self.view.update_map()
        self.view.update_scatter_colors()
        self.assertEqual(len(self.view.scatter.points()), 0)
        self.assertIn('No measured values', self.view.map_notice.text())

    def test_recorded_trace_clears_on_field_transition(self):
        trace = self.path.with_name('trace.csv')
        trace.write_text('Time (ms),Contraction (Raw a.u.)\n0,2\n1,3\n')
        self.view.draw_recorded_wave({'Cell_ID':'1','Trace_File':'trace.csv'})
        self.assertEqual(list(self.view.wave_curve.getData()[1]), [2,3])
        self.view.field_selector.setCurrentIndex(1)
        self.assertIsNone(self.view.wave_curve.getData()[0])

    def test_valid_empty_replacement_clears_old_data_and_click_identities(self):
        empty = self.path.with_name('empty.csv')
        empty.write_text('Well_ID,Cell_ID,Peak_Amplitude_au\n')
        self.view.load_csv(str(empty))
        self.assertEqual(self.view.dataset_path, str(empty))
        self.assertEqual(len(self.view.scatter.points()), 0)
        self.assertEqual(self.view.current_row_ids, [])
        self.assertEqual(self.view.cell_selector.count(), 0)
        self.assertEqual(self.view.lbl_amp.text(), '-')
        self.assertFalse(self.view.stats_tabs.isHidden())
        self.assertIn('no cell measurements', self.view.map_notice.text())

    def test_inspector_heading_keeps_title_style_for_empty_well_and_cell(self):
        title = self.view.lbl_target
        title.ensurePolished()
        self.assertTrue(title.font().bold())
        self.assertEqual(title.font().pointSize(), self.view.font().pointSize())
        self.assertIn('Well Averages: A01', title.text())
        self.view.cell_selector.setCurrentIndex(1)
        self.assertIs(self.view.lbl_target, title)
        self.assertEqual(title.text(), 'Cell 1')
        self.assertIn('r1', self.view.cell_context.text())
        self.assertTrue(title.font().bold())
        empty = self.path.with_name('empty-title.csv')
        empty.write_text('Well_ID,Cell_ID\n')
        self.view.load_csv(str(empty))
        self.assertIn('Select a cell on the map', title.text())
        self.assertEqual(title.font().pointSize(), self.view.font().pointSize())


class AnnotationPresentationTests(unittest.TestCase):

    def test_model_buttons_distinguish_disabled_state_and_preserve_enabled_colors(self):
        from tempo_ui import install_ui
        from PyQt6.QtCore import Qt
        from PyQt6.QtGui import QImage
        install_ui(APP)
        with tempfile.TemporaryDirectory() as directory, ExitStack() as patches:
            patches.enter_context(patch.object(gui.tempo_backend, 'UNIVERSAL_DATASET_DIR', directory))
            patches.enter_context(patch.object(gui, 'run_background', return_value=None))
            patches.enter_context(patch.object(gui.tempo_hub, 'cached_lock_manifest', return_value={'images': {}}))
            patches.enter_context(patch.object(gui.tempo_hub, 'get_collaborator_id', return_value='Color Fixture'))
            view = gui.AnnotatorTab()
            try:
                view.show(); APP.processEvents()
                pairs = [(view.btn_auto_nano, '#0088cc'),
                         (view.btn_auto_stardist, '#9b51e0'), (view.btn_auto_cellpose, '#e67e22')]
                with patch.object(view, 'start_auto_annotation') as start:
                    for button, color in pairs:
                        self.assertFalse(button.isEnabled())
                        rendered = button.grab().toImage()
                        self.assertEqual(rendered.pixelColor(8, 8).name(), '#252b34')
                        QTest.mouseClick(button, Qt.MouseButton.LeftButton)
                    start.assert_not_called()
                path = Path(directory) / 'model-colors.png'
                image = QImage(640, 480, QImage.Format.Format_RGB32)
                image.fill(Qt.GlobalColor.gray)
                self.assertTrue(image.save(str(path)))
                view.current_file = str(path)
                view.view.set_image(str(path))
                view.set_auto_buttons_enabled(True)
                APP.processEvents()
                for button, color in pairs:
                    self.assertTrue(button.isEnabled())
                    self.assertEqual(button.grab().toImage().pixelColor(8, 8).name(), color)
                view._current_file_readonly = True
                view.set_auto_buttons_enabled(True)
                APP.processEvents()
                with patch.object(view, 'start_auto_annotation') as start:
                    for button, color in pairs:
                        enabled = button is view.btn_auto_nano
                        self.assertEqual(button.isEnabled(), enabled)
                        self.assertEqual(button.grab().toImage().pixelColor(8, 8).name(), color if enabled else '#252b34')
                        if not enabled:
                            QTest.mouseClick(button, Qt.MouseButton.LeftButton)
                    start.assert_not_called()
            finally:
                view.close(); view.deleteLater(); APP.processEvents()


class BackgroundLifecycleTests(unittest.TestCase):

    def test_read_timeout_ignores_late_success(self):
        with patch('tempo_tasks.run_background') as background:
            dialog = ReadTaskDialog(None, 'Reading', lambda: 'late success', timeout_ms=10)
        self.addCleanup(dialog.close)
        from time import monotonic
        deadline = monotonic() + 2
        while not dialog.settled and monotonic() < deadline:
            QTest.qWait(10)
        self.assertIsInstance(dialog.error, TimeoutError)
        background.call_args.args[1]('late success', None)
        self.assertIsNone(dialog.result_value)
        self.assertIsInstance(dialog.error, TimeoutError)

    def test_worker_cancellation_is_not_success(self):
        worker = gui.ProcessingWorker('batch', queue=['a'])
        states = []; worker.state_signal.connect(states.append)
        worker.cancel()
        with patch.object(gui.tempo_backend, 'process_assay_result') as process:
            worker.run()
        process.assert_not_called()
        self.assertIn('cancelled', states[-1])

    def test_stalled_hardware_release_keeps_gui_responsive(self):
        gate = threading.Event()
        view = gui.CaptureTab(); self.addCleanup(view.close)
        view.core = Mock(); view.core.unloadAllDevices.side_effect = lambda: gate.wait(2)
        ticks = []
        self.assertFalse(view.cleanup())
        QTimer.singleShot(5, lambda: ticks.append(True))
        QTest.qWait(30)
        self.assertEqual(ticks, [True])
        self.assertFalse(getattr(view, '_cleanup_done', False))
        gate.set(); self.assertTrue(view._cleanup_worker.wait(3000)); QTest.qWait(20)
        self.assertTrue(view._cleanup_done)
