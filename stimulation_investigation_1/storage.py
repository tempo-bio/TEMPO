"""Lossless collection storage. No signal processing or stimulation control."""
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import tempfile
from uuid import uuid4


FORMAT = 'tempo.stimulation-investigation-1'
VERSION = 1
CONDITION_FIELDS = (
    ('name', 'Condition'), ('well', 'Well'), ('medium', 'Medium'),
    ('fill_volume_ul', 'Fill volume (µL)'), ('commanded_voltage_v', 'Commanded voltage (V)'),
    ('frequency_hz', 'Frequency (Hz)'), ('pulse_width_ms', 'Pulse width (ms)'),
    ('output', 'Output'), ('electrode_material', 'Material'),
    ('electrode_dimensions', 'Dimensions'), ('electrode_position_well', 'Position from well edge'),
    ('wire_material', 'Material'), ('wire_dimensions', 'Dimensions'),
    ('wire_position_well', 'Position from well edge'),
    ('wire_position_electrodes', 'Position from electrodes'), ('notes', 'Condition notes'),
)
SCOPE_FIELDS = (
    ('instrument', 'Instrument / probe'), ('measurement_window', 'Measurement window'),
    ('baseline_ch1_v', 'CH1 baseline (V)'), ('baseline_ch2_v', 'CH2 baseline (V)'),
    ('pulse_ch1_v', 'CH1 pulse (V)'), ('pulse_ch2_v', 'CH2 pulse (V)'),
    ('difference_v', 'Scope difference (V)'), ('pulse_width_ms', 'Measured width (ms)'),
    ('time_per_division', 'Time / division'), ('volts_per_division', 'Volts / division'),
    ('probe_attenuation', 'Probe attenuation'), ('coupling', 'Coupling'),
)


def now():
    return datetime.now(timezone.utc).isoformat()


def new_condition(name=''):
    return dict(id=uuid4().hex, created_at=now(),
                **{key: name if key == 'name' else '' for key, _ in CONDITION_FIELDS})


def new_record(condition, instrument, label=''):
    if instrument not in ('oscilloscope', 'arduino'):
        raise ValueError('Choose Oscilloscope or Arduino.')
    return dict(id=uuid4().hex, instrument=instrument, label=label, created_at=now(),
                condition_id=condition['id'], condition_snapshot=deepcopy(condition),
                notes='', attachments=[], values={}, capture=None)


def validate(document):
    if not isinstance(document, dict) or document.get('format') != FORMAT or document.get('version') != VERSION:
        raise ValueError('This file is not a supported Stimulation Investigation 1 investigation.')
    if not isinstance(document.get('id'), str) or not isinstance(document.get('name'), str):
        raise ValueError('The investigation identity is missing.')
    conditions, records = document.get('conditions'), document.get('records')
    if not isinstance(conditions, list) or not isinstance(records, list):
        raise ValueError('The condition or measurement records are missing.')
    identifiers = set()
    for condition in conditions:
        if not isinstance(condition, dict) or not isinstance(condition.get('id'), str) or not condition['id']:
            raise ValueError('A condition has no identifier.')
        if condition['id'] in identifiers:
            raise ValueError('Condition identifiers must be unique.')
        identifiers.add(condition['id'])
        if any(not isinstance(condition.get(key), str) for key, _ in CONDITION_FIELDS):
            raise ValueError('A condition contains invalid fields.')
    record_ids = set()
    for record in records:
        if not isinstance(record, dict) or record.get('condition_id') not in identifiers:
            raise ValueError('A measurement refers to a missing condition.')
        if not isinstance(record.get('id'), str) or not record['id'] or record['id'] in record_ids:
            raise ValueError('Measurement identifiers must be unique.')
        record_ids.add(record['id'])
        if record.get('instrument') not in ('oscilloscope', 'arduino'):
            raise ValueError('A measurement has an unknown instrument.')
        if not isinstance(record.get('condition_snapshot'), dict) or record['condition_snapshot'].get('id') != record['condition_id']:
            raise ValueError('A measurement is missing its condition snapshot.')
        if any(not isinstance(record['condition_snapshot'].get(key), str) for key, _ in CONDITION_FIELDS):
            raise ValueError('A measurement contains an invalid condition snapshot.')
        if not isinstance(record.get('created_at'), str):
            raise ValueError('A measurement is missing its recorded timestamp.')
        if not isinstance(record.get('values'), dict) or any(not isinstance(v, str) for v in record['values'].values()):
            raise ValueError('Manual measurements must retain their entered text.')
        if not isinstance(record.get('label'), str) or not isinstance(record.get('notes'), str):
            raise ValueError('A measurement label or notes are invalid.')
        if not isinstance(record.get('attachments'), list):
            raise ValueError('A measurement has invalid attachments.')
        for attachment in record['attachments']:
            if not isinstance(attachment, dict) or not all(isinstance(attachment.get(k), str) for k in ('name', 'file', 'sha256')):
                raise ValueError('An attachment has invalid metadata.')
            path = Path(attachment['file'])
            if path.is_absolute() or '..' in path.parts or len(path.parts) != 1 or '\\' in attachment['file']:
                raise ValueError('An attachment path is not a local filename.')
        capture = record.get('capture')
        if capture is not None:
            if record['instrument'] != 'arduino' or not isinstance(capture, dict) or not isinstance(capture.get('samples'), list):
                raise ValueError('A capture contains invalid raw data.')
            if capture.get('state') not in ('recording', 'complete', 'cancelled', 'interrupted'):
                raise ValueError('A capture has an invalid completion state.')
            target = capture.get('requested_readings')
            if type(target) is not int or not 1 <= target <= 64 or len(capture['samples']) > target:
                raise ValueError('A capture has an invalid requested reading count.')
            if capture['state'] == 'complete' and len(capture['samples']) != target:
                raise ValueError('A complete capture is missing raw readings.')
            from .capture import SAMPLE_FIELDS, parse_page
            for offset in range(0, len(capture['samples']), 6):
                rows = capture['samples'][offset:offset + 6]
                if any(not isinstance(row, dict) or any(key not in row for key in SAMPLE_FIELDS) for row in rows):
                    raise ValueError('A saved capture contains a malformed reading.')
                page = dict(si1_protocol=1, si1=dict(id=capture.get('device_capture_id'),
                    state='recording', count=len(capture['samples']), target=target, adc_bits=14,
                    offset=offset, samples=[[row[key] for key in SAMPLE_FIELDS] for row in rows]))
                parse_page(page, capture.get('device_capture_id'), offset, target)
    # Reject non-JSON and non-finite values before touching the saved file.
    json.dumps(document, allow_nan=False)


class Investigation:
    def __init__(self, name='Stimulation Investigation 1'):
        self.document = dict(format=FORMAT, version=VERSION, id=uuid4().hex, name=name,
                             created_at=now(), updated_at=now(), conditions=[], records=[])
        self.path = None
        self._saved_bytes = None

    @classmethod
    def open(cls, path):
        path = Path(path).resolve()
        payload = path.read_bytes()
        if len(payload) > 64 * 1024 * 1024:
            raise ValueError('The investigation exceeds the supported 64 MB file size.')
        document = json.loads(payload)
        validate(document)
        value = cls()
        value.document, value.path, value._saved_bytes = document, path, payload
        return value

    def add_condition(self, name=''):
        condition = new_condition(name)
        self.document['conditions'].append(condition)
        return condition

    def duplicate_condition(self, identifier):
        original = self.condition(identifier)
        duplicate = deepcopy(original)
        duplicate.update(id=uuid4().hex, created_at=now(), name=original['name'] + ' copy')
        self.document['conditions'].append(duplicate)
        return duplicate

    def condition(self, identifier):
        return next(c for c in self.document['conditions'] if c['id'] == identifier)

    def add_record(self, condition_id, instrument, label=''):
        record = new_record(self.condition(condition_id), instrument, label)
        self.document['records'].append(record)
        return record

    @property
    def attachments_dir(self):
        if self.path is None:
            raise ValueError('Save the investigation before attaching a file.')
        return self.path.parent / (self.path.name + '.files')

    def attach(self, record, source):
        source = Path(source)
        if not source.is_file():
            raise ValueError('Choose an existing attachment file.')
        self.attachments_dir.mkdir(exist_ok=True)
        filename = uuid4().hex + source.suffix
        destination = self.attachments_dir / filename
        digest = hashlib.sha256()
        try:
            with source.open('rb') as incoming, destination.open('xb') as outgoing:
                for block in iter(lambda: incoming.read(1024 * 1024), b''):
                    digest.update(block)
                    outgoing.write(block)
                outgoing.flush()
                os.fsync(outgoing.fileno())
        except BaseException:
            destination.unlink(missing_ok=True)
            raise
        attachment = dict(name=source.name, file=filename, sha256=digest.hexdigest())
        record['attachments'].append(attachment)
        return attachment

    def attachment_path(self, attachment):
        # Opened files never supply an arbitrary external path to the application.
        filename = attachment['file']
        if Path(filename).name != filename or '\\' in filename or filename in ('.', '..'):
            raise ValueError('Invalid attachment filename.')
        root = self.attachments_dir.resolve()
        path = (root / filename).resolve()
        if path.parent != root:
            raise ValueError('The attachment points outside its investigation folder.')
        return path

    def save(self, path=None):
        destination = Path(path).resolve() if path is not None else self.path
        if destination is None:
            raise ValueError('Choose a location for the investigation.')
        if self.path is not None and destination != self.path:
            raise ValueError('Use Export to copy a saved investigation to another location.')
        if destination.exists():
            if self._saved_bytes is None or destination.read_bytes() != self._saved_bytes:
                raise FileExistsError('The investigation changed on disk. Export to preserve these changes, then reopen the saved file.')
        self.document['updated_at'] = now()
        validate(self.document)
        payload = (json.dumps(self.document, ensure_ascii=False, indent=2, allow_nan=False) + '\n').encode('utf-8')
        temp = None
        try:
            with tempfile.NamedTemporaryFile(dir=destination.parent, prefix='.' + destination.name + '-', delete=False) as stream:
                temp = Path(stream.name)
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temp, destination)
        finally:
            if temp is not None:
                temp.unlink(missing_ok=True)
        self.path, self._saved_bytes = destination, payload
        return destination

    def export(self, path):
        """A self-contained ZIP with the document and copied attachments; no derived values."""
        import zipfile
        validate(self.document)
        destination = Path(path)
        # Refuse replacement: a research export must not silently overwrite another run.
        try:
            with destination.open('xb') as stream, zipfile.ZipFile(stream, 'w', zipfile.ZIP_DEFLATED) as archive:
                archive.writestr('investigation.json', json.dumps(self.document, ensure_ascii=False, indent=2, allow_nan=False))
                for record in self.document['records']:
                    for attachment in record['attachments']:
                        source = self.attachment_path(attachment)
                        if hashlib.sha256(source.read_bytes()).hexdigest() != attachment['sha256']:
                            raise ValueError('An attachment changed on disk: ' + attachment['name'])
                        archive.write(source, 'investigation.json.files/' + attachment['file'])
        except BaseException:
            # Only remove a file created by this attempt, never an existing destination.
            if 'stream' in locals():
                destination.unlink(missing_ok=True)
            raise
        return destination
