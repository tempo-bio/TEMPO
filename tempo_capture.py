"""Capture-session state, field accounting, and calibrated search geometry."""
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import json
import math
import re
from pathlib import Path
import threading
import time
import uuid

import numpy as np
from tempo_processing import atomic_write

CAPTURE_VERSION = 1
ACTIVE_PHASES = frozenset(('pre_pacing', 'ready', 'recording', 'between_fields', 'stopping', 'focus_paused'))


class FocusPause:
    """The response window measures explicit resume, not incidental interaction."""
    def __init__(self,timeout=60.,clock=time.monotonic):
        self.clock=clock;self.started=clock();self.timeout=timeout
        self.resume_requested=None

    @property
    def elapsed(self):
        return max(0.,self.clock()-self.started)

    @property
    def expired(self):
        end=self.resume_requested if self.resume_requested is not None else self.clock()
        return self.timeout is not None and end-self.started>=self.timeout

    def request_resume(self):
        if self.resume_requested is None:self.resume_requested=self.clock()


def utc_now():
    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True)
class FieldCalibration:
    """Affine camera-pixel displacement to stage micrometres, at one optical configuration."""
    matrix: tuple
    image_size: tuple
    signature: str

    def __post_init__(self):
        matrix = np.asarray(self.matrix, dtype=float)
        if matrix.shape != (2, 2) or not np.isfinite(matrix).all() or abs(np.linalg.det(matrix)) < 1e-12:
            raise ValueError('Camera-to-stage calibration must be finite and nonsingular.')
        if len(self.image_size) != 2 or any(type(n) is not int or n <= 0 for n in self.image_size):
            raise ValueError('A calibration needs the camera image dimensions.')
        if not self.signature:
            raise ValueError('A calibration needs its optical configuration.')

    @property
    def footprint(self):
        # Axis-aligned envelope is conservative for rotated or reflected cameras.
        return tuple(map(float,np.abs(np.asarray(self.matrix)) @ np.asarray(self.image_size)))

    def matches(self, signature, image_size):
        return self.signature == signature and self.image_size == tuple(image_size)

    def registration(self, recording_id, well_id, field_id, position, coordinate_system):
        matrix = np.asarray(self.matrix)
        origin = np.asarray(position) - matrix @ (np.asarray(self.image_size) / 2)
        return dict(version=1, units='um', recording_id=recording_id, well_id=well_id,
                    field_id=field_id, coordinate_system=coordinate_system,
                    image_size_px=list(self.image_size), pixel_to_well=np.column_stack((matrix, origin)).tolist())


def search_grid(corner_a, corner_b, calibration, limit):
    """Bounded serpentine centres, with every complete footprint inside the marked rectangle."""
    if type(limit) is not int or not 1 <= limit <= 10000:
        raise ValueError('Set a maximum number of fields between 1 and 10000.')
    points = np.asarray([corner_a, corner_b], dtype=float)
    if points.shape != (2, 2) or not np.isfinite(points).all():
        raise ValueError('Mark both corners of the usable area with the stage.')
    low, high = points.min(axis=0), points.max(axis=0)
    width, height = calibration.footprint
    nx, ny = np.floor((high - low + 1e-8) / [width, height]).astype(int)
    if nx <= 0 or ny <= 0:
        raise ValueError('The marked area must contain at least one complete camera field.')
    result = []
    for row in range(min(int(ny), limit)):
        indices = range(int(nx)) if row % 2 == 0 else range(int(nx)-1, -1, -1)
        for col in indices:
            result.append((float(low[0] + (col+.5)*width), float(low[1] + (row+.5)*height)))
            if len(result) >= limit:
                return result
    return result


def validate_capture_profile(value):
    if not isinstance(value,dict) or not isinstance(value.get('channels',{}),dict) or not isinstance(value.get('areas',{}),dict):
        raise ValueError('Invalid channel or search-area setup. Reconfigure Channels / Fields.')
    if type(value.get('multiplexer',False)) is not bool or not isinstance(value.get('optics',{}),dict):
        raise ValueError('Invalid stimulation hardware setup.')
    names=[]
    for channel,well in value.get('channels',{}).items():
        if channel not in tuple(str(i) for i in range(8)) or not isinstance(well,str) or not re.fullmatch(r'[A-H](0[1-9]|1[0-2])',well):
            raise ValueError('Channel assignments must identify a channel from 0–7 and a plate well.')
        names.append(well)
    if len(names)!=len(set(names)):
        raise ValueError('A well is assigned to more than one channel.')
    for well,area in value.get('areas',{}).items():
        if not isinstance(well,str) or not re.fullmatch(r'[A-H](0[1-9]|1[0-2])',well) or not isinstance(area,dict):
            raise ValueError('Invalid saved well search area.')
        for key in ('corner_a','corner_b'):
            if key in area:
                point=np.asarray(area[key],dtype=float)
                if point.shape!=(2,) or not np.isfinite(point).all(): raise ValueError('Saved area corners must be finite stage positions.')
    calibration=value.get('calibration')
    if calibration is not None:
        FieldCalibration(tuple(map(tuple,calibration['matrix'])),tuple(calibration['image_size']),calibration['signature'])
    optics=value.get('optics',{})
    for key in ('pixel_size_um','angle_deg'):
        if key in optics and (not isinstance(optics[key],(float,int)) or not math.isfinite(optics[key])):
            raise ValueError('Invalid saved optical calibration.')
    json.dumps(value,allow_nan=False)
    return value


class CaptureSession:
    def __init__(self, directory, experiment, well, parameters=None, clock=time.monotonic,
                 calibration=None, channel=None):
        self.clock = clock
        self.lock = threading.RLock()
        self.started_at = None
        self.pacing_epoch_at = None
        self.calibration = calibration
        self.document = dict(version=CAPTURE_VERSION, session_id=uuid.uuid4().hex,
            experiment_id=experiment, well_id=well, channel=channel,
            phase='new', created_at=utc_now(), stimulated=parameters is not None,
            requested_parameters=asdict(parameters) if parameters else None,
            acknowledged_status=None, fields=[], events=[], healthy_cell_estimate=0,
            output_confirmed=False, completion_reason='')
        self.path = Path(directory) / ('capture_' + self.document['session_id'] + '.json')
        self._field = None

    @property
    def phase(self):
        return self.document['phase']

    @property
    def active(self):
        return self.phase in ACTIVE_PHASES

    @property
    def elapsed(self):
        return max(0., self.clock() - self.started_at) if self.started_at is not None else 0.

    def _save(self):
        atomic_write(self.path, self.document)

    def _transition(self, phase, reason=''):
        self.document['phase'] = phase
        self.document['events'].append(dict(phase=phase, time=utc_now(), elapsed_s=self.elapsed, reason=reason))
        self._save()

    def start(self, status=None):
        with self.lock:
            if self.phase != 'new':
                raise ValueError('This well session has already started.')
            if self.document['stimulated']:
                if status is None or status.mode != 'PACING' or status.cycling:
                    raise ValueError('Single-channel pacing must be acknowledged before pre-pacing.')
                if asdict(status.parameters) != self.document['requested_parameters']:
                    raise ValueError('Acknowledged settings do not match the well session.')
                self.document['acknowledged_status'] = status.to_dict()
                self.document['output_confirmed'] = True
            else:
                self.document['output_confirmed'] = True
            self.started_at = self.clock()
            self.pacing_epoch_at = self.started_at
            self.document['pacing_epoch'] = 1 if self.document['stimulated'] else None
            self._transition('pre_pacing' if self.document['stimulated'] else 'ready')

    def restart_pacing(self, status):
        with self.lock:
            if not self.document['stimulated'] or self.phase != 'focus_paused':
                raise ValueError('Pacing can restart only after a stimulated focus pause.')
            if status.mode != 'PACING' or status.cycling or asdict(status.parameters) != self.document['requested_parameters']:
                raise ValueError('Pacing restart settings were not acknowledged.')
            self.pacing_epoch_at = self.clock()
            self.document['pacing_epoch'] += 1
            self.document['acknowledged_status'] = status.to_dict()
            self.document['output_confirmed'] = True
            self._transition('pre_pacing','Focus timeout; possible viability and measurement-validity effects.')

    def begin_imaging(self):
        with self.lock:
            if self.phase != 'pre_pacing':
                raise ValueError('This well is not pre-pacing.')
            elapsed = max(0.,self.clock()-self.pacing_epoch_at)
            self.document.setdefault('initial_prepacing_s',elapsed)
            self.document['current_prepacing_s'] = elapsed
            self._transition('ready')

    def begin_field(self, output_path, field_id, counts=None, position=None, footprint=None):
        with self.lock:
            if self.phase not in ('ready', 'between_fields'):
                raise ValueError('Complete pre-pacing before recording a field.')
            if position is not None:
                position = tuple(map(float,position))
                if len(position)!=2 or not all(map(math.isfinite,position)):
                    raise ValueError('The stage returned an invalid field position.')
            if footprint is not None:
                footprint = tuple(map(float,footprint))
                if len(footprint)!=2 or not all(math.isfinite(n) and n>0 for n in footprint):
                    raise ValueError('Invalid calibrated field footprint.')
            field = dict(field_id=str(field_id), recording_id=Path(output_path).stem,
                path=str(Path(output_path).absolute()), counts=counts,
                position_um=list(position) if position is not None else None,
                footprint_um=list(footprint) if footprint is not None else None,
                prepacing_elapsed_s=max(0.,self.clock()-self.pacing_epoch_at) if self.document['stimulated'] else None,
                pacing_epoch=self.document.get('pacing_epoch'),
                configured_prepacing_completed_s=self.document.get('current_prepacing_s'),
                status='recording', started_at=utc_now(), credited_cells=0, duplicate=False)
            for previous in self.document['fields']:
                if previous['status'] != 'completed':
                    continue
                same_id = previous['field_id'] == str(field_id)
                overlap = False
                if previous.get('credited_cells',0) > 0 and position is not None and footprint is not None and previous.get('position_um') is not None and previous.get('footprint_um'):
                    overlap = all(abs(a-b) < (c+d)/2 - 1e-6 for a,b,c,d in zip(
                        position, previous['position_um'], footprint, previous['footprint_um']))
                if same_id or overlap:
                    field['duplicate'] = True
                    break
            self.document['fields'].append(field)
            self._field = field
            self._transition('recording')
            self._save_field(field)
            if self.calibration and position is not None:
                registration = self.calibration.registration(field['recording_id'], self.document['well_id'],
                    field['field_id'], position, self.document['session_id'] + '-stage')
                atomic_write(str(output_path) + '.spatial.json', registration)
            return field

    def _save_field(self, field):
        if not field.get('path'):
            return
        atomic_write(field['path'] + '.capture.json', dict(version=CAPTURE_VERSION,
            session_id=self.document['session_id'], experiment_id=self.document['experiment_id'],
            well_id=self.document['well_id'], stimulated=self.document['stimulated'],
            channel=self.document['channel'], requested_parameters=self.document['requested_parameters'],
            acknowledged_status=self.document['acknowledged_status'], session_phase=self.phase,
            output_confirmed=self.document['output_confirmed'], **field))

    def complete_field(self, frames, error='', cancelled=False):
        with self.lock:
            field = self._field
            if field is None:
                return
            field.update(status='failed' if error else ('cancelled' if cancelled else 'completed'),
                         frames=int(frames), error=str(error), finished_at=utc_now())
            if not error and not cancelled and frames > 0 and not field['duplicate'] and field['counts'] is not None:
                field['credited_cells'] = int(field['counts'].get('live_count', 0))
                self.document['healthy_cell_estimate'] += field['credited_cells']
            self._field = None
            self._transition('between_fields')
            self._save_field(field)

    def finish(self, status=None, reason='', interrupted=False):
        with self.lock:
            if self.phase in ('completed', 'interrupted'):
                return
            if self._field is not None:
                self.complete_field(0, error=reason or 'Session interrupted.')
            self._transition('stopping')
            confirmed = not self.document['stimulated'] or (status is not None and status.mode == 'REST_DISCHARGE')
            self.document.update(output_confirmed=confirmed, completion_reason=reason,
                                 finished_at=utc_now(), final_status=status.to_dict() if status else None)
            self._transition('interrupted' if interrupted or not confirmed else 'completed', reason)
            for field in self.document['fields']:
                self._save_field(field)


def read_capture_metadata(video_path):
    path = Path(str(video_path) + '.capture.json')
    if not path.exists():
        return None
    document = json.loads(path.read_text(encoding='utf-8'))
    if (not isinstance(document, dict) or type(document.get('version')) is not int or document.get('version') != CAPTURE_VERSION or
            document.get('recording_id') != Path(video_path).stem):
        raise ValueError('Capture metadata does not identify this recording or uses an unsupported version.')
    for key in ('session_id', 'well_id', 'field_id'):
        if not isinstance(document.get(key), str) or not document[key]:
            raise ValueError('Capture metadata is missing ' + key + '.')
    if type(document.get('stimulated')) is not bool or type(document.get('output_confirmed')) is not bool:
        raise ValueError('Capture metadata has an invalid stimulation state.')
    parameters = document.get('requested_parameters')
    if document.get('status') not in ('recording','completed','failed','cancelled'):
        raise ValueError('Capture metadata has an invalid recording outcome.')
    if document['stimulated']:
        from tempo_stimulation import StimulusParameters
        if not isinstance(parameters,dict): raise ValueError('Capture metadata is missing stimulation settings.')
        StimulusParameters(**parameters)
        acknowledged = document.get('acknowledged_status')
        if not isinstance(acknowledged, dict) or acknowledged.get('parameters') != parameters:
            raise ValueError('Capture metadata has inconsistent stimulation settings.')
        elapsed=document.get('prepacing_elapsed_s')
        if isinstance(elapsed,bool) or not isinstance(elapsed,(float,int)) or not math.isfinite(elapsed) or elapsed < 0:
            raise ValueError('Capture metadata has an invalid pre-pacing duration.')
        if document.get('channel') != parameters['channel']:
            raise ValueError('Capture metadata has inconsistent channel assignments.')
    json.dumps(document, allow_nan=False)
    return document


def capture_columns(metadata):
    """Additive result columns; legacy recordings retain explicitly missing values."""
    metadata = metadata or {}
    params = metadata.get('requested_parameters') or {}
    return dict(Capture_Session_ID=metadata.get('session_id', ''),
        Stimulation_Enabled=metadata.get('stimulated'), Stimulation_Channel=metadata.get('channel'),
        Requested_Stimulation_Amplitude_V=params.get('amplitude_v'),
        Stimulation_Frequency_Hz=params.get('frequency_hz'),
        Stimulation_Pulse_Width_ms=params.get('pulse_width_ms'),
        Prepacing_Elapsed_s=metadata.get('prepacing_elapsed_s'),
        Capture_Outcome=metadata.get('status', ''), Capture_Output_Confirmed=metadata.get('output_confirmed'))
