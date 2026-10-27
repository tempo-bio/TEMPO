"""Prevent the Windows packaging check from launching macOS GUI fixtures."""
import json
import os
import platform
import subprocess
import sys
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import tempo_cloud
import tempo_release
from tempo_release import verify_installation


def deferred_dashboard_probe(release_worker):
    """Exercise the release probe in an isolated Qt process with real close guards."""
    import threading
    import time
    from PyQt6 import sip
    from PyQt6.QtCore import QTimer
    from PyQt6.QtWidgets import QApplication
    from tempo_tasks import dispose_closed_views, dispose_application
    with patch('platform.machine',return_value='x86_64'):
        import tempo_gui as gui
    gate=threading.Event();entered=threading.Event();views=[];accepted=[];scheduled=[];close_started=[]
    original=gui.TempoDashboard
    class DeferredDashboard(original):
        def __init__(self):
            super().__init__();views.append(self)
            controls=self.tab_capture.capture_controls
            controls._profile_loaded=True
            def work():
                entered.set();gate.wait(10)
            controls._async(work)
            assert entered.wait(1)
        def closeEvent(self,event):
            if not close_started:close_started.append(time.monotonic())
            super().closeEvent(event)
            accepted.append(event.isAccepted())
            if not event.isAccepted() and release_worker and not scheduled:
                scheduled.append(True);QTimer.singleShot(100,gate.set)
    try:
        with patch.object(gui,'TempoDashboard',DeferredDashboard):
            if release_worker:
                result=tempo_release.probe_action('offscreen Qt startup and close')
                assert result['application_disposed']
                assert accepted[0] is False and accepted[-1] is True
                assert QApplication.instance() is None
            else:
                try:tempo_release.probe_action('offscreen Qt startup and close')
                except RuntimeError as error:assert 'shutdown did not complete' in str(error)
                else:raise AssertionError('A pending worker must prevent certification.')
                assert close_started and time.monotonic()-close_started[0]>=4.5
    finally:
        # The failure path must release its simulated worker before Qt teardown.
        gate.set();application=QApplication.instance()
        if application is not None:
            for view in views:
                if sip.isdeleted(view):continue
                task=view.tab_capture.capture_controls._task
                if task is not None:assert task.wait(2000)
                for _ in range(20):application.processEvents()
                assert not sip.isdeleted(view), 'Probe retry continued after the deadline.'
                assert view.close();dispose_closed_views(view)
            dispose_application(application)
    print('DEFERRED DASHBOARD PROBE PASSED')


class ReleaseVerificationTests(unittest.TestCase):
    def run_deferred_dashboard(self,release_worker):
        command=[sys.executable,'-X','utf8','-c',
                 'from tests.test_release_verification import deferred_dashboard_probe; '
                 f'deferred_dashboard_probe({release_worker!r})']
        if sys.platform=='darwin':command=['arch','-'+platform.machine(),*command]
        result=subprocess.run(command,cwd=Path(__file__).resolve().parents[1],
                              env=dict(os.environ,QT_QPA_PLATFORM='offscreen',HF_HUB_OFFLINE='1'),
                              text=True,encoding='utf-8',capture_output=True,timeout=40)
        self.assertEqual(result.returncode,0,result.stdout+'\n'+result.stderr)
        self.assertIn('DEFERRED DASHBOARD PROBE PASSED',result.stdout)

    def test_dashboard_probe_waits_for_guarded_worker_before_disposal(self):
        self.run_deferred_dashboard(True)

    def test_dashboard_probe_keeps_deadline_when_worker_does_not_settle(self):
        self.run_deferred_dashboard(False)

    def test_secure_credential_store_failure_prevents_windows_release_certification(self):
        def probe(name, destination):
            return dict(check=name, success=name != 'keyring', error='Native credential storage unavailable', details={})
        with tempfile.TemporaryDirectory() as directory, patch.object(tempo_release.sys, 'platform', 'win32'), \
             patch.object(tempo_release, 'run_probe', side_effect=probe):
            report = Path(directory) / 'verification.json'
            self.assertEqual(verify_installation(report), 1)
            result = json.loads(report.read_text())
        self.assertFalse(result['success'])
        self.assertIn('keyring', result['required_checks'])
        self.assertNotIn('keyring', result['checks'])
        self.assertIn('Native credential storage unavailable', result['error'])

    def test_intermittent_dashboard_shutdown_failure_prevents_certification(self):
        attempts = []
        seen = []
        def probe(name, destination):
            seen.append(name)
            success = True
            if name == 'offscreen Qt startup and close':
                attempts.append(destination)
                success = len(attempts) != 4
            return dict(check=name, success=success, error='native shutdown crash', details={})
        with tempfile.TemporaryDirectory() as directory, patch.object(tempo_release.sys,'platform','win32'), patch.object(tempo_release,'run_probe',side_effect=probe):
            report = Path(directory)/'verification.json'
            self.assertEqual(verify_installation(report), 1)
            result = json.loads(report.read_text())
        self.assertEqual(len(set(attempts)), 5)
        self.assertNotIn('offscreen Qt startup and close', result['checks'])
        self.assertEqual(result['failures'][0]['attempt'], 4)
        self.assertEqual(set(seen), set(tempo_release.REQUIRED_CHECKS))

    def test_windows_verification_reports_plot_probe_failure(self):
        def probe(name,destination):
            return dict(check=name,success=name != 'offscreen plot resize and disposal',error='plot probe failed',details={})
        with tempfile.TemporaryDirectory() as directory, patch.object(tempo_release.sys,'platform','win32'), patch.object(tempo_release,'run_probe',side_effect=probe):
            report=Path(directory)/'verification.json'
            self.assertEqual(verify_installation(report),1)
            result=json.loads(report.read_text())
            self.assertFalse(result['success'])
            self.assertIn('plot probe failed',result['error'])
            self.assertNotIn('offscreen plot resize and disposal',result['checks'])

    def test_non_windows_verification_stops_before_runtime_or_gui_imports(self):
        with tempfile.TemporaryDirectory() as directory, patch('tempo_release.sys.platform', 'darwin'), \
             patch('tempo_release.importlib.import_module') as load:
            report = Path(directory)/'verification.json'
            self.assertEqual(verify_installation(report), 1)
            load.assert_not_called()
            result = json.loads(report.read_text())
            self.assertFalse(result['success'])
            self.assertIn('Windows-only', result['error'])


if __name__ == '__main__':
    unittest.main()
