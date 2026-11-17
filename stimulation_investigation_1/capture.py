"""Asynchronous raw acquisition through TEMPO's existing serial service."""
import time
from uuid import uuid4

from .storage import now


SAMPLE_FIELDS = ('started_us', 'finished_us', 'input', 'raw_count', 'mode_before',
                 'mode_after', 'high_before', 'high_after', 'cycle_before_us',
                 'cycle_after_us', 'output_before', 'output_after')
MODE_NAMES = {0: 'REST_DISCHARGE', 1: 'PACING', 2: 'CONSTANT_STIM',
              3: 'SQUARE_10HZ', 4: 'DISCONNECTED_HIZ'}


def parse_page(reply, identifier, offset, target):
    data = reply.get('si1')
    if reply.get('si1_protocol') != 1 or not isinstance(data, dict) or data.get('id') != identifier:
        raise ValueError('The Arduino returned a different investigation capture.')
    if data.get('offset') != offset or data.get('target') != target or data.get('adc_bits') != 14:
        raise ValueError('The Arduino capture configuration changed.')
    count = data.get('count')
    if type(count) is not int or not offset <= count <= target:
        raise ValueError('The Arduino returned an invalid sample count.')
    if data.get('state') not in ('armed', 'recording', 'complete', 'cancelled'):
        raise ValueError('The Arduino returned an unknown capture state.')
    rows = data.get('samples')
    if not isinstance(rows, list) or len(rows) > 6 or offset + len(rows) > count:
        raise ValueError('The Arduino returned an invalid sample page.')
    parsed = []
    for index, row in enumerate(rows):
        if not isinstance(row, list) or len(row) != len(SAMPLE_FIELDS):
            raise ValueError('The Arduino returned a malformed reading.')
        value = dict(zip(SAMPLE_FIELDS, row))
        for key in ('started_us', 'finished_us', 'cycle_before_us', 'cycle_after_us'):
            if type(value[key]) is not int or not 0 <= value[key] <= 0xffffffff:
                raise ValueError('A device timestamp is invalid.')
        if type(value['raw_count']) is not int or not 0 <= value['raw_count'] <= 16383:
            raise ValueError('An ADC reading is outside its raw range.')
        if type(value['input']) is not int or value['input'] != (1 if (offset + index) % 2 == 0 else 2):
            raise ValueError('Arduino inputs are out of sequence.')
        for key in ('mode_before', 'mode_after'):
            if type(value[key]) is not int or value[key] not in MODE_NAMES:
                raise ValueError('A reading has an unknown stimulation mode.')
        for key in ('output_before', 'output_after'):
            if type(value[key]) is not int or not 0 <= value[key] <= 7:
                raise ValueError('A reading has an invalid output identifier.')
        if type(value['high_before']) is not bool or type(value['high_after']) is not bool:
            raise ValueError('A reading is missing its output phase.')
        parsed.append(value)
    if count > offset and not rows:
        raise ValueError('The Arduino omitted available raw readings.')
    if data['state'] == 'complete' and count != target:
        raise ValueError('The Arduino reported an incomplete capture as complete.')
    return data, parsed


class RawCapture:
    """GUI calls tick; only PacerService's worker accesses the serial port.

    Save each received page before requesting the next one. Capture cancellation
    never starts or stops stimulation. Acquisition's Stop keeps its own priority.
    """
    def __init__(self, investigation, condition_id, service, *, readings=64,
                 interval_us=500, next_pulse=False, label='', clock=time.monotonic):
        if type(readings) is not int or not 1 <= readings <= 64:
            raise ValueError('Choose between 1 and 64 raw readings.')
        if type(interval_us) is not int or not 100 <= interval_us <= 1000000:
            raise ValueError('Choose a requested interval from 100 to 1000000 µs.')
        if type(next_pulse) is not bool:
            raise ValueError('Choose a capture trigger.')
        if not service or not service.connected:
            raise ConnectionError('Connect the Arduino in Acquisition first.')
        if service.investigation_protocol != 1:
            raise ConnectionError('Install the Stimulation Investigation 1 development firmware, then reconnect in Acquisition.')
        if investigation.path is None:
            raise ValueError('Save the investigation before recording Arduino inputs.')
        self.investigation, self.service, self.clock = investigation, service, clock
        self.identifier = int(uuid4().hex[:8], 16) or 1
        self.target = readings
        self.record = investigation.add_record(condition_id, 'arduino', label)
        status = service.status.to_dict() if service.status else None
        self.record['capture'] = dict(state='recording', started_at=now(), finished_at=None,
            firmware=service.firmware, port=service.port, protocol=1, adc_bits=14,
            input_names={'1': 'A1', '2': 'A2'}, mode_names=MODE_NAMES,
            requested_readings=readings, requested_interval_us=interval_us,
            trigger='next_pulse' if next_pulse else 'immediate',
            device_capture_id=self.identifier, stimulation_at_start=status,
            stimulation_at_end=None, samples=[], error='')
        try:
            investigation.save()
        except Exception as exc:
            self.record['capture'].update(state='interrupted', finished_at=now(),
                error='Recording was not started: ' + str(exc))
            raise
        self.pending = None
        self.command = 'start'
        self.cancel_requested = False
        self.cancel_sent = False
        self.done = False
        self.save_error = None
        self.device_state = 'starting'
        self.deadline = clock() + 80
        # No output command is issued by this package.
        self.pending = service.submit(f'si1 start {self.identifier} {readings} {interval_us} {int(next_pulse)}')

    def cancel(self):
        self.cancel_requested = True

    def _finish(self, state, error=''):
        capture = self.record['capture']
        capture.update(state=state, finished_at=now(), error=error,
                       stimulation_at_end=self.service.status.to_dict() if self.service.status else None)
        self.done = True
        try:
            self.investigation.save()
        except Exception as exc:
            self.save_error = exc
            raise

    def tick(self):
        if self.done:
            return
        try:
            if self.pending is not None:
                if not self.pending.done():
                    if self.clock() > self.deadline:
                        raise TimeoutError('Arduino capture timed out. Received readings were retained.')
                    return
                reply = self.pending.result()
                self.device_state = reply.get('si1', {}).get('state', '') if isinstance(reply, dict) else ''
                self.pending = None
                if self.command == 'fetch':
                    capture = self.record['capture']
                    data, rows = parse_page(reply, self.identifier, len(capture['samples']), self.target)
                    capture['samples'].extend(rows)
                    if data['state'] in ('complete', 'cancelled') and len(capture['samples']) == data['count']:
                        self._finish(data['state'])
                        return
                    if rows:
                        self.investigation.save()
                elif not isinstance(reply, dict) or reply.get('si1', {}).get('id') != self.identifier:
                    raise ValueError('The Arduino did not acknowledge this capture.')
            if self.clock() > self.deadline:
                raise TimeoutError('Arduino capture timed out. Received readings were retained.')
            if self.cancel_requested and not self.cancel_sent:
                self.command = 'cancel'
                self.cancel_sent = True
                self.pending = self.service.submit(f'si1 cancel {self.identifier}')
            else:
                self.command = 'fetch'
                self.pending = self.service.submit(f'si1 fetch {self.identifier} {len(self.record["capture"]["samples"])}')
        except Exception as exc:
            # Ask only to cancel sampling; output ownership remains in Acquisition.
            if self.service.connected:
                self.service.submit(f'si1 cancel {self.identifier}')
            if not self.done:
                self._finish('interrupted', str(exc))
            raise
