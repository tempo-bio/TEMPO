"""Headless assistance reports observed stages and bounds stalled child processes."""
import json
from pathlib import Path
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


if __name__ == '__main__':
    unittest.main()
