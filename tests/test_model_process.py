"""Windows worker ownership ABI and orderly termination, without native apps."""
import ctypes
import subprocess
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import tempo_process as processes
from tempo_process import WindowsJob


class WindowsJobTests(unittest.TestCase):
    def test_headless_worker_timeout_still_closes_its_job_and_process_tree(self):
        process, job = Mock(), Mock()
        process.wait.side_effect = subprocess.TimeoutExpired(['worker'], 3)
        with patch.object(processes, 'os', SimpleNamespace(name='nt')), \
             patch.object(processes, 'job_for_child', return_value=job), \
             patch.object(processes.subprocess, 'Popen', return_value=process) as launch, \
             patch.object(processes, 'stop_tree') as stop:
            with self.assertRaises(subprocess.TimeoutExpired):
                processes.run_model_worker(['worker'], timeout=3)
        self.assertTrue(launch.call_args.kwargs['creationflags'] & 0x4)
        job.start.assert_called_once_with(process)
        job.close.assert_called_once()
        stop.assert_called_once_with(process)

    def test_job_owns_resumed_process_and_waits_for_descendants_before_closing(self):
        kernel = Mock()
        kernel.CreateJobObjectW.return_value = 17
        configured, active = [], [1, 0]
        def configure(handle, kind, pointer, size):
            self.assertEqual((handle, kind), (17, 9))
            self.assertEqual(size, 144)
            configured.append(pointer._obj.basic.flags)
            return True
        def query(handle, kind, pointer, size, returned):
            self.assertEqual((handle, kind, size), (17, 1, 48))
            pointer._obj.active_processes = active.pop(0)
            return True
        kernel.SetInformationJobObject.side_effect = configure
        kernel.QueryInformationJobObject.side_effect = query
        process = SimpleNamespace(_handle=19, pid=23)
        with patch.object(ctypes, 'WinDLL', return_value=kernel, create=True), \
             patch('psutil.Process') as inspect, patch('tempo_process.time.sleep'):
            job = WindowsJob()
            job.start(process)
            inspect.assert_called_once_with(23)
            inspect.return_value.resume.assert_called_once()
            kernel.AssignProcessToJobObject.assert_called_once_with(17, 19)
            self.assertEqual(configured, [0x2000])
            job.close()
            job.close()
        self.assertEqual(active, [])
        kernel.TerminateJobObject.assert_called_once_with(17, 1)
        kernel.CloseHandle.assert_called_once_with(17)
