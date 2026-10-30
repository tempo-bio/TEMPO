"""Headless assistance reports observed stages and bounds stalled child processes."""
import json
from pathlib import Path
import runpy
import sys
import tempfile
import subprocess
import time
import unittest
from unittest.mock import Mock, patch

import tempo_assistance_runtime as runtime


class AssistanceRuntimeTests(unittest.TestCase):
    def test_child_writes_actual_stages_and_result_without_a_desktop(self):
        with tempfile.TemporaryDirectory() as folder:
            result = Path(folder) / 'result.json'
            status = Path(folder) / 'status.json'
            def infer(image, model, **kwargs):
                self.assertEqual((image, model), ('image.png', 'scout'))
                kwargs['progress']('weights')
                kwargs['progress']('image')
                kwargs['progress']('inference')
                return {'success': True, 'result_kind': 'scout', 'boxes': []}
            with patch('yolo_inference_helper.run_inference', side_effect=infer):
                code = runtime.assist_command('image.png', 'scout', '.15', '.4', result, status)
            self.assertEqual(code, 0)
            self.assertEqual(json.loads(result.read_text())['boxes'], [])
            self.assertEqual(json.loads(status.read_text()), {'stage': 'inference'})
            self.assertNotIn('image.png', status.read_text())

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

    def test_failed_parent_cannot_leave_a_running_helper(self):
        real_popen = subprocess.Popen
        with tempfile.TemporaryDirectory() as folder:
            started, finished = Path(folder)/'started', Path(folder)/'finished'
            child = ('import time;from pathlib import Path;Path('+repr(str(started))+').touch();'
                     'time.sleep(0.7);Path('+repr(str(finished))+').touch()')
            parent = ('import subprocess,sys,time;from pathlib import Path;'
                      'subprocess.Popen([sys.executable,"-c",'+repr(child)+']);'
                      'end=time.monotonic()+3\n'
                      'while not Path('+repr(str(started))+').exists() and time.monotonic()<end: time.sleep(.01)\n'
                      'sys.exit(1)')
            def launch(arguments, **kwargs):
                return real_popen([sys.executable, '-c', parent], **kwargs)
            with patch.object(runtime.subprocess, 'Popen', side_effect=launch):
                with self.assertRaisesRegex(RuntimeError, 'valid result'):
                    runtime.run_assistance('fixture.png', 'scout', .15, .4, timeout=5)
            self.assertTrue(started.exists())
            time.sleep(1)
            self.assertFalse(finished.exists())

    def test_rosetta_launch_explicitly_requests_arm64(self):
        def launch(arguments, **kwargs):
            self.assertEqual(arguments[:3], ['arch', '-arm64', '/fixture/.venv/bin/python'])
            Path(arguments[-2]).write_text('{"success":true,"boxes":[]}')
            Path(arguments[-1]).write_text('{"stage":"result"}')
            return Mock(returncode=0, poll=Mock(return_value=0))
        with patch.object(runtime.sys, 'platform', 'darwin'), \
             patch.object(runtime.sys, 'executable', '/fixture/.venv_x86/bin/python'), \
             patch.object(runtime, '__file__', '/fixture/tempo_assistance_runtime.py'), \
             patch.object(runtime.Path, 'is_file', return_value=True), \
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
