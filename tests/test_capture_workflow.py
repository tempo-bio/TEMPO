"""Bounded, isolated capture/GUI fixtures. No cameras, stages, network, or serial hardware."""
import copy
from concurrent.futures import Future
from dataclasses import asdict
import os
os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock,patch
import numpy as np
from PyQt6.QtCore import QThread,Qt,pyqtSignal
from PyQt6.QtTest import QTest
from PyQt6.QtWidgets import QApplication
from tempo_capture import FieldCalibration
from tempo_stimulation import StimulusParameters,PacerStatus
from tempo_capture_ui import FieldCollectionWorker
from tempo_stimulation_scope import recommended_scales,waveform_segments
from tests.qt_lifecycle import close_and_dispose

APP=QApplication.instance() or QApplication([])
with patch('platform.machine',return_value='x86_64'): import tempo_gui as gui


class SimulatedService:
    def __init__(self):
        self.connected=True; self.running=False; self.error=''; self.commands=[]
        self.status=PacerStatus('REST_DISCHARGE',StimulusParameters(),False,100,2000,4.77)
        self.closed=Future()
    def submit(self,command):
        self.commands.append(command)
        params=self.status.parameters
        mode=self.status.mode
        if command.startswith('set '):
            _,v,hz,ms,ch,confirmation=command.split()
            params=StimulusParameters(float(v),float(hz),float(ms),int(ch))
        if command=='stop': mode='REST_DISCHARGE'
        if command=='pace': mode='PACING'
        self.status=PacerStatus(mode,params,False,100,2000,4.77)
        future=Future(); future.set_result(self.status); return future
    def close(self):
        self.submit('stop'); self.connected=False
        if not self.closed.done(): self.closed.set_result(self.status)
        return self.closed


class NativeAcquisitionTimingTests(unittest.TestCase):
    def test_requested_duration_uses_camera_time_instead_of_requested_frame_count(self):
        from types import SimpleNamespace
        from tempo_recording import read_metadata,timeline
        with tempfile.TemporaryDirectory() as directory:
            core=Mock();core.getImageWidth.return_value=32;core.getImageHeight.return_value=32
            core.getImageBitDepth.return_value=12;core.isSequenceRunning.return_value=True
            core.isBufferOverflowed.return_value=False;core.getRemainingImageCount.return_value=1
            images=[SimpleNamespace(pix=np.full((32,32),index,np.uint16),tags={'ElapsedTime-ms':index*5,'FrameIndex':index}) for index in range(8)]
            core.popNextTaggedImage.side_effect=images
            path=Path(directory)/'field.tif'
            worker=gui.CameraAcquisitionWorker(core=core,output_path=str(path),target_fps=100,
                native=True,duration_s=.03,recording_options={'retention':{'owned':True}})
            errors=[];finished=[];worker.error_signal.connect(errors.append);worker.finished_signal.connect(lambda *args:finished.append(args))
            with patch.object(gui,'HAS_MMCORE',True):worker.run()
            self.assertFalse(errors,errors);self.assertEqual(finished[0][1],7)
            self.assertEqual(read_metadata(path)['frames'],7)
            values,basis=timeline(path,7,100)
            self.assertEqual(basis,'camera');self.assertEqual(values[-1],30.)


class FocusPausePresentationTests(unittest.TestCase):
    def test_timeout_warning_is_visible_and_acknowledged_stop_is_not_reported_unconfirmed(self):
        from types import SimpleNamespace
        from PyQt6.QtGui import QFont
        from PyQt6.QtCore import QPoint,QRect
        from tempo_capture import CaptureSession,FocusPause
        original_font=APP.font();self.addCleanup(lambda:APP.setFont(original_font))
        with tempfile.TemporaryDirectory() as directory,patch('tempo_capture_ui.CaptureTools.showEvent',lambda self,event:None),patch.object(gui.CaptureTab,'update_frame',lambda self:None),patch.object(gui.tempo_preferences,'save_to_disk',return_value=True):
            for font,size in ((12,(960,620)),(18,(1100,760))):
                with self.subTest(font=font):
                    APP.setFont(QFont('Arial',font));view=gui.CaptureTab();view.resize(*size)
                    controls=view.capture_controls;view.core=SimpleNamespace();service=SimulatedService();service.submit('pace')
                    session=CaptureSession(directory,'fixture','A01',StimulusParameters());session.start(service.status);session.begin_imaging();session._transition('focus_paused')
                    controls.session=session;controls.service=service;service.submit('stop')
                    now=[0.];pause=FocusPause(clock=lambda:now[0]);now[0]=65.
                    view.scout_worker=SimpleNamespace(isRunning=lambda:True,is_running=True,focus_paused=True,focus_timed_out=True,focus_pause=pause,protocol_flags=['focus_timeout'])
                    try:
                        view.show()
                        for _ in range(6):APP.processEvents()
                        controls.show_focus_pause(dict(assessment=dict(focus='unavailable',reason='No reviewed profile.',flags=['focus_unavailable'])))
                        for _ in range(12):APP.processEvents()
                        viewport=view.acquisition_scroll.viewport()
                        bounds=QRect(controls.focus_warning.mapTo(viewport,QPoint()),controls.focus_warning.size())
                        self.assertTrue(viewport.rect().contains(bounds),(viewport.rect(),bounds))
                        self.assertEqual(view.btn_record.text(),'Resume Collection');self.assertTrue(view.btn_record.isEnabled())
                        self.assertIn('pacing stopped',controls.status.text())
                        self.assertNotIn('unconfirmed',controls.status.text().lower())
                        self.assertTrue(view.stimulation_panel.isAncestorOf(controls.stop_button))
                    finally:
                        view.timer.stop();controls.focus_warning_timer.stop();view.scout_worker=None
                        controls.session=None;controls.service=None;view.core=None;view.close()
                        from tempo_tasks import dispose_closed_views
                        for _ in range(4):APP.processEvents()
                        dispose_closed_views(view)


class SimulatedAcquisition(QThread):
    finished_signal=pyqtSignal(str,int,float)
    error_signal=pyqtSignal(str)
    def __init__(self,core=None,output_path='',**kwargs):
        super().__init__(); self.output_path=output_path
    def run(self):
        Path(self.output_path).touch()
        self.finished_signal.emit(self.output_path,15,100.)
    def stop(self): pass


class CollectionWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.core=Mock(); self.core.getXYStageDevice.return_value='stage'
        self.core.isSequenceRunning.return_value=False; self.core.deviceBusy.return_value=False
        self.core.getImage.return_value=np.zeros((10,10),dtype=np.uint8)
        self.position=(0,0)
        self.core.setXYPosition.side_effect=lambda stage,x,y:setattr(self,'position',(x,y))
        self.core.getXPosition.side_effect=lambda stage:self.position[0]
        self.core.getYPosition.side_effect=lambda stage:self.position[1]
        self.context=dict(signature='optics',image_size=(10,10),position=(0,0))
        self.calibration=FieldCalibration(((1,0),(0,1)),(10,10),'optics')
        self.counts=dict(live_count=5,dead_count=2,healthy_groups=3,dead_groups=0)
        self.service=SimulatedService(); self.enqueued=[]
        self.errors=[]; self.results=[]
    def worker(self,**kwargs):
        options=dict(core=self.core,plans=[dict(well='A01',channel=0,corner_a=(0,0),corner_b=(40,10))],
            experiment='experiment',root=self.temp.name,target=10,maximum=4,threshold=3,duration=.1,
            fps=100,calibration=self.calibration,model='fixture.pt',confidence=.15,
            service=self.service,parameters=StimulusParameters(),prepacing_seconds=0,
            acquisition_factory=SimulatedAcquisition,enqueue=lambda path:self.enqueued.append((path,list(self.service.commands))))
        options.update(kwargs)
        worker=FieldCollectionWorker(**options)
        worker.error_signal.connect(self.errors.append,Qt.ConnectionType.DirectConnection)
        worker.result_signal.connect(self.results.append,Qt.ConnectionType.DirectConnection)
        return worker
    def run_worker(self,worker,counts=None):
        with patch('tempo_capture_ui.optical_context',return_value=self.context),patch('tempo_capture_ui.count_frame',return_value=counts or self.counts): worker.run()

    def test_two_fields_share_one_pacing_train_and_queue_after_final_stop(self):
        worker=self.worker(); self.run_worker(worker)
        self.assertFalse(self.errors); self.assertEqual(len(self.enqueued),2)
        self.assertEqual(self.service.commands.count('pace'),1)
        self.assertEqual(self.service.commands.count('stop'),2)
        self.assertTrue(all(commands[-1]=='stop' for path,commands in self.enqueued))
        self.assertEqual(worker.session.document['healthy_cell_estimate'],10)
        self.assertEqual(worker.session.phase,'completed')

    def test_groups_do_not_qualify_or_contribute_and_search_reports_shortfall(self):
        worker=self.worker(maximum=2)
        counts=dict(self.counts,live_count=1,healthy_groups=100)
        self.run_worker(worker,counts)
        self.assertFalse(self.enqueued); self.assertFalse(self.errors)
        self.assertIn('Shortfall',self.results[0]); self.assertEqual(len(worker.session.document['fields']),2)

    def test_next_well_starts_only_after_previous_well_is_stopped(self):
        worker=self.worker(target=5,plans=[dict(well=f'A0{i+1}',channel=i,corner_a=(0,0),corner_b=(40,10)) for i in range(2)])
        self.run_worker(worker)
        self.assertFalse(self.errors); self.assertEqual(self.service.commands.count('pace'),2)
        starts=[i for i,cmd in enumerate(self.service.commands) if cmd=='pace']
        self.assertIn('stop',self.service.commands[starts[0]+1:starts[1]])

    def test_configuration_change_retains_first_recording_and_stops(self):
        worker=self.worker()
        contexts=[self.context,self.context,dict(self.context,signature='changed')]
        with patch('tempo_capture_ui.optical_context',side_effect=contexts),patch('tempo_capture_ui.count_frame',return_value=self.counts): worker.run()
        self.assertEqual(len(self.enqueued),1); self.assertEqual(worker.session.phase,'interrupted')
        self.assertIn('Optical configuration changed',self.errors[0]); self.assertEqual(self.service.commands[-1],'stop')

    def test_stage_motion_failure_prevents_stimulation_and_imaging(self):
        self.core.setXYPosition.side_effect=RuntimeError('stage lost')
        worker=self.worker(); self.run_worker(worker)
        self.assertIn('stage lost',self.errors[0]); self.assertNotIn('pace',self.service.commands)
        self.core.snapImage.assert_not_called()

    def test_stage_clamping_is_not_recorded_as_a_new_field(self):
        self.core.setXYPosition.side_effect=None
        worker=self.worker(); self.run_worker(worker)
        self.assertIn('did not reach',self.errors[0])
        self.core.snapImage.assert_not_called()
        self.assertNotIn('pace',self.service.commands)

    def test_manual_prepacing_waits_for_begin_imaging(self):
        worker=self.worker(prepacing_seconds=None,target=5)
        with patch('tempo_capture_ui.optical_context',return_value=self.context),patch('tempo_capture_ui.count_frame',return_value=self.counts):
            worker.start()
            try:
                deadline=time.monotonic()+2
                while (worker.session is None or worker.session.phase!='pre_pacing') and time.monotonic()<deadline: QTest.qWait(10)
                self.assertIsNotNone(worker.session); self.assertEqual(worker.session.phase,'pre_pacing')
                self.assertFalse(self.enqueued)
                worker.imaging_ready.set()
                self.assertTrue(worker.wait(2000))
            finally:
                worker.stop(); worker.wait(2000)
        self.assertEqual(len(self.enqueued),1)

    def test_begin_imaging_immediately_after_session_publication_is_not_lost(self):
        worker=self.worker(prepacing_seconds=None,target=5)
        worker.session_signal.connect(lambda session:worker.imaging_ready.set(),Qt.ConnectionType.DirectConnection)
        self.run_worker(worker)
        self.assertFalse(self.errors); self.assertEqual(len(self.enqueued),1)

    def test_running_well_is_adopted_without_restarting_pacing(self):
        from tempo_capture import CaptureSession
        self.service.submit('pace')
        session=CaptureSession(self.temp.name,'experiment','A01',StimulusParameters(),channel=0)
        session.start(self.service.status); session.begin_imaging()
        worker=self.worker(initial_session=session)
        self.run_worker(worker)
        self.assertFalse(self.errors)
        self.assertIs(worker.session,session)
        self.assertEqual(self.service.commands.count('pace'),1)
        self.assertEqual(len(self.enqueued),2)

    def test_adopted_well_already_at_target_does_not_record_an_extra_field(self):
        from tempo_capture import CaptureSession
        self.service.submit('pace')
        session=CaptureSession(self.temp.name,'experiment','A01',StimulusParameters(),channel=0)
        session.start(self.service.status); session.begin_imaging()
        session.document['healthy_cell_estimate']=10
        worker=self.worker(initial_session=session)
        self.run_worker(worker)
        self.assertFalse(self.errors); self.assertFalse(self.enqueued)
        self.assertEqual(session.phase,'completed')

    def test_focus_response_within_minute_keeps_pacing_and_records_only_field_override(self):
        from tempo_capture import CaptureSession,FocusPause
        worker=self.worker(prepacing_seconds=0)
        self.service.submit('pace')
        worker.session=CaptureSession(self.temp.name,'experiment','A01',StimulusParameters(),channel=0)
        worker.session.start(self.service.status);worker.session.begin_imaging()
        now=[0.];pause=FocusPause(clock=lambda:now[0])
        def respond(details):
            now[0]=30.;pause.request_resume();worker.resume_actor='reviewer';worker.focus_resume.set()
        worker.focus_signal.connect(respond,Qt.ConnectionType.DirectConnection)
        with patch('tempo_capture.FocusPause',return_value=pause):
            worker._review_focus('FOV001',self.counts,dict(focus='unavailable',flags=['focus_unavailable']))
        self.assertEqual(self.service.status.mode,'PACING')
        self.assertNotIn('stop',self.service.commands)
        event=worker.session.document['focus_events'][0]
        self.assertTrue(event['override']);self.assertFalse(event['timed_out'])
        self.assertEqual(worker.protocol_flags,[])

    def test_focus_timeout_stops_then_repeats_prepacing_and_flags_later_fields(self):
        from tempo_capture import CaptureSession,FocusPause
        worker=self.worker(prepacing_seconds=0)
        self.service.submit('pace')
        worker.session=CaptureSession(self.temp.name,'experiment','A01',StimulusParameters(),channel=0)
        worker.session.start(self.service.status);worker.session.begin_imaging()
        worker.session.document['fields'].append(dict(field_id='FOV001',status='completed',counts=dict(self.counts)))
        initial=worker.session.document['initial_prepacing_s']
        now=[0.];pause=FocusPause(clock=lambda:now[0])
        def respond(details):
            now[0]=61.
            if details['timed_out']:
                pause.request_resume();worker.resume_actor='reviewer';worker.focus_resume.set()
        worker.focus_signal.connect(respond,Qt.ConnectionType.DirectConnection)
        with patch('tempo_capture.FocusPause',return_value=pause):
            worker._review_focus('FOV002',self.counts,dict(focus='out_of_focus',flags=['focus']))
        self.assertIn('stop',self.service.commands)
        self.assertEqual(self.service.commands[-1],'pace')
        self.assertIn('possible_viability_effect',worker.protocol_flags)
        self.assertIn('possible_measurement_validity_effect',worker.protocol_flags)
        self.assertNotIn('quality',worker.session.document['fields'][0]['counts'])
        self.assertTrue(worker.session.document['focus_events'][0]['timed_out'])
        self.assertIn('resume_prepacing_s',worker.session.document['focus_events'][0])
        self.assertEqual(worker.session.document['initial_prepacing_s'],initial)


class ManualCaptureTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        queue_patch=patch.object(gui.tempo_backend,'QUEUE_FILE',str(Path(self.temp.name)/'queue.json'))
        queue_patch.start();self.addCleanup(queue_patch.stop)
        self.view=gui.CaptureTab(); self.tools=self.view.capture_controls
        self.tools._profile_loaded=True; self.tools._last_stage_probe=time.monotonic()+100
        self.tools.profile_path=Path(self.temp.name)/'setup.json'
        self.service=SimulatedService(); self.tools.service=self.service
        self.tools.width.setValue(2); self.tools.enabled.setChecked(True)
        self.view.cv_cap=Mock()
        self.view.cv_cap.get.side_effect=lambda prop: 20
        self.view.cv_cap.read.return_value=(True,np.zeros((20,20,3),dtype=np.uint8))
        self.addCleanup(self.close)
    def close(self):
        deadline=time.monotonic()+3
        while not self.tools.shutdown() and time.monotonic()<deadline: QTest.qWait(10)
        close_and_dispose(self.view); APP.processEvents()
    def settle(self,condition):
        deadline=time.monotonic()+3
        while not condition() and time.monotonic()<deadline: QTest.qWait(10)
        self.assertTrue(condition())
    def test_manual_well_locks_settings_and_recording_stop_keeps_pacing(self):
        self.tools.poll_timer.start()
        factory=gui.CameraAcquisitionWorker
        def bounded(**kwargs): return factory(**dict(kwargs,max_frames=3))
        writer=Mock(); writer.isOpened.return_value=True
        with patch.object(gui.tempo_backend,'RAW_DIR',self.temp.name),patch('tempo_capture_ui.local_scout_path',return_value='fixture.pt'),\
             patch('tempo_capture_ui.count_frame',return_value=dict(live_count=5,dead_count=1,healthy_groups=1,dead_groups=0)),\
             patch.object(gui,'CameraAcquisitionWorker',side_effect=bounded),patch.object(gui.cv2,'VideoWriter',return_value=writer):
            self.tools.start_well()
            self.settle(lambda:self.tools.session is not None)
            self.assertEqual(self.tools.session.phase,'pre_pacing'); self.assertFalse(self.tools.frequency.isEnabled())
            self.tools.begin_imaging(); self.settle(lambda:self.tools.session.phase=='ready' and not self.tools.busy())
            self.tools.request_record(); self.settle(lambda:self.tools.session.phase=='between_fields')
            self.assertEqual(self.service.status.mode,'PACING')
            self.assertEqual(self.tools.session.document['healthy_cell_estimate'],5)
            self.tools.finish_well(); self.settle(lambda:self.tools.session.phase=='completed')
            self.assertEqual(self.service.status.mode,'REST_DISCHARGE')
    def test_auto_option_requires_detected_stage(self):
        self.tools.refresh_controls(); self.assertTrue(self.tools.automatic_group.isHidden())
        self.tools._stage_present=True; self.tools.refresh_controls(); self.assertFalse(self.tools.automatic_group.isHidden())

    def test_stop_during_parameter_setup_prevents_a_later_pace_command(self):
        gate=Future()
        original=self.service.submit
        def delayed(command):
            if command.startswith('set '):
                self.service.commands.append(command)
                return gate
            return original(command)
        self.service.submit=delayed
        self.tools.poll_timer.start()
        with patch.object(gui.tempo_backend,'RAW_DIR',self.temp.name):
            self.tools.start_well()
            self.settle(lambda:any(command.startswith('set ') for command in self.service.commands))
            self.tools.emergency_stop()
            gate.set_result(self.service.status)
            self.settle(lambda:not self.tools.busy())
            self.assertNotIn('pace',self.service.commands)

    def test_pending_automatic_worker_cancels_without_starting(self):
        worker=SimulatedAcquisition()
        worker.is_running=True
        self.view.scout_worker=worker
        self.view._pending_camera_worker=worker
        self.assertTrue(self.tools.automatic_running())
        self.tools.stop_automatic()
        self.assertIsNone(self.view._pending_camera_worker)
        self.assertFalse(worker.isRunning())

    def test_manual_stop_recording_control_remains_enabled_and_keeps_pacing(self):
        writer=Mock(); writer.isOpened.return_value=True
        writer.metadata={}
        frame=np.zeros((20,20,3),dtype=np.uint8)
        self.view.cv_cap.read.side_effect=lambda:(time.sleep(.01) or True,frame)
        counts=dict(live_count=5,dead_count=0,healthy_groups=0,dead_groups=0)
        with patch.object(gui.tempo_backend,'RAW_DIR',self.temp.name),patch('tempo_capture_ui.local_scout_path',return_value='fixture.pt'),\
             patch('tempo_capture_ui.count_frame',return_value=counts),patch('tempo_recording.NativeWriter',return_value=writer):
            self.tools.start_well(); self.settle(lambda:self.tools.session is not None)
            self.tools.begin_imaging(); self.settle(lambda:self.tools.session.phase=='ready' and not self.tools.busy())
            self.tools.request_record(); self.settle(lambda:writer.write.called)
            self.assertTrue(self.view.btn_record.isEnabled())
            self.view.btn_record.click()
            self.settle(lambda:self.tools.session.phase=='between_fields')
            self.assertEqual(self.service.status.mode,'PACING')

    def test_counting_restores_recording_control(self):
        counts=dict(live_count=5,dead_count=0,healthy_groups=0,dead_groups=0)
        with patch('tempo_capture_ui.local_scout_path',return_value='fixture.pt'),patch('tempo_capture_ui.count_frame',return_value=counts):
            self.tools.count_current(); self.settle(lambda:not self.tools.busy())
            self.assertTrue(self.view.btn_record.isEnabled())

    def test_unconfirmed_stop_during_count_does_not_restart_camera_preview(self):
        self.view.core=Mock(); self.view.core.isSequenceRunning.return_value=True
        self.view.core.stopSequenceAcquisition.side_effect=OSError('driver refused stop')
        self.view.timer.start(1000)
        with patch('tempo_capture_ui.local_scout_path',return_value='fixture.pt'):
            self.tools.count_current(); self.settle(lambda:not self.tools.busy())
        self.view.core.startContinuousSequenceAcquisition.assert_not_called()
        self.assertFalse(self.view.btn_record.isEnabled())
        self.assertEqual(self.view.btn_cam.text(),'Retry Stop Feed')

    def test_controls_belong_to_the_requested_side_panels(self):
        for control in (self.view.btn_record,self.view.btn_snap,self.view.spin_exposure,
                        self.view.spin_rec_fps,self.view.chk_overlap,self.view.z_index,self.tools.field_group,self.tools.automatic_group):
            self.assertTrue(self.view.camera_panel.isAncestorOf(control),control.objectName() or type(control).__name__)
        for control in (self.tools.connection_group,self.tools.amplitude,self.tools.enabled,self.tools.scope_preview):
            self.assertTrue(self.view.stimulation_panel.isAncestorOf(control))
        self.assertTrue(self.view.camera_settings_dialog.isAncestorOf(self.view.btn_load_cfg))
        self.assertTrue(self.view.camera_settings_dialog.isAncestorOf(self.view.prop_combo))
        self.assertTrue(self.view.z_index.isEnabled())
        self.assertIn('Single output',self.tools.channel_label.text())
        self.assertNotIn('Channel 0',self.tools.channel_label.text())

    def test_chosen_compact_values_are_editable_with_native_arrow_controls(self):
        from PyQt6.QtWidgets import QStyle,QStyleOptionSpinBox,QPushButton
        self.tools.refresh_controls()
        self.view.resize(1280,820); self.view.show(); APP.processEvents()
        try:
            control=self.tools.amplitude
            control.lineEdit().selectAll()
            QTest.keyClicks(control.lineEdit(),'0.75 V'); QTest.keyClick(control.lineEdit(),Qt.Key.Key_Return)
            self.assertAlmostEqual(control.value(),.75)
            option=QStyleOptionSpinBox(); control.initStyleOption(option)
            for subcontrol,expected in ((QStyle.SubControl.SC_SpinBoxUp,.80),(QStyle.SubControl.SC_SpinBoxDown,.75)):
                bounds=control.style().subControlRect(QStyle.ComplexControl.CC_SpinBox,option,subcontrol,control)
                self.assertFalse(bounds.isEmpty())
                QTest.mouseClick(control,Qt.MouseButton.LeftButton,pos=bounds.center())
                self.assertAlmostEqual(control.value(),expected)
            self.assertAlmostEqual(self.tools.scope_preview.trace.voltage,.75)
            self.assertLess(self.tools.off_label.mapTo(self.view,self.tools.off_label.rect().center()).x(),self.tools.enabled.mapTo(self.view,self.tools.enabled.rect().center()).x())
            self.assertLess(self.tools.enabled.mapTo(self.view,self.tools.enabled.rect().center()).x(),self.tools.on_label.mapTo(self.view,self.tools.on_label.rect().center()).x())
            self.assertFalse(any(button.text()=='Stimulation Settings…' for button in self.view.findChildren(QPushButton)))
        finally:
            self.view.hide(); self.tools.poll_timer.stop()

    def test_prepacing_overlay_supports_click_drag_and_keyboard(self):
        from PyQt6.QtCore import QPoint
        self.tools.refresh_controls()
        self.view.resize(1280,820); self.view.show(); APP.processEvents()
        try:
            slider=self.tools.pre_mode
            left=QPoint(slider.width()//4,slider.height()//2)
            right=QPoint(3*slider.width()//4,slider.height()//2)
            QTest.mouseClick(slider,Qt.MouseButton.LeftButton,pos=right)
            self.assertEqual(slider.currentData(),'timed')
            self.assertFalse(self.tools.pre_duration.isHidden())
            QTest.mouseClick(slider,Qt.MouseButton.LeftButton,pos=left)
            self.assertEqual(slider.currentData(),'manual')
            QTest.mousePress(slider,Qt.MouseButton.LeftButton,pos=left)
            QTest.mouseMove(slider,right,20)
            QTest.mouseRelease(slider,Qt.MouseButton.LeftButton,pos=right)
            self.assertEqual(slider.currentData(),'timed')
            QTest.keyClick(slider,Qt.Key.Key_Left)
            self.assertEqual(slider.currentData(),'manual')
            self.assertTrue(self.tools.pre_duration.isHidden())
            slider.setEnabled(False)
            QTest.mouseClick(slider,Qt.MouseButton.LeftButton,pos=right)
            self.assertEqual(slider.currentData(),'manual')
        finally:
            self.view.hide(); self.tools.poll_timer.stop()

    def test_switch_starts_pacing_and_off_completes_the_well(self):
        self.tools.enabled.setChecked(False); self.tools.refresh_controls(); self.tools.poll_timer.start()
        with patch.object(gui.tempo_backend,'RAW_DIR',self.temp.name):
            self.tools.enabled.click()
            self.settle(lambda:self.tools.session is not None and not self.tools.busy())
            self.assertEqual(self.service.status.mode,'PACING')
            self.assertTrue(self.tools.enabled.isChecked())
            self.assertEqual(self.view.btn_record.text(),'Begin Imaging')
            self.tools.enabled.click()
            self.settle(lambda:self.tools.session.phase=='completed' and not self.tools.busy())
            self.assertEqual(self.service.status.mode,'REST_DISCHARGE')
            self.assertFalse(self.tools.enabled.isChecked())

    def test_switch_pending_start_can_be_cancelled_before_acknowledgment(self):
        gate=Future(); original=self.service.submit
        def delayed(command):
            if command.startswith('set '):
                self.service.commands.append(command); return gate
            return original(command)
        self.service.submit=delayed
        self.tools.enabled.setChecked(False); self.tools.refresh_controls(); self.tools.poll_timer.start()
        with patch.object(gui.tempo_backend,'RAW_DIR',self.temp.name):
            self.tools.enabled.click()
            self.settle(lambda:any(cmd.startswith('set ') for cmd in self.service.commands))
            self.assertFalse(self.tools.enabled.isChecked())
            self.assertIn('Starting',self.tools.status.text())
            self.tools.enabled.click()
            gate.set_result(self.service.status)
            self.settle(lambda:not self.tools.busy())
            self.assertNotIn('pace',self.service.commands)

    def test_failed_start_does_not_leave_switch_on(self):
        original=self.service.submit
        def rejected(command):
            if command=='pace':
                future=Future(); future.set_exception(RuntimeError('settings rejected')); return future
            return original(command)
        self.service.submit=rejected
        self.tools.enabled.setChecked(False); self.tools.refresh_controls(); self.tools.poll_timer.start()
        with patch.object(gui.tempo_backend,'RAW_DIR',self.temp.name):
            self.tools.enabled.click(); self.settle(lambda:not self.tools.busy())
            self.assertFalse(self.tools.enabled.isChecked())
            self.assertFalse(self.tools._output_requested)
            self.assertIsNone(self.tools.session)

    def test_timed_queued_record_can_be_cancelled_without_stopping_stimulation(self):
        self.tools.pre_mode.setCurrentIndex(1); self.tools.pre_duration.setValue(100)
        self.tools.poll_timer.start()
        with patch.object(gui.tempo_backend,'RAW_DIR',self.temp.name):
            self.tools.start_well(stimulate=True); self.settle(lambda:self.tools.session is not None and not self.tools.busy())
            self.view.btn_record.click(); self.assertTrue(self.tools._record_after_prepacing)
            self.view.btn_record.click(); self.assertFalse(self.tools._record_after_prepacing)
            self.assertEqual(self.service.status.mode,'PACING')
            self.assertEqual(self.tools.session.phase,'pre_pacing')

    def test_background_estimate_discards_a_result_after_field_change(self):
        gate=threading.Event()
        counts=dict(live_count=23,dead_count=1,healthy_groups=0,dead_groups=0)
        def infer(*args): gate.wait(2); return counts
        with patch('tempo_capture_ui.local_scout_path',return_value='fixture.pt'),patch('tempo_capture_ui.count_frame',side_effect=infer):
            self.tools.preview_for_counting(np.zeros((20,20),dtype=np.uint8))
            self.assertIsNotNone(self.tools._count_task)
            self.view.fov_id.setValue(2)
            gate.set(); self.settle(lambda:self.tools._count_task is None)
            self.assertNotIn('23',self.tools.counts_label.text())
            self.view.cv_cap.read.assert_not_called()


class ScopeTests(unittest.TestCase):
    def test_recommended_scales_show_pulse_and_train(self):
        self.assertEqual(recommended_scales(.6,1,.002),(.001,.2))
        self.assertEqual(recommended_scales(.6,1,.002,view='train'),(.2,.2))
    def test_analytic_narrow_pulses_keep_both_edges(self):
        points=waveform_segments(.6,1,.0001,'pace',-.2,1.8)
        self.assertIn((0.,.6),points); self.assertIn((.0001,0),points)
        self.assertEqual(waveform_segments(.6,1,.002,'constant',0,1),[(0,.6),(1,.6)])
    def test_scope_scale_controls_and_fixed_trigger_follow_signal(self):
        from tempo_stimulation_scope import ScopePreview
        scope=ScopePreview(); self.addCleanup(close_and_dispose,scope)
        scope.open_view(); APP.processEvents()
        self.addCleanup(close_and_dispose,scope.dialog)
        scope.time_combo.setCurrentIndex(scope.time_combo.findData(.1))
        self.assertTrue(scope.custom); self.assertEqual(scope.trace.time_div,.1)
        scope.set_signal(.8,2,5,'square')
        self.assertEqual(scope.trace.frequency,10); self.assertEqual(scope.trace.pulse_width_s,.05)
        scope.recommend(); self.assertFalse(scope.custom)
        scope.set_signal(.6,8,2,'cycle'); self.assertEqual(scope.trace.frequency,1)
        scope.set_signal(.6,1,2,'constant'); self.assertIn('Auto trigger',scope.trigger.text())
        scope.set_signal(.6,1,2,'isolated')
        self.assertIn('no triggerable signal',scope.trigger.text())
        self.assertFalse(scope.dialog.grab().isNull())
        from PyQt6.QtWidgets import QPushButton,QTextBrowser,QLabel
        self.assertFalse(any('ideal waveform' in label.text().lower() or 'output off' in label.text().lower()
                             or 'simulated commanded output' in label.text().lower() for label in scope.findChildren(QLabel)))
        self.assertFalse(any(button.text()=='Close' for button in scope.dialog.findChildren(QPushButton)))
        scope.scope_help(); self.addCleanup(close_and_dispose,scope._help_dialog)
        guide=scope._help_dialog.findChild(QTextBrowser).toPlainText()
        for topic in ('probe attenuation','0.2 V/div','0.3 V','Clipped pulse','synchronization'):
            self.assertIn(topic,guide)


if __name__=='__main__': unittest.main()
