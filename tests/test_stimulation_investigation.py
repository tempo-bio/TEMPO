"""Collection integrity and raw capture tests; no hardware or fitted signals."""
from concurrent.futures import Future
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import zipfile

from stimulation_investigation_1 import Investigation
from stimulation_investigation_1.capture import RawCapture, parse_page
from stimulation_investigation_1.firmware import development_sketch
from tempo_stimulation import PacerStatus, StimulusParameters, PacerService
from tests.test_stimulation import TranscriptPort, reply


class InvestigationStorageTests(unittest.TestCase):
    def test_independent_instruments_and_condition_snapshots_survive_round_trip(self):
        with tempfile.TemporaryDirectory() as directory:
            value = Investigation()
            first = value.add_condition('Scope only')
            first.update(medium='Custom medium / batch Ω', wire_dimensions='Left: 0.2 mm; right: 0.25 mm')
            scope = value.add_record(first['id'], 'oscilloscope', 'Run 1')
            scope['values'] = {'pulse_ch1_v': '-0.0010', 'difference_v': '0.300 ± 0.002'}
            first['medium'] = 'Next batch'
            second = value.add_condition('Arduino only')
            value.add_record(second['id'], 'arduino')
            value.add_condition('No readings yet')
            path = value.save(Path(directory) / 'investigation.json')
            loaded = Investigation.open(path)
            self.assertEqual(loaded.document, value.document)
            self.assertEqual(loaded.document['records'][0]['condition_snapshot']['medium'], 'Custom medium / batch Ω')
            self.assertEqual(loaded.document['records'][0]['values']['difference_v'], '0.300 ± 0.002')
            duplicate = loaded.duplicate_condition(first['id'])
            self.assertNotEqual(duplicate['id'], first['id'])
            self.assertEqual(len(loaded.document['records']), 2)

    def test_failed_save_and_external_edit_do_not_destroy_saved_work(self):
        with tempfile.TemporaryDirectory() as directory:
            value = Investigation(); value.add_condition('Initial')
            path = value.save(Path(directory) / 'data.json')
            previous = path.read_bytes()
            value.document['conditions'][0]['notes'] = 'Unsaved changes'
            with patch('stimulation_investigation_1.storage.os.replace', side_effect=OSError('Disk unavailable')):
                with self.assertRaises(OSError): value.save()
            self.assertEqual(path.read_bytes(), previous)
            self.assertEqual([p.resolve() for p in Path(directory).iterdir()], [path])
            path.write_text(previous.decode() + ' ')
            with self.assertRaisesRegex(FileExistsError, 'changed on disk'): value.save()
            self.assertEqual(path.read_text(), previous.decode() + ' ')

    def test_export_is_portable_and_keeps_same_named_attachments_separate(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            value = Investigation(); condition = value.add_condition('Attachments')
            record = value.add_record(condition['id'], 'oscilloscope')
            value.save(root / 'data.json')
            for name, content in [('one', b'first trace'), ('two', b'second trace')]:
                (root / name).mkdir(); source = root / name / 'trace.csv'; source.write_bytes(content)
                value.attach(record, source)
            value.save()
            target = value.export(root / 'export.zip')
            with zipfile.ZipFile(target) as archive:
                archive.extractall(root / 'extracted')
            loaded = Investigation.open(root / 'extracted' / 'investigation.json')
            self.assertEqual([loaded.attachment_path(a).read_bytes() for a in loaded.document['records'][0]['attachments']],
                             [b'first trace', b'second trace'])
            previous = target.read_bytes()
            with self.assertRaises(FileExistsError): value.export(target)
            self.assertEqual(target.read_bytes(), previous)

    def test_untrusted_attachment_paths_and_nonfinite_data_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); value = Investigation(); condition = value.add_condition('Test')
            record = value.add_record(condition['id'], 'oscilloscope')
            for filename in ('../secret', '/tmp/secret', '..\\secret'):
                record['attachments'] = [dict(name='trace', file=filename, sha256='x')]
                with self.assertRaises(ValueError): value.save(root / 'invalid.json')
            record['attachments'] = []
            record['capture'] = {'samples': [], 'state': 'complete'}
            with self.assertRaises(ValueError): value.save(root / 'invalid.json')
            self.assertFalse((root / 'invalid.json').exists())


def result(value=None, error=None):
    future = Future()
    if error: future.set_exception(error)
    else: future.set_result(value)
    return future


class CaptureService:
    def __init__(self):
        self.connected = True; self.investigation_protocol = 1; self.firmware = 'dev'; self.port = 'fixture'
        self.status = PacerStatus('REST_DISCHARGE', StimulusParameters(), False, 0, 2000, 4.77)
        self.commands = []; self.rows = []; self.capture_id = None
        self.state = 'recording'; self.fail = False

    def submit(self, command):
        self.commands.append(command)
        if self.fail: return result(error=ConnectionError('Cable removed'))
        parts = command.split()
        self.capture_id = int(parts[2])
        if parts[1] == 'start': self.target = int(parts[3])
        if parts[1] == 'cancel': self.state = 'cancelled'
        offset = int(parts[3]) if parts[1] == 'fetch' else 0
        return result(dict(si1_protocol=1, si1=dict(id=self.capture_id, state=self.state,
            count=len(self.rows), target=self.target, adc_bits=14, offset=offset,
            samples=self.rows[offset:offset+6] if parts[1] == 'fetch' else [])))


def raw_row(index):
    return [1000+index*100, 1050+index*100, 1 if index % 2 == 0 else 2,
            500+index, 0, 0, False, False, 0, 0, 0, 0]


class InvestigationCaptureTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(); self.addCleanup(self.directory.cleanup)
        self.value = Investigation(); self.condition = self.value.add_condition('Raw capture')
        self.path = self.value.save(Path(self.directory.name) / 'capture.json')
        self.service = CaptureService()

    def tick_until_done(self, capture):
        for _ in range(20):
            if capture.done: break
            capture.tick()
        self.assertTrue(capture.done)

    def test_capture_pages_are_saved_without_conversion_or_stimulation_commands(self):
        capture = RawCapture(self.value, self.condition['id'], self.service, readings=8)
        self.service.rows = [raw_row(i) for i in range(8)]; self.service.state = 'complete'
        self.tick_until_done(capture)
        loaded = Investigation.open(self.path).document['records'][0]
        self.assertEqual(loaded['capture']['state'], 'complete')
        self.assertEqual([r['raw_count'] for r in loaded['capture']['samples']], list(range(500, 508)))
        self.assertTrue(all(c.startswith('si1 ') for c in self.service.commands))
        self.assertFalse(any('voltage' in key for key in loaded['capture']['samples'][0]))

    def test_cancellation_retains_partial_readings_and_leaves_output_alone(self):
        capture = RawCapture(self.value, self.condition['id'], self.service, readings=8)
        self.service.rows = [raw_row(i) for i in range(3)]
        capture.cancel(); self.tick_until_done(capture)
        self.assertEqual(capture.record['capture']['state'], 'cancelled')
        self.assertEqual(len(capture.record['capture']['samples']), 3)
        self.assertTrue(all(c.startswith('si1 ') for c in self.service.commands))

    def test_disconnect_retains_already_saved_page_as_interrupted(self):
        capture = RawCapture(self.value, self.condition['id'], self.service, readings=12)
        self.service.rows = [raw_row(i) for i in range(6)]
        capture.tick(); capture.tick()
        self.service.fail = True
        capture.tick()
        with self.assertRaises(ConnectionError): capture.tick()
        saved = Investigation.open(self.path).document['records'][0]['capture']
        self.assertEqual(saved['state'], 'interrupted'); self.assertEqual(len(saved['samples']), 6)

    def test_stock_firmware_cannot_start_capture_or_create_placeholder_measurement(self):
        self.service.investigation_protocol = None
        with self.assertRaisesRegex(ConnectionError, 'development firmware'):
            RawCapture(self.value, self.condition['id'], self.service)
        self.assertEqual(self.service.commands, []); self.assertEqual(self.value.document['records'], [])

    def test_wrong_capture_missing_data_and_invalid_counts_are_rejected(self):
        self.service.target = 2
        page = self.service.submit('si1 fetch 1 0').result()
        for changes in (dict(id=2), dict(count=2, samples=[]),
                        dict(count=1, samples=[[0, 1, 1, 16384, 0, 0, False, False, 0, 0, 0, 0]])):
            altered = json.loads(json.dumps(page)); altered['si1'].update(changes)
            with self.assertRaises(ValueError): parse_page(altered, 1, 0, 2)

    def test_extension_exchanges_share_the_existing_port_and_status_api(self):
        port = TranscriptPort(); original = port.write
        def write(payload):
            if b' si1 ' not in payload: return original(payload)
            identifier = int(payload.decode().split()[0])
            message = reply(identifier, si1_protocol=1, si1={'id': 12})
            port.rx.extend(json.dumps(message).encode() + b'\n'); return len(payload)
        port.write = write
        service = PacerService(); service._board = port
        self.assertIsInstance(service._exchange('status'), PacerStatus)
        self.assertEqual(service._exchange('si1 fetch 12 0')['si1']['id'], 12)
        self.assertEqual(service.investigation_protocol, 1)

    def test_development_sketch_is_staged_without_forking_pacing_source(self):
        with development_sketch() as sketch:
            self.assertTrue((sketch / 'investigation_1.h').is_file())
            self.assertTrue((sketch / 'stimulation_investigation_1.ino').read_text().startswith('#define TEMPO_INVESTIGATION_1 1\n'))
        self.assertFalse(sketch.exists())


if __name__ == '__main__':
    unittest.main()
