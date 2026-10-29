"""The frozen nuclear worker dispatches headlessly instead of opening TEMPO."""
from pathlib import Path
import json
import runpy
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import numpy as np
import tempo_nuclear_training as nuclear


class NuclearWorkerLaunchTests(unittest.TestCase):
    def test_incomplete_helper_arguments_cannot_fall_through_to_dashboard(self):
        root = Path(__file__).resolve().parents[1]
        with patch.object(sys, 'argv', ['TEMPO.exe', '--predict-nuclei']), \
             patch('tempo_runtime.prepare_windows_runtime', side_effect=AssertionError('GUI bootstrap reached')):
            with self.assertRaises(SystemExit) as result:
                runpy.run_path(str(root/'tempo_gui.py'), run_name='__main__')
        self.assertEqual(result.exception.code, 2)

    def test_source_and_frozen_launches_return_maps_through_the_same_files(self):
        image = np.zeros((7, 9), dtype=np.float32)
        for frozen in (False, True):
            commands = []
            def child(command, **kwargs):
                commands.append(command)
                with np.load(command[-2], allow_pickle=False) as inputs:
                    np.savez(command[-1], **{key: inputs[key]+.5 for key in inputs.files})
                return SimpleNamespace(returncode=0, stderr='')
            with self.subTest(frozen=frozen), \
                 patch.object(sys, 'frozen', frozen, create=True), \
                 patch('subprocess.run', side_effect=child):
                result = nuclear.predict_maps_isolated('fixture-nuclei.zip', [image])
            np.testing.assert_array_equal(result[0], image+.5)
            self.assertEqual(commands[0][0], sys.executable)
            if frozen:
                self.assertEqual(commands[0][1], '--predict-nuclei')
                self.assertEqual(len(commands[0]), 5)
            else:
                self.assertEqual(Path(commands[0][1]).name, 'tempo_nuclear_training.py')
                self.assertEqual(commands[0][2], '--predict')

    def test_windows_child_uses_hidden_console_flag_and_frozen_dispatch(self):
        observed = []
        def child(command, **kwargs):
            observed.append((command, kwargs))
            np.savez(command[-1], **{'0': np.zeros((7, 9), dtype=np.float32)})
            return SimpleNamespace(returncode=0, stderr='')
        with patch.object(sys, 'frozen', True, create=True), \
             patch.dict(sys.modules, {'os': SimpleNamespace(name='nt')}), \
             patch('subprocess.CREATE_NO_WINDOW', 0x08000000, create=True), \
             patch('subprocess.run', side_effect=child):
            nuclear.predict_maps_isolated('fixture.zip', [np.zeros((7, 9))])
        self.assertEqual(observed[0][0][1], '--predict-nuclei')
        self.assertEqual(observed[0][1]['creationflags'], 0x08000000)

    def test_successful_child_without_output_reports_helper_failure_and_cleans_input(self):
        commands = []
        def child(command, **kwargs):
            commands.append(command)
            return SimpleNamespace(returncode=0, stderr='')
        with patch('subprocess.run', side_effect=child):
            with self.assertRaisesRegex(RuntimeError, 'without producing prediction maps'):
                nuclear.predict_maps_isolated('fixture.zip', [np.zeros((7, 9))])
        self.assertFalse(Path(commands[0][-2]).parent.exists())

    def test_failed_windowed_child_returns_redacted_sidecar_without_stderr(self):
        token = 'hf_' + 'A'*32
        def child(command, **kwargs):
            with patch.object(nuclear, 'run_prediction', side_effect=ValueError('Invalid checkpoint '+token)):
                code = nuclear.prediction_command(*command[-3:])
            return SimpleNamespace(returncode=code, stderr='')
        with patch('subprocess.run', side_effect=child):
            with self.assertRaisesRegex(RuntimeError, 'Invalid checkpoint') as failure:
                nuclear.predict_maps_isolated('fixture.zip', [np.zeros((7, 9))])
        self.assertNotIn(token, str(failure.exception))

    def test_failed_child_without_sidecar_or_stderr_still_has_actionable_error(self):
        with patch('subprocess.run', return_value=SimpleNamespace(returncode=7, stderr=None)):
            with self.assertRaisesRegex(RuntimeError, 'worker exited with code 7'):
                nuclear.predict_maps_isolated('fixture.zip', [np.zeros((7, 9))])

    def test_parent_rejects_wrong_keys_shapes_and_nonfinite_or_nonnumeric_maps(self):
        candidates = [
            {'1': np.zeros((7, 9))},
            {'0': np.zeros((9, 7))},
            {'0': np.full((7, 9), np.nan)},
            {'0': np.zeros((7, 9), dtype=np.complex64)},
            {'0': np.full((7, 9), 'bad')},
            {'0': np.zeros((7, 9)), '1': np.zeros((7, 9))},
        ]
        for candidate in candidates:
            def child(command, **kwargs):
                np.savez(command[-1], **candidate)
                return SimpleNamespace(returncode=0, stderr='')
            with self.subTest(keys=list(candidate)), patch('subprocess.run', side_effect=child):
                with self.assertRaisesRegex(RuntimeError, 'prediction.map'):
                    nuclear.predict_maps_isolated('fixture.zip', [np.zeros((7, 9))])

    def test_frozen_entry_point_exits_before_gui_or_model_runtime_bootstrap(self):
        root = Path(__file__).resolve().parents[1]
        arguments = ['TEMPO.exe', '--predict-nuclei', 'model.zip', 'images.npz', 'maps.npz']
        with patch.object(sys, 'argv', arguments), \
             patch.object(nuclear, 'run_prediction', return_value=0) as predict, \
             patch('tempo_runtime.prepare_windows_runtime', side_effect=AssertionError('GUI bootstrap reached')):
            with self.assertRaises(SystemExit) as exit_result:
                runpy.run_path(str(root/'tempo_gui.py'), run_name='__main__')
        self.assertEqual(exit_result.exception.code, 0)
        predict.assert_called_once_with('model.zip', 'images.npz', 'maps.npz')

    def test_prediction_entry_point_writes_maps_without_gui(self):
        with tempfile.TemporaryDirectory() as directory:
            inputs, output = Path(directory)/'images.npz', Path(directory)/'maps.npz'
            np.savez(inputs, **{'0': np.zeros((7, 9), dtype=np.float32)})
            with patch('tempo_training.configure_tensorflow'), \
                 patch('tempo_training.unpack_stardist', return_value=object()), \
                 patch('tempo_nuclei.continuous_map', side_effect=lambda model, image: image+.5):
                self.assertEqual(nuclear.run_prediction('fixture.zip', inputs, output), 0)
            with np.load(output, allow_pickle=False) as maps:
                np.testing.assert_array_equal(maps['0'], np.full((7, 9), .5))

    def test_frozen_entry_point_failure_exits_without_gui_or_traceback_dependency(self):
        root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)/'maps.npz'
            arguments = ['TEMPO.exe', '--predict-nuclei', 'model.zip', 'images.npz', str(output)]
            with patch.object(sys, 'argv', arguments), \
                 patch.object(nuclear, 'run_prediction', side_effect=ValueError('Invalid nuclear helper')), \
                 patch('tempo_runtime.prepare_windows_runtime', side_effect=AssertionError('GUI bootstrap reached')):
                with self.assertRaises(SystemExit) as exit_result:
                    runpy.run_path(str(root/'tempo_gui.py'), run_name='__main__')
            self.assertEqual(exit_result.exception.code, 1)
            failure = json.loads(Path(str(output)+'.error.json').read_text())
            self.assertEqual(failure, dict(error_type='ValueError', message='Invalid nuclear helper'))
