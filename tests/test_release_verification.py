"""Prevent the Windows packaging check from launching macOS GUI fixtures."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import tempo_cloud
import tempo_release
from tempo_release import verify_installation


class ReleaseVerificationTests(unittest.TestCase):
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
