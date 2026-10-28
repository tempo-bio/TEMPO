"""Failure handling uses mocked hardware; no camera or model is started."""
import os
os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch
from PyQt6.QtWidgets import QApplication, QPushButton
from tests.qt_lifecycle import close_and_dispose, wait_for_qt
from tempo_processing import ProcessingResult
from tests.presentation_fixtures import application_style_name, set_application_style
APP = QApplication.instance() or QApplication([])
with patch('platform.machine',return_value='x86_64'):
    import tempo_gui as gui


class AcquisitionAnalysisTests(unittest.TestCase):
    def setUp(self):
        directory=tempfile.TemporaryDirectory();self.addCleanup(directory.cleanup)
        for key,name in (('QUEUE_FILE','queue.json'),('RAW_DIR','Raw'),('PROCESSED_DIR','Processed')):
            patcher=patch.object(gui.tempo_backend,key,str(Path(directory.name)/name))
            patcher.start();self.addCleanup(patcher.stop)

    def test_advanced_properties_only_appear_after_discovery(self):
        view = gui.CaptureTab(); self.addCleanup(close_and_dispose, view)
        self.assertTrue(view.prop_group.isHidden())
        core = Mock()
        core.getCameraDevice.return_value = 'Camera'
        core.getExposure.return_value = 4
        core.getDevicePropertyNames.return_value = ['Gain']
        core.getProperty.return_value = '10'
        core.getAllowedPropertyValues.return_value = []
        with patch.object(gui, 'get_configured_core', return_value=core):
            view.load_cfg_from_path('synthetic.cfg')
            self.assertFalse(view.prop_group.isHidden())
            self.assertTrue(view.btn_apply_prop.isEnabled())
            core.getDevicePropertyNames.side_effect = OSError('disconnected')
            view.load_cfg_from_path('synthetic.cfg')
        self.assertTrue(view.prop_group.isHidden())
        self.assertFalse(view.btn_apply_prop.isEnabled())
        self.assertIn('advanced properties', view.camera_error.summary.text())

    def test_property_read_failure_clears_stale_value_and_disables_apply(self):
        from types import SimpleNamespace
        from PyQt6.QtWidgets import QLineEdit, QPushButton
        view = gui.CaptureTab(); self.addCleanup(close_and_dispose, view)
        view.core = Mock(); view.camera_device = 'Camera'
        view.prop_val_input.setText('old value')
        view.core.getProperty.side_effect = OSError('camera disconnected')
        gui.CaptureTab.on_property_selected(view, 'Gain')
        self.assertEqual(view.prop_val_input.text(), '')
        self.assertFalse(view.btn_apply_prop.isEnabled())
        self.assertIn('camera disconnected', view.camera_error.details.toPlainText())
        view.core.getProperty.side_effect = None
        view.core.getProperty.return_value = '10'
        view.core.getAllowedPropertyValues.return_value = []
        gui.CaptureTab.on_property_selected(view, 'Gain')
        self.assertEqual(view.prop_val_input.text(), '10')
        self.assertTrue(view.btn_apply_prop.isEnabled())

    def test_single_processing_entry_points_reject_duplicate_jobs(self):
        from types import SimpleNamespace
        view = SimpleNamespace(_processing_busy=True)
        with patch.object(gui.QFileDialog, 'getOpenFileName') as choose:
            gui.TempoDashboard.select_and_process_file(view)
            gui.TempoDashboard.run_auto_cell_detection(view)
        choose.assert_not_called()

    def test_cancelled_cell_selection_replaces_stale_completion_status(self):
        from types import SimpleNamespace
        from PyQt6.QtWidgets import QLabel
        view = SimpleNamespace(_processing_busy=False, job_status=QLabel('Processing complete.'), log_to_console=Mock())
        dialog = Mock(); dialog.exec.return_value = gui.QDialog.DialogCode.Rejected
        with patch.object(gui.QFileDialog, 'getOpenFileName', return_value=('sample.avi', '')), \
             patch.object(gui, 'ROIDialog', return_value=dialog), \
             patch.object(gui, 'ProcessingWorker') as worker:
            gui.TempoDashboard.select_and_process_file(view)
        worker.assert_not_called()
        self.assertEqual(view.job_status.text(), 'Cell selection cancelled. Processing was not started.')

    def test_processing_queue_wires_and_starts_with_plain_text_log(self):
        from types import SimpleNamespace
        from PyQt6.QtWidgets import QListWidget, QLabel, QTextEdit, QCheckBox
        view = SimpleNamespace(_processing_busy=False, queue_list=QListWidget(),
                               job_status=QLabel(), console_output=QTextEdit(),
                               chk_debug_avis=QCheckBox(), lock_buttons=Mock(), unlock_buttons=Mock(),
                               btn_process_batch=QPushButton(), record_processing_result=Mock())
        view.refresh_processing_queue = lambda: view.queue_list.addItem('sample.avi')
        view.log_to_console = lambda message: gui.TempoDashboard.log_to_console(view, message)
        worker = Mock()
        with patch.object(gui, 'ProcessingWorker', return_value=worker), patch.object(gui.tempo_backend, 'read_queue', return_value=['sample.avi']):
            gui.TempoDashboard.run_batch_processing(view)
        worker.start.assert_called_once()
        handler = worker.log_signal.connect.call_args.args[0]
        handler('Failed to read <sample>.avi')
        self.assertEqual(view.console_output.toPlainText(), 'Failed to read <sample>.avi\n')
        self.assertEqual(view.job_status.text(), '')

    def test_recording_stop_failure_does_not_resume_preview(self):
        from types import SimpleNamespace
        from PyQt6.QtWidgets import QLabel, QPushButton
        view = SimpleNamespace(acq_worker=SimpleNamespace(stop_confirmed=False), core=Mock(),
                               timer=Mock(), _preview_was_active=True, _camera_control_states=[],
                               btn_cam=QPushButton(), btn_snap=QPushButton(), btn_record=QPushButton(),
                               feed_label=QLabel(), camera_error=gui.ErrorDetails())
        view.mark_camera_stop_unconfirmed = lambda: gui.CaptureTab.mark_camera_stop_unconfirmed(view)
        gui.CaptureTab.resume_camera_controls(view)
        view.timer.start.assert_not_called()
        view.core.startContinuousSequenceAcquisition.assert_not_called()
        self.assertEqual(view.btn_cam.text(), 'Retry Stop Feed')
        self.assertFalse(view.btn_record.isEnabled())

    def test_recording_folder_failure_does_not_start_or_pause_camera(self):
        from types import SimpleNamespace
        from PyQt6.QtWidgets import QLabel, QLineEdit
        view = SimpleNamespace(acq_worker=None, scout_worker=None, core=Mock(), cv_cap=None,
                               camera_error=gui.ErrorDetails(), exp_id=QLineEdit('experiment'),
                               generate_filename=lambda ext: 'recording.' + ext,
                               lbl_acq_status=QLabel(), pause_camera_controls=Mock())
        with patch.object(gui.os, 'makedirs', side_effect=PermissionError('read only')), \
             patch.object(gui, 'CameraAcquisitionWorker') as worker:
            gui.CaptureTab.toggle_recording(view)
        worker.assert_not_called(); view.pause_camera_controls.assert_not_called()
        self.assertIn('not started', view.lbl_acq_status.text())
        self.assertIn('read only', view.camera_error.details.toPlainText())

    def test_preview_failure_disables_capture_and_keeps_core_stop_pending(self):
        from PyQt6.QtTest import QTest
        for hardware in ('core', 'usb'):
            with self.subTest(hardware=hardware):
                device = Mock()
                device.getRemainingImageCount.side_effect = RuntimeError('camera disconnected')
                device.read.return_value = (False, None)
                view = gui.CaptureTab(); self.addCleanup(close_and_dispose, view)
                view.capture_controls._profile_loaded = True
                view.core = device if hardware == 'core' else None
                view.cv_cap = device if hardware == 'usb' else None
                view.timer.start(1000)
                view.update_frame()
                self.assertTrue(wait_for_qt(lambda: not view._preview_pending), 'Preview failure was not delivered.')
                self.assertFalse(view.btn_snap.isEnabled())
                self.assertFalse(view.btn_record.isEnabled())
                self.assertFalse(view.camera_error.isHidden())
                self.assertEqual(view.btn_cam.text(), 'Retry Stop Feed' if hardware == 'core' else 'Start Hardware Feed')
                device.startContinuousSequenceAcquisition.assert_not_called()

    def test_snapshot_missing_frame_reports_failure_without_writing(self):
        from types import SimpleNamespace
        from PyQt6.QtWidgets import QLabel
        cap = Mock(); cap.read.return_value = (False, None)
        view = gui.CaptureTab(); self.addCleanup(close_and_dispose, view)
        view.cv_cap = cap
        initial_fov = view.fov_id.value()
        with patch.object(gui.cv2, 'imwrite') as write:
            gui.CaptureTab.capture_snapshot(view)
        write.assert_not_called()
        self.assertEqual(view.fov_id.value(), initial_fov)
        self.assertEqual(view.lbl_acq_status.text(), 'Snapshot was not saved.')
        self.assertIn('did not return a frame', view.camera_error.details.toPlainText())

    def test_hardware_adapter_discovery_runs_outside_gui_thread(self):
        import threading
        with patch.object(gui.QTimer, 'singleShot'):
            view = gui.HardwareWizardDialog()
        self.addCleanup(close_and_dispose, view)
        threads = []
        core = Mock()
        core.getDeviceAdapterNames.side_effect = lambda: (threads.append(threading.get_ident()) or [])
        with patch.object(gui, 'HAS_MMCORE', True), patch.object(gui, 'get_configured_core', return_value=core):
            view.load_adapters()
        self.assertEqual(len(threads), 1)
        self.assertNotEqual(threads[0], threading.get_ident())
        self.assertIn('No camera adapters found', view.status_lbl.text())

    def test_analysis_measurements_fit_sidebar_at_large_font(self):
        from PyQt6.QtGui import QFont
        from PyQt6.QtWidgets import QStyleFactory
        previous = APP.font()
        previous_style = application_style_name(APP)
        try:
            APP.setFont(QFont('Arial', 18))
            styles = [previous_style] + [style for style in QStyleFactory.keys() if style.lower() == 'windows']
            for style in styles:
                with self.subTest(style=style):
                    set_application_style(APP, style)
                    view = gui.AnalyzerTab(); self.addCleanup(close_and_dispose, view)
                    view.resize(1100, 760); view.stats_tabs.show(); view.show()
                    for show_trace in (False, True):
                        view.grp_wave.setVisible(show_trace)
                        for index in (0, 1):
                            view.stats_tabs.setCurrentIndex(index)
                            for _ in range(5): APP.processEvents()
                            self.assertEqual(view.inspector_scroll.horizontalScrollBar().maximum(), 0)
                    # Width adaptation must retain vertical access to all groups.
                    view.resize(1100, 400)
                    for _ in range(5): APP.processEvents()
                    self.assertGreater(view.inspector_scroll.verticalScrollBar().maximum(), 0)
                    view.close()
        finally:
            set_application_style(APP, previous_style)
            APP.setFont(previous)

    def test_hardware_adapter_failure_is_inline_and_reload_recovers(self):
        with patch.object(gui.QTimer, 'singleShot'):
            view = gui.HardwareWizardDialog()
        self.addCleanup(close_and_dispose, view)
        with patch.object(gui, 'HAS_MMCORE', True), patch.object(gui, 'get_configured_core', side_effect=RuntimeError('driver missing')):
            view.load_adapters()
        self.assertIn('driver missing', view.hardware_error.details.toPlainText())
        self.assertFalse(view.btn_generate.isEnabled())
        core = Mock(); core.getDeviceAdapterNames.return_value = []
        with patch.object(gui, 'HAS_MMCORE', True), patch.object(gui, 'get_configured_core', return_value=core):
            view.load_adapters()
        self.assertTrue(view.hardware_error.isHidden())

    def test_hardware_connection_failure_keeps_dialog_retryable(self):
        with patch.object(gui.QTimer, 'singleShot'):
            view = gui.HardwareWizardDialog()
        self.addCleanup(close_and_dispose, view)
        view.adapter_combo.blockSignals(True); view.adapter_combo.addItem('Adapter')
        view.device_combo.addItem('Camera')
        core = Mock(); core.initializeAllDevices.side_effect = RuntimeError('camera unplugged')
        with patch.object(gui, 'get_configured_core', return_value=core):
            view.generate_config()
        self.assertTrue(view.btn_generate.isEnabled())
        self.assertIsNone(view.cfg_file)
        self.assertIn('camera unplugged', view.hardware_error.details.toPlainText())
        self.assertEqual(core.unloadAllDevices.call_count, 2)

    def test_camera_scan_runs_in_background_and_prevents_duplicate_scan(self):
        view = gui.CameraIndexDialog(); self.addCleanup(close_and_dispose, view)
        missing = Mock(); missing.isOpened.return_value = False
        available = Mock(); available.isOpened.return_value = True
        with patch.object(gui, 'TaskThread') as worker:
            view.autodetect_cameras()
            view.autodetect_cameras()
        worker.assert_called_once()
        worker.return_value.start.assert_called_once()
        self.assertFalse(view.btn_connect.isEnabled())
        action = worker.call_args.args[0]
        completed = view.on_camera_scan_finished
        with patch.object(gui.cv2, 'VideoCapture', side_effect=[missing, available]):
            result = action()
        missing.release.assert_called_once(); available.release.assert_called_once()
        completed(result, None)
        self.assertEqual(view.cam_index.value(), 1)
        self.assertTrue(view.btn_connect.isEnabled())

    def test_camera_scan_releases_failed_probe_and_allows_retry(self):
        view = gui.CameraIndexDialog(); self.addCleanup(close_and_dispose, view)
        camera = Mock(); camera.isOpened.side_effect = RuntimeError('driver unavailable')
        with patch.object(gui, 'TaskThread') as worker:
            view.autodetect_cameras()
        action = worker.call_args.args[0]
        completed = view.on_camera_scan_finished
        with patch.object(gui.cv2, 'VideoCapture', return_value=camera):
            with self.assertRaises(RuntimeError) as raised:
                action()
        camera.release.assert_called_once()
        completed(None, raised.exception)
        self.assertTrue(view.btn_autodetect.isEnabled())
        self.assertIn('driver unavailable', view.scan_error.details.toPlainText())
        with patch.object(gui, 'TaskThread') as worker:
            view.autodetect_cameras()
        self.assertTrue(view.scan_error.isHidden())
        completed = view.on_camera_scan_finished
        completed(-1, None)
        self.assertIn('No camera found', view.status_lbl.text())

    def test_failed_pause_does_not_apply_exposure_or_restart_acquisition(self):
        from types import SimpleNamespace
        from PyQt6.QtWidgets import QPushButton, QLabel
        from tempo_ui import ErrorDetails
        core = Mock(); core.isSequenceRunning.return_value = True
        core.stopSequenceAcquisition.side_effect = RuntimeError('stop failed')
        view = gui.CaptureTab(); self.addCleanup(close_and_dispose, view)
        view.core = core
        gui.CaptureTab.change_exposure(view, 10)
        gui.CaptureTab.change_exposure(view, 20)
        core.setExposure.assert_not_called()
        core.startContinuousSequenceAcquisition.assert_not_called()
        core.stopSequenceAcquisition.assert_called_once()
        self.assertEqual(view.btn_cam.text(), 'Retry Stop Feed')

    def test_feed_stop_failure_retries_stop_instead_of_starting_again(self):
        from types import SimpleNamespace
        from PyQt6.QtWidgets import QPushButton, QLabel
        from tempo_ui import ErrorDetails
        core = Mock(); core.stopSequenceAcquisition.side_effect = [RuntimeError('driver busy'), None]
        view = gui.CaptureTab(); self.addCleanup(close_and_dispose, view)
        view.core = core; view.timer.start(1000)
        gui.CaptureTab.toggle_camera(view)
        self.assertEqual(view.btn_cam.text(), 'Retry Stop Feed')
        self.assertFalse(view.btn_record.isEnabled())
        gui.CaptureTab.toggle_camera(view)
        self.assertEqual(core.stopSequenceAcquisition.call_count, 2)
        core.startContinuousSequenceAcquisition.assert_not_called()
        self.assertEqual(view.btn_cam.text(), 'Start Hardware Feed')
        self.assertTrue(view.camera_error.isHidden())

    def test_exposure_failure_resumes_feed_and_reports_error(self):
        from types import SimpleNamespace
        from tempo_ui import ErrorDetails
        core = Mock(); core.isSequenceRunning.return_value = True
        core.setExposure.side_effect = RuntimeError('outside camera limits')
        view = gui.CaptureTab(); self.addCleanup(close_and_dispose, view)
        view.core = core
        gui.CaptureTab.change_exposure(view, 100)
        core.stopSequenceAcquisition.assert_called_once()
        core.startContinuousSequenceAcquisition.assert_called_once_with(0)
        self.assertIn('outside camera limits', view.camera_error.details.toPlainText())

    def test_feed_restart_failure_disables_capture_controls(self):
        from types import SimpleNamespace
        from PyQt6.QtWidgets import QPushButton
        from tempo_ui import ErrorDetails
        core = Mock(); core.startContinuousSequenceAcquisition.side_effect = RuntimeError('disconnected')
        view = gui.CaptureTab(); self.addCleanup(close_and_dispose, view)
        view.core = core; view.timer.start(1000)
        gui.CaptureTab.resume_camera_after_setting(view)
        self.assertFalse(view.timer.isActive())
        self.assertFalse(view.btn_snap.isEnabled()); self.assertFalse(view.btn_record.isEnabled())
        self.assertIn('disconnected', view.camera_error.details.toPlainText())

    def test_preset_reports_failed_persistence_without_hiding_active_values(self):
        from types import SimpleNamespace
        from PyQt6.QtWidgets import QSpinBox, QLabel
        fps = QSpinBox(); fps.setRange(1, 1000)
        exposure = QSpinBox(); exposure.setEnabled(False)
        view = SimpleNamespace(spin_rec_fps=fps, spin_exposure=exposure, lbl_acq_status=QLabel())
        with patch.object(gui.tempo_preferences, 'set'), \
             patch.object(gui.tempo_preferences, 'save_to_disk', return_value=False):
            gui.CaptureTab.apply_flir_preset(view)
        self.assertEqual(fps.value(), 226)
        self.assertIn('226 fps', view.lbl_acq_status.text())
        self.assertIn('could not be saved', view.lbl_acq_status.text())
        self.assertNotIn('Exposure requested', view.lbl_acq_status.text())

    def test_queue_refresh_cannot_enable_a_second_processing_job(self):
        from types import SimpleNamespace
        from PyQt6.QtWidgets import QListWidget, QLabel, QPushButton
        view = SimpleNamespace(queue_list=QListWidget(), queue_summary=QLabel(),
                               job_status=QLabel('Processing video'), queue_error=gui.ErrorDetails(), btn_process_batch=QPushButton(),
                               _processing_busy=True)
        with tempfile.TemporaryDirectory() as directory:
            queue = Path(directory)/'queue.json'; queue.write_text('["example.avi"]')
            with patch.object(gui.tempo_backend, 'QUEUE_FILE', str(queue)):
                gui.TempoDashboard.refresh_processing_queue(view)
                # The shared action remains available for cancellation. The busy
                # guard still prevents refresh from permitting a second job.
                self.assertTrue(view.btn_process_batch.isEnabled())
                self.assertEqual(view.queue_summary.text(), 'Queued videos (1); 0 ready')
                with patch.object(gui, 'ProcessingWorker') as worker:
                    gui.TempoDashboard.run_batch_processing(view)
                worker.assert_not_called()
                view._processing_busy = False
                gui.TempoDashboard.refresh_processing_queue(view)
                self.assertFalse(view.btn_process_batch.isEnabled())  # Missing paths are never ready.
                queue.write_text('{"example.avi": true}')
                gui.TempoDashboard.refresh_processing_queue(view)
                self.assertFalse(view.btn_process_batch.isEnabled())
                self.assertIn('Expected a list', view.queue_error.details.toPlainText())
                self.assertEqual(view.job_status.text(), 'Processing video')
                queue.write_text('[]')
                gui.TempoDashboard.refresh_processing_queue(view)
                self.assertTrue(view.queue_error.isHidden())

    def test_scan_requires_stage_instead_of_simulating(self):
        worker=self.collection_worker(None); errors=[]
        worker.error_signal.connect(errors.append)
        with patch.object(gui,'detect_cells_via_bridge') as detect:
            worker.run()
        self.assertIn('requires a connected XY stage',errors[0])
        detect.assert_not_called()

    def test_position_failure_stops_before_camera_capture(self):
        core=Mock(); core.getXYStageDevice.return_value='stage'
        core.setXYPosition.side_effect=RuntimeError('motion failed')
        worker=self.collection_worker(core); errors=[]
        worker.error_signal.connect(errors.append)
        with patch('tempo_capture_ui.optical_context',return_value=dict(signature='fixture',image_size=(12,12),position=(0,0))): worker.run()
        self.assertIn('Could not position the microscope stage',errors[0])
        core.snapImage.assert_not_called()

    def test_missing_frame_is_not_replaced_by_synthetic_image(self):
        core=Mock(); core.getXYStageDevice.return_value='stage'; core.snapImage.side_effect=RuntimeError('camera offline')
        core.deviceBusy.return_value=False
        worker=self.collection_worker(core); errors=[]
        worker.error_signal.connect(errors.append)
        with patch('tempo_capture_ui.optical_context',return_value=dict(signature='fixture',image_size=(12,12),position=(0,0))), patch.object(gui,'detect_cells_via_bridge') as detect:
            worker.run()
        self.assertIn('camera offline',errors[0]); detect.assert_not_called()

    def collection_worker(self,core):
        from tempo_capture import FieldCalibration
        from tempo_capture_ui import FieldCollectionWorker
        directory=tempfile.TemporaryDirectory(); self.addCleanup(directory.cleanup)
        if core is not None and core.setXYPosition.side_effect is None:
            core._fixture_position=(0,0)
            core.setXYPosition.side_effect=lambda stage,x,y:setattr(core,'_fixture_position',(x,y))
            core.getXPosition.side_effect=lambda stage:core._fixture_position[0]
            core.getYPosition.side_effect=lambda stage:core._fixture_position[1]
        return FieldCollectionWorker(core,[dict(well='A01',corner_a=[0,0],corner_b=[36,12])],
            'fixture',directory.name,10,3,3,1,10,FieldCalibration(((1,0),(0,1)),(12,12),'fixture'),
            'fixture.pt',.15,acquisition_factory=gui.CameraAcquisitionWorker,enqueue=Mock())

    def test_invalid_csv_is_reported_without_replacing_existing_results(self):
        view=gui.AnalyzerTab(); self.addCleanup(close_and_dispose, view)
        previous=object(); view.df=previous
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'invalid.csv'; path.write_text('wrong,column\n1,2\n')
            with patch.object(gui.QFileDialog,'getOpenFileName',return_value=(str(path),'')):
                view.load_csv()
        self.assertIs(view.df,previous)
        self.assertIn('Missing columns',view.analysis_error.details.toPlainText())
        self.assertIn('previously loaded results are unchanged',view.analysis_error.summary.text())

    def test_measurements_have_readable_names_and_stable_data_keys(self):
        view=gui.AnalyzerTab(); self.addCleanup(close_and_dispose, view)
        self.assertNotIn('_',view.metric_combo.currentText())
        self.assertEqual(view.metric_combo.currentData(),gui.tempo_preferences.get('analyzer','default_metric'))
        self.assertTrue(view.lbl_target.wordWrap())
        self.assertFalse(view.glw.isHidden())  # Empty workspace retains its boundary.

    def test_processing_failure_is_not_reported_as_complete(self):
        worker=gui.ProcessingWorker('single',filepath='test.avi')
        states=[]; finished=[]
        worker.state_signal.connect(states.append); worker.finished_signal.connect(lambda:finished.append(True))
        with patch.object(gui.tempo_backend,'process_assay_result',return_value=ProcessingResult('test.avi')): worker.run()
        self.assertTrue(states[-1].startswith('Processing failed.'))
        self.assertEqual(finished,[True])

    def test_processing_exception_details_stay_out_of_primary_status(self):
        worker = gui.ProcessingWorker('single', filepath='test.avi')
        states, logs = [], []
        worker.state_signal.connect(states.append); worker.log_signal.connect(logs.append)
        with patch.object(gui.tempo_backend, 'process_assay_result', side_effect=RuntimeError('internal diagnostic <tensor>')):
            worker.run()
        self.assertNotIn('tensor', states[-1])
        self.assertIn('internal diagnostic <tensor>', logs)

    def test_batch_partial_failure_is_visible(self):
        worker=gui.ProcessingWorker('batch'); states=[]
        worker.state_signal.connect(states.append)
        with patch.object(gui.tempo_backend,'process_entire_queue',return_value={'completed':2,'failed':1}): worker.run()
        self.assertIn('1 failed and remain queued',states[-1])

    def test_recording_without_camera_is_failure(self):
        worker=gui.CameraAcquisitionWorker(); errors=[]; completed=[]
        worker.error_signal.connect(errors.append); worker.finished_signal.connect(lambda *args:completed.append(args))
        worker.run()
        self.assertIn('No camera', errors[0]); self.assertFalse(completed)

    def test_recording_writer_failure_releases_resources(self):
        core=Mock(); core.getImageWidth.return_value=32; core.getImageHeight.return_value=32
        writer=Mock(); writer.isOpened.return_value=False
        with tempfile.TemporaryDirectory() as folder:
            worker=gui.CameraAcquisitionWorker(core=core,output_path=str(Path(folder)/'test.avi'))
            errors=[]; worker.error_signal.connect(errors.append)
            with patch.object(gui,'HAS_MMCORE',True), patch.object(gui.cv2,'VideoWriter',return_value=writer):
                worker.run()
        self.assertIn('Could not open',errors[0]); writer.release.assert_called_once()
        core.stopSequenceAcquisition.assert_called_once()

    def test_camera_stall_stops_and_finalizes_recording(self):
        core=Mock(); core.getImageWidth.return_value=32; core.getImageHeight.return_value=32
        core.getRemainingImageCount.return_value=0; core.isBufferOverflowed.return_value=False
        writer=Mock(); writer.isOpened.return_value=True
        with tempfile.TemporaryDirectory() as folder:
            worker=gui.CameraAcquisitionWorker(core=core,output_path=str(Path(folder)/'test.avi'))
            errors=[]; worker.error_signal.connect(errors.append)
            with patch.object(gui,'HAS_MMCORE',True), patch.object(gui.cv2,'VideoWriter',return_value=writer), patch.object(gui.time,'monotonic',side_effect=[0,11]):
                worker.run()
        self.assertIn('10 seconds',errors[0]); writer.release.assert_called_once()
        core.stopSequenceAcquisition.assert_called_once()

    def test_usb_read_failure_is_not_success(self):
        cap=Mock(); cap.get.return_value=32; cap.read.return_value=(False,None)
        writer=Mock(); writer.isOpened.return_value=True
        with tempfile.TemporaryDirectory() as folder:
            worker=gui.CameraAcquisitionWorker(cv_cap=cap,output_path=str(Path(folder)/'test.avi'))
            errors=[]; completed=[]
            worker.error_signal.connect(errors.append); worker.finished_signal.connect(lambda *args:completed.append(args))
            with patch.object(gui.cv2,'VideoWriter',return_value=writer): worker.run()
        self.assertIn('stopped returning frames',errors[0]); self.assertFalse(completed)
        writer.release.assert_called_once()

    def test_close_does_not_unload_hardware_while_worker_is_running(self):
        import threading
        from PyQt6.QtTest import QTest
        worker=Mock(); worker.isRunning.return_value=True
        release = threading.Event()
        worker.wait.side_effect = lambda: release.wait(2)
        view = gui.CaptureTab(); self.addCleanup(close_and_dispose, view)
        view.capture_controls._profile_loaded = True
        view.acq_worker = worker; view.core = Mock()
        core = view.core
        self.assertFalse(view.cleanup())
        APP.processEvents()
        core.unloadAllDevices.assert_not_called()
        release.set()
        self.assertTrue(view._cleanup_worker.wait(3000))
        self.assertTrue(wait_for_qt(lambda: getattr(view, '_cleanup_done', False)))
        core.unloadAllDevices.assert_called_once()
        self.assertTrue(view._cleanup_done)

    def test_plate_navigator_does_not_expand_analysis_window(self):
        view=gui.AnalyzerTab(); self.addCleanup(close_and_dispose, view)
        view.resize(1100,760); view.show(); APP.processEvents(); before=view.size()
        view.plate_toggle.setEnabled(True); view.plate_toggle.setChecked(True); APP.processEvents()
        self.assertEqual(view.size(),before)
        view.plate_dialog.reject()
        self.assertFalse(view.plate_toggle.isChecked())

    def test_missing_summary_does_not_generate_zero_duration_waveform(self):
        view=gui.AnalyzerTab(); self.addCleanup(close_and_dispose, view)
        view.draw_recorded_wave({'Time_to_Peak_ms':100,'Peak_Amplitude_au':4})
        self.assertIn('unavailable', view.wave_note.text())
        self.assertIsNone(view.wave_curve.getData()[0])

    def test_analysis_applies_saved_metric_and_palette(self):
        view=gui.AnalyzerTab(); self.addCleanup(close_and_dispose, view)
        with patch.object(gui.tempo_preferences,'get',side_effect=lambda category,key,default=None: {'default_metric':'Time_to_Peak_ms','colormap_palette':'plasma'}.get(key,default)):
            view.apply_display_preferences()
        self.assertEqual(view.metric_combo.currentData(),'Time_to_Peak_ms')
        self.assertEqual(view.map_colormap.name,'plasma')
