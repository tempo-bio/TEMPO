"""Local focus calibration drafts, original planes, and reviewed profiles."""

from dataclasses import asdict, dataclass
from io import BytesIO
import json
from pathlib import Path
import re
from uuid import uuid4

import cv2
import numpy as np
from tifffile import TiffFile

from tempo_annotation_store import atomic_bytes
from tempo_processing import atomic_write, file_lock
from tempo_quality import METHOD, fit_profile
from tempo_recording import metadata_path, read_metadata

TIFF_SUFFIXES = {'.tif', '.tiff'}
LABELS = {'sharp': 'Sharp', 'out_of_focus': 'Out of focus', 'mixed': 'Mixed focus',
          'unavailable': 'Unavailable', 'skip': 'Skip'}


def valid_signature(value):
    return isinstance(value, str) and re.fullmatch(r'[0-9a-f]{64}', value) is not None


def read_focus_reference(path):
    """Use native recording metadata, including when only the sidecar was copied."""
    path = Path(path)
    recording = path.with_suffix('').with_suffix('') if path.name.endswith('.recording.json') else path
    if recording.suffix.lower() not in TIFF_SUFFIXES:
        raise ValueError('Choose a TEMPO Native lossless recording or its .recording.json sidecar.')
    try:
        metadata = read_metadata(recording)
    except (AttributeError, ValueError) as exc:
        raise ValueError(f'Could not read {metadata_path(recording)}. {exc}') from exc
    if metadata.get('status') != 'completed' or not metadata.get('frames'):
        raise ValueError(f'Expected a completed TEMPO recording in {metadata_path(recording)}.')
    dtype = metadata.get('dtype')
    bits = metadata.get('sensor_bits')
    if bits is None and dtype == 'uint8':
        bits = 8  # Older USB recordings omit their 8-bit preview depth.
    return FocusSettings(metadata.get('quality_signature'), bits, metadata.get('shape'), dtype)


@dataclass(frozen=True)
class FocusSettings:
    signature: str
    sensor_bits: int
    shape: tuple
    dtype: str

    def __post_init__(self):
        if not valid_signature(self.signature):
            raise ValueError('The recording or camera has no usable focus signature.')
        if (self.dtype not in ('uint8', 'uint16') or not isinstance(self.shape, (tuple, list))
                or len(self.shape) not in (2, 3)
                or any(type(size) is not int or size < 1 for size in self.shape)
                or (len(self.shape) == 3 and self.shape[2] not in (3, 4))):
            raise ValueError('The recording or camera has no supported native image shape and pixel type.')
        if type(self.sensor_bits) is not int or not 1 <= self.sensor_bits <= np.iinfo(self.dtype).bits:
            raise ValueError('The recording or camera has no valid active sensor bit depth.')
        object.__setattr__(self, 'shape', tuple(self.shape))

    @classmethod
    def from_frame(cls, frame, context):
        return cls(context.get('quality_signature'), context.get('sensor_bits'), frame.shape, frame.dtype.name)

    def check_frame(self, frame):
        if frame.shape != self.shape or frame.dtype.name != self.dtype:
            raise ValueError(f'Expected {self.dtype} camera pixels with shape {self.shape}.')


def read_planes(path, *, single=False):
    path = Path(path)
    if path.suffix.lower() not in TIFF_SUFFIXES:
        raise ValueError('Choose a .tif or .tiff image.')
    data = path.read_bytes()
    with TiffFile(BytesIO(data)) as tif:
        count = len(tif.pages)
        if single and count != 1:
            raise ValueError('Use one TIFF file per focal plane.')
        if count > 1:
            axes, shape = tif.series[0].axes, tif.series[0].shape
            if any(axis in 'TC' and size > 1 for axis, size in zip(axes, shape)):
                raise ValueError('Choose a Z-stack with a single time point and channel.')
    encoded = np.frombuffer(data, dtype=np.uint8)
    if count == 1:
        frame = cv2.imdecode(encoded, cv2.IMREAD_UNCHANGED)
        if frame is not None:
            return [frame]
    elif count and hasattr(cv2, 'imdecodemulti'):
        ok, frames = cv2.imdecodemulti(encoded, cv2.IMREAD_UNCHANGED)
        if ok and len(frames) == count:
            return frames
    if count > 1:
        # Older OpenCV releases lack imdecodemulti. TEMPO's TIFF reader keeps
        # native pixels and converts the file's RGB channels to camera order.
        from tempo_recording import NativeCapture
        reader = NativeCapture(path)
        try:
            return [reader.read()[1] for _ in range(count)]
        finally:
            reader.release()
    raise ValueError('Could not decode the TIFF.')


class FocusCalibration:
    def __init__(self, data_dir, settings):
        self.settings = settings
        self.folder = Path(data_dir) / 'Focus Calibration' / settings.signature
        self.manifest = self.folder / 'draft.json'

    def load(self):
        try:
            draft = json.loads(self.manifest.read_text(encoding='utf-8'))
        except FileNotFoundError:
            settings = dict(asdict(self.settings), shape=list(self.settings.shape))
            return dict(version=1, settings=settings, revision=0, fields=[], candidate=None)
        if draft.get('version') != 1 or FocusSettings(**draft['settings']) != self.settings:
            raise ValueError('Saved focus calibration does not match the camera settings.')
        return draft

    def _current(self, revision):
        draft = self.load()
        if draft['revision'] != revision:
            raise ValueError('This calibration changed in another window. Choose the same camera settings or recording metadata again to reload the saved draft.')
        return draft

    def _save_edit(self, draft):
        draft['revision'] += 1
        draft['candidate'] = None
        atomic_write(self.manifest, draft)
        return draft

    @staticmethod
    def _field(draft, field_id):
        for field in draft['fields']:
            if field['id'] == field_id:
                return field
        raise ValueError('Select a saved field.')

    def add_field(self, revision, well):
        with file_lock(self.manifest):
            draft = self._current(revision)
            fields = draft['fields']
            partition = 'test' if fields and all(field['partition'] == 'train' for field in fields) else 'train'
            number = len(fields) + 1
            name = f'{well} · Field {number}' if well else f'Field {number}'
            fields.append(dict(id=number, name=name, partition=partition, planes=[]))
            return self._save_edit(draft)

    def set_partition(self, revision, field_id, partition):
        if partition not in ('train', 'test'):
            raise ValueError('Choose whether this field is for fitting or checking.')
        with file_lock(self.manifest):
            draft = self._current(revision)
            self._field(draft, field_id)['partition'] = partition
            return self._save_edit(draft)

    def _plane_path(self, plane_id):
        if not isinstance(plane_id, str) or re.fullmatch(r'[0-9a-f]{32}', plane_id) is None:
            raise ValueError('The saved plane identifier is invalid.')
        return self.folder / 'planes' / f'{plane_id}.tif'

    def capture_plane(self, revision, field_id, frame, settings, z_index):
        if settings != self.settings:
            raise ValueError('The camera settings changed. Use the original settings or open their calibration draft.')
        return self._add_planes(revision, field_id, [(frame, None, z_index)])

    def import_planes(self, revision, field_id, paths):
        def examples():
            for path in map(Path, paths):
                try:
                    frames = read_planes(path)
                    match = re.search(r'(?:^|_)Z(-?\d+)(?:_|$)', path.stem, re.IGNORECASE)
                    for index, frame in enumerate(frames, start=1):
                        name = path.name if len(frames) == 1 else f'{path.name} · Plane {index}'
                        z_index = int(match[1]) if match and len(frames) == 1 else None
                        yield frame, name, z_index
                except (OSError, ValueError, cv2.error) as exc:
                    raise ValueError(f'{path.name} could not be imported. {exc}') from exc
        return self._add_planes(revision, field_id, examples())

    def _add_planes(self, revision, field_id, examples):
        with file_lock(self.manifest):
            draft = self._current(revision)
            field = self._field(draft, field_id)
            added = []
            try:
                for frame, source, z_index in examples:
                    self.settings.check_frame(frame)
                    plane_id = uuid4().hex
                    ok, encoded = cv2.imencode('.tif', frame)
                    if not ok:
                        raise ValueError('Could not save native camera pixels.')
                    path = self._plane_path(plane_id)
                    atomic_bytes(path, encoded.tobytes())
                    added.append(path)
                    field['planes'].append(dict(id=plane_id, source=source, z_index=z_index, label=None))
                if not added:
                    raise ValueError('Choose at least one focal plane.')
                return self._save_edit(draft)
            except BaseException:
                for path in added:
                    path.unlink(missing_ok=True)
                raise

    def read_plane(self, plane_id):
        return read_planes(self._plane_path(plane_id), single=True)[0]

    def label_plane(self, revision, field_id, plane_id, label):
        if label not in ('sharp', 'out_of_focus', 'skip', None):
            raise ValueError('Choose Sharp, Out of focus or Skip.')
        with file_lock(self.manifest):
            draft = self._current(revision)
            for plane in self._field(draft, field_id)['planes']:
                if plane['id'] == plane_id:
                    plane['label'] = label
                    return self._save_edit(draft)
        raise ValueError('Select a saved plane.')

    def fit(self, revision):
        draft = self._current(revision)
        records = []
        for field in draft['fields']:
            for plane in field['planes']:
                if plane['label'] in ('sharp', 'out_of_focus'):
                    frame = self.read_plane(plane['id'])
                    self.settings.check_frame(frame)
                    records.append(dict(field=field['name'], partition=field['partition'],
                                        label=plane['label'], frame=frame, plane_id=plane['id']))
        profile = fit_profile(records, self.settings.signature, sensor_bits=self.settings.sensor_bits)
        checked_planes = (row['plane_id'] for row in records if row['partition'] == 'test')
        for outcome, plane_id in zip(profile['validation'], checked_planes):
            outcome['plane_id'] = plane_id
        with file_lock(self.manifest):
            draft = self._current(revision)
            draft['candidate'] = profile
            atomic_write(self.manifest, draft)
        return draft

    def apply(self, revision, config_dir):
        with file_lock(self.manifest):
            profile = self._current(revision)['candidate']
            if not profile:
                raise ValueError('Fit and review the current examples before applying a profile.')
            return update_profile(config_dir, self.settings.signature, profile)


def _read_json(path):
    try:
        value = json.loads(path.read_text(encoding='utf-8'))
    except FileNotFoundError:
        return {}
    if not isinstance(value, dict):
        raise ValueError(f'Could not read {path.name}.')
    return value


def _matching_profile(profile, signature=None):
    return (isinstance(profile, dict) and profile.get('method') == METHOD and profile.get('id')
            and valid_signature(profile.get('signature'))
            and (signature is None or profile['signature'] == signature))


def load_profiles(config_dir):
    """Read current profiles and preserve the older single-profile file."""
    config_dir = Path(config_dir)
    profiles, errors = {}, []
    for path in [config_dir / 'focus_profile.json', *sorted((config_dir / 'focus_profiles').glob('*.json'))]:
        try:
            saved = _read_json(path)
            profile = saved if path.name == 'focus_profile.json' else saved.get('current')
            if saved and not _matching_profile(profile, None if path.name == 'focus_profile.json' else path.stem):
                raise ValueError(f'Invalid focus profile in {path.name}.')
            if profile:
                profiles[profile['signature']] = profile
        except (OSError, ValueError) as exc:
            errors.append(str(exc))
    return profiles, errors


def _profile_path(config_dir, signature):
    if not valid_signature(signature):
        raise ValueError('Invalid focus settings signature.')
    return Path(config_dir) / 'focus_profiles' / f'{signature}.json'


def previous_profile(config_dir, signature):
    return _read_json(_profile_path(config_dir, signature)).get('previous')


def update_profile(config_dir, signature, profile=None):
    """Replace the current profile and keep one reversible change per configuration."""
    path = _profile_path(config_dir, signature)
    with file_lock(path):
        saved = _read_json(path)
        current = saved.get('current')
        if current is None:
            try:
                legacy = _read_json(Path(config_dir) / 'focus_profile.json')
            except (OSError, ValueError):
                legacy = None
            if _matching_profile(legacy, signature):
                current = legacy
        selected = saved.get('previous') if profile is None else profile
        if not _matching_profile(selected, signature):
            raise ValueError('No matching focus profile is available.')
        if current != selected:
            atomic_write(path, dict(current=selected, previous=current))
        return selected
