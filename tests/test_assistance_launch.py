"""Launch contracts runnable with Python's standard library on every host."""
import json
from pathlib import Path
import runpy
import sys
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

import tempo_assistance_runtime as runtime


class AssistanceLaunchTests(unittest.TestCase):
    def test_cancel_before_launch_never_starts_a_model(self):
        with patch.object(runtime.subprocess, 'Popen') as launch:
            for model in ('scout', 'stardist', 'cellpose'):
                with self.subTest(model=model), self.assertRaises(runtime.AssistanceCancelled):
                    runtime.run_assistance('image.png', model, .15, .4, cancel_requested=lambda:True)
        launch.assert_not_called()

    def test_cancel_while_running_settles_worker_tree(self):
        cancelled = threading.Event()
        process = Mock(returncode=None, poll=Mock(return_value=None))
        def launch(*args, **kwargs):
            cancelled.set()
            return process
        with patch.object(runtime.subprocess, 'Popen', side_effect=launch), \
             patch.object(runtime, '_job_for_child', return_value=None), \
             patch.object(runtime, '_stop_tree') as stop:
            with self.assertRaises(runtime.AssistanceCancelled):
                runtime.run_assistance('image.png','cellpose',.15,.4,cancel_requested=cancelled.is_set)
        stop.assert_called_once_with(process)

    def test_cancel_after_worker_success_discards_the_completed_result(self):
        cancelled = threading.Event()
        process = Mock(returncode=0, poll=Mock(return_value=0))
        def launch(arguments, **kwargs):
            Path(arguments[-2]).write_text('{"success":true,"boxes":[]}')
            cancelled.set()
            return process
        with patch.object(runtime.subprocess, 'Popen', side_effect=launch), \
             patch.object(runtime, '_job_for_child', return_value=None), \
             patch.object(runtime, '_stop_tree') as stop:
            with self.assertRaises(runtime.AssistanceCancelled):
                runtime.run_assistance('image.png','scout',.15,.4,cancel_requested=cancelled.is_set)
        stop.assert_called_once_with(process)

    def test_cancel_during_cleanup_cannot_return_a_success(self):
        cancelled = threading.Event()
        process = Mock(returncode=0, poll=Mock(return_value=0))
        def launch(arguments, **kwargs):
            Path(arguments[-2]).write_text('{"success":true,"boxes":[]}')
            return process
        with patch.object(runtime.subprocess, 'Popen', side_effect=launch), \
             patch.object(runtime, '_job_for_child', return_value=None), \
             patch.object(runtime, '_stop_tree', side_effect=lambda child:cancelled.set()):
            with self.assertRaises(runtime.AssistanceCancelled):
                runtime.run_assistance('image.png','stardist',.15,.4,cancel_requested=cancelled.is_set)

    def test_parent_polls_real_status_and_reads_file_result(self):
        seen = []
        stages = []
        def launch(arguments, **kwargs):
            self.assertEqual(Path(arguments[1]).name, 'tempo_gui.py')
            self.assertEqual(arguments[2], '--assist-model')
            Path(arguments[-1]).write_text(json.dumps({'stage': 'inference'}))
            Path(arguments[-2]).write_text(json.dumps({'success': True, 'boxes': []}))
            process = Mock(returncode=0)
            process.poll.side_effect = [None, 0, 0]
            return process
        with patch.object(runtime.subprocess, 'Popen', side_effect=launch), \
             patch.object(runtime, '_job_for_child', return_value=None), \
             patch.object(runtime, '_stop_tree'), \
             patch.object(runtime.time, 'sleep'):
            result = runtime.run_assistance('image.png', 'scout', .15, .4,
                                            progress=seen.append, stage=stages.append)
        self.assertEqual(result['boxes'], [])
        self.assertEqual(seen, ['Running model…'])
        self.assertEqual(stages, ['inference'])

    def test_timeout_stops_child_and_returns_explicit_failure(self):
        process = Mock(returncode=None)
        process.poll.return_value = None
        with patch.object(runtime.subprocess, 'Popen', return_value=process), \
             patch.object(runtime, '_job_for_child', return_value=None), \
             patch.object(runtime.time, 'monotonic', side_effect=[0, 901]), \
             patch.object(runtime, '_stop_tree') as stop:
            with self.assertRaisesRegex(TimeoutError, '15 minutes'):
                runtime.run_assistance('image.png', 'stardist', .15, .4)
        stop.assert_called_once_with(process)

    def test_cuda_child_selected_only_for_cellpose_when_verified(self):
        def launch(arguments, **kwargs):
            self.assertEqual(arguments[0:2], ['C:/runtime/TEMPO.exe', '--assist-model-cuda'])
            Path(arguments[-2]).write_text(json.dumps({'success': True, 'result_kind': 'cells', 'boxes': []}))
            process = Mock(returncode=0)
            process.poll.return_value = 0
            return process
        with patch.object(runtime.sys, 'platform', 'win32'), \
             patch.object(runtime.sys, 'frozen', True, create=True), \
             patch('tempo_nvidia_runtime.active_executable', return_value='C:/runtime/TEMPO.exe'), \
             patch.object(runtime, '_job_for_child', return_value=None), \
             patch.object(runtime, '_stop_tree'), \
             patch.object(runtime.subprocess, 'Popen', side_effect=launch):
            result = runtime.run_assistance('image.png', 'cellpose', .15, .4)
        self.assertTrue(result['success'])

    def test_rosetta_launch_explicitly_requests_arm64(self):
        # Simulating the target OS does not change pathlib's host path flavor.
        with tempfile.TemporaryDirectory(prefix='tempo helper fixture ') as folder:
            root = Path(folder).resolve()
            native = root / '.venv' / 'bin' / 'python'
            native.parent.mkdir(parents=True)
            native.touch()
            def launch(arguments, **kwargs):
                self.assertEqual(arguments[:3], ['arch', '-arm64', str(native)])
                self.assertEqual(arguments[3], str(root / 'tempo_gui.py'))
                self.assertEqual(arguments[4], '--assist-model')
                Path(arguments[-2]).write_text('{"success":true,"boxes":[]}')
                Path(arguments[-1]).write_text('{"stage":"result"}')
                return Mock(returncode=0, poll=Mock(return_value=0))
            with patch.object(runtime.sys, 'platform', 'darwin'), \
                 patch.object(runtime.sys, 'executable', str(root / '.venv_x86' / 'bin' / 'python')), \
                 patch.object(runtime, '__file__', str(root / 'tempo_assistance_runtime.py')), \
                 patch.object(runtime, '_job_for_child', return_value=None), \
                 patch.object(runtime, '_stop_tree'), \
                 patch.object(runtime.subprocess, 'Popen', side_effect=launch):
                self.assertTrue(runtime.run_assistance('fixture.png','scout',.15,.4)['success'])

    def test_early_command_exits_before_desktop_imports(self):
        source = Path(__file__).resolve().parents[1] / 'tempo_gui.py'
        for flag, expected_cuda in (('--assist-model', False), ('--assist-model-cuda', True)):
            with self.subTest(flag=flag), patch.object(sys, 'argv',
                    [str(source), flag, 'image.png', 'cellpose', '.15', '.4', 'out.json', 'state.json']), \
                 patch.object(runtime, 'assist_command', return_value=0) as command:
                with self.assertRaises(SystemExit) as exited:
                    runpy.run_path(str(source), run_name='__main__')
                self.assertEqual(exited.exception.code, 0)
                self.assertEqual(command.call_args.kwargs['require_cuda'], expected_cuda)


if __name__ == '__main__':
    unittest.main()
