"""Offscreen collection workflow tests; no native TEMPO or physical devices."""
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
os.environ.setdefault('MPLCONFIGDIR', str(__import__('pathlib').Path('.agent-artifacts/matplotlib').resolve()))
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from PyQt6.QtWidgets import QApplication, QMessageBox
from PyQt6.QtCore import Qt, QPoint
from PyQt6.QtTest import QTest

from stimulation_investigation_1 import Investigation
from tests.investigation_fixtures import investigation_view, populate
from tests.qt_lifecycle import wait_for_qt


APP = QApplication.instance() or QApplication([])


class InvestigationUiTests(unittest.TestCase):
    def setUp(self):
        self.qt_errors = []
        hook = patch('sys.excepthook', lambda *error: self.qt_errors.append(error))
        hook.start(); self.addCleanup(hook.stop)

    def tearDown(self):
        self.assertEqual(self.qt_errors, [], 'An exception escaped a Qt callback.')

    def test_modes_replace_only_left_and_center_and_retain_drafts(self):
        with investigation_view() as view:
            workspace = view.investigation_workspace
            self.assertTrue(view.investigation_mode)
            self.assertIs(view.capture_splitter.widget(0), workspace.sidebar)
            self.assertIs(view.capture_splitter.widget(1), workspace)
            self.assertIs(view.capture_splitter.widget(2), view.stimulation_panel)
            workspace.add_condition()
            workspace.condition_widgets['medium'].setText('Unlisted medium Ω')
            self.assertEqual(workspace.selected_condition['medium'], 'Unlisted medium Ω')
            view.capture_mode.setCurrentIndex(0)
            self.assertIs(view.capture_splitter.widget(0), view.camera_panel)
            self.assertIs(view.capture_splitter.widget(1), view.imaging_center)
            view.capture_mode.setCurrentIndex(1)
            self.assertEqual(workspace.condition_widgets['medium'].text(), 'Unlisted medium Ω')
            self.assertEqual(view.capture_controls.service.commands, [])

    def test_mode_selector_supports_real_click_and_keyboard_without_focus_paint(self):
        with investigation_view() as view:
            mode = view.capture_mode
            QTest.mouseClick(mode, Qt.MouseButton.LeftButton, pos=QPoint(mode.width() * 3 // 4, mode.height() // 2))
            QTest.mouseMove(view, QPoint(view.width() - 2, 2)); APP.processEvents()
            clicked = mode.grab().toImage()
            view.capture_controls.amplitude.setFocus(); APP.processEvents()
            self.assertEqual(clicked, mode.grab().toImage())
            QTest.mouseClick(mode, Qt.MouseButton.LeftButton, pos=QPoint(mode.width() // 4, mode.height() // 2))
            self.assertFalse(view.investigation_mode)
            QTest.keyClick(mode, Qt.Key.Key_Right)
            self.assertTrue(view.investigation_mode)
            self.assertEqual(view.capture_controls.service.commands, [])

    def test_scope_data_save_reload_and_separate_empty_arduino_records(self):
        with tempfile.TemporaryDirectory() as directory, investigation_view() as view:
            workspace = view.investigation_workspace
            condition = populate(workspace, 1)
            workspace.scope_widgets['difference_v'].setText('0.295 ± 0.003')
            path = Path(directory) / 'experiment.json'
            with patch('stimulation_investigation_1.ui.QFileDialog.getSaveFileName', return_value=(str(path), '')):
                self.assertTrue(workspace.save())
            self.assertFalse(workspace.dirty)
            self.assertEqual(workspace.arduino_list.count(), 0)
            loaded = Investigation.open(path)
            self.assertEqual(loaded.document['records'][0]['values']['difference_v'], '0.295 ± 0.003')
            self.assertEqual(loaded.document['records'][0]['condition_snapshot']['id'], condition['id'])
            workspace.condition_widgets['electrode_dimensions'].setPlainText('Left 5 × 12 mm\nRight 6 × 12 mm')
            self.assertEqual(workspace.selected_condition['electrode_dimensions'], 'Left 5 × 12 mm\nRight 6 × 12 mm')

    def test_investigation_pacing_uses_existing_controls_without_camera_or_well_session(self):
        with investigation_view() as view:
            tools = view.capture_controls
            tools.amplitude.setValue(.6); tools.frequency.setValue(1); tools.width.setValue(2)
            tools.request_output(True)
            self.assertTrue(wait_for_qt(lambda: not tools.busy() and not tools._starting_output))
            self.assertEqual(tools.service.status.mode, 'PACING')
            self.assertIsNone(tools.session)
            self.assertIsNone(view.core); self.assertIsNone(view.cv_cap)
            self.assertTrue(tools.enabled.isChecked())
            view.capture_mode.setCurrentIndex(0)
            self.assertTrue(view.investigation_mode)
            tools.emergency_stop()
            self.assertTrue(wait_for_qt(lambda: not tools.busy()))
            self.assertEqual(tools.service.status.mode, 'REST_DISCHARGE')
            view.capture_mode.setCurrentIndex(0)
            self.assertFalse(view.investigation_mode)

    def test_raw_capture_is_saved_and_never_commands_output(self):
        from tests.test_stimulation_investigation import raw_row
        with tempfile.TemporaryDirectory() as directory, investigation_view() as view:
            workspace = view.investigation_workspace
            workspace.add_condition()
            workspace.investigation.save(Path(directory) / 'capture.json'); workspace.mark_saved()
            workspace.readings.setValue(4)
            workspace.record_arduino()
            service = view.capture_controls.service
            service.rows = [raw_row(i) for i in range(4)]; service.state = 'complete'
            self.assertTrue(wait_for_qt(lambda: not workspace.capturing))
            self.assertEqual(workspace.raw_table.rowCount(), 4)
            self.assertTrue(all(command.startswith('si1 ') for command in service.commands))
            self.assertEqual(workspace.scope_list.count(), 0)
            self.assertFalse(workspace.dirty)

    def test_stock_firmware_and_unsaved_exit_states_remain_actionable(self):
        with investigation_view(protocol=None) as view:
            workspace = view.investigation_workspace
            workspace.add_condition(); workspace.tabs.setCurrentIndex(1); workspace.refresh_device()
            self.assertFalse(workspace.record_button.isEnabled())
            self.assertTrue(workspace.install_button.isVisible())
            with patch('stimulation_investigation_1.ui.QMessageBox.question', return_value=QMessageBox.StandardButton.Cancel):
                self.assertFalse(workspace.prepare_close())
            with patch('stimulation_investigation_1.ui.QMessageBox.question', return_value=QMessageBox.StandardButton.Discard) as ask:
                self.assertTrue(workspace.prepare_close(confirm_once=True))
                self.assertTrue(workspace.prepare_close(confirm_once=True))
                self.assertEqual(ask.call_count, 1)

    def test_draft_edits_are_not_marked_saved_while_waiting_for_a_capture_reply(self):
        from concurrent.futures import Future
        with tempfile.TemporaryDirectory() as directory, investigation_view() as view:
            workspace = view.investigation_workspace
            populate(workspace, 1)
            workspace.investigation.save(Path(directory) / 'capture.json'); workspace.mark_saved()
            workspace.record_arduino()
            workspace.capture.pending = Future()
            workspace.scope_widgets['instrument'].setText('Updated during capture')
            workspace.poll()
            self.assertTrue(workspace.dirty)
            workspace.capture.pending.set_exception(ConnectionError('Fixture disconnect'))
            workspace.poll()


if __name__ == '__main__': unittest.main()
