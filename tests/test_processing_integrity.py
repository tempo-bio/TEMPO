"""Recovery contracts with isolated queues, outputs, and synthetic recordings."""
import json
import os
from pathlib import Path
import tempfile
import threading
import platform
import subprocess
import sys
import unittest
from unittest.mock import patch, Mock
import cv2
import numpy as np
import pandas as pd
import tempo_backend as backend
from tempo_processing import ProcessingResult, ProcessingCancelled, CancellationToken, atomic_write, read_queue, read_result_history, record_result
from tempo_angles import axial_statistics
from tempo_analysis import read_results, summarize_cells, read_trace


class QueueIntegrityTests(unittest.TestCase):

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.queue = self.root / 'queue.json'
        patcher = patch.object(backend, 'QUEUE_FILE', str(self.queue))
        patcher.start(); self.addCleanup(patcher.stop)

    def test_completion_preserves_arrivals_and_failed_inputs_and_forwards_setting(self):
        atomic_write(self.queue, ['a', 'b'])
        def process(path, **kwargs):
            self.assertTrue(kwargs['export_debug_avis'])
            if path == 'a':
                backend.add_to_queue(str(self.root / 'new'))
            return ProcessingResult(path, outcome='success' if path == 'a' else 'failure')
        with patch.object(backend, 'process_assay_result', side_effect=process) as action:
            result = backend.process_entire_queue(export_debug_avis=True)
        self.assertEqual(action.call_count, 2)
        self.assertEqual(read_queue(self.queue), ['b', str(self.root / 'new')])
        self.assertEqual((result['completed'], result['failed']), (1, 1))

    def test_no_results_are_terminal_and_cancel_preserves_unstarted(self):
        atomic_write(self.queue, ['a', 'b', 'c'])
        token = CancellationToken()
        def process(path, **kwargs):
            if path == 'b': token.cancel()
            return ProcessingResult(path, outcome='no_cells' if path == 'a' else 'cancelled')
        with patch.object(backend, 'process_assay_result', side_effect=process):
            result = backend.process_entire_queue(cancel=token)
        self.assertEqual(read_queue(self.queue), ['b', 'c'])
        self.assertTrue(result['cancelled'])

    def test_malformed_queue_is_never_replaced(self):
        self.queue.write_text('{broken')
        with self.assertRaises(ValueError): backend.add_to_queue('new')
        self.assertEqual(self.queue.read_text(), '{broken')

    def test_atomic_replace_failure_preserves_queue(self):
        atomic_write(self.queue, ['a'])
        with patch('tempo_processing.os.replace', side_effect=OSError('disk failure')):
            with self.assertRaises(OSError): backend.add_to_queue('new')
        self.assertEqual(read_queue(self.queue), ['a'])
        self.assertEqual(list(self.root.glob('*.tmp')), [])

    def test_windows_lock_contention_releases_local_lock_and_preserves_queue(self):
        from types import SimpleNamespace
        import tempo_processing as processing
        atomic_write(self.queue, ['original'])
        windows = SimpleNamespace(LK_LOCK=1, LK_NBLCK=2, LK_UNLCK=3, locking=Mock())
        with patch.object(processing, 'os', SimpleNamespace(name='nt', path=os.path)), \
             patch.dict(sys.modules, {'msvcrt': windows}):
            windows.locking.side_effect = OSError('busy')
            with self.assertRaises(BlockingIOError):
                with processing.file_lock(self.queue, blocking=False):
                    self.fail('A contended queue must never be changed.')
            self.assertEqual(read_queue(self.queue), ['original'])
            windows.locking.side_effect = None
            with processing.file_lock(self.queue, blocking=False):
                self.assertEqual(read_queue(self.queue), ['original'])
        self.assertEqual([call.args[1] for call in windows.locking.call_args_list], [2, 2, 3])
        self.assertEqual(Path(str(self.queue) + '.lock').read_bytes(), b'\0')

    def test_concurrent_additions_are_not_lost(self):
        workers = [threading.Thread(target=backend.add_to_queue, args=(str(self.root / str(i)),)) for i in range(12)]
        for worker in workers: worker.start()
        for worker in workers: worker.join()
        self.assertEqual(len(read_queue(self.queue)), 12)

    def test_separate_process_additions_are_not_lost(self):
        command = [sys.executable, '-c',
                   'import sys; from tempo_processing import edit_queue; '
                   'edit_queue(sys.argv[1], lambda queue: queue + [sys.argv[2]])']
        if sys.platform == 'darwin':
            command = ['arch', '-' + platform.machine(), *command]
        children = [subprocess.Popen(command + [str(self.queue), str(index)],
                                     cwd=Path(__file__).resolve().parents[1]) for index in range(8)]
        for child in children:
            self.assertEqual(child.wait(timeout=10), 0)
        self.assertEqual(set(read_queue(self.queue)), {str(index) for index in range(8)})

    def test_relink_and_remove_preserve_recording(self):
        recording = self.root / 'new.avi'; recording.write_bytes(b'video')
        atomic_write(self.queue, ['missing'])
        backend.relink_queue('missing', str(recording))
        backend.remove_from_queue([str(recording)])
        self.assertEqual(read_queue(self.queue), [])
        self.assertEqual(recording.read_bytes(), b'video')

    def test_outcomes_survive_restart_and_retry_replaces_same_attempt(self):
        history = str(self.queue) + '.results.json'
        record_result(history, ProcessingResult('a', '/output/a', outcome='failure'))
        record_result(history, ProcessingResult('b', '/output/b', outcome='no_cells'))
        record_result(history, ProcessingResult('a', '/output/a', outcome='success'))
        outcomes = read_result_history(history)
        self.assertEqual([(r.input_path, r.outcome) for r in outcomes], [('b','no_cells'), ('a','success')])

    def test_history_failure_preserves_pending_recording(self):
        atomic_write(self.queue, ['a'])
        Path(str(self.queue) + '.results.json').write_text('{broken')
        with patch.object(backend, 'process_assay_result', return_value=ProcessingResult('a', outcome='no_cells')):
            result = backend.process_entire_queue()
        self.assertEqual(read_queue(self.queue), ['a'])
        self.assertEqual(result['failed'], 1)
        self.assertEqual(Path(str(self.queue) + '.results.json').read_text(), '{broken')

    def test_running_entry_rejects_remove_relink_and_duplicate_processing(self):
        recording = self.root / 'video.avi'; recording.write_bytes(b'fixture')
        replacement = self.root / 'replacement.avi'; replacement.write_bytes(b'fixture')
        atomic_write(self.queue, [str(recording)])
        with backend.claim_queue_entries([str(recording)]):
            self.assertTrue(backend.queue_entry_running(str(recording)))
            with self.assertRaises(ValueError): backend.remove_from_queue([str(recording)])
            with self.assertRaises(ValueError): backend.relink_queue(str(recording), str(replacement))
            with patch.object(backend, 'process_assay_result') as process:
                result = backend.process_entire_queue()
            process.assert_not_called()
            self.assertEqual(result['failed'], 1)
        self.assertEqual(read_queue(self.queue), [str(recording)])
        self.assertFalse(backend.queue_entry_running(str(recording)))

    def test_running_entry_protection_survives_another_process(self):
        recording = str(self.root / 'video.avi')
        atomic_write(self.queue, [recording])
        code = '''import sys
import tempo_backend as backend
backend.QUEUE_FILE = sys.argv[1]
assert backend.queue_entry_running(sys.argv[2])
try:
    backend.remove_from_queue([sys.argv[2]])
except ValueError:
    pass
else:
    raise AssertionError('Removed a running entry')
'''
        command = [sys.executable, '-c', code, str(self.queue), recording]
        if sys.platform == 'darwin':
            command = ['arch', '-' + platform.machine(), *command]
        with backend.claim_queue_entries([recording]):
            child = subprocess.run(command, capture_output=True, text=True, timeout=10)
        self.assertEqual(child.returncode, 0, child.stdout + child.stderr)
        self.assertEqual(read_queue(self.queue), [recording])


class ProcessingIntegrityTests(unittest.TestCase):

    def setUp(self):
        self.child_python = sys.executable
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.video = self.root / 'A01_FOV002.avi'
        writer = cv2.VideoWriter(str(self.video), cv2.VideoWriter_fourcc(*'MJPG'), 20, (32, 32))
        for i in range(12): writer.write(np.full((32, 32, 3), 50 + i, dtype=np.uint8))
        writer.release()
        for patcher in (patch.object(backend, 'PROCESSED_DIR', str(self.root)),
                        patch.object(backend, 'processing_provenance', return_value={'fixture': 1}),
                        patch.object(backend.sys, 'executable', str(Path(backend.BASE_DIR, '.venv', 'bin', 'python')))):
            patcher.start(); self.addCleanup(patcher.stop)
        self.cells = [{'id': 'cell_1', 'polygon': [[2,2],[20,2],[20,20],[2,20]]}]

    def test_no_cells_never_opens_manual_selector(self):
        with patch.object(backend, 'run_yolo_inference', return_value=[]), patch.object(cv2, 'namedWindow') as window:
            result = backend.process_assay_result(str(self.video))
        self.assertEqual(result.outcome, 'no_cells'); window.assert_not_called()
        self.assertTrue(Path(result.output_dir, 'processing.json').is_file())

    def test_inference_failure_is_not_empty_detection(self):
        with patch('yolo_inference_helper.detect_cells_via_bridge', return_value={'success': False, 'error': 'model failed'}):
            result = backend.process_assay_result(str(self.video))
        self.assertEqual(result.outcome, 'failure'); self.assertIn('model failed', result.error)

    def test_no_beats_preserves_raw_trace(self):
        with patch.object(backend, 'run_yolo_inference', return_value=self.cells):
            result = backend.process_assay_result(str(self.video))
        self.assertEqual(result.outcome, 'no_beats', result.error)
        self.assertTrue(Path(result.output_dir, 'cell_1_raw_kinematics.csv').is_file())
        compiled = read_results(result.results_csv)
        self.assertEqual(compiled.loc[0, 'Total_Beats_Analyzed'], 0)
        self.assertTrue(pd.isna(compiled.loc[0, 'Peak_Amplitude_au']))
        times, values = read_trace(result.results_csv, compiled.iloc[0], 'Contraction (Raw a.u.)')
        self.assertEqual(len(times), 12)

    def test_capture_settings_survive_processing_provenance_and_cell_export(self):
        from tempo_capture import CaptureSession,read_capture_metadata
        from tempo_stimulation import StimulusParameters,PacerStatus
        parameters=StimulusParameters()
        session=CaptureSession(self.root,'fixture','A01',parameters,channel=0)
        session.start(PacerStatus('PACING',parameters,False,100,2000,4.77))
        session.begin_imaging()
        session.begin_field(self.video,'FOV002',dict(live_count=1))
        session.complete_field(12)
        session.finish(PacerStatus('REST_DISCHARGE',parameters,False,100,2000,4.77))
        metadata=read_capture_metadata(self.video)
        with patch.object(backend,'processing_provenance',return_value={'capture_metadata':metadata}):
            result=backend.process_assay_result(str(self.video),cells_data=self.cells,_native=True)
        self.assertTrue(result.resolved,result.error)
        compiled=read_results(result.results_csv)
        self.assertEqual(compiled.loc[0,'Capture_Session_ID'],session.document['session_id'])
        self.assertEqual(compiled.loc[0,'Requested_Stimulation_Amplitude_V'],.6)
        self.assertEqual(compiled.loc[0,'Stimulation_Pulse_Width_ms'],2)
        self.assertEqual(compiled.loc[0,'Well_ID'],'A01')
        saved=json.loads(Path(result.output_dir,'processing.json').read_text())
        self.assertEqual(saved['provenance']['capture_metadata'],metadata)
        self.assertTrue(Path(result.output_dir,'capture.json').exists())

    def test_registered_recording_keeps_exact_output_association_through_native_and_bridge(self):
        from tempo_spatial import registration_for_row
        document = dict(version=1, recording_id=self.video.stem, well_id='A01', field_id='FOV002',
                        units='um', coordinate_system='A01-calibrated', image_size_px=[32,32],
                        pixel_to_well=[[2,0,100],[0,2,200]])
        Path(str(self.video)+'.spatial.json').write_text(json.dumps(document))
        provenance = {'spatial_registration': document}
        # Native exercises the preserved per-cell association without changing input annotations.
        with patch.object(backend, 'processing_provenance', return_value=provenance):
            result = backend.process_assay_result(str(self.video), cells_data=self.cells)
        self.assertTrue(result.resolved, result.error)
        compiled = read_results(result.results_csv)
        registration = registration_for_row(result.results_csv, compiled.iloc[0])
        self.assertEqual(registration['recording_id'], Path(result.output_dir).name)
        self.assertEqual(registration['source_recording_id'], self.video.stem)
        self.assertEqual(compiled.loc[0, 'Spatial_Metadata_File'], 'spatial.json')
        self.assertNotIn('spatial_metadata_file', self.cells[0])
        manifest = json.loads(Path(result.output_dir, 'processing.json').read_text())
        self.assertIn('spatial.json', manifest['artifacts'])
        if sys.platform == 'darwin':
            # Bridge runs the backend child without Qt, using the real sidecar/provenance reader.
            with patch.object(backend.sys, 'executable', str(Path(backend.BASE_DIR, '.venv_x86', 'bin', 'python'))):
                bridged = backend.process_assay_result(str(self.video), cells_data=self.cells)
            self.assertTrue(bridged.resolved, bridged.error)
            row = read_results(bridged.results_csv).iloc[0]
            self.assertEqual(registration_for_row(bridged.results_csv, row)['recording_id'], Path(bridged.output_dir).name)

    def test_spatial_dimension_or_cell_identity_conflicts_do_not_extract(self):
        document = dict(version=1, recording_id=self.video.stem, well_id='A01', field_id='FOV002',
                        units='um', coordinate_system='A01', image_size_px=[64,64], pixel_to_well=[[1,0,0],[0,1,0]])
        for wrong_dimensions in (True, False):
            document['image_size_px'] = [64,64] if wrong_dimensions else [32,32]
            cells = [dict(self.cells[0], well_id='B01')]
            with patch.object(backend, 'processing_provenance', return_value={'spatial_registration': document}), \
                 patch.object(backend, 'process_cell_masks') as extract:
                result = backend.process_assay_result(str(self.video), cells_data=cells)
            self.assertEqual(result.outcome, 'failure')
            extract.assert_not_called()
            self.assertIn('dimensions' if wrong_dimensions else 'conflicts', result.error)

    def test_job_uses_recorded_settings_when_live_settings_change(self):
        from copy import deepcopy
        from tempo_config import DEFAULT_PREFERENCES, tempo_preferences
        import vk_math_engine as math
        snapshot = deepcopy(DEFAULT_PREFERENCES['kinetics'])
        snapshot['enable_asls_detrending']['value'] = False
        snapshot['enable_lowpass_filter']['value'] = False
        snapshot['savgol_window_pct']['value'] = 9
        snapshot['peak_prominence_pct']['value'] = 45
        provenance = {'config': {'kinetics': snapshot}}
        original = math.analyze_transients
        with patch.object(backend, 'processing_provenance', return_value=provenance), \
             patch.object(tempo_preferences, 'get_category', side_effect=AssertionError('live settings read')), \
             patch.object(math, 'asls_baseline_detrend') as detrend, \
             patch.object(math, 'apply_butterworth_lowpass') as lowpass, \
             patch.object(math, 'analyze_transients', wraps=original) as analyze:
            result = backend.process_assay_result(str(self.video), cells_data=self.cells)
        self.assertTrue(result.resolved, result.error)
        detrend.assert_not_called(); lowpass.assert_not_called()
        self.assertEqual(analyze.call_args.kwargs['savgol_window_pct'], 9)
        self.assertEqual(analyze.call_args.kwargs['peak_prominence_pct'], 45)
        self.assertEqual(json.loads(Path(result.output_dir, 'processing.json').read_text())['provenance'], provenance)

    def test_cancellation_during_extraction_releases_capture_and_video_writer(self):
        from masking_engine import process_cell_masks
        capture, writer = Mock(), Mock()
        capture.get.side_effect = lambda key: 20 if key == cv2.CAP_PROP_FPS else 32
        token = CancellationToken()
        def read():
            token.cancel()
            return True, np.full((32, 32, 3), 50, dtype=np.uint8)
        capture.read.side_effect = read
        with patch('masking_engine.cv2.VideoCapture', return_value=capture), \
             patch('masking_engine.cv2.VideoWriter', return_value=writer):
            with self.assertRaises(ProcessingCancelled):
                process_cell_masks(str(self.video), str(self.root), self.cells, export_debug_avis=True, cancel=token)
        capture.release.assert_called_once(); writer.release.assert_called_once()

    def test_measured_beats_produce_exact_compiled_trace_and_field_identity(self):
        writer = cv2.VideoWriter(str(self.video), cv2.VideoWriter_fourcc(*'MJPG'), 100, (32, 32))
        for index in range(240):
            intensity = int(40 + 120 * np.sin(np.pi * index / 60) ** 2)
            writer.write(np.full((32, 32, 3), intensity, dtype=np.uint8))
        writer.release()
        if sys.platform == 'darwin':
            with patch.object(backend.sys, 'executable', str(Path(backend.BASE_DIR, '.venv_x86', 'bin', 'python'))):
                result = backend.process_assay_result(str(self.video), cells_data=self.cells, export_debug_avis=True)
        else:
            result = backend.process_assay_result(str(self.video), cells_data=self.cells, export_debug_avis=True)
        self.assertEqual(result.outcome, 'success', result.error)
        self.assertTrue(Path(result.output_dir, 'masked_cell_1.avi').is_file())
        compiled = read_results(result.results_csv)
        self.assertEqual(compiled.loc[0, 'Well_ID'], 'A01')
        self.assertEqual(compiled.loc[0, 'FOV_ID'], 'FOV002')
        self.assertEqual(compiled.loc[0, 'Centroid_X'], 11)
        self.assertEqual(compiled.loc[0, 'Recording_ID'], Path(result.output_dir).name)
        time_values, measurements = read_trace(result.results_csv, compiled.iloc[0], 'Contraction (Raw a.u.)')
        self.assertEqual(len(time_values), 240)
        self.assertGreater(np.ptp(measurements), 50)

    def test_statistics_retry_uses_exact_output_without_extracting_again(self):
        with patch.object(backend, 'run_yolo_inference', return_value=self.cells), \
             patch.object(backend, '_statistics', side_effect=OSError('stats disk error')):
            result = backend.process_assay_result(str(self.video))
        self.assertEqual(result.outcome, 'failure')
        self.assertIn('kinematics', result.completed_stages)
        with patch.object(backend, 'run_yolo_inference') as detect, patch.object(backend, 'process_cell_masks') as extract:
            retried = backend.process_assay_result(str(self.video), retry_output=result.output_dir)
        self.assertEqual(retried.output_dir, result.output_dir)
        self.assertEqual(retried.outcome, 'no_beats', retried.error)
        detect.assert_not_called(); extract.assert_not_called()

    def test_retry_rejects_changed_provenance_without_touching_previous_output(self):
        with patch.object(backend, 'run_yolo_inference', return_value=self.cells), patch.object(backend, '_statistics', side_effect=OSError('stats')):
            result = backend.process_assay_result(str(self.video))
        before = Path(result.output_dir, 'processing.json').read_bytes()
        with patch.object(backend, 'processing_provenance', return_value={'fixture': 2}):
            retry = backend.process_assay_result(str(self.video), retry_output=result.output_dir)
        self.assertEqual(retry.outcome, 'failure')
        self.assertIn('changed', retry.error)
        self.assertEqual(before, Path(result.output_dir, 'processing.json').read_bytes())

    def test_retry_rejects_missing_extraction_without_rewriting_record(self):
        with patch.object(backend, 'run_yolo_inference', return_value=self.cells), patch.object(backend, '_statistics', side_effect=OSError('stats')):
            result = backend.process_assay_result(str(self.video))
        record = Path(result.output_dir, 'processing.json')
        before = record.read_bytes()
        Path(result.output_dir, 'cell_1_raw_kinematics.csv').unlink()
        retry = backend.process_assay_result(str(self.video), retry_output=result.output_dir)
        self.assertEqual(retry.outcome, 'failure')
        self.assertEqual(record.read_bytes(), before)

    def test_backend_cli_returns_output_identity_without_qt(self):
        request = self.root / 'request.json'
        response = self.root / 'response.json'
        atomic_write(request, {'video_path': str(self.video), 'cells_data': self.cells,
                              'output_root': str(self.root / 'isolated-output')})
        command = [self.child_python, str(Path(backend.__file__).resolve()),
                   '--request', str(request), '--result', str(response)]
        if sys.platform == 'darwin':
            command = ['arch', '-arm64', str(Path(backend.BASE_DIR, '.venv', 'bin', 'python')), *command[1:]]
        child = subprocess.run(command, capture_output=True, text=True, timeout=30)
        self.assertEqual(child.returncode, 0, child.stdout + child.stderr)
        result = ProcessingResult(**json.loads(response.read_text()))
        self.assertEqual(result.outcome, 'no_beats', result.error)
        self.assertTrue(Path(result.output_dir).is_relative_to(self.root))
        self.assertTrue(Path(result.output_dir, 'cell_1_raw_kinematics.csv').is_file())

    @unittest.skipUnless(sys.platform == 'darwin', 'Rosetta bridge is macOS-specific')
    def test_rosetta_bridge_returns_native_output_identity(self):
        with patch.object(backend.sys, 'executable', str(Path(backend.BASE_DIR, '.venv_x86', 'bin', 'python'))):
            result = backend.process_assay_result(str(self.video), cells_data=self.cells)
        self.assertEqual(result.outcome, 'no_beats', result.error)
        self.assertTrue(Path(result.output_dir).is_relative_to(self.root))
        self.assertEqual(result.input_path, str(self.video))

    @unittest.skipUnless(sys.platform == 'darwin', 'Native bridge is macOS-specific')
    def test_rosetta_cancellation_reaches_native_child(self):
        class CancelAfterLaunch(CancellationToken):
            def __init__(self):
                super().__init__(); self.checks = 0
            def is_cancelled(self):
                self.checks += 1
                return self.checks > 1
        with patch.object(backend.sys, 'executable', str(Path(backend.BASE_DIR, '.venv_x86', 'bin', 'python'))):
            result = backend.process_assay_result(str(self.video), cells_data=self.cells, cancel=CancelAfterLaunch())
        self.assertEqual(result.outcome, 'cancelled', result.error)
        self.assertEqual(result.input_path, str(self.video))

    def test_cancel_before_start_does_not_launch_detection(self):
        token = CancellationToken(); token.cancel()
        with patch.object(backend, 'run_yolo_inference') as detect:
            result = backend.process_assay_result(str(self.video), cancel=token)
        self.assertEqual(result.outcome, 'cancelled'); detect.assert_not_called()

    def test_bad_video_is_failure_not_no_cells(self):
        self.video.write_bytes(b'invalid')
        with patch.object(backend, 'run_yolo_inference') as detect:
            result = backend.process_assay_result(str(self.video))
        self.assertEqual(result.outcome, 'failure'); detect.assert_not_called()


class AnalysisIntegrityTests(unittest.TestCase):

    def test_legacy_raw_amplitude_is_not_claimed_as_area_normalized(self):
        from tempo_statistics import compile_master_dataset
        with tempfile.TemporaryDirectory() as directory:
            Path(directory, 'cell_1_transients_summary.csv').write_text('Peak Amplitude (a.u.)\n5\n')
            result = compile_master_dataset(directory)
        self.assertEqual(result.loc[0, 'Peak_Amplitude_Raw_au'], 5)
        self.assertTrue(pd.isna(result.loc[0, 'Peak_Amplitude_AreaNorm_au']))

    def test_peak_threshold_applies_and_low_fps_derivative_remains_valid(self):
        from vk_math_engine import analyze_transients
        values = np.zeros(200)
        values[[40, 100, 160]] = [100, 40, 100]
        signal = [(index * 10, float(value)) for index, value in enumerate(values)]
        low = analyze_transients(signal, enable_lowpass=False, peak_prominence_pct=30)
        high = analyze_transients(signal, enable_lowpass=False, peak_prominence_pct=60)
        self.assertEqual(len(low), 3); self.assertEqual(len(high), 2)
        slow = analyze_transients(signal, fps=30, enable_lowpass=False)
        self.assertTrue(all(np.isfinite(row['Max_Contraction_Velocity']) for row in slow))

    def test_axial_wraparound_and_undefined_resultants(self):
        mean, spread = axial_statistics([1, 179])
        self.assertAlmostEqual(mean, 0)
        self.assertAlmostEqual(spread, 1, places=2)
        self.assertTrue(np.isnan(axial_statistics([0,90])[0]))
        self.assertTrue(np.isnan(axial_statistics([np.nan])[0]))
        self.assertAlmostEqual(axial_statistics([45,45, np.nan])[0], 45)

    def test_field_and_recording_identity_survive_aggregation(self):
        data = pd.DataFrame({'Recording_ID':['r1','r1','r2'], 'Well_ID':['A01']*3,
            'FOV_ID':['FOV001','FOV002','FOV001'], 'Cell_ID':['1']*3,
            'Centroid_X':[1,2,3], 'Centroid_Y':[4,5,6], 'Contraction_Angle_deg':[1,179,0]})
        means, deviations = summarize_cells(data)
        self.assertEqual(len(means), 3)
        self.assertEqual(list(means['Centroid_X']), [1,2,3])

    def test_unknown_metadata_is_not_fabricated_and_invalid_numbers_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory, 'results.csv')
            path.write_text('Well_ID,Cell_ID,Peak_Amplitude_au\nA01,1,2\n')
            data = read_results(path)
            self.assertEqual(data.loc[0, 'FOV_ID'], 'Unknown')
            self.assertTrue(np.isnan(data.loc[0, 'Centroid_X']))
            for bad in ('oops', 'inf'):
                path.write_text(f'Well_ID,Cell_ID,Peak_Amplitude_au\nA01,1,{bad}\n')
                with self.assertRaises(ValueError): read_results(path)

    def test_trace_uses_declared_relative_file_only(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory, 'results.csv')
            Path(directory, 'cell_raw.csv').write_text('Time (ms),Contraction (Raw a.u.)\n0,4\n10,5\n')
            x,y = read_trace(path, {'Trace_File':'cell_raw.csv'}, 'Contraction (Raw a.u.)')
            self.assertEqual(list(y), [4,5])
            with self.assertRaises(ValueError): read_trace(path, {'Trace_File':'../wrong.csv'}, 'Contraction (Raw a.u.)')
            with self.assertRaises(ValueError): read_trace(path, {}, 'Contraction (Raw a.u.)')

    def test_compiler_preserves_centroid_zero_and_unknown_field(self):
        from tempo_statistics import compile_master_dataset
        with tempfile.TemporaryDirectory() as directory:
            Path(directory, 'cell_1_transients_summary.csv').write_text('Peak Amplitude (a.u.),Contraction Angle (deg)\n1,179\n1,1\n')
            result = compile_master_dataset(directory, [{'id':'cell_1', 'centroid_x':0, 'centroid_y':12}])
            self.assertEqual(result.loc[0,'Centroid_X'], 0)
            self.assertEqual(result.loc[0,'FOV_ID'], 'Unknown')
            self.assertAlmostEqual(result.loc[0,'Contraction_Angle_deg'], 0)

    def test_malformed_summary_cannot_report_successful_statistics(self):
        from tempo_statistics import compile_master_dataset
        with tempfile.TemporaryDirectory() as directory:
            for bad in ('broken', 'inf'):
                Path(directory, 'cell_transients_summary.csv').write_text(f'Peak Amplitude (a.u.)\n{bad}\n')
                with self.assertRaises(ValueError): compile_master_dataset(directory)

    def test_statistics_replace_failure_preserves_previous_file(self):
        from tempo_statistics import generate_run_statistics
        with tempfile.TemporaryDirectory() as directory:
            master = Path(directory, 'Master_Cell_Data.csv'); master.write_text('previous research output')
            with patch('tempo_statistics.os.replace', side_effect=OSError('storage full')):
                with self.assertRaises(OSError): generate_run_statistics(pd.DataFrame({'value':[1]}), directory, replace_existing=True)
            self.assertEqual(master.read_text(), 'previous research output')
            self.assertEqual(list(Path(directory).glob('*.tmp')), [])

    def test_statistics_do_not_overwrite_historical_research(self):
        from tempo_statistics import generate_run_statistics
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory, 'Master_Cell_Data.csv'); path.write_text('historical data')
            with self.assertRaises(FileExistsError):
                generate_run_statistics(pd.DataFrame({'value':[1]}), directory)
            self.assertEqual(path.read_text(), 'historical data')
