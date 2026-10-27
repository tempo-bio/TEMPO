"""Acquisition tools and bounded collection workers; camera display stays in CaptureTab."""
from dataclasses import asdict
from pathlib import Path
import copy
import hashlib
import json
import math
import re
import tempfile
import threading
import time

import cv2
import numpy as np
from PyQt6.QtCore import QThread, QTimer, Qt, QEvent, pyqtSignal
from PyQt6.QtGui import QKeySequence, QShortcut, QFont
from PyQt6.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QFormLayout, QLabel,
    QComboBox, QDoubleSpinBox, QSpinBox, QPushButton, QCheckBox, QGroupBox,
    QDialog, QDialogButtonBox, QListWidget, QAbstractItemView, QScrollArea, QSizePolicy, QMenu, QTextBrowser, QGridLayout)
from tempo_capture import CaptureSession, FieldCalibration, search_grid, validate_capture_profile
from tempo_config import tempo_preferences, get_config_dir
from tempo_processing import atomic_write
from tempo_regions import region_counts
from tempo_stimulation import PacerService, StimulusParameters
from tempo_tasks import TaskThread
from tempo_ui import InformationButton, ErrorDetails, FlowRow, OnOffSwitch, SlidingChoice, confirm_action


def optical_context(core, cv_cap=None):
    if core is None:
        if cv_cap is None:
            raise RuntimeError('Connect a camera first.')
        size = (int(cv_cap.get(cv2.CAP_PROP_FRAME_WIDTH)), int(cv_cap.get(cv2.CAP_PROP_FRAME_HEIGHT)))
        context=dict(image_size=size, signature='usb-' + str(size), position=None)
        return quality_context(context,core,cv_cap)
    camera = core.getCameraDevice()
    signature = dict(camera=str(camera), size=[int(core.getImageWidth()), int(core.getImageHeight())])
    for name in ('getROI', 'getPixelSizeUm', 'getCurrentPixelSizeConfig'):
        try:
            value = getattr(core, name)()
            signature[name] = list(value) if isinstance(value, tuple) else value
        except Exception:
            signature[name] = None
    try:
        for name in core.getAvailableConfigGroups():
            if re.search('objective|magnif|zoom|binning|optics', str(name), re.I):
                signature[str(name)] = str(core.getCurrentConfig(name))
        signature['binning'] = str(core.getProperty(camera, 'Binning'))
    except Exception:
        pass
    position = None
    try:
        stage = core.getXYStageDevice()
        if stage:
            position = (float(core.getXPosition(stage)), float(core.getYPosition(stage)))
    except Exception:
        pass
    context=dict(image_size=tuple(signature['size']),
        signature=hashlib.sha256(json.dumps(signature, sort_keys=True).encode()).hexdigest(), position=position)
    return quality_context(context,core,cv_cap)


def quality_context(context,core=None,cap=None):
    """Exposure changes invalidate focus profiles without invalidating XY geometry."""
    settings={'optical_signature':context['signature']}
    try:
        if core is not None:
            settings['exposure_ms']=float(core.getExposure())
            settings['sensor_bits']=int(core.getImageBitDepth())
            camera=core.getCameraDevice()
            for name in ('Gain','GainRaw','AnalogGain','PixelType'):
                try:settings[name]=str(core.getProperty(camera,name))
                except Exception:settings[name]=None
        else:
            settings['exposure']=float(cap.get(cv2.CAP_PROP_EXPOSURE))
            settings['gain']=float(cap.get(cv2.CAP_PROP_GAIN))
            settings['sensor_bits']=8
        encoded=json.dumps(settings,sort_keys=True,allow_nan=False)
    except (ValueError,TypeError,AttributeError,RuntimeError):
        return dict(context,quality_signature=None,sensor_bits=None)
    return dict(context,quality_signature=hashlib.sha256(encoded.encode()).hexdigest(),sensor_bits=settings['sensor_bits'])


class CameraStopUnconfirmed(RuntimeError):
    pass


def fresh_frame(core, cv_cap=None):
    """Called only after preview ownership has been surrendered to this worker."""
    if core is not None:
        try:
            if core.isSequenceRunning():
                core.stopSequenceAcquisition()
        except Exception as exc:
            raise CameraStopUnconfirmed('The camera did not confirm that its previous acquisition stopped. Retry Stop Feed before another capture.') from exc
        core.clearCircularBuffer()
        core.snapImage()
        frame = np.asarray(core.getImage()).copy()
        if frame.size == 0:
            raise RuntimeError('No camera image received.')
        return frame
    if cv_cap is None:
        raise RuntimeError('Connect a camera first.')
    ok, frame = cv_cap.read()
    if not ok or frame is None:
        raise RuntimeError('No camera image received.')
    return frame


def local_scout_path():
    from tempo_model_catalog import MODEL_FILES, local_model_path
    candidate = local_model_path(MODEL_FILES['nano_scout'])
    if not candidate.is_file():
        raise RuntimeError('Install the Nano Scout model from Settings before using cell counting or automatic collection.')
    return str(candidate)


def count_frame(frame, model, confidence):
    from yolo_inference_helper import detect_cells_via_bridge
    # The Rosetta bridge accepts paths. Never leave a captured research frame in scratch.
    with tempfile.TemporaryDirectory(prefix='tempo-field-count-') as directory:
        path = str(Path(directory) / 'field.png')
        if not cv2.imwrite(path, frame):
            raise OSError('Could not prepare the camera image for counting.')
        response = detect_cells_via_bridge(path, model_path_or_type=model, conf=confidence)
    if not isinstance(response,dict):
        raise RuntimeError('The cell detector returned an invalid response.')
    if response.get('success') is not True:
        raise RuntimeError(response.get('error') or 'Cell counting failed.')
    if not isinstance(response.get('boxes'),list) or any(not isinstance(box,dict) for box in response['boxes']):
        raise RuntimeError('The cell detector returned invalid field counts.')
    counts = region_counts(response['boxes'])
    digest = hashlib.sha256()
    with Path(model).open('rb') as stream:
        for block in iter(lambda: stream.read(1024*1024),b''): digest.update(block)
    return dict(counts, model=Path(model).name, model_sha256=digest.hexdigest(), confidence=confidence, measured_at=time.time())


def check_field(frame, model, confidence, signature, profile, sensor_bits=None):
    from tempo_quality import assess
    quality=assess(frame,signature,profile,sensor_bits=sensor_bits)
    counts=None;error=''
    if model is not None:
        try:
            counts=count_frame(frame,model,confidence)
            counts['quality']=quality
        except Exception as exc:
            error=str(exc)
    return dict(counts=counts,quality=quality,counting_error=error,signature=signature)


class FirmwareWorker(QThread):
    progress = pyqtSignal(str)
    outcome = pyqtSignal(object)

    def __init__(self, service, port, parent=None):
        super().__init__(parent)
        from tempo_firmware import FirmwareInstaller
        self.service, self.port = service, port
        self.verified_port = None
        self.installer = FirmwareInstaller(Path(get_config_dir()).parent/'arduino', self.progress.emit)

    def run(self):
        try:
            from tempo_firmware import verify_firmware
            if self.service:
                self.service.close().result(timeout=3)
            sketch = Path(__file__).resolve().parent/'firmware'/'tempo_pacer'
            self.verified_port = self.installer.install(self.port, sketch,
                lambda port: verify_firmware(port,serial_number=self.installer.serial_number))
            self.outcome.emit(None)
        except Exception as exc:
            self.outcome.emit(exc)


class FirmwareDialog(QDialog):
    def __init__(self, tools, section='outputs'):
        super().__init__(tools)
        self.tools = tools
        self.worker = None
        self._close_after_cancel = False
        self.setWindowTitle('TEMPO · Install / Update Firmware')
        self.resize(470,300)
        layout = QVBoxLayout(self)
        instruction = QLabel('Connect the Uno R4 Minima by USB. Installation replaces its current sketch. Keep it connected until verification finishes.')
        instruction.setWordWrap(True); layout.addWidget(instruction)
        self.port = QComboBox()
        for i in range(tools.port.count()): self.port.addItem(tools.port.itemText(i),tools.port.itemData(i))
        self.port.setCurrentIndex(tools.port.currentIndex()); layout.addWidget(self.port)
        self.status = QLabel('First-time setup downloads Arduino tools and board support. The Arduino IDE is not required.')
        self.status.setWordWrap(True); layout.addWidget(self.status)
        self.error = ErrorDetails(); layout.addWidget(self.error)
        self.install_button = QPushButton('Install / Update Firmware'); self.install_button.clicked.connect(self.install)
        layout.addWidget(self.install_button)
        self.close_button = QPushButton('Close'); self.close_button.clicked.connect(self.close); layout.addWidget(self.close_button)

    def install(self):
        if self.worker and not self.worker.wait(0): return
        if not self.port.currentData():
            self.status.setText('Close this window, refresh the serial ports, and choose the Uno R4 Minima.'); return
        if self.tools.camera_busy() or self.tools.busy() or (self.tools.session and self.tools.session.active):
            self.status.setText('Finish capture and stimulation before updating firmware.'); return
        self.worker = FirmwareWorker(self.tools.service,self.port.currentData(),self)
        self.worker.progress.connect(self.progress)
        self.worker.outcome.connect(self.completed)
        self.worker.finished.connect(self.tools.refresh_controls)
        self.worker.finished.connect(self.finish_cancellation)
        self.install_button.setEnabled(False); self.close_button.setText('Cancel Setup'); self.port.setEnabled(False)
        self.error.clear()
        self.worker.start()
        self.tools.refresh_controls()

    def completed(self, error):
        self.close_button.setEnabled(True); self.close_button.setText('Close'); self.port.setEnabled(True); self.install_button.setEnabled(True)
        if error: self.error.set_error(str(error),error)
        else:
            port = self.worker.verified_port
            if port:
                index = self.tools.port.findData(port)
                if index < 0:
                    self.tools.port.addItem(port + ' · Uno R4 Minima',port)
                    index = self.tools.port.findData(port)
                self.tools.port.setCurrentIndex(index)
            self.status.setText('Firmware verified. Resting discharge confirmed. Close this window and connect the stimulator.')
        self.tools.refresh_controls()

    def progress(self, text):
        self.status.setText(text)
        self.close_button.setEnabled(not self.worker.installer.uploading)

    def finish_cancellation(self):
        if self._close_after_cancel:
            if not self.worker.wait(0):
                QTimer.singleShot(20,self.finish_cancellation)
            else:
                self.close()

    def closeEvent(self,event):
        if self.worker is not None and not self.worker.wait(0):
            if self.worker.installer.cancel_setup():
                self._close_after_cancel = True
                self.status.setText('Cancelling setup before upload…')
                self.close_button.setEnabled(False)
            else:
                self.status.setText('Firmware upload is running. Keep the Arduino connected until verification finishes.')
            event.ignore(); return
        super().closeEvent(event)

    def reject(self):
        if self.worker is None or self.worker.wait(0): super().reject()
        else: self.close()


class FieldCollectionWorker(QThread):
    status_signal = pyqtSignal(str)
    session_signal = pyqtSignal(object)
    fields_signal = pyqtSignal(object)
    error_signal = pyqtSignal(str)
    result_signal = pyqtSignal(str)
    focus_signal = pyqtSignal(object)

    def __init__(self, core, plans, experiment, root, target, maximum, threshold, duration,
                 fps, calibration, model, confidence, service=None, parameters=None,
                 prepacing_seconds=None, acquisition_factory=None, enqueue=None, parent=None, initial_session=None,
                 recording_options=None):
        super().__init__(parent)
        self.core, self.plans = core, copy.deepcopy(plans)
        self.experiment, self.root = experiment, root
        self.target, self.maximum, self.threshold = target, maximum, threshold
        self.duration, self.fps = duration, fps
        self.calibration, self.model, self.confidence = calibration, model, confidence
        self.service, self.parameters = service, parameters
        self.prepacing_seconds = prepacing_seconds
        self.acquisition_factory, self.enqueue = acquisition_factory, enqueue
        self.cancelled = threading.Event()
        self.imaging_ready = threading.Event()
        self.session = initial_session
        self.initial_session = initial_session
        self.acquisition = None
        self.is_running = True
        self.stop_confirmed = True
        self.recording_options = recording_options
        self.focus_paused=False
        self.focus_timed_out=False
        self.focus_resume=threading.Event()
        self.focus_pause=None
        self.protocol_flags=[]
        self.resume_actor=''

    def stop(self):
        self.is_running = False
        self.cancelled.set()
        if self.acquisition is not None:
            self.acquisition.stop()
        if self.service is not None and self.service.connected:
            self.service.submit('stop')

    def _check(self):
        if self.cancelled.is_set():
            raise InterruptedError('Collection stopped. Completed recordings were retained.')
        if self.service is not None and (not self.service.connected or self.service.status is None):
            raise ConnectionError(self.service.error or 'Stimulator output is unconfirmed.')
        if self.service is not None and self.session is not None and self.session.active and not (self.focus_paused and self.focus_timed_out):
            if self.service.status.mode != 'PACING' or self.service.status.cycling:
                raise ConnectionError('Continuous single-channel pacing was interrupted.')

    def _move(self, stage, position):
        self._check()
        self.stop_confirmed = False
        if self.core.isSequenceRunning(): self.core.stopSequenceAcquisition()
        self.stop_confirmed = True
        self.core.clearCircularBuffer()
        try:
            self.core.setXYPosition(stage, *position)
        except Exception as exc:
            raise RuntimeError('Could not position the microscope stage: ' + str(exc)) from exc
        deadline = time.monotonic() + 15
        while self.core.deviceBusy(stage):
            self._check()
            if time.monotonic() > deadline:
                raise TimeoutError('The stage did not finish moving within 15 seconds.')
            self.cancelled.wait(.02)
        self.cancelled.wait(float(tempo_preferences.get('scout', 'settle_dwell_ms', 40)) / 1000)
        self._check()
        actual = (float(self.core.getXPosition(stage)),float(self.core.getYPosition(stage)))
        tolerance = max(.5,min(self.calibration.footprint)*.01)
        if not all(math.isfinite(n) for n in actual) or any(abs(a-b)>tolerance for a,b in zip(actual,position)):
            raise RuntimeError('The microscope stage did not reach the requested field. Collection stopped before imaging.')
        return actual

    def _stop_output(self):
        if self.service is None:
            return None
        return self.service.submit('stop').result(timeout=2)

    def _focus_timeout(self,event):
        from tempo_capture import utc_now
        self.focus_timed_out=True
        self.protocol_flags=['focus_timeout','possible_viability_effect','possible_measurement_validity_effect']
        self.session.document['protocol_flags']=list(self.protocol_flags)
        event.update(timed_out=True,stop_requested_at=utc_now())
        self.session._save()
        status=self._stop_output()
        confirmed=status is not None and status.mode=='REST_DISCHARGE'
        self.session.document['output_confirmed']=confirmed
        event.update(stop_confirmed=confirmed,stopped_at=utc_now() if confirmed else None)
        self.session._save()
        if not confirmed:raise RuntimeError('Focus response timed out; stimulation stop is unconfirmed.')

    def _review_focus(self,field_id,counts,assessment):
        from tempo_capture import FocusPause,utc_now
        self.focus_pause=FocusPause(60. if self.session.document['stimulated'] else None)
        self.focus_resume.clear();self.focus_paused=True;self.focus_timed_out=False
        event=dict(field_id=field_id,started_at=utc_now(),assessment=assessment,timed_out=False)
        self.session.document.setdefault('focus_events',[]).append(event)
        self.session._transition('focus_paused','Focus review required.')
        self.core.startContinuousSequenceAcquisition(0)
        self.focus_signal.emit(dict(field_id=field_id,timed_out=False,assessment=assessment))
        try:
            while not self.focus_resume.wait(.05):
                self._check()
                if self.focus_pause.expired and not self.focus_timed_out:
                    self._focus_timeout(event)
                    self.focus_signal.emit(dict(field_id=field_id,timed_out=True,assessment=assessment))
            # A resume after the deadline must not bypass timeout handling merely
            # because the GUI event arrived between two timer polls.
            if self.focus_pause.expired and not self.focus_timed_out:
                self._focus_timeout(event)
            self.core.stopSequenceAcquisition();self.core.clearCircularBuffer()
            event.update(resumed_at=utc_now(),elapsed_s=self.focus_pause.elapsed,
                         response_elapsed_s=self.focus_pause.resume_requested-self.focus_pause.started,
                         actor=self.resume_actor,override=True)
            if self.focus_timed_out:
                status=self.service.submit('pace').result(2)
                if status.mode!='PACING':raise RuntimeError('Pacing restart is unconfirmed.')
                self.session.restart_pacing(status)
                self.focus_timed_out=False
                self.imaging_ready.clear()
                began=time.monotonic()
                self.focus_paused=False
                self.session_signal.emit(self.session)
                while self.session.phase=='pre_pacing':
                    self._check()
                    elapsed=time.monotonic()-began
                    self.status_signal.emit(f'Pre-pacing after focus timeout · {elapsed:.0f} s · data may be affected')
                    if ((self.prepacing_seconds is not None and elapsed>=self.prepacing_seconds)
                            or self.imaging_ready.wait(.05)):
                        event['resume_prepacing_s']=elapsed
                        self.session.begin_imaging()
            else:
                self.session._transition('between_fields','Focus reviewed; pacing continued.')
            self.session._save()
        finally:
            try:
                if self.core.isSequenceRunning():self.core.stopSequenceAcquisition()
            except Exception:
                self.stop_confirmed=False
                raise
            self.focus_paused=False

    def run(self):
        summaries = []
        try:
            stage = self.core.getXYStageDevice() if self.core is not None else None
            if not stage:
                raise RuntimeError('Automatic collection requires a connected XY stage.')
            if self.target <= 0 or self.maximum <= 0 or self.duration <= 0:
                raise ValueError('Enter a cell target, field limit, and recording duration.')
            for plan_index, plan in enumerate(self.plans):
                self.protocol_flags=[] if plan_index or self.initial_session is None else list(self.initial_session.document.get('protocol_flags',[]))
                self._check()
                context = optical_context(self.core)
                if not self.calibration.matches(context['signature'], context['image_size']):
                    raise RuntimeError('Camera or optical configuration changed. Recalibrate the field size.')
                grid = search_grid(plan['corner_a'], plan['corner_b'], self.calibration, self.maximum)
                self._move(stage, grid[0])
                params = None
                status = None
                adopting = plan_index == 0 and self.initial_session is not None
                if self.parameters is not None:
                    params = StimulusParameters(**dict(asdict(self.parameters), channel=plan['channel']))
                    if not adopting:
                        self.service.submit('stop').result(2)
                        self.service.submit(params.command(plan.get('confirmed', False))).result(2)
                        self._check()
                        status = self.service.submit('pace').result(2)
                self.imaging_ready.clear()
                if adopting:
                    if self.session.document['well_id'] != plan['well']:
                        raise ValueError('The running stimulation well must be collected first.')
                    if self.session.document['stimulated'] and self.session.document['requested_parameters'] != asdict(params):
                        raise ValueError('Collection settings must match the running stimulation.')
                    self.session.calibration = self.calibration
                else:
                    self.session = CaptureSession(self.root, self.experiment, plan['well'], params,
                        calibration=self.calibration, channel=plan.get('channel') if params else None)
                    self.session.start(status)
                self.session_signal.emit(self.session)
                if params:
                    while self.session.phase == 'pre_pacing':
                        self._check()
                        elapsed = self.session.elapsed
                        self.status_signal.emit(f"{plan['well']} · Pre-pacing {elapsed:.0f} s")
                        if ((self.prepacing_seconds is not None and elapsed >= self.prepacing_seconds)
                                or self.imaging_ready.wait(.05)):
                            self.session.begin_imaging()
                field_offset = max((int(field['field_id'][3:]) for field in self.session.document['fields'] if re.fullmatch(r'FOV\d+',field['field_id'])), default=0)
                for index, position in enumerate(grid):
                    if self.session.document['healthy_cell_estimate'] >= self.target: break
                    self._check()
                    context = optical_context(self.core)
                    if not self.calibration.matches(context['signature'], context['image_size']):
                        raise RuntimeError('Optical configuration changed during collection. Recalibrate before restarting.')
                    if context['position'] != tuple(position):
                        position = self._move(stage, position)
                    else:
                        position = tuple(context['position'])
                    frame = fresh_frame(self.core)
                    counts = count_frame(frame, self.model, self.confidence)
                    self._check()
                    field_id = f'FOV{field_offset+index+1:03d}'
                    self.status_signal.emit(f"{plan['well']} · Field {index+1}/{len(grid)} · {self.session.document['healthy_cell_estimate']}/{self.target} healthy cells")
                    if counts['live_count'] < self.threshold:
                        self.session.document['fields'].append(dict(field_id=field_id, path=None, counts=counts,
                            position_um=list(position), status='skipped', credited_cells=0, duplicate=False))
                        self.session._save()
                        self.fields_signal.emit(copy.deepcopy(self.session.document))
                        continue
                    if self.recording_options is not None:
                        from tempo_quality import assess
                        assessment=assess(frame,context.get('quality_signature'),self.recording_options.get('focus_profile'),sensor_bits=context.get('sensor_bits'))
                        if assessment['focus']!='sharp':
                            self._review_focus(field_id,counts,assessment)
                            frame=fresh_frame(self.core)
                            context=optical_context(self.core)
                            if not self.calibration.matches(context['signature'],context['image_size']):
                                raise RuntimeError('Optical configuration changed during focus review. Recalibrate before restarting.')
                            counts=count_frame(frame,self.model,self.confidence)
                            assessment=assess(frame,context.get('quality_signature'),self.recording_options.get('focus_profile'),sensor_bits=context.get('sensor_bits'))
                            assessment['flags'].append('focus_override')
                            if counts['live_count'] < self.threshold:
                                self.session.document['fields'].append(dict(field_id=field_id,path=None,counts=counts,
                                    quality=assessment,position_um=list(position),status='skipped',credited_cells=0,
                                    duplicate=False,reason='Cell estimate below threshold after focus review.'))
                                self.session._save()
                                self.fields_signal.emit(copy.deepcopy(self.session.document))
                                continue
                        assessment['flags'].extend(flag for flag in self.protocol_flags if flag not in assessment['flags'])
                        counts['quality']=assessment
                    suffix = 'tif' if self.recording_options is not None else 'avi'
                    filename = f'Assay_{self.experiment}_{plan["well"]}_{field_id}_{self.fps:g}fps_{time.time_ns()}.{suffix}'
                    path = str(Path(self.root) / filename)
                    self.session.begin_field(path, field_id, counts, position, self.calibration.footprint)
                    options=copy.deepcopy(self.recording_options)
                    if options is not None:
                        options.setdefault('metadata',{}).update(optical_signature=context['signature'],
                            quality_signature=context.get('quality_signature'),protocol_flags=list(self.protocol_flags),quality_assessments=[counts['quality']])
                    native_options = dict(native=True,recording_options=options,duration_s=self.duration) if options is not None else {}
                    acq = self.acquisition_factory(core=self.core, output_path=path, target_fps=self.fps,
                                                  max_frames=0 if options is not None else max(1, int(self.duration * self.fps)),**native_options)
                    self.acquisition = acq
                    results, errors = [], []
                    acq.finished_signal.connect(lambda p, n, f: results.append((p, n)), Qt.ConnectionType.DirectConnection)
                    acq.error_signal.connect(errors.append, Qt.ConnectionType.DirectConnection)
                    self._check()
                    acq.run()
                    self.stop_confirmed = getattr(acq,'stop_confirmed',True)
                    self.acquisition = None
                    if errors or not results:
                        error = errors[0] if errors else 'Recording ended without a result.'
                        self.session.complete_field(0, error)
                        raise RuntimeError(error)
                    self.session.complete_field(results[0][1], cancelled=self.cancelled.is_set())
                    self._check()
                    self.fields_signal.emit(copy.deepcopy(self.session.document))
                    if self.session.document['healthy_cell_estimate'] >= self.target:
                        break
                total = self.session.document['healthy_cell_estimate']
                reason = ('Cell target reached.' if total >= self.target else
                          f'Shortfall: {total}/{self.target} healthy cells; search area or field limit reached.')
                self.session.finish(self._stop_output(), reason)
                self._enqueue_completed()
                summaries.append(plan['well'] + ': ' + reason)
                self.fields_signal.emit(copy.deepcopy(self.session.document))
            self.result_signal.emit('\n'.join(summaries))
        except Exception as exc:
            message = str(exc)
            try:
                status = self._stop_output()
            except Exception as stop_error:
                status = None
                message += ' Output is unconfirmed: ' + str(stop_error)
            if self.session is not None:
                try:
                    self.session.finish(status, message, interrupted=True)
                    self._enqueue_completed()
                    self.fields_signal.emit(copy.deepcopy(self.session.document))
                except Exception as save_error:
                    message += ' Session details could not be saved: ' + str(save_error)
            if summaries: self.result_signal.emit('\n'.join(summaries))
            self.error_signal.emit(message)

    def _enqueue_completed(self):
        for field in self.session.document['fields']:
            if field['status'] == 'completed' and field.get('path'):
                self.enqueue(field['path'])


class CaptureSetupDialog(QDialog):
    """Infrequent setup is kept off the routine acquisition panels."""
    def __init__(self, tools):
        super().__init__(tools)
        self.tools = tools
        self.setWindowTitle('TEMPO · ' + ('Output Assignments' if section == 'outputs' else 'Field Calibration'))
        self.resize(510, 460)
        outer = QVBoxLayout(self)
        scroll = QScrollArea(); scroll.setWidgetResizable(True)
        body = QWidget(); layout = QVBoxLayout(body)
        self.multiplexer = QCheckBox('Eight-channel multiplexer fitted')
        self.multiplexer.setChecked(tools.profile.get('multiplexer', False))
        layout.addWidget(self.multiplexer)
        self.multiplexer.setVisible(section == 'outputs')
        mapping = QFormLayout(); mapping.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapLongRows)
        self.channels = []
        for channel in range(8):
            combo = QComboBox(); combo.addItem('Unassigned', None)
            for index in range(tools.capture.well_id.count()):
                well = tools.capture.well_id.itemText(index); combo.addItem(well, well)
            assigned = tools.profile.get('channels', {}).get(str(channel))
            combo.setCurrentIndex(max(0, combo.findData(assigned)))
            mapping.addRow(f'Output {channel + 1}', combo); self.channels.append(combo)
        mapping_widget = QWidget(); mapping_widget.setLayout(mapping)
        mapping_widget.setVisible(section == 'outputs'); layout.addWidget(mapping_widget)
        self.multiplexer.toggled.connect(self.update_channels); self.update_channels()
        optics = QGroupBox('Field Calibration'); form = QFormLayout(optics)
        form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapLongRows)
        self.pixel_size = QDoubleSpinBox(); self.pixel_size.setDecimals(5); self.pixel_size.setRange(0, 1000); self.pixel_size.setSuffix(' µm/px')
        self.angle = QDoubleSpinBox(); self.angle.setRange(-180,180); self.angle.setSuffix('°')
        self.flip_x = QCheckBox('Reverse camera X'); self.flip_y = QCheckBox('Reverse camera Y')
        values = tools.profile.get('optics', {})
        self.pixel_size.setValue(values.get('pixel_size_um', 0)); self.angle.setValue(values.get('angle_deg', 0))
        self.flip_x.setChecked(values.get('flip_x', False)); self.flip_y.setChecked(values.get('flip_y', False))
        form.addRow('Calibrated pixel size', self.pixel_size); form.addRow('Camera-to-stage rotation', self.angle)
        form.addRow(self.flip_x); form.addRow(self.flip_y)
        form.addRow(InformationButton('Calibration', 'Use a measured pixel size and camera-to-stage orientation. Rotation is counterclockwise from camera X to positive stage X; reverse axes where needed. Mark a rectangle fully inside each well. Changing the objective, ROI, binning, camera, or pixel-size configuration invalidates calibration.'))
        layout.addWidget(optics)
        optics.setVisible(section == 'calibration')
        self.error = QLabel(); self.error.setWordWrap(True); layout.addWidget(self.error)
        scroll.setWidget(body); outer.addWidget(scroll)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.save); buttons.rejected.connect(self.reject); outer.addWidget(buttons)

    def update_channels(self):
        for index, combo in enumerate(self.channels):
            combo.setEnabled(index == 0 or self.multiplexer.isChecked())

    def save(self):
        try:
            mapping = {str(i): combo.currentData() for i,combo in enumerate(self.channels)
                       if combo.currentData() is not None and (i == 0 or self.multiplexer.isChecked())}
            if len(set(mapping.values())) != len(mapping):
                raise ValueError('Assign each well to only one channel.')
            values = dict(pixel_size_um=self.pixel_size.value(), angle_deg=self.angle.value(),
                          flip_x=self.flip_x.isChecked(), flip_y=self.flip_y.isChecked())
            profile = copy.deepcopy(self.tools.profile)
            profile.update(multiplexer=self.multiplexer.isChecked(), channels=mapping, optics=values)
            if values != profile.get('calibrated_optics'):
                profile.pop('calibration', None)
            # Persistence is completed asynchronously by the tools after this dialog closes.
            self.tools.profile = profile
            self.accept()
        except ValueError as exc:
            self.error.setText(str(exc))


class CaptureTools(QWidget):
    def __init__(self, capture):
        super().__init__(capture)
        self.capture = capture
        self.service = None
        self.session = None
        self._task = None
        self._task_pending = False
        self._pending = []
        self._record_after_prepacing = False
        self._record_context = None
        self._finish_pending = False
        self._finish_reason = ('Well imaging finished.', False)
        self._session_cancel = threading.Event()
        self._shutdown_requested = False
        self._last_stage_probe = 0
        self._last_service_error = ''
        self._stage_present = False
        self._collection_stopping = False
        self._last_collection_result = ''
        self._output_requested = False
        self._starting_output = False
        self._count_task = None
        self._quality_task=None
        self._last_quality_check=0.
        self._latest_quality_at=''
        self._last_background_count = 0.
        self._count_generation = 0
        self._focus_resume_pending=False
        self._focus_flash=False
        self.focus_warning_timer=QTimer(self)
        self.focus_warning_timer.setInterval(500)
        self.focus_warning_timer.timeout.connect(self.update_focus_warning)
        self.profile = dict(channels={}, areas={})
        self.profile_path = Path(get_config_dir()) / 'capture_setup.json'
        self._profile_loaded = False
        self._firmware_dialog = None
        self._build()
        self.poll_timer = QTimer(self); self.poll_timer.setInterval(100); self.poll_timer.timeout.connect(self.poll)
        # Start only when displayed; hidden test fixtures must not spawn work.

    def _build(self):
        layout = QVBoxLayout(self); layout.setContentsMargins(0,0,0,0)
        stimulation = QWidget(); box = QVBoxLayout(stimulation)
        box.setContentsMargins(8,0,8,0)
        self.connection_group = QGroupBox('Stimulator Connection')
        connection_box = QVBoxLayout(self.connection_group)
        heading = QHBoxLayout()
        self.off_label = QLabel('Stimulation off')
        self.on_label = QLabel('Stimulation on')
        for label in (self.off_label,self.on_label): label.setMinimumWidth(label.fontMetrics().horizontalAdvance(label.text())+4)
        self.enabled = OnOffSwitch(''); self.enabled.setAccessibleName('Stimulation')
        self.enabled.setSizePolicy(QSizePolicy.Policy.Fixed,QSizePolicy.Policy.Fixed)
        heading.setSpacing(6)
        heading.addStretch(); heading.addWidget(self.off_label); heading.addWidget(self.enabled); heading.addWidget(self.on_label); heading.addStretch()
        self.enabled.clicked.connect(self.request_output)
        self.information = InformationButton('Stimulation',
            'Stop Recording keeps stimulation running. Switching stimulation off ends this well. Requested voltage is not an electrode measurement; device confirmation thresholds are not biological safety guarantees. Stimulation Guide explains the workflow and recorded metadata.')
        box.addLayout(heading)
        self.port = QComboBox(); self.port.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon); self.port.setMinimumContentsLength(12)
        self.refresh_ports_button = QPushButton('Refresh'); self.refresh_ports_button.clicked.connect(self.refresh_ports)
        row = QHBoxLayout(); row.addWidget(self.port, 1); row.addWidget(self.refresh_ports_button); connection_box.addLayout(row)
        self.connect_button = QPushButton('Connect'); self.connect_button.clicked.connect(self.connect_device)
        connection_actions = FlowRow(); connection_actions.flow.addWidget(self.connect_button)
        connection_box.addWidget(connection_actions)
        self.amplitude = QDoubleSpinBox(); self.amplitude.setRange(0,1); self.amplitude.setDecimals(2); self.amplitude.setSingleStep(.05); self.amplitude.setSuffix(' V')
        self.frequency = QDoubleSpinBox(); self.frequency.setRange(.1,100); self.frequency.setDecimals(1); self.frequency.setSingleStep(.1); self.frequency.setSuffix(' Hz')
        self.width = QDoubleSpinBox(); self.width.setRange(.1,9999); self.width.setDecimals(1); self.width.setSingleStep(.1); self.width.setSuffix(' ms')
        self.amplitude.setValue(float(tempo_preferences.get('pacing','amplitude_v',.6)))
        self.frequency.setValue(float(tempo_preferences.get('pacing','pacing_frequency_hz',1)))
        self.width.setValue(float(tempo_preferences.get('pacing','pulse_width_ms',2)))
        self.values_widget = QWidget()
        self.values_widget.setSizePolicy(QSizePolicy.Policy.Ignored,QSizePolicy.Policy.Preferred)
        self.values_widget.installEventFilter(self)
        self.parameter_grid = QGridLayout(self.values_widget); self.parameter_grid.setContentsMargins(0,4,0,4)
        self.parameter_labels = []
        for label, control in (('Voltage',self.amplitude),('Frequency',self.frequency),('Pulse width',self.width)):
            self.parameter_labels.append(QLabel(label))
            value_font = QFont(self.font()); value_font.setPointSizeF(self.font().pointSizeF()*19/16)
            control.setFont(value_font)
            control.setStyleSheet('QDoubleSpinBox { color: #d2f0e6; padding: 4px; } QDoubleSpinBox:disabled { color: #d2f0e6; }')
            control.setSizePolicy(QSizePolicy.Policy.Ignored,QSizePolicy.Policy.Fixed)
            control.setMinimumWidth(control.fontMetrics().horizontalAdvance(control.text())+30)
            control.setAccessibleName(label)
        self._parameter_columns = None
        box.addWidget(self.values_widget)
        self.channel_label = QLabel('Single output'); self.channel_label.setWordWrap(True)
        output_row = QHBoxLayout(); output_row.addWidget(self.channel_label,1); output_row.addWidget(self.information)
        box.addLayout(output_row)
        self.pre_mode = SlidingChoice(('Manual','Timed'),('manual','timed'))
        self.pre_mode.setAccessibleName('Pre-pacing mode')
        self.pre_duration = QDoubleSpinBox(); self.pre_duration.setRange(0,86400); self.pre_duration.setSuffix(' s'); self.pre_duration.setSpecialValueText('Enter duration')
        self.pre_duration.setAccessibleName('Pre-pacing duration')
        self.pre_duration.setVisible(False); self.pre_mode.currentIndexChanged.connect(self.refresh_controls)
        pre_heading = QLabel('Pre-pacing'); box.addWidget(pre_heading)
        pre_controls = QVBoxLayout(); pre_controls.addWidget(self.pre_mode); pre_controls.addWidget(self.pre_duration)
        box.addLayout(pre_controls)
        from tempo_stimulation_scope import ScopePreview
        self.scope_preview = ScopePreview(self)
        box.addWidget(self.scope_preview)
        box.addStretch(1)
        for control in (self.amplitude,self.frequency,self.width):
            control.valueChanged.connect(self.update_scope)
            control.valueChanged.connect(self.arrange_parameters)
        self.setup_button = QPushButton('Setup…')
        menu = QMenu(self.setup_button)
        self.output_setup_action = menu.addAction('Output assignments…', lambda: self.open_setup('outputs'))
        self.firmware_action = menu.addAction('Install / update firmware…', self.open_firmware)
        self.diagnostics_action = menu.addAction('Advanced diagnostics…', self.diagnostics)
        menu.addAction('Stimulation guide…', self.stimulation_help)
        self.setup_button.setMenu(menu)
        # One setup menu replaces unrelated standalone buttons in the camera panel.
        connection_actions.flow.addWidget(self.setup_button)
        layout.addWidget(self.connection_group)
        self.status = QLabel('Output off.'); self.status.setWordWrap(True)
        box.insertWidget(1, self.status)
        layout.addWidget(stimulation,1)
        self.error = ErrorDetails(); layout.addWidget(self.error)
        field_group = self.field_group = QGroupBox('Fields'); fields = QVBoxLayout(field_group)
        self.counts_label = QLabel('Cell estimate unavailable.'); self.counts_label.setWordWrap(True); fields.addWidget(self.counts_label)
        self.focus_label = QLabel('Focus: Unavailable · Beta')
        self.focus_label.setWordWrap(True);fields.addWidget(self.focus_label)
        self.focus_warning=QLabel();self.focus_warning.setWordWrap(True)
        self.focus_warning.hide();fields.addWidget(self.focus_warning)
        self.focus_profile = None
        self.quality_signature = None
        self.quality_sensor_bits = None
        profile_path = Path(get_config_dir())/'focus_profile.json'
        if profile_path.is_file():
            try:
                self.focus_profile = json.loads(profile_path.read_text())
            except (OSError,ValueError):
                self.focus_label.setText('Focus: Unavailable · Beta — profile could not be read')
        count_row = FlowRow()
        self.count_button = QPushButton('Count Field'); self.count_button.clicked.connect(self.count_current)
        count_row.flow.addWidget(self.count_button)
        self.count_info = InformationButton('Field counts', 'Only individually detected healthy cells count toward the target. Groups and dead cells are reported separately. Counts are model estimates. Repeated field identifiers and calibrated overlapping fields are not credited again; without stage coordinates, assign a new field identifier only after moving to a different field.')
        count_row.flow.addWidget(self.count_info); fields.addWidget(count_row)
        self.collection_results_button = QPushButton('Collection Results…')
        self.collection_results_button.clicked.connect(self.show_collection_results)
        self.collection_results_button.hide(); fields.addWidget(self.collection_results_button)
        self.history = QListWidget(); self.history.setWordWrap(True); self.history.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.history.setSizePolicy(QSizePolicy.Policy.Expanding,QSizePolicy.Policy.Fixed)
        fields.addWidget(self.history)
        self.history_summary = QLabel('No fields recorded.'); fields.addWidget(self.history_summary)
        self.history_button = QPushButton('History…'); self.history_button.clicked.connect(self.show_history)
        count_row.flow.addWidget(self.history_button); self.history_button.setEnabled(False)
        self.automatic_group = QGroupBox('Automatic Collection'); compact_auto = QVBoxLayout(self.automatic_group)
        self.automatic_dialog = QDialog(self)
        self.automatic_dialog.setWindowTitle('TEMPO · Automatic Collection')
        auto = QVBoxLayout(self.automatic_dialog)
        self.area_label = QLabel('Mark the usable area for this well.'); self.area_label.setWordWrap(True); auto.addWidget(self.area_label)
        corners = FlowRow()
        self.corner_a = QPushButton('Mark First Corner'); self.corner_b = QPushButton('Mark Opposite Corner')
        self.corner_a.clicked.connect(lambda: self.mark_corner('corner_a')); self.corner_b.clicked.connect(lambda: self.mark_corner('corner_b'))
        corners.flow.addWidget(self.corner_a); corners.flow.addWidget(self.corner_b); auto.addWidget(corners)
        limits = QFormLayout(); limits.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapLongRows)
        self.target = QSpinBox(); self.target.setRange(0,100000); self.target.setSpecialValueText('Enter target')
        self.maximum = QSpinBox(); self.maximum.setRange(0,10000); self.maximum.setSpecialValueText('Enter limit')
        self.threshold = QSpinBox(); self.threshold.setRange(1,1000); self.threshold.setValue(int(tempo_preferences.get('scout','live_threshold',3)))
        self.duration = QDoubleSpinBox(); self.duration.setRange(.1,3600); self.duration.setValue(float(tempo_preferences.get('scout','scout_duration_sec',15))); self.duration.setSuffix(' s/field')
        self.scope = QComboBox(); self.scope.addItem('Current well','current'); self.scope.addItem('All eligible wells','all')
        for label, value in [('Row A','A'),('Rows A–B','AB'),('Rows A–D','ABCD')]: self.scope.addItem(label, value)
        saved_range = tempo_preferences.get('scout','default_traversal','All 96 Wells (Serpentine)')
        self.scope.setCurrentIndex(self.scope.findData({'Row A Only (12 wells)':'A','Rows A-B (24 wells)':'AB','Rows A-D (48 wells)':'ABCD'}.get(saved_range,'current')))
        for label,control in (('Cell target',self.target),('Maximum fields',self.maximum),('Minimum cells/field',self.threshold),('Record time',self.duration),('Wells',self.scope)):
            limits.addRow(label,control)
        auto.addLayout(limits)
        self.calibration_button = QPushButton('Field Calibration…'); self.calibration_button.clicked.connect(lambda: self.open_setup('calibration'))
        auto.addWidget(self.calibration_button)
        self.auto_button = QPushButton('Automatic Collection…')
        self.auto_button.clicked.connect(self.open_automatic)
        compact_auto.addWidget(self.auto_button)
        self.auto_start_button = QPushButton('Start Collection'); self.auto_start_button.clicked.connect(self.start_automatic)
        auto.addWidget(self.auto_start_button)
        self.automatic_group.hide()
        self.stop_button = QPushButton('Stop Stimulation'); self.stop_button.setProperty('danger',True)
        self.stop_button.clicked.connect(self.emergency_stop); layout.addWidget(self.stop_button)
        self.capture.well_id.currentTextChanged.connect(self.selection_changed)
        self.capture.fov_id.valueChanged.connect(self.field_changed)
        self.refresh_controls()
        self.arrange_parameters()

    def arrange_parameters(self, *args):
        controls = (self.amplitude,self.frequency,self.width)
        for control in controls:
            control.setMinimumWidth(control.fontMetrics().horizontalAdvance(control.text())+30)
        required = [max(label.sizeHint().width(),control.minimumWidth()) for label,control in zip(self.parameter_labels,controls)]
        available = self.values_widget.width()
        spacing = self.parameter_grid.horizontalSpacing()
        columns = 3 if sum(required)+2*spacing <= available else (2 if 2*max(required)+spacing <= available else 1)
        if columns == self._parameter_columns: return
        self._parameter_columns = columns
        while self.parameter_grid.count(): self.parameter_grid.takeAt(0)
        for column in range(3): self.parameter_grid.setColumnStretch(column,0)
        for index,(label,control) in enumerate(zip(self.parameter_labels,controls)):
            row,column = divmod(index,columns)
            self.parameter_grid.addWidget(label,2*row,column)
            self.parameter_grid.addWidget(control,2*row+1,column)
            self.parameter_grid.setColumnStretch(column,1)

    def minimum_stimulation_width(self):
        heading = self.off_label.minimumWidth()+self.enabled.sizeHint().width()+self.on_label.minimumWidth()+50
        return max(300,heading)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self.arrange_parameters()

    def eventFilter(self, source, event):
        if source is self.values_widget and event.type() == QEvent.Type.Resize and event.size().width() != event.oldSize().width():
            QTimer.singleShot(0,self.arrange_parameters)
        return super().eventFilter(source,event)

    def request_output(self, checked):
        if self._starting_output or self._finish_pending:
            self._output_requested = False
            self.finish_well(interrupted=True,reason='Stimulation start cancelled.')
            return
        if checked:
            self._output_requested = True
            self.start_well(stimulate=True)
            if not self._starting_output:
                self._output_requested = bool(self.session and self.session.active and self.session.document['stimulated'])
            self.refresh_controls()
        else:
            self._output_requested = False
            self.finish_well(interrupted=self.capture.is_recording, reason='Stimulation switched off.')

    def field_changed(self, *args):
        self._count_generation += 1
        self.counts_label.setText('Cell estimate unavailable.')
        self._latest_quality_at=''
        self.focus_label.setText('Focus: Unavailable · Beta')

    def capture_failure(self, message):
        if self._starting_output or (self.session and self.session.active):
            self.finish_well(interrupted=True,reason=message)

    def stimulation_help(self):
        self.show_guide('Stimulation Guide', '''<h2>Capture while pacing one well</h2>
<p>Select the experiment, well, and field in the Camera panel. With a multiplexer, assign that well to exactly one output in Setup → Output assignments. These assignments describe wiring, not microscope positions.</p>
<h3>Pre-pacing</h3><p>Set voltage, frequency, and pulse width before switching Stimulation on. The defaults for new profiles are 0.6 V, 1 Hz, and 2 ms. Timed mode requires a duration: Record can queue the first field until that duration has elapsed. Manual mode keeps pacing until you choose Begin Imaging in the Camera panel. That action starts the first recording.</p>
<h3>More than one field</h3><p>Stop Recording ends only the current field; stimulation continues. Move to another field and assign its field identifier before recording again. Settings remain fixed until stimulation is switched off. Switching off between recordings completes the well. Switching off during recording interrupts that field. Stop Stimulation interrupts the workflow and prioritizes stopping output.</p>
<h3>Counting and automatic collection</h3><p>Healthy individual-cell estimates contribute to the target only after a recording succeeds. Dead cells and groups remain visible but do not independently qualify a field. Repeated identifiers and calibrated overlaps do not add credit. Without stage coordinates, field identities are operator assigned and counts remain estimates. Recording History shows the full field list and any failed or excluded fields.</p>
<p>Automatic Collection requires a connected XY stage, a detector, calibrated field scale and orientation, and two marked corners inside each selected well. Set the healthy-cell target, maximum inspected fields, minimum cells per field, and recording duration. If the search ends below target, completed recordings are retained and the shortfall is reported.</p>
<h3>Output state and saved data</h3><p>Starting and Stopping mean TEMPO is waiting for the controller. Output unconfirmed must not be read as output off. Loss of valid communication triggers the firmware watchdog; reconnecting never restarts stimulation automatically. Requested and acknowledged settings, counts, field positions when available, and interruption status are saved beside recordings.</p>
<p>The DAC request is not a measured electrode voltage. Device confirmation thresholds are not verified biological safety limits. Host timestamps do not measure pulse-to-frame synchronization. Use the oscilloscope guide for interpreting the expected-signal drawing and setting up a physical instrument.</p>''')

    def show_guide(self, title, html):
        dialog = QDialog(self.capture); dialog.setWindowTitle('TEMPO · ' + title)
        layout = QVBoxLayout(dialog); text = QTextBrowser(); text.setHtml(html); layout.addWidget(text)
        QShortcut(QKeySequence.StandardKey.Close, dialog, activated=dialog.close)
        dialog.resize(600,560); dialog.show(); self._guide_dialog = dialog

    def show_history(self):
        dialog = QDialog(self.capture); dialog.setWindowTitle('TEMPO · Recording History')
        layout = QVBoxLayout(dialog); history = QListWidget(); history.setWordWrap(True)
        for row in range(self.history.count()): history.addItem(self.history.item(row).clone())
        layout.addWidget(history)
        QShortcut(QKeySequence.StandardKey.Close,dialog,activated=dialog.close)
        dialog.resize(580,420); dialog.show(); self._history_dialog = dialog

    def busy(self):
        return self._task_pending

    def update_scope(self):
        mode = 'pace'
        params = None
        if self.service and self.service.connected and self.service.status:
            status = self.service.status
            mode = {'CONSTANT_STIM':'constant','SQUARE_10HZ':'square','DISCONNECTED_HIZ':'isolated'}.get(status.mode,'cycle' if status.cycling else 'pace')
            if status.mode != 'REST_DISCHARGE': params = status.parameters
        self.scope_preview.set_signal(params.amplitude_v if params else self.amplitude.value(),
            params.frequency_hz if params else self.frequency.value(),params.pulse_width_ms if params else self.width.value(),mode)

    def firmware_busy(self):
        dialog = self._firmware_dialog
        return dialog is not None and dialog.worker is not None and not dialog.worker.wait(0)

    def open_firmware(self):
        if self.busy() or self.camera_busy() or (self.session and self.session.active): return
        if self._firmware_dialog and self._firmware_dialog.isVisible():
            self._firmware_dialog.raise_(); return
        def show():
            self._firmware_dialog = FirmwareDialog(self)
            self._firmware_dialog.show()
        if not self.port.count():
            from tempo_stimulation import serial_ports
            def discovered(ports):
                for device,label in ports: self.port.addItem(label,device)
                show()
            self._async(serial_ports,discovered)
        else:
            show()

    def automatic_running(self):
        worker = getattr(self.capture,'scout_worker',None)
        return worker is not None and (self.capture._pending_camera_worker is worker or
            (not worker.wait(0) if isinstance(worker,QThread) else worker.isRunning()))

    def camera_busy(self):
        worker = getattr(self.capture,'acq_worker',None)
        collection=getattr(self.capture,'scout_worker',None)
        return self.capture.is_recording or (worker is not None and
            (not worker.wait(0) if isinstance(worker,QThread) else worker.isRunning())) or (
                self.automatic_running() and getattr(collection,'focus_paused',False) is not True)

    def parameters(self, well=None):
        channel = 0
        if self.profile.get('multiplexer'):
            well = well or self.capture.well_id.currentText()
            channels = [int(ch) for ch,name in self.profile.get('channels',{}).items() if name == well]
            if len(channels) != 1:
                raise ValueError('Assign this well to an output channel in Channels / Fields.')
            channel = channels[0]
        return StimulusParameters(self.amplitude.value(),self.frequency.value(),self.width.value(),channel)

    def _confirm(self, parameters):
        return not parameters.needs_confirmation or confirm_action(self,'Confirm Stimulation Settings',
            f'{parameters.amplitude_v:g} V, {parameters.frequency_hz:g} Hz, {parameters.pulse_width_ms:g} ms exceeds the V2 device confirmation thresholds. Apply these values for this well?', 'Apply Values')

    def _async(self, action, done=None, camera=False):
        if self.busy():
            raise RuntimeError('Wait for the current capture action to finish.')
        if camera:
            self.capture.pause_camera_controls()
            self.capture.btn_record.setEnabled(False)
        previous = self.capture._preview_task.done_event if camera and self.capture._preview_task is not None else None
        def work():
            if previous is not None:
                previous.wait()
            return action()
        self._task = TaskThread(work,self)
        self._task_pending = True
        result = []
        self._task.completed.connect(lambda value,error: result.append((value,error)))
        task = self._task
        def completed():
            if not task.wait(0):
                QTimer.singleShot(10,completed)
                return
            value,error = result[0]
            try:
                if isinstance(error,CameraStopUnconfirmed):
                    self.capture.mark_camera_stop_unconfirmed()
                    self.capture._preview_was_active = False
                if camera and not self._shutdown_requested:
                    self.capture.resume_camera_controls()
                if error:
                    if isinstance(error,CameraStopUnconfirmed):
                        self.capture.camera_error.set_error(str(error),error.__cause__ or error)
                    else:
                        self.show_error(error)
                elif done:
                    done(value)
            except Exception as exc:
                self.show_error(exc)
            finally:
                if self._task is task:
                    self._task = None
                self._task_pending = False
                task.deleteLater()
                self.refresh_controls()
        self._task.finished.connect(completed)
        self._task.start()
        self.refresh_controls()

    def show_error(self, error):
        if self._starting_output:
            self._starting_output = False
            self._output_requested = False
        self.error.set_error(str(error),error)

    def refresh_ports(self):
        if self.busy(): return
        def discover():
            from tempo_stimulation import serial_ports
            return serial_ports()
        def apply(ports):
            self.port.clear()
            for device,label in ports: self.port.addItem(label,device)
            if not ports: self.status.setText('No serial ports found. Connect the stimulator and refresh.')
        self._async(discover,apply)

    def connect_device(self):
        if self.service is not None and self.service.running:
            if self.session and self.session.active:
                self.finish_well()
                return
            self._pending.append((self.service.close(),lambda _: self.status.setText('Stimulator disconnected; resting discharge confirmed.')))
            return
        if self.port.currentData() is None:
            self.refresh_ports(); return
        self.error.clear()
        self.service = PacerService()
        self.status.setText('Connecting stimulator…')
        self._pending.append((self.service.connect(self.port.currentData()),lambda _: self.status.setText('Resting discharge.')))
        self.refresh_controls()

    def _directory(self):
        import tempo_backend
        experiment = self.capture.exp_id.text().strip() or 'Assay001'
        if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]*',experiment):
            raise ValueError('Use letters, numbers, dots, underscores, or hyphens for the experiment ID.')
        return experiment,str(Path(tempo_backend.RAW_DIR)/experiment)

    def start_well(self, record_after=False, stimulate=None):
        try:
            if self.busy() or self.camera_busy(): return
            use_stimulation = self.enabled.isChecked() if stimulate is None else stimulate
            previous_session = None
            if self.session and self.session.active:
                if use_stimulation and not self.session.document['stimulated']:
                    previous_session = self.session
                else:
                    if record_after: self.request_record()
                    return
            params = self.parameters() if use_stimulation else None
            if use_stimulation and (self.service is None or not self.service.connected):
                raise ValueError('Connect the stimulator before starting this well.')
            if use_stimulation and self.pre_mode.currentData() == 'timed' and self.pre_duration.value() <= 0:
                raise ValueError('Enter a pre-pacing duration greater than zero.')
            if params and not self._confirm(params): return
            self._output_requested = use_stimulation
            self._starting_output = use_stimulation
            experiment,directory = self._directory()
            well = self.capture.well_id.currentText()
            self._record_after_prepacing = record_after
            cancelled = self._session_cancel = threading.Event()
            self.error.clear()
            def work():
                status = None
                if previous_session: previous_session.finish(None,'Stimulation started for this well.')
                session = CaptureSession(directory,experiment,well,params,channel=params.channel if params else None)
                try:
                    if params:
                        self.service.submit('stop').result(2)
                        if cancelled.is_set(): raise InterruptedError('Well start cancelled.')
                        self.service.submit(params.command(params.needs_confirmation)).result(2)
                        if cancelled.is_set(): raise InterruptedError('Well start cancelled.')
                        status = self.service.submit('pace').result(2)
                        if cancelled.is_set(): raise InterruptedError('Well start cancelled.')
                    session.start(status)
                    return session
                except Exception:
                    if params: self.service.submit('stop')
                    raise
            def started(session):
                self._starting_output = False
                self.session = session
                self._last_collection_result = ''
                self.collection_results_button.hide()
                self.history.clear()
                self.refresh_controls()
                if not use_stimulation and record_after: QTimer.singleShot(0,self.request_record)
            self._async(work,started)
        except Exception as exc: self.show_error(exc)

    def begin_imaging(self):
        if self.automatic_running():
            self.capture.scout_worker.imaging_ready.set(); return
        if self.session is None or self.session.phase != 'pre_pacing' or self.busy(): return
        def ready(_):
            self.refresh_controls()
            if self._record_after_prepacing:
                self._record_after_prepacing = False
                QTimer.singleShot(0,self.request_record)
        self._async(self.session.begin_imaging,ready)

    def request_record(self):
        try:
            if self.automatic_running():
                worker=getattr(self.capture,'scout_worker',None)
                if getattr(worker,'focus_paused',False) is True:
                    self.resume_focus();return
                if self.session and self.session.phase == 'pre_pacing' and self.pre_mode.currentData() == 'manual': self.begin_imaging()
                return
            if self.diagnostics_open():
                raise ValueError('Close diagnostics before starting a capture session.')
            if self.busy() or self.camera_busy(): return
            if self.session is None or not self.session.active:
                self.start_well(True); return
            if self.session.phase == 'pre_pacing':
                if self.pre_mode.currentData() == 'manual':
                    self._record_after_prepacing = True
                    self.begin_imaging()
                    return
                self._record_after_prepacing = not self._record_after_prepacing
                self.refresh_controls()
                return
            if self.session.phase not in ('ready','between_fields'):
                raise ValueError('This well is not ready to record.')
            if self.session.document['stimulated'] and (self.service is None or not self.service.connected or
                    self.service.status is None or self.service.status.mode != 'PACING'):
                raise RuntimeError('Pacing is unconfirmed. Stop and restart the well before recording.')
            core,cap = self.capture.core,self.capture.cv_cap
            signature = copy.deepcopy(self.profile.get('calibration'))
            confidence = float(tempo_preferences.get('annotator','confidence_threshold',.15))
            try: model = local_scout_path()
            except RuntimeError: model = None
            def prepare():
                context = optical_context(core,cap)
                frame = fresh_frame(core,cap)
                context['counts'] = None
                try:
                    if model: context['counts'] = count_frame(frame,model,confidence)
                except Exception as exc:
                    context['counting_error'] = str(exc)
                context['calibration'] = None
                if signature:
                    calibration = FieldCalibration(tuple(map(tuple,signature['matrix'])),tuple(signature['image_size']),signature['signature'])
                    if calibration.matches(context['signature'],context['image_size']): context['calibration'] = calibration
                return context
            def ready(context):
                if not self.session.active or self._finish_pending or self._shutdown_requested: return
                if self.session.document['stimulated'] and (not self.service.connected or
                        self.service.status is None or self.service.status.mode != 'PACING'):
                    self.finish_well(interrupted=True,reason='Pacing was lost before recording.')
                    return
                self._record_context = context
                self._show_counts(context['counts'])
                if context.get('counting_error'):
                    self.show_error(RuntimeError('Recording without a cell estimate: ' + context['counting_error']))
                self.capture._toggle_recording()
            self._async(prepare,ready,camera=True)
        except Exception as exc: self.show_error(exc)

    def recording_hooks(self):
        session = self.session
        context = self._record_context
        if session is None or context is None: return None
        field_id = f'FOV{self.capture.fov_id.value():03d}'
        def started(path):
            session.calibration = context['calibration']
            session.begin_field(path,field_id,context['counts'],context['position'],
                context['calibration'].footprint if context['calibration'] is not None else None)
        def completed(frames,error):
            session.complete_field(frames,error,cancelled=self._finish_reason[1] or self._shutdown_requested)
        return started,completed

    def recording_finished(self, error=''):
        self._record_context = None
        if self.session:
            self.show_fields(self.session.document)
        if error: self.finish_well(interrupted=True,reason=error)
        elif self._finish_pending: self.finish_well()

    def finish_well(self, checked=False, interrupted=False, reason='Well imaging finished.'):
        self._output_requested = False
        self._record_after_prepacing = False
        self._session_cancel.set()
        if self.automatic_running():
            self.stop_automatic(); return
        self._finish_pending = True
        if interrupted or not self._finish_reason[1]:
            self._finish_reason = (reason,interrupted)
        reason,interrupted = self._finish_reason
        stop = self.service.submit('stop') if self.service and self.service.connected else None
        if self.capture.is_recording:
            if self.capture.acq_worker: self.capture.acq_worker.stop()
            return
        if self.busy(): return
        session = self.session
        def finish():
            try:
                status = stop.result(2) if stop else None
            except Exception:
                if session and session.active: session.finish(None,reason,interrupted=True)
                raise
            if session and session.active: session.finish(status,reason,interrupted=interrupted)
            return status
        def done(_):
            self._starting_output = False
            self._finish_pending = False
            self._finish_reason = ('Well imaging finished.',False)
            if session: self.show_fields(session.document)
            self.refresh_controls()
        try: self._async(finish,done)
        except Exception as exc: self.show_error(exc)

    def emergency_stop(self):
        self._output_requested = False
        self._record_after_prepacing = False
        if self.service and self.service.connected:
            self._pending.append((self.service.submit('stop'),lambda _: None))
        if self.automatic_running(): self.stop_automatic()
        if self.capture.acq_worker and self.capture.is_recording: self.capture.acq_worker.stop()
        self.finish_well(interrupted=True,reason='Stop Stimulation requested.')

    def count_current(self):
        try:
            if self.camera_busy() or self.busy(): return
            try: model = local_scout_path()
            except RuntimeError: model = None
            core,cap = self.capture.core,self.capture.cv_cap
            confidence = float(tempo_preferences.get('annotator','confidence_threshold',.15))
            def inspect():
                frame=fresh_frame(core,cap)
                context=optical_context(core,cap)
                return check_field(frame,model,confidence,context.get('quality_signature'),self.focus_profile,context.get('sensor_bits'))
            self._async(inspect,self._show_field_check,camera=True)
        except Exception as exc: self.show_error(exc)

    def preview_for_counting(self, frame):
        """At most one inference on already acquired preview data; never own the camera."""
        if self._shutdown_requested or self._focus_resume_pending or self.camera_busy() or self.busy():
            return
        self.preview_for_quality(frame)
        if self._count_task is not None:return
        if time.monotonic() - self._last_background_count < 2:
            return
        self._last_background_count = time.monotonic()
        try: model = local_scout_path()
        except RuntimeError: model = None
        identity = (self.capture.well_id.currentText(), self.capture.fov_id.value(), self._count_generation)
        confidence = float(tempo_preferences.get('annotator','confidence_threshold',.15))
        result = []
        signature=self.quality_signature
        bits=self.quality_sensor_bits
        task = self._count_task = TaskThread(lambda: check_field(frame,model,confidence,signature,self.focus_profile,bits), self)
        task.completed.connect(lambda value,error: result.append(value) if error is None else None)
        def finished():
            if result and not self._shutdown_requested and not self.camera_busy() and identity == (self.capture.well_id.currentText(),self.capture.fov_id.value(),self._count_generation):
                self._show_field_check(result[0])
            self._count_task = None
            task.deleteLater()
        task.finished.connect(finished)
        task.start()

    def preview_for_quality(self,frame):
        if self._quality_task is not None or time.monotonic()-self._last_quality_check<.5:return
        from tempo_quality import assess
        self._last_quality_check=time.monotonic()
        generation=self._count_generation
        signature=self.quality_signature
        result=[]
        bits=self.quality_sensor_bits
        task=self._quality_task=TaskThread(lambda:assess(frame,signature,self.focus_profile,
            'preview-'+str(time.monotonic_ns()),bits),self)
        task.completed.connect(lambda value,error:result.append(value) if error is None else None)
        def finished():
            if result and not self._shutdown_requested and generation==self._count_generation and not self.camera_busy():
                self._show_quality(result[0])
            self._quality_task=None
            task.deleteLater()
        task.finished.connect(finished);task.start()

    def preview_context(self,context):
        signature=context.get('quality_signature')
        if signature!=self.quality_signature:
            self._count_generation+=1
            self._latest_quality_at=''
            self.focus_label.setText('Focus: Unavailable · Beta')
        self.quality_signature=signature
        self.quality_sensor_bits=context.get('sensor_bits')

    def _show_counts(self, counts):
        if counts is None:
            self.counts_label.setText('Cell estimate unavailable. Install Nano Scout to count fields.')
        else:
            self.counts_label.setText(f"{counts['live_count']} healthy · {counts['dead_count']} dead\nGroups: {counts['healthy_groups']} healthy · {counts['dead_groups']} dead")
        if counts and counts.get('quality'):
            self._show_quality(counts['quality'])

    def _show_quality(self, assessment):
        when=assessment.get('assessed_at','')
        if when and when<self._latest_quality_at:return
        self._latest_quality_at=when
        labels={'sharp':'Sharp','out_of_focus':'Out of focus','mixed':'Mixed focus','unavailable':'Unavailable'}
        self.focus_label.setText('Focus: '+labels.get(assessment.get('focus'),'Unavailable')+' · Beta')
        self.focus_label.setToolTip(assessment.get('reason',''))

    def _show_field_check(self, result):
        self.quality_signature=result['signature']
        self._show_counts(result['counts'])
        self._show_quality(result['quality'])
        if result['counting_error']:
            self.counts_label.setText('Cell estimate unavailable: '+result['counting_error'])

    def show_fields(self, document):
        self.history.clear()
        for field in document['fields']:
            count = field.get('counts')
            value = str(count['live_count']) + ' healthy' if count else 'count unavailable'
            suffix = ' · overlap/repeat, not credited' if field.get('duplicate') else ''
            self.history.addItem(f"{field['field_id']} · {field['status']} · {value}{suffix}")
            position = field.get('position_um')
            if position is not None:
                self.history.item(self.history.count()-1).setToolTip(f"Stage X {position[0]:g} µm · Y {position[1]:g} µm")
        if self.history.count(): self.history.scrollToBottom()
        rows = min(3, self.history.count())
        self.history.setFixedHeight(sum(max(self.fontMetrics().height()+6,self.history.sizeHintForRow(i)) for i in range(max(0,self.history.count()-rows),self.history.count()))+6)
        total = document.get('healthy_cell_estimate',0)
        self.history_summary.setText(f'{self.history.count()} fields · {total} healthy cells credited (estimate)' if self.history.count() else 'No fields recorded.')
        self.history_summary.setWordWrap(True)
        self.history_button.setEnabled(self.history.count() > 0)
        self.refresh_controls()

    def selection_changed(self):
        self._count_generation += 1
        self._record_context = None
        self.counts_label.setText('Cell estimate unavailable.')
        self.quality_signature=None
        self._latest_quality_at=''
        self.focus_label.setText('Focus: Unavailable · Beta')
        if self.session and self.session.active and not self.session.document['stimulated'] and not self.capture.is_recording and not self.busy() and not self.automatic_running():
            self.finish_well(reason='Selected another well.')
        self.refresh_controls()

    def open_setup(self, section='outputs'):
        if self.busy() or self.camera_busy() or (self.session and self.session.active and self.session.document['stimulated']): return
        dialog = CaptureSetupDialog(self, section=section)
        if dialog.exec() != QDialog.DialogCode.Accepted: return
        profile = copy.deepcopy(self.profile)
        core,cap = self.capture.core,self.capture.cv_cap
        def save():
            optics = profile.get('optics',{})
            if section == 'calibration' and optics.get('pixel_size_um',0) > 0:
                context = optical_context(core,cap)
                angle = math.radians(optics['angle_deg']); scale = optics['pixel_size_um']
                matrix = np.array([[math.cos(angle),-math.sin(angle)],[math.sin(angle),math.cos(angle)]]) @ np.diag([
                    -scale if optics['flip_x'] else scale,-scale if optics['flip_y'] else scale])
                profile['calibration'] = dict(matrix=matrix.tolist(),image_size=list(context['image_size']),signature=context['signature'])
                profile['calibrated_optics'] = optics
            atomic_write(self.profile_path,profile)
            return profile
        self._async(save,lambda value: setattr(self,'profile',value),camera=core is not None or cap is not None)

    def mark_corner(self, which):
        if self.busy() or self.camera_busy() or (self.session and self.session.active): return
        core = self.capture.core
        well = self.capture.well_id.currentText()
        profile = copy.deepcopy(self.profile)
        def mark():
            context = optical_context(core)
            if context['position'] is None: raise RuntimeError('Connect an XY stage before marking the search area.')
            area = profile.setdefault('areas',{}).setdefault(well,{})
            area[which] = list(context['position'])
            area['signature'] = context['signature']
            atomic_write(self.profile_path,profile)
            return profile
        self._async(mark,lambda value: setattr(self,'profile',value),camera=True)

    def start_automatic(self):
        try:
            if self.automatic_running(): self.stop_automatic(); return
            if self.busy() or self.camera_busy(): return
            initial_session = self.session if self.session and self.session.active else None
            calibration_doc = self.profile.get('calibration')
            if not calibration_doc: raise ValueError('Open Field Calibration to calibrate the field size.')
            calibration = FieldCalibration(tuple(map(tuple,calibration_doc['matrix'])),tuple(calibration_doc['image_size']),calibration_doc['signature'])
            if self.target.value() <= 0 or self.maximum.value() <= 0: raise ValueError('Enter a cell target and a maximum number of fields.')
            model = local_scout_path()
            experiment,directory = self._directory()
            params = self.parameters() if self.enabled.isChecked() else None
            if params and (self.service is None or not self.service.connected): raise ValueError('Connect the stimulator first.')
            if params and initial_session is None and not self._confirm(params): return
            if params and self.pre_mode.currentData() == 'timed' and self.pre_duration.value() <= 0: raise ValueError('Enter a pre-pacing duration greater than zero.')
            selection = self.scope.currentData()
            current = self.capture.well_id.currentText()
            names = [current] if selection == 'current' else sorted(self.profile.get('areas',{}))
            if selection not in ('all','current'): names = [well for well in names if well[0] in selection]
            if initial_session:
                if current not in names: raise ValueError('Include the currently paced well in this collection.')
                names = [current] + [well for well in names if well != current]
            if not names: raise ValueError('Mark a search area for at least one well.')
            if params and not self.profile.get('multiplexer') and len(names) > 1:
                raise ValueError('A single output can collect one well at a time. Select Current well.')
            plans = []
            for well in names:
                area = self.profile.get('areas',{}).get(well,{})
                if area.get('signature') != calibration.signature:
                    if selection != 'current': continue
                    raise ValueError(f'Mark both area corners for {well} using the current calibration.')
                if params and self.profile.get('multiplexer') and well not in self.profile.get('channels',{}).values():
                    if selection != 'current': continue
                    raise ValueError(f'Assign an output to {well}.')
                search_grid(area.get('corner_a'),area.get('corner_b'),calibration,self.maximum.value())
                ch = self.parameters(well).channel if params else None
                plans.append(dict(well=well,channel=ch,confirmed=params.needs_confirmation if params else False,**area))
            if not plans or (initial_session and plans[0]['well'] != current): raise ValueError('No eligible search area for the selected wells. Calibrate and mark both corners first.')
            tempo_preferences.set('scout','live_threshold',self.threshold.value())
            tempo_preferences.set('scout','scout_duration_sec',self.duration.value())
            tempo_preferences.set('scout','default_traversal',{'A':'Row A Only (12 wells)','AB':'Rows A-B (24 wells)','ABCD':'Rows A-D (48 wells)'}.get(selection,'All 96 Wells (Serpentine)'))
            import tempo_backend
            from tempo_gui import CameraAcquisitionWorker
            worker = FieldCollectionWorker(self.capture.core,plans,experiment,directory,self.target.value(),self.maximum.value(),
                self.threshold.value(),self.duration.value(),self.capture.spin_rec_fps.value(),calibration,model,
                float(tempo_preferences.get('annotator','confidence_threshold',.15)),service=self.service if params else None,
                parameters=params,prepacing_seconds=self.pre_duration.value() if self.pre_mode.currentData() == 'timed' else None,
                acquisition_factory=CameraAcquisitionWorker,enqueue=tempo_backend.add_to_queue,parent=self.capture,initial_session=initial_session,
                recording_options=self.capture.recording_options())
            self.capture.scout_worker = worker
            self._collection_stopping = False
            self._last_collection_result = ''
            self.collection_results_button.hide()
            worker.status_signal.connect(self.status.setText)
            worker.focus_signal.connect(self.show_focus_pause)
            worker.session_signal.connect(self._auto_session)
            worker.fields_signal.connect(self.show_fields)
            worker.error_signal.connect(self.show_error)
            worker.result_signal.connect(self.collection_finished)
            worker.finished.connect(self.capture.resume_camera_controls)
            worker.finished.connect(self.refresh_controls)
            def process_completed():
                window=self.capture.window()
                paths=[field['path'] for field in worker.session.document['fields']
                       if field.get('status')=='completed' and field.get('path')] if worker.session else []
                if hasattr(window,'start_captured_processing'):
                    window.start_captured_processing(paths)
            worker.finished.connect(process_completed)
            self.capture.pause_camera_controls()
            self.capture.btn_record.setEnabled(False)
            self.capture._pending_camera_worker = worker
            self.capture.start_pending_camera_worker()
            self._async(tempo_preferences.save_to_disk,lambda saved: self.show_error(RuntimeError('Collection settings could not be saved.')) if not saved else None)
            self.automatic_dialog.accept()
            self.refresh_controls()
        except Exception as exc: self.show_error(exc)

    def open_automatic(self):
        if self.automatic_running():
            self.stop_automatic()
        else:
            self.automatic_dialog.show()
            self.automatic_dialog.raise_()

    def show_focus_pause(self,details):
        if self._shutdown_requested:return
        self._show_quality(details['assessment'])
        self.focus_warning.show()
        self.focus_warning_timer.start()
        self.capture.timer.start(33)
        self.quality_signature=None
        self.capture.z_index.setEnabled(True)
        self.update_focus_warning()
        self.refresh_controls()
        QTimer.singleShot(0,lambda:self.capture.acquisition_scroll.ensureWidgetVisible(self.focus_warning))

    def update_focus_warning(self):
        worker=getattr(self.capture,'scout_worker',None)
        if getattr(worker,'focus_paused',False) is not True:
            self.focus_warning_timer.stop()
            self.focus_warning.setStyleSheet('')
            if worker and getattr(worker,'protocol_flags',[]):
                self.focus_warning.setText('Extended focus delay: possible viability and measurement-validity effects remain flagged.')
                self.focus_warning.show()
            else:self.focus_warning.hide()
            return
        if worker.focus_timed_out:
            confirmed=bool(self.service and self.service.status and self.service.status.mode=='REST_DISCHARGE')
            self.focus_warning.setText('Focus response exceeded 60 s. '+('Pacing stopped.' if confirmed else 'Pacing stop unconfirmed.')+
                ' Possible viability and measurement-validity effects. Resume requires pre-pacing.')
            self._focus_flash=not self._focus_flash
            self.focus_warning.setStyleSheet('color: '+('#ffd06a' if self._focus_flash else '#f09663')+';')
        elif self.session.document['stimulated']:
            remaining=max(0,60-worker.focus_pause.elapsed)
            self.focus_warning.setText(f'Focus review required. Pacing continues: {remaining:.0f} s to respond. Inspect/refocus, then Resume Collection.')
        else:
            self.focus_warning.setText('Focus review required. Inspect/refocus, then Resume Collection.')

    def resume_focus(self):
        worker=getattr(self.capture,'scout_worker',None)
        if getattr(worker,'focus_paused',False) is not True or self._focus_resume_pending:return
        late=worker.focus_timed_out or worker.focus_pause.expired
        message=('The focus delay exceeded 60 seconds. Cell viability and measurement validity may have been affected. '
                 'This and subsequent fields in this well remain flagged. Pacing will restart with pre-pacing.' if late else
                 'Continue this field after your focus review? The focus assessment and your override will remain attached. Later fields are checked again.')
        if not confirm_action(self,'Resume Collection',message,'Resume Collection'):return
        if not late and (worker.focus_timed_out or worker.focus_pause.expired):
            if not confirm_action(self,'Focus Response Timed Out',
                'The 60-second response window has elapsed. Possible viability and measurement-validity effects will remain flagged. Resume repeats pre-pacing.',
                'Resume with Warning'):return
        import getpass
        worker.focus_pause.request_resume()
        worker.resume_actor='local:'+getpass.getuser()
        worker.recording_options['retention']['z_index']=self.capture.z_index.value()
        self._focus_resume_pending=True
        self.capture.timer.stop()
        def drained():
            if self._preview_pending_for_focus() or self._count_task is not None:
                QTimer.singleShot(20,drained);return
            worker.focus_resume.set()
            def acknowledged():
                if getattr(worker,'focus_paused',False) is True and not worker.wait(0):
                    QTimer.singleShot(50,acknowledged);return
                self._focus_resume_pending=False
                self.refresh_controls()
            QTimer.singleShot(50,acknowledged)
        drained()

    def _preview_pending_for_focus(self):
        return getattr(self.capture,'_preview_pending',False)

    def stop_automatic(self):
        self._collection_stopping = True
        self.capture.stop_plate_scout()
        self.refresh_controls()

    def _auto_session(self, session):
        changed = self.session is not session
        self.session = session
        self.capture.well_id.setCurrentText(session.document['well_id'])
        if changed:
            self.capture.fov_id.setValue(1)
            self.history.clear()
            if not session.document.get('protocol_flags'):self.focus_warning.hide()
        self.refresh_controls()

    def collection_finished(self, text):
        self._last_collection_result = text
        self.collection_results_button.setVisible(bool(text))
        self.refresh_controls()

    def show_collection_results(self):
        dialog = QDialog(self); dialog.setWindowTitle('TEMPO · Collection Results')
        layout = QVBoxLayout(dialog)
        label = QLabel(self._last_collection_result); label.setWordWrap(True); layout.addWidget(label)
        close = QPushButton('Close'); close.clicked.connect(dialog.close); layout.addWidget(close)
        dialog.resize(470,300); dialog.show(); self._results_dialog = dialog

    def diagnostics(self):
        if self.session and self.session.active or self.camera_busy(): return
        if self.service is None or not self.service.connected:
            self.show_error(ValueError('Connect the stimulator before opening diagnostics.')); return
        dialog = QDialog(self); dialog.setWindowTitle('TEMPO · Stimulation Diagnostics')
        layout = QVBoxLayout(dialog)
        mode = QComboBox(); mode.addItem('Constant output','constant'); mode.addItem('Scope test · 10 Hz, 50% duty','square')
        if self.profile.get('multiplexer'): mode.addItem('Cycle eight channels','cycle')
        layout.addWidget(mode)
        label = QLabel(); label.setWordWrap(True); layout.addWidget(label)
        mode.currentIndexChanged.connect(lambda: label.setText(f'Each channel receives {self.frequency.value()/8:g} Hz.' if mode.currentData() == 'cycle' else ''))
        start = QPushButton('Start Diagnostic'); stop = QPushButton('Stop Output'); isolate = QPushButton('Isolate Electrode')
        layout.addWidget(start); layout.addWidget(stop); layout.addWidget(isolate)
        def begin():
            try:
                params = self.parameters()
                if not self._confirm(params): return
                if not confirm_action(dialog,'Start Diagnostic Output',f'Start {mode.currentText().lower()} on the connected output?', 'Start Output'): return
                # Read the mode on the GUI thread before scheduling.
                command = mode.currentData()
                cancelled = self._session_cancel = threading.Event()
                def work():
                    self.service.submit('stop').result(2)
                    if cancelled.is_set(): raise InterruptedError('Diagnostic start cancelled.')
                    self.service.submit(params.command(params.needs_confirmation)).result(2)
                    if cancelled.is_set(): raise InterruptedError('Diagnostic start cancelled.')
                    return self.service.submit(command).result(2)
                self._async(work)
            except Exception as exc: self.show_error(exc)
        start.clicked.connect(begin)
        def stop_output(command):
            self._session_cancel.set()
            self._pending.append((self.service.submit(command),lambda _: None))
        stop.clicked.connect(lambda: stop_output('stop'))
        isolate.clicked.connect(lambda: stop_output('isolate'))
        def closed(_):
            self._session_cancel.set()
            self._pending.append((self.service.submit('stop'),lambda _: None))
            self.refresh_controls()
        dialog.finished.connect(closed)
        dialog.setModal(False); dialog.show(); self._diagnostic_dialog = dialog
        self.refresh_controls()

    def diagnostics_open(self):
        dialog = getattr(self,'_diagnostic_dialog',None)
        return dialog is not None and dialog.isVisible()

    def poll(self):
        pending,self._pending = self._pending,[]
        for future,callback in pending:
            if not future.done(): self._pending.append((future,callback)); continue
            try: callback(future.result())
            except Exception as exc: self.show_error(exc)
        if self._shutdown_requested: return
        if self.service and self.service.error and self.service.error != self._last_service_error:
            self._last_service_error = self.service.error
            self.show_error(ConnectionError(self.service.error))
        if not self._profile_loaded and not self.busy() and not self.camera_busy():
            self._profile_loaded = True
            def load():
                if not self.profile_path.exists(): return self.profile
                value = json.loads(self.profile_path.read_text())
                return validate_capture_profile(value)
            self._async(load,lambda value: setattr(self,'profile',value))
        if self.session and self.session.active and self.session.document['stimulated']:
            collection=getattr(self.capture,'scout_worker',None)
            expected_focus_stop=(getattr(collection,'focus_paused',False) is True and
                getattr(collection,'focus_timed_out',False) is True and self.service and self.service.status and
                self.service.status.mode=='REST_DISCHARGE')
            if (self.service is None or not self.service.connected or self.service.status is None or
                    self.service.status.mode != 'PACING') and not expected_focus_stop and not self._finish_pending:
                self.error.set_error('Stimulation stopped or output is unconfirmed. The well session has been interrupted.',RuntimeError(self.service.error if self.service else 'Disconnected'))
                self.finish_well(interrupted=True,reason='Pacing lost or stopped.')
            elif (not self.automatic_running() and self.session.phase == 'pre_pacing' and
                    self.pre_mode.currentData() == 'timed' and self.session.elapsed >= self.pre_duration.value()):
                self.begin_imaging()
        if self._finish_pending and not self.camera_busy() and not self.busy():
            if self.session is None or not self.session.active:
                self._finish_pending = False
            else:
                self.finish_well()
        if time.monotonic() - self._last_stage_probe > 2 and not self.camera_busy() and not self.busy():
            self._last_stage_probe = time.monotonic()
            core = self.capture.core
            if core is None:
                self._stage_present = False
            else:
                def probe():
                    try: return bool(core.getXYStageDevice())
                    except Exception: return False
                self._async(probe,lambda value: setattr(self,'_stage_present',value))
        self.refresh_controls()

    def refresh_controls(self):
        active = self.session is not None and self.session.active
        automatic = self.automatic_running()
        busy = self.busy()
        connected = self.service is not None and self.service.connected
        status = self.service.status if connected else None
        camera = self.capture.core is not None or self.capture.cv_cap is not None
        pre = active and self.session.phase == 'pre_pacing'
        manual = pre and self.pre_mode.currentData() == 'manual'
        stimulated = active and self.session.document['stimulated']
        locked = stimulated or automatic or busy or self._finish_pending or self.firmware_busy() or self.diagnostics_open()
        self.update_scope()
        for control in (self.amplitude,self.frequency,self.width,self.pre_mode,self.pre_duration):
            control.setEnabled(not locked)
        self.pre_duration.setVisible(self.pre_mode.currentData() == 'timed')
        output_on = bool(status and status.mode == 'PACING' and stimulated and not self._starting_output)
        self.enabled.setChecked(output_on)
        self.enabled.setEnabled(bool(output_on or self._starting_output or self._finish_pending or
            (connected and not self.camera_busy() and not busy and not automatic and not self.firmware_busy() and not self.diagnostics_open())))
        self.enabled.setAccessibleDescription('Requests stimulation on or off. Output state is shown separately.')
        self.setup_button.setEnabled(not self._shutdown_requested)
        self.output_setup_action.setEnabled(not locked)
        self.firmware_action.setEnabled(not locked)
        self.diagnostics_action.setEnabled(connected and not locked)
        for control in (self.capture.well_id,self.capture.exp_id): control.setEnabled(not locked and not self.capture.is_recording)
        self.capture.fov_id.setEnabled(not self.capture.is_recording and not automatic and not busy)
        self.stop_button.setEnabled(bool(connected or active or automatic or self._starting_output))
        self.field_group.setVisible(camera or self.session is not None)
        self.history.setVisible(self.history.count() > 0)
        rows = min(3 if self.capture.height() >= 900 else 1, self.history.count())
        self.history.setFixedHeight(sum(max(self.fontMetrics().height()+6,self.history.sizeHintForRow(i)) for i in range(max(0,self.history.count()-rows),self.history.count()))+6)
        self.count_button.setEnabled(camera and not self.camera_busy() and not busy and not automatic)
        feed_unconfirmed = getattr(self.capture,'_feed_stop_pending',False)
        if self.capture.is_recording:
            worker = self.capture.acq_worker
            self.capture.btn_record.setEnabled(not automatic and worker is not None and getattr(worker,'is_running',True))
        else:
            available = camera and not self.camera_busy() and not busy and not self.firmware_busy() and not self.diagnostics_open() and not feed_unconfirmed
            self.capture.btn_record.setEnabled((manual and automatic and not self._collection_stopping) or available)
            collection=getattr(self.capture,'scout_worker',None)
            if automatic and getattr(collection,'focus_paused',False) is True:
                self.capture.btn_record.setEnabled(not self._focus_resume_pending)
                self.capture.btn_record.setText('Resume Collection')
            elif manual:
                self.capture.btn_record.setText('Begin Imaging')
            elif pre:
                self.capture.btn_record.setText('Cancel Pending Recording' if self._record_after_prepacing else 'Record After Pre-pacing')
            elif not self.camera_busy():
                self.capture.btn_record.setText('Record Field')
        if feed_unconfirmed: self.count_button.setEnabled(False)
        self.automatic_group.setVisible(self._stage_present)
        self.auto_button.setText('Stopping…' if automatic and self._collection_stopping else ('Stop Automatic Collection' if automatic else 'Automatic Collection…'))
        collection_available = self._stage_present and camera and not busy and not self.camera_busy() and not self._finish_pending and not self.firmware_busy() and not self.diagnostics_open() and not feed_unconfirmed
        self.auto_button.setEnabled((automatic and not self._collection_stopping) or collection_available)
        for control in (self.corner_a,self.corner_b,self.target,self.maximum,self.threshold,self.duration,self.scope):
            control.setEnabled(not automatic and not busy and not self._finish_pending)
        self.calibration_button.setEnabled(not locked)
        self.refresh_ports_button.setEnabled(not connected and not busy)
        self.port.setEnabled(not connected and not busy)
        self.port.setVisible(not connected)
        self.refresh_ports_button.setVisible(not connected)
        self.connect_button.setToolTip(self.port.currentText() if connected else '')
        self.connect_button.setText('Disconnect' if self.service and self.service.running else 'Connect')
        self.connect_button.setEnabled(not locked)
        well = self.capture.well_id.currentText()
        channels = [ch for ch,name in self.profile.get('channels',{}).items() if name == well]
        output = ('Output ' + str(int(channels[0])+1) if len(channels) == 1 else 'Assign an output') if self.profile.get('multiplexer') else 'Single output'
        self.channel_label.setText(output + ' → ' + well if self.profile.get('multiplexer') else output)
        area = self.profile.get('areas',{}).get(well,{})
        self.area_label.setText(f'{well} · Usable area marked.' if area.get('corner_a') is not None and area.get('corner_b') is not None else f'{well} · Mark both corners inside the well.')
        if self._finish_pending:
            self.status.setText('Stopping stimulation…' if connected else 'Output unconfirmed · stopping session…')
        elif self._starting_output:
            self.status.setText('Starting stimulation…')
        elif stimulated and not output_on:
            collection=getattr(self.capture,'scout_worker',None)
            confirmed_focus_stop=(getattr(collection,'focus_timed_out',False) is True and
                                  status is not None and status.mode=='REST_DISCHARGE')
            self.status.setText('Focus timeout · pacing stopped' if confirmed_focus_stop else 'Output unconfirmed')
        elif stimulated:
            phase = 'Pre-pacing' if pre else 'Pacing'
            epoch=getattr(self.session,'pacing_epoch_at',None)
            elapsed_s=max(0.,self.session.clock()-epoch) if epoch is not None else self.session.elapsed
            elapsed = f'{elapsed_s:.0f} s'
            if pre and self.pre_mode.currentData() == 'timed': elapsed += f' / {self.pre_duration.value():g} s'
            self.status.setText(f'{well} · {phase} · {elapsed}')
        elif status and status.mode != 'REST_DISCHARGE':
            text = {'CONSTANT_STIM':'Constant output','SQUARE_10HZ':'Scope test · 10 Hz, 50% duty','DISCONNECTED_HIZ':'Electrode isolated','PACING':'Pacing'}.get(status.mode,status.mode)
            if status.cycling: text = f'Cycling outputs · {status.parameters.frequency_hz/8:g} Hz per output'
            self.status.setText(text)
        elif self.session and self.session.document['stimulated'] and not self.session.document['output_confirmed']:
            self.status.setText('Output unconfirmed')
        elif not connected:
            self.status.setText('Stimulator not connected')
        else:
            self.status.clear()
        self.status.setVisible(bool(self.status.text()))

    def shutdown(self):
        """Requested before dashboard thread guards; never wait on the GUI thread."""
        self._shutdown_requested = True
        self._session_cancel.set()
        self.poll_timer.stop()
        self.focus_warning_timer.stop()
        if self.firmware_busy(): self._firmware_dialog.worker.installer.cancel_setup()
        if self.automatic_running(): self.capture.stop_plate_scout()
        if self.capture.acq_worker and self.capture.is_recording: self.capture.acq_worker.stop()
        if self.service:
            self.service.close()
        # Camera ownership is settled separately by CaptureTab.cleanup().
        if self.busy() or self._count_task is not None or self._quality_task is not None or self.automatic_running() or self.firmware_busy() or (self.service and self.service.running): return False
        if self.session and self.session.active:
            session = self.session
            status = self.service.status if self.service else None
            self._async(lambda: session.finish(status,'Application closed.',interrupted=True))
            return False
        if self.service and self.service.closed.done():
            try: self.service.closed.result()
            except Exception as exc:
                self.show_error(RuntimeError('Output could not be confirmed on disconnect. The device watchdog is configured for 2 seconds. ' + str(exc)))
        return True

    def showEvent(self,event):
        super().showEvent(event)
        if not self._shutdown_requested:
            self.poll_timer.start()

    def hideEvent(self,event):
        super().hideEvent(event)
        # An active session must still receive failure/timer supervision when
        # the user changes top-level tabs. Idle hidden fixtures need no polling.
        if not (self.session and self.session.active) and not (self.service and self.service.running):
            self.poll_timer.stop()
