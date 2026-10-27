"""Bounded offscreen shutdown checks; no native windows, drivers or cloud calls."""
import os
os.environ['QT_QPA_PLATFORM'] = 'offscreen'
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from unittest.mock import Mock
import numpy as np
from PyQt6.QtCore import QTimer, QThread, QEvent, QCoreApplication
from PyQt6.QtTest import QTest
from PyQt6.QtWidgets import QApplication, QDialog
from PyQt6 import sip
from tempo_tasks import (run_background, release_background_signals, run_task,
                         pending_task_titles, dispose_closed_views, TaskThread)

APP = QApplication.instance() or QApplication([])
with patch('platform.machine', return_value='x86_64'):
    import tempo_gui as gui


class BackgroundDeliveryTests(unittest.TestCase):
    def wait_for_callback(self, calls):
        deadline = time.monotonic() + 1
        while not calls and time.monotonic() < deadline:
            QTest.qWait(10)
        self.assertTrue(calls, 'The background result was not delivered within one second.')

    def tearDown(self):
        release_background_signals()

    def test_action_uses_python_worker_and_result_is_delivered_on_gui_thread(self):
        origin = threading.get_ident()
        workers, callbacks = [], []
        def action():
            workers.append(threading.get_ident())
            return 42
        result = run_background(action, lambda value, error: callbacks.append((threading.get_ident(), value, error)))
        self.assertTrue(result.done_event.wait(1))
        self.assertFalse(callbacks)
        self.wait_for_callback(callbacks)
        self.assertEqual(callbacks, [(origin, 42, None)])
        self.assertNotEqual(workers, [origin])

    def test_late_read_never_emits_or_calls_gui_after_emitters_are_disposed(self):
        gate = threading.Event(); calls = []
        result = run_background(lambda: gate.wait(1), lambda *args: calls.append(args))
        done = result.done_event
        try:
            release_background_signals()
            self.assertTrue(sip.isdeleted(result))
        finally:
            gate.set()
            self.assertTrue(done.wait(2))
        QTest.qWait(40)
        self.assertFalse(calls)

    def test_background_error_reaches_gui_without_cross_thread_qt_emission(self):
        calls = []
        def fail():
            raise ConnectionError('fixture offline')
        result = run_background(fail, lambda value, error: calls.append((value, error)))
        self.assertTrue(result.done_event.wait(1))
        self.wait_for_callback(calls)
        self.assertEqual(len(calls), 1)
        self.assertIsNone(calls[0][0])
        self.assertIsInstance(calls[0][1], ConnectionError)


class ProtectedTaskTests(unittest.TestCase):
    def test_modal_write_ignores_quit_and_keeps_event_loop_responsive_until_settled(self):
        gate = threading.Event(); events = []
        def try_quit():
            events.append(('pending', pending_task_titles()))
            event = QEvent(QEvent.Type.Quit)
            QCoreApplication.sendEvent(APP, event)
            events.append(('quit_ignored', not event.isAccepted()))
            QTimer.singleShot(20, lambda: (events.append(('responsive', True)), gate.set()))
        QTimer.singleShot(20, try_quit)
        try:
            value = run_task(None, 'Saving protected fixture', lambda: gate.wait(1) and 17)
        finally:
            gate.set()
        self.assertEqual(value, 17)
        self.assertIn(('pending', ['Saving protected fixture']), events)
        self.assertIn(('quit_ignored', True), events)
        self.assertIn(('responsive', True), events)
        self.assertFalse(pending_task_titles())

    def test_modal_error_keeps_actual_failure_and_clears_guard(self):
        def fail():
            raise OSError('fixture disk error')
        with self.assertRaisesRegex(OSError, 'fixture disk error'):
            run_task(None, 'Failing protected fixture', fail)
        self.assertFalse(pending_task_titles())

    def test_disposal_refuses_unsettled_child_worker(self):
        gate = threading.Event(); view = QDialog()
        worker = TaskThread(lambda: gate.wait(1), view)
        worker.start()
        try:
            with self.assertRaisesRegex(RuntimeError, 'worker thread'):
                dispose_closed_views(view)
            self.assertFalse(sip.isdeleted(view))
        finally:
            gate.set(); self.assertTrue(worker.wait(2000))
            dispose_closed_views(view)


class PreviewShutdownTests(unittest.TestCase):
    def test_camera_scan_cannot_close_until_probe_releases_camera(self):
        gate = threading.Event(); entered = threading.Event(); cap = Mock()
        def opened():
            entered.set(); gate.wait(2)
            return True
        cap.isOpened.side_effect = opened
        dialog = gui.CameraIndexDialog(); dialog.show()
        with patch.object(gui.cv2, 'VideoCapture', return_value=cap):
            dialog.autodetect_cameras()
            try:
                self.assertTrue(entered.wait(1))
                self.assertFalse(dialog.close())
                self.assertTrue(dialog.isVisible())
                ticks = []
                QTimer.singleShot(5, lambda: ticks.append(True))
                deadline = time.monotonic() + 1
                while not ticks and time.monotonic() < deadline:
                    QTest.qWait(10)
                self.assertEqual(ticks, [True])
                cap.release.assert_not_called()
            finally:
                gate.set(); self.assertTrue(dialog._scan_worker.wait(2000)); QTest.qWait(40)
                dialog.close(); dispose_closed_views(dialog)
        cap.release.assert_called_once()

    def test_camera_setup_tracks_core_before_wizard_and_releases_usb_first(self):
        view = gui.CaptureTab(); self.addCleanup(view.close)
        cap, core = Mock(), Mock(); view.cv_cap = cap
        core.isSequenceRunning.return_value = False
        dialog = Mock(); dialog.exec.return_value = gui.QDialog.DialogCode.Rejected
        def wizard(parent):
            self.assertIs(parent.core, core)
            self.assertTrue(parent._camera_setup_busy)
            cap.release.assert_called_once()
            self.assertIsNone(parent.cv_cap)
            return dialog
        with patch.object(gui, 'HAS_MMCORE', True), \
             patch.object(gui, 'get_configured_core', return_value=core), \
             patch.object(gui, 'HardwareWizardDialog', side_effect=wizard):
            view.run_hardware_wizard()
        self.assertFalse(view._camera_setup_busy)
        self.assertIs(view.core, core)
        self.assertFalse(view.timer.isActive())

    def test_stop_feed_waits_for_preview_off_gui_and_protects_driver_outcome(self):
        gate = threading.Event(); entered = threading.Event(); calls = []
        view = gui.CaptureTab(); core = Mock(); view.core = core
        core.getRemainingImageCount.return_value = 1
        def read():
            entered.set(); gate.wait(2)
            calls.append('preview_returned')
            return np.zeros((12, 12), dtype=np.uint16)
        def stop():
            self.assertNotEqual(threading.get_ident(), origin)
            calls.append('driver_stopped')
        origin = threading.get_ident()
        core.getLastImage.side_effect = read
        core.stopSequenceAcquisition.side_effect = stop
        view.timer.start(1000); view.update_frame()
        self.assertTrue(entered.wait(1))
        events = []
        def while_waiting():
            events.append(pending_task_titles())
            event = QEvent(QEvent.Type.Quit)
            QCoreApplication.sendEvent(APP, event)
            events.append(not event.isAccepted())
            core.stopSequenceAcquisition.assert_not_called()
            gate.set()
        QTimer.singleShot(20, while_waiting)
        try:
            view.toggle_camera()
        finally:
            gate.set(); view.timer.stop(); view.close()
        self.assertEqual(calls, ['preview_returned', 'driver_stopped'])
        self.assertEqual(events, [['Stopping camera preview'], True])
        self.assertFalse(view.timer.isActive())
        self.assertEqual(view.btn_cam.text(), 'Start Hardware Feed')

    def test_snapshot_waits_for_preview_before_reading_camera_again(self):
        gate = threading.Event(); entered = threading.Event(); calls = []
        view = gui.CaptureTab(); cap = Mock(); view.cv_cap = cap
        def read():
            if not calls:
                calls.append('preview_entered'); entered.set(); gate.wait(2)
                calls.append('preview_returned')
            else:
                self.assertEqual(calls[-1], 'preview_returned')
                calls.append('snapshot_read')
            return True, np.zeros((12, 12, 3), dtype=np.uint8)
        cap.read.side_effect = read
        view.timer.start(1000); view.update_frame()
        self.assertTrue(entered.wait(1))
        QTimer.singleShot(20, gate.set)
        try:
            with tempfile.TemporaryDirectory() as directory, \
                 patch.object(gui.tempo_backend, 'RAW_DIR', directory), \
                 patch.object(gui.cv2, 'imwrite', return_value=True) as write:
                view.capture_snapshot()
                write.assert_called_once()
        finally:
            gate.set(); view.timer.stop(); view.close()
        self.assertEqual(calls, ['preview_entered', 'preview_returned', 'snapshot_read'])
        self.assertIn('Snapshot saved', view.lbl_acq_status.text())

    def test_setting_restart_failure_disables_preview_and_capture(self):
        view = gui.CaptureTab(); self.addCleanup(view.close)
        core = Mock(); view.core = core
        core.isSequenceRunning.return_value = True
        core.startContinuousSequenceAcquisition.side_effect = OSError('fixture restart failed')
        view.timer.start(1000)
        view.change_exposure(10)
        core.setExposure.assert_called_once_with(10)
        self.assertFalse(view.timer.isActive())
        self.assertFalse(view.btn_record.isEnabled())
        self.assertIn('restart failed', view.camera_error.details.toPlainText())

    def test_stopping_pending_recording_or_scan_never_launches_it(self):
        for kind in ('recording', 'scan'):
            with self.subTest(kind=kind):
                gate = threading.Event(); view = gui.CaptureTab(); cap = Mock(); view.cv_cap = cap
                view.btn_cam.setEnabled(True)
                cap.read.side_effect = lambda: (gate.wait(2), np.zeros((12, 12, 3), dtype=np.uint8))
                view.timer.start(1000); view.update_frame()
                view.pause_camera_controls()
                delayed = Mock(); delayed.isRunning.return_value = False
                delayed.wait.return_value = True; delayed.stop_confirmed = True
                if kind == 'recording':
                    view.acq_worker = delayed
                else:
                    view.scout_worker = delayed
                view._pending_camera_worker = delayed
                view.start_pending_camera_worker()
                try:
                    if kind == 'recording':
                        view.toggle_recording()
                    else:
                        view.stop_plate_scout()
                    self.assertIsNone(view._pending_camera_worker)
                    gate.set(); QTest.qWait(100)
                    delayed.stop.assert_called_once()
                    delayed.start.assert_not_called()
                    self.assertTrue(view.btn_cam.isEnabled())
                finally:
                    gate.set()
                    if view._preview_task is not None:
                        self.assertTrue(view._preview_task.done_event.wait(2))
                    view.timer.stop(); view._preview_wait_timer.stop(); view.close()

    def test_stalled_preview_cannot_block_gui_or_release_camera_early(self):
        gate = threading.Event(); entered = threading.Event(); threads = []
        view = gui.CaptureTab(); cap = Mock(); view.cv_cap = cap
        def read():
            threads.append(threading.get_ident()); entered.set()
            gate.wait(2)
            return True, np.zeros((12, 12, 3), dtype=np.uint8)
        cap.read.side_effect = read
        view.timer.start(1000)
        view.update_frame()
        try:
            self.assertTrue(entered.wait(1))
            view.update_frame()
            cap.read.assert_called_once()
            self.assertNotEqual(threads, [threading.get_ident()])
            self.assertFalse(view.cleanup())
            ticks = []
            QTimer.singleShot(5, lambda: ticks.append(True))
            deadline = time.monotonic() + 1
            while not ticks and time.monotonic() < deadline:
                QTest.qWait(10)
            self.assertEqual(ticks, [True])
            cap.release.assert_not_called()
            self.assertFalse(getattr(view, '_cleanup_done', False))
        finally:
            gate.set(); self.assertTrue(view._cleanup_worker.wait(3000)); QTest.qWait(40)
            view.close()
        cap.release.assert_called_once()
        self.assertTrue(view._cleanup_done)

    def test_recording_waits_for_preview_and_quit_does_not_start_delayed_worker(self):
        gate = threading.Event(); entered = threading.Event()
        view = gui.CaptureTab(); cap = Mock(); view.cv_cap = cap
        def read():
            entered.set(); gate.wait(2)
            return True, np.zeros((12, 12, 3), dtype=np.uint8)
        cap.read.side_effect = read
        view.timer.start(1000); view.update_frame()
        self.assertTrue(entered.wait(1))
        delayed = Mock(); delayed.is_running = True
        view._pending_camera_worker = view.acq_worker = delayed
        view.start_pending_camera_worker()
        try:
            QTest.qWait(30)
            delayed.start.assert_not_called()
            self.assertFalse(view.cleanup())
        finally:
            gate.set(); self.assertTrue(view._cleanup_worker.wait(3000)); QTest.qWait(40)
            view.close()
        delayed.stop.assert_called_once()
        delayed.start.assert_not_called()

    def test_capture_start_waits_for_preview_callback_before_starting_recording(self):
        gate = threading.Event(); view = gui.CaptureTab(); cap = Mock(); view.cv_cap = cap
        cap.read.side_effect = lambda: (gate.wait(2), np.zeros((12, 12, 3), dtype=np.uint8))
        view.timer.start(1000); view.update_frame()
        delayed = Mock(); delayed.is_running = True
        view._pending_camera_worker = delayed
        view.timer.stop(); view.start_pending_camera_worker()
        try:
            delayed.start.assert_not_called()
            gate.set(); QTest.qWait(80)
            delayed.start.assert_called_once()
        finally:
            gate.set()
            if view._preview_task is not None:
                self.assertTrue(view._preview_task.done_event.wait(2))
            view._preview_wait_timer.stop(); view.close()


class DashboardCloseTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        for replacement in (
            patch.object(gui.tempo_backend, 'UNIVERSAL_DATASET_DIR', str(root)),
            patch.object(gui.tempo_backend, 'QUEUE_FILE', str(root/'queue.json')),
            patch.object(gui.tempo_hub, 'get_collaborator_id', return_value='Shutdown Test'),
            patch.object(gui.tempo_hub, 'cached_lock_manifest', return_value={'images': {}}),
            patch.object(gui.tempo_hub, 'fetch_lock_manifest', return_value={'images': {}}),
            patch.object(gui.tempo_hub, 'is_maintainer', return_value=False),
        ):
            replacement.start(); self.addCleanup(replacement.stop)
        self.view = gui.TempoDashboard()
        # Exercise shutdown without racing an unrelated saved-profile read on
        # the first polling tick. Capture workers under test remain real.
        self.view.tab_capture.capture_controls._profile_loaded = True
        self.view.show()
        QTest.qWait(40)
        self.view.tab_annotator._sync_timer.stop()
        self.view.tab_annotator.lease_timer.stop()
        self.view.tab_maintainer.timer.stop()
        self.addCleanup(self.dispose)

    def dispose(self):
        self.view.tab_annotator._sync_busy = False
        if getattr(self.view, 'worker', None) and not isinstance(self.view.worker, QThread):
            self.view.worker = None
        self.view.close(); dispose_closed_views(self.view)

    def test_close_waits_for_annotation_thread_without_losing_draft(self):
        gate = threading.Event()
        worker = TaskThread(lambda: gate.wait(1), self.view.tab_annotator)
        self.view.tab_annotator.auto_worker = worker
        worker.start()
        try:
            self.assertFalse(self.view.close())
            self.assertTrue(self.view.isVisible())
            self.assertIn('Cell detection', self.view.statusBar().currentMessage())
            self.assertIs(self.view.tabs.currentWidget(), self.view.tab_annotator)
        finally:
            gate.set(); self.assertTrue(worker.wait(2000)); QTest.qWait(20)
        self.assertTrue(self.view.close())

    def test_unsynced_confirmation_survives_deferred_shutdown_but_changes_require_a_new_decision(self):
        annotator=self.view.tab_annotator
        annotator.universal_annotations={'fixture.png':dict(sync_pending=True,boxes=[])}
        with patch.object(gui,'confirm_action',return_value=True) as confirm,patch.object(self.view.tab_capture.capture_controls,'shutdown',return_value=False):
            self.assertFalse(self.view.close());self.assertFalse(self.view.close())
            self.assertEqual(confirm.call_count,1)
            annotator.universal_annotations['fixture.png']['boxes']=[dict(class_id=0,polygon=[[0,0],[1,0],[0,1]])]
            self.assertFalse(self.view.close())
            self.assertEqual(confirm.call_count,2)

    def test_cloud_sync_blocks_close_until_callback_has_applied_outcome(self):
        self.view.tab_annotator._sync_busy = True
        self.assertFalse(self.view.close())
        self.assertIn('sync outcome', self.view.statusBar().currentMessage())
        self.assertTrue(self.view.isVisible())
        self.view.tab_annotator._sync_busy = False
        self.assertTrue(self.view.close())

    def test_camera_setup_blocks_cleanup_until_modal_stack_has_returned(self):
        self.view.tab_capture._camera_setup_busy = True
        self.assertFalse(self.view.close())
        self.assertIn('camera setup', self.view.statusBar().currentMessage())
        self.assertFalse(getattr(self.view.tab_capture, '_cleanup_pending', False))
        self.view.tab_capture._camera_setup_busy = False
        self.assertTrue(self.view.close())

    def test_native_thread_cleanup_is_polled_without_blocking_gui(self):
        settled = False
        self.view.worker = SimpleNamespace(isRunning=lambda: False, wait=lambda timeout: settled)
        self.assertFalse(self.view.close())
        self.assertIn('releasing its resources', self.view.statusBar().currentMessage())
        ticks = []
        QTimer.singleShot(5, lambda: ticks.append(True))
        deadline = time.monotonic() + 1
        while not ticks and time.monotonic() < deadline:
            QTest.qWait(10)
        self.assertEqual(ticks, [True])
        settled = True
        self.assertTrue(self.view.close())

    def test_accepted_close_stops_timers_that_could_launch_more_writes(self):
        self.assertTrue(self.view.close())
        self.assertTrue(self.view.tab_annotator._shutdown_requested)
        self.assertFalse(self.view.tab_annotator._sync_timer.isActive())
        self.assertFalse(self.view.tab_annotator.lease_timer.isActive())
        self.assertFalse(self.view.tab_maintainer.timer.isActive())
        with patch.object(gui, 'run_background') as start:
            self.view.tab_annotator.sync_pending_annotations()
        start.assert_not_called()

    def test_camera_cleanup_failure_keeps_window_and_resumes_saved_draft_sync(self):
        cap = Mock(); cap.release.side_effect = OSError('fixture release failed')
        capture, annotator = self.view.tab_capture, self.view.tab_annotator
        capture.cv_cap = cap
        annotator.universal_annotations = {'fixture.png': {'sync_pending': True}}
        try:
            with patch.object(gui, 'confirm_action', return_value=True):
                self.assertFalse(self.view.close())
            self.assertTrue(capture._cleanup_worker.wait(2000)); QTest.qWait(40)
            self.assertTrue(self.view.isVisible())
            self.assertFalse(annotator._shutdown_requested)
            self.assertTrue(annotator._sync_timer.isActive())
            self.assertTrue(annotator.universal_annotations['fixture.png']['sync_pending'])
            self.assertIs(self.view.tabs.currentWidget(), capture)
            self.assertIn('release failed', capture.camera_error.details.toPlainText())
        finally:
            annotator.universal_annotations = {}
            annotator._sync_timer.stop()
            cap.release.side_effect = None
            capture.cleanup()
            self.assertTrue(capture._cleanup_worker.wait(2000)); QTest.qWait(40)


if __name__ == '__main__':
    unittest.main()
