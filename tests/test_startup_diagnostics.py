"""Startup evidence records outcomes and locations without local values."""
import json
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import tempo_startup_diagnostics as diagnostics


class StartupDiagnosticsTests(unittest.TestCase):
    def test_progress_lock_owner_matches_thread_and_inspection_does_not_acquire(self):
        lock = threading.RLock()
        class Progress:
            _lock = lock
        with patch.dict(diagnostics.sys.modules, {'tqdm.std': SimpleNamespace(tqdm=Progress)}):
            with lock:
                report = diagnostics.progress_locks()[0]['lock']
                self.assertEqual(report['owner_thread_id'], threading.get_ident())
                self.assertEqual(report['count'], 1)
        self.assertFalse(lock._is_owned())

    def test_report_has_outcomes_and_stacks_without_exception_messages_or_locals(self):
        secret_value = 'private-token-and-settings-sentinel'
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder)/'report.json'
            recorder = diagnostics.StartupRecorder(path)
            recorder.record('settings.download.response', status=200, count=120)
            recorder.record('settings.refresh.failed', error=ValueError(secret_value))
            recorder.snapshot()
            payload = path.read_text()
            report = json.loads(payload)
            self.assertNotIn(secret_value, payload)
            self.assertEqual(report['events'][0]['http_status'], 200)
            self.assertEqual(report['events'][0]['count'], 120)
            self.assertEqual(report['events'][1]['error_type'], 'ValueError')
            self.assertTrue(report['snapshots'][0]['threads'])
            self.assertEqual(list(Path(folder).iterdir()), [path])

    def test_opt_in_removes_only_diagnostic_arguments_and_can_stop(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(diagnostics, '_recorder', None):
            arguments = ['TEMPO.exe', '--diagnose-startup', str(Path(folder)/'report.json')]
            diagnostics.start_from_arguments(arguments)
            recorder = diagnostics._recorder
            try:
                self.assertEqual(arguments, ['TEMPO.exe'])
                diagnostics.record('dashboard.shown')
                diagnostics.finish()
                recorder.thread.join(timeout=1)
                self.assertFalse(recorder.thread.is_alive())
            finally:
                recorder.stop()

    def test_unavailable_output_does_not_interrupt_application_callbacks(self):
        with patch.object(diagnostics, '_recorder') as recorder:
            recorder.record.side_effect = PermissionError('unavailable report')
            diagnostics.record('settings.apply.completed')
