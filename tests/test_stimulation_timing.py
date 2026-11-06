"""Unit tests for Feature B stimulation and video timing data pipeline."""
from dataclasses import dataclass
import json
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

import numpy as np
from PyQt6.QtWidgets import QApplication

APP = QApplication.instance() or QApplication([])
with patch('platform.machine', return_value='x86_64'):
    import tempo_gui as gui
from tests.qt_lifecycle import close_and_dispose
from tempo_capture import CaptureSession, read_capture_metadata, capture_columns
from tempo_processing import file_lock
from tempo_recording import NativeWriter, read_metadata, read_pulse_events, timeline
from tempo_stimulation import ClockSyncModel, PacerService, PacerStatus, StimulusParameters


@dataclass(frozen=True)
class MockPulseEvent:
    seq: int
    edge: int
    device_t_us: int
    mapped_host_ns: int
    uncertainty_ms: float


class MockPacerService:
    def __init__(self, supports_timing=True, model=None, pulses=None):
        self.supports_timing = supports_timing
        self.clock_model = model or dict(alpha=1.000002, beta=-150000.0, uncertainty_ms=0.45, total_pulses=10)
        self.pulses = list(pulses or [])
        self.connected = True
        self.status = PacerStatus('PACING', StimulusParameters(), False, 100, 2000, 4.77,
                                  protocol=2 if supports_timing else 1, supports_timing=supports_timing)

    def pop_recent_pulses(self):
        result = list(self.pulses)
        self.pulses.clear()
        return result

    def get_pulses_in_window(self, start_ns, end_ns):
        return [p for p in self.pulses if start_ns <= p['mapped_host_ns'] <= end_ns]

    def get_timing_alignment(self):
        if not self.supports_timing:
            return 'unsupported_firmware'
        return dict(self.clock_model)


class FramePulseSerializationTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.root = Path(self.temp_dir.name)

    def test_write_frame_without_pulse_events(self):
        path = self.root / 'frame_no_pulse.tif'
        writer = NativeWriter(path, 100.0)
        writer.write(np.zeros((32, 32), dtype=np.uint8), camera_time_ms=0.0)
        writer.close()

        journal = Path(str(path) + '.frames.jsonl')
        self.assertTrue(journal.exists())
        lines = [json.loads(line) for line in journal.read_text().splitlines() if line.strip()]
        self.assertEqual(len(lines), 1)
        self.assertEqual(lines[0]['index'], 0)
        self.assertNotIn('pulse_events', lines[0])

    def test_write_frame_with_empty_and_populated_pulse_events(self):
        path = self.root / 'frame_pulses.tif'
        writer = NativeWriter(path, 100.0)
        writer.write(np.zeros((32, 32), dtype=np.uint8), camera_time_ms=0.0, pulse_events=[])

        events = [
            dict(seq=1, edge=1, device_t_us=100500, mapped_host_ns=1000000000, uncertainty_ms=0.5),
            dict(seq=1, edge=0, device_t_us=102500, mapped_host_ns=1002000000, uncertainty_ms=0.5),
        ]
        writer.write(np.zeros((32, 32), dtype=np.uint8), camera_time_ms=10.0, pulse_events=events)
        writer.close()

        journal = Path(str(path) + '.frames.jsonl')
        lines = [json.loads(line) for line in journal.read_text().splitlines() if line.strip()]
        self.assertEqual(len(lines), 2)
        self.assertEqual(lines[0]['pulse_events'], [])
        self.assertEqual(len(lines[1]['pulse_events']), 2)
        self.assertEqual(lines[1]['pulse_events'][0]['seq'], 1)
        self.assertEqual(lines[1]['pulse_events'][0]['edge'], 1)
        self.assertEqual(lines[1]['pulse_events'][1]['edge'], 0)

    def test_write_frame_with_pulse_dataclass_objects(self):
        path = self.root / 'frame_objects.tif'
        writer = NativeWriter(path, 100.0)
        obj = MockPulseEvent(seq=5, edge=1, device_t_us=500000, mapped_host_ns=5000000000, uncertainty_ms=0.3)
        writer.write(np.zeros((32, 32), dtype=np.uint8), camera_time_ms=0.0, pulse_events=[obj])
        writer.close()

        extracted = read_pulse_events(path)
        self.assertEqual(len(extracted), 1)
        self.assertEqual(extracted[0]['seq'], 5)
        self.assertEqual(extracted[0]['edge'], 1)
        self.assertEqual(extracted[0]['device_t_us'], 500000)
        self.assertEqual(extracted[0]['frame_index'], 0)


class NativeWriterTimingPersistenceTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.root = Path(self.temp_dir.name)

    def test_close_persists_timing_alignment_in_recording_json(self):
        path = self.root / 'timing_meta.tif'
        writer = NativeWriter(path, 100.0)
        writer.write(np.zeros((32, 32), dtype=np.uint8), camera_time_ms=0.0)
        model = dict(alpha=1.000003, beta=2048.0, uncertainty_ms=0.25, total_pulses=42)
        writer.close(timing_alignment=model)

        meta = read_metadata(path)
        self.assertIn('timing_alignment', meta)
        self.assertAlmostEqual(meta['timing_alignment']['alpha'], 1.000003)
        self.assertEqual(meta['timing_alignment']['total_pulses'], 42)

    def test_close_auto_tallies_pulse_count_if_omitted(self):
        path = self.root / 'timing_auto_count.tif'
        writer = NativeWriter(path, 100.0)
        ev1 = [dict(seq=1, edge=1, device_t_us=1000, mapped_host_ns=10000, uncertainty_ms=0.1)]
        ev2 = [dict(seq=1, edge=0, device_t_us=3000, mapped_host_ns=12000, uncertainty_ms=0.1)]
        writer.write(np.zeros((32, 32), dtype=np.uint8), camera_time_ms=0.0, pulse_events=ev1)
        writer.write(np.zeros((32, 32), dtype=np.uint8), camera_time_ms=10.0, pulse_events=ev2)
        writer.close(timing_alignment=dict(alpha=1.0, beta=0.0, uncertainty_ms=0.1))

        meta = read_metadata(path)
        self.assertEqual(meta['timing_alignment']['total_pulses'], 2)

    def test_close_persists_unsupported_firmware(self):
        path = self.root / 'legacy_writer.tif'
        writer = NativeWriter(path, 100.0)
        writer.write(np.zeros((32, 32), dtype=np.uint8), camera_time_ms=0.0)
        writer.close(timing_alignment='unsupported_firmware')

        meta = read_metadata(path)
        self.assertEqual(meta['timing_alignment'], 'unsupported_firmware')

    def test_close_releases_lock_and_marks_failed_on_invalid_timing_alignment(self):
        path = self.root / 'timing_invalid_keys.tif'
        writer = NativeWriter(path, 100.0)
        writer.write(np.zeros((32, 32), dtype=np.uint8), camera_time_ms=0.0)
        # Missing required keys 'beta' and 'uncertainty_ms'
        writer.close(timing_alignment={'alpha': 1.0})

        # Lock must be released and re-acquirable
        with file_lock(str(path) + '.capture-owner', blocking=False):
            pass

        meta = read_metadata(path)
        self.assertEqual(meta['status'], 'failed')
        self.assertIn('Invalid timing alignment', meta['error'])

    def test_close_releases_lock_on_corrupt_types_in_timing_alignment(self):
        path = self.root / 'timing_corrupt_type.tif'
        writer = NativeWriter(path, 100.0)
        writer.write(np.zeros((32, 32), dtype=np.uint8), camera_time_ms=0.0)
        # Invalid type for alpha
        writer.close(timing_alignment={'alpha': 'not_a_float', 'beta': 0.0, 'uncertainty_ms': 0.5})

        with file_lock(str(path) + '.capture-owner', blocking=False):
            pass

        meta = read_metadata(path)
        self.assertEqual(meta['status'], 'failed')
        self.assertIn('Invalid timing alignment', meta['error'])


class CaptureSessionPulseRoutingTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.root = Path(self.temp_dir.name)
        self.parameters = StimulusParameters()

    def test_route_pulse_events_to_writer_and_complete_field(self):
        pacer = MockPacerService(pulses=[
            dict(seq=1, edge=1, device_t_us=100000, mapped_host_ns=2000000000, uncertainty_ms=0.4),
            dict(seq=1, edge=0, device_t_us=102000, mapped_host_ns=2002000000, uncertainty_ms=0.4),
        ])
        session = CaptureSession(self.root, 'EXP01', 'A01', self.parameters, pacer_service=pacer)
        session.start(pacer.status)
        session.begin_imaging()

        field_path = self.root / 'field_001.tif'
        writer = NativeWriter(field_path, 100.0)
        session.begin_field(field_path, 'FOV001', writer=writer)

        session.write_field_frame(np.zeros((32, 32), dtype=np.uint8), camera_time_ms=0.0)
        writer.close()
        session.complete_field(1)
        session.finish(pacer.status)

        cap_meta = read_capture_metadata(field_path)
        self.assertIsNotNone(cap_meta)
        self.assertIn('timing_alignment', cap_meta)
        self.assertIsInstance(cap_meta['timing_alignment'], dict)
        self.assertEqual(cap_meta['timing_alignment']['pulse_seq_start'], 1)
        self.assertEqual(cap_meta['timing_alignment']['pulse_seq_end'], 1)
        self.assertEqual(cap_meta['timing_alignment']['total_pulses'], 2)

    def test_legacy_firmware_persists_unsupported_in_capture_json(self):
        legacy_pacer = MockPacerService(supports_timing=False)
        session = CaptureSession(self.root, 'EXP01', 'A01', self.parameters, pacer_service=legacy_pacer)
        session.start(legacy_pacer.status)
        session.begin_imaging()

        field_path = self.root / 'field_legacy.tif'
        session.begin_field(field_path, 'FOV001')
        session.complete_field(50)
        session.finish(legacy_pacer.status)

        cap_meta = read_capture_metadata(field_path)
        self.assertIsNotNone(cap_meta)
        self.assertEqual(cap_meta.get('timing_alignment'), 'unsupported_firmware')

    def test_unstimulated_session_has_no_timing_alignment(self):
        session = CaptureSession(self.root, 'EXP01', 'A01', parameters=None)
        session.start()
        field_path = self.root / 'unstimulated_field.tif'
        session.begin_field(field_path, 'FOV001')
        session.complete_field(50)
        session.finish()

        cap_meta = read_capture_metadata(field_path)
        self.assertIsNotNone(cap_meta)
        self.assertNotIn('timing_alignment', cap_meta)


class BackwardCompatibilityTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.root = Path(self.temp_dir.name)

    def test_timeline_unaffected_by_pulse_events(self):
        path = self.root / 'timeline_test.tif'
        writer = NativeWriter(path, 100.0)
        for i in range(5):
            pulses = [dict(seq=i, edge=1, device_t_us=i*1000, mapped_host_ns=i*10000, uncertainty_ms=0.2)] if i % 2 == 0 else []
            writer.write(np.zeros((32, 32), dtype=np.uint8), camera_time_ms=i*10.0, pulse_events=pulses)
        writer.close()

        times, source = timeline(path, 5, 100.0)
        self.assertEqual(source, 'camera')
        self.assertEqual(len(times), 5)
        np.testing.assert_allclose(times, np.arange(5) * 10.0)

    def test_capture_columns_backward_compatibility(self):
        meta = dict(session_id='sess_1', stimulated=True, channel=0, requested_parameters=dict(amplitude_v=0.6, frequency_hz=1.0, pulse_width_ms=2.0),
                    prepacing_elapsed_s=30.0, status='completed', output_confirmed=True, timing_alignment=dict(alpha=1.0, beta=0.0, uncertainty_ms=0.5))
        cols = capture_columns(meta)
        self.assertTrue(cols['Stimulation_Enabled'])
        self.assertEqual(cols['Capture_Session_ID'], 'sess_1')


class AnalyzerTabPulseMarkerTests(unittest.TestCase):
    def setUp(self):
        self.tab = gui.AnalyzerTab()
        self.addCleanup(lambda: close_and_dispose(self.tab))

    def test_draw_recorded_wave_renders_pulse_markers_and_clears_on_redraw(self):
        with patch('tempo_gui.read_trace', return_value=(np.arange(10, dtype=float)*10.0, np.zeros(10))):
            cell_data = {
                'Trace_File': 'trace.csv',
                'pulse_events': [
                    {'seq': 1, 'edge': 1, 'time_ms': 20.0, 'voltage': 0.6, 'width': 2.0},
                    {'seq': 1, 'edge': 0, 'time_ms': 22.0, 'voltage': 0.6, 'width': 2.0},
                ]
            }
            self.tab.draw_recorded_wave(cell_data)
            self.assertEqual(len(self.tab._pulse_markers), 2)
            self.assertEqual(self.tab._pulse_markers[0].toolTip(), 'Pulse #1: Rising (0.6 V, 2 ms)')
            self.assertEqual(self.tab._pulse_markers[1].toolTip(), 'Pulse #1: Falling (0.6 V, 2 ms)')

            # Redraw with None clears previous markers
            self.tab.draw_recorded_wave(None)
            self.assertEqual(len(self.tab._pulse_markers), 0)

    def test_draw_recorded_wave_handles_non_numeric_and_invalid_timestamps(self):
        with patch('tempo_gui.read_trace', return_value=(np.arange(10, dtype=float)*10.0, np.zeros(10))):
            cell_data = {
                'Trace_File': 'trace.csv',
                'pulse_events': [
                    {'seq': 1, 'edge': 1, 'time_ms': '50.0'},
                    {'seq': 2, 'edge': 0, 'time_ms': True},
                    {'seq': 3, 'edge': 1, 'time_ms': None},
                    {'seq': 4, 'edge': 0, 'time_ms': float('nan')},
                    {'seq': 5, 'edge': 1, 'time_ms': float('inf')},
                    {'seq': 6, 'edge': 1, 'time_ms': 25.0},
                ]
            }
            self.tab.draw_recorded_wave(cell_data)
            # Only the single valid float timestamp (25.0) should be rendered as a marker
            self.assertEqual(len(self.tab._pulse_markers), 1)
            self.assertEqual(self.tab._pulse_markers[0].pos().x(), 25.0)

    def test_load_pulse_events_recovers_events_from_crash_truncated_jsonl(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            jsonl = root / 'crash_rec.frames.jsonl'
            # Line 1 is valid, line 2 is crash-interrupted midway
            jsonl.write_text(
                '{"camera_time_ms": 10.0, "pulse_events": [{"seq": 1, "edge": 1, "time_ms": 10.5}]}\n'
                '{"camera_time_ms": 20.0, "pulse_events": [{"seq": 2\n',
                encoding='utf-8'
            )
            tab = gui.AnalyzerTab()
            try:
                tab.dataset_path = str(root / 'dataset.csv')
                events = tab._load_pulse_events({'Recording_ID': 'crash_rec', 'Trace_File': 'trace.csv'})
                self.assertEqual(len(events), 1)
                self.assertEqual(events[0]['seq'], 1)
                self.assertEqual(events[0]['time_ms'], 10.5)
            finally:
                close_and_dispose(tab)


class ClockSyncModelRolloverTests(unittest.TestCase):
    def test_rollover_unwrapping_across_32bit_boundary(self):
        model = ClockSyncModel()
        base_h = 10_000_000_000_000  # 10,000 seconds in ns
        # 10 samples before 2^32 wrap (wrap is at dev = 4,294,967,296)
        for i in range(10):
            dev = 4_294_967_296 - 10_000_000 + i * 1_000_000
            host = base_h + i * 1_000_000_000
            model.record_sample(host - 500_000, dev, host + 500_000)

        # Device wraps at 10s: at 11s, device micros() wrapped to 1,000,000 us
        host_after = base_h + 11_000_000_000
        dev_after = 1_000_000
        model.record_sample(host_after - 500_000, dev_after, host_after + 500_000)

        mapped, uncert = model.map_device_time(dev_after)
        err_ns = abs(mapped - host_after)
        self.assertLess(err_ns, 1_000_000, f"Mapping error {err_ns} ns exceeded 1 ms threshold")
        self.assertLessEqual(uncert, 5.0, f"Uncertainty {uncert} ms unexpectedly large")
        self.assertAlmostEqual(model.alpha, 1000.0, delta=1.0)

    def test_samples_windowing_bounds_memory_to_64(self):
        model = ClockSyncModel()
        for i in range(100):
            model.record_sample(i * 1_000_000_000, i * 1_000_000, i * 1_000_000_000 + 1_000_000)
        self.assertEqual(len(model.samples), 64)


class PacerServiceRobustnessTests(unittest.TestCase):
    def test_handle_event_safely_ignores_malformed_payloads(self):
        svc = PacerService()
        svc._pending_syncs[1] = 1_000_000_000
        # Malformed sync_ack payloads
        svc._handle_event({'evt': 'sync_ack', 'nonce': 1, 't_us': 'not_an_int'})
        svc._handle_event({'evt': 'sync_ack', 'nonce': 'bad_nonce', 't_us': 1000})
        svc._handle_event({'evt': 'sync_ack', 'nonce': 1, 't_us': None})
        svc._handle_event({'evt': 'sync_ack'})

        # Malformed edge payloads
        svc._handle_event({'evt': 'edge', 'seq': 'not_int', 'edge': 1, 't_us': 100})
        svc._handle_event({'evt': 'edge', 'seq': 1, 'edge': 'invalid', 't_us': 100})
        svc._handle_event({'evt': 'edge', 'seq': 1, 'edge': 1, 't_us': 'corrupted'})
        svc._handle_event({'evt': 'edge'})

        # Verify pulse list and clock model remained uncorrupted
        self.assertEqual(len(svc._pulse_events), 0)
        self.assertEqual(svc.clock_model.total_pulses, 0)

    def test_drain_rx_caps_unbounded_buffer_growth(self):
        svc = PacerService()
        svc._board = Mock()
        svc._board.read.return_value = b'noise_without_newline_' * 20
        for _ in range(50):
            svc._drain_rx()
        self.assertLessEqual(len(svc._rx), 8192)


class CaptureToolsFirmwareNoticeTests(unittest.TestCase):
    def setUp(self):
        with patch('tempo_capture_ui.CaptureTools.showEvent', lambda self, event: None), \
             patch.object(gui.CaptureTab, 'update_frame', lambda self: None), \
             patch.object(gui.tempo_preferences, 'save_to_disk', return_value=True):
            self.view = gui.CaptureTab()
            self.view.show()
            APP.processEvents()
            self.addCleanup(lambda: close_and_dispose(self.view))
            self.controls = self.view.capture_controls

    def test_firmware_notice_displayed_for_protocol_v1_and_hidden_for_v2(self):
        service = Mock()
        service.connected = True
        service.status = PacerStatus('REST_DISCHARGE', StimulusParameters(), False, 100, 2000, 4.77,
                                     protocol=1, supports_timing=False)
        self.controls.service = service
        self.controls.refresh_controls()
        self.assertFalse(self.controls.firmware_notice.isHidden())
        self.assertEqual(self.controls.firmware_notice.text(),
                         'Firmware v1 detected. Update firmware via Setup to enable video synchronization.')
        self.assertFalse(self.view.firmware_notice.isHidden())

        # Protocol v2
        service.status = PacerStatus('REST_DISCHARGE', StimulusParameters(), False, 100, 2000, 4.77,
                                     protocol=2, supports_timing=True)
        self.controls.refresh_controls()
        self.assertTrue(self.controls.firmware_notice.isHidden())

        # Disconnected
        service.connected = False
        self.controls.refresh_controls()
        self.assertTrue(self.controls.firmware_notice.isHidden())


if __name__ == '__main__':
    unittest.main()
