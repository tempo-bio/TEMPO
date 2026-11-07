"""Read-only installation detection and lifecycle routing."""
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch
import sys

import tempo_update_installation as installation


class InstallationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = (Path(temporary.name) / 'custom location').resolve()
        self.current = self.root / 'current'
        self.current.mkdir(parents=True)
        self.executable = self.current / 'TEMPO.exe'
        self.executable.touch()
        (self.root/'Update.exe').touch()
        (self.current/'sq.version').write_text(
            '<package xmlns="urn:nuget"><metadata><id>TEMPO.Bio.Desktop</id>'
            '<version>1.0.2</version></metadata></package>')

    def detect(self):
        with patch.object(sys, 'platform', 'win32'), patch.object(sys, 'frozen', True, create=True), \
             patch.object(sys, 'executable', str(self.executable)):
            return installation.detect_installation()

    def test_identifies_package_and_rejects_invalid_or_missing_identity(self):
        found = self.detect()
        self.assertEqual((found.kind, found.root, found.uninstaller),
                         ('velopack', self.root, self.root/'Update.exe'))
        manifest = self.current/'sq.version'
        for xml in ('<package><metadata><id>other</id></metadata></package>',
                    '<package><metadata><id>TEMPO.Bio.Desktop</id><id>other</id></metadata></package>',
                    '<!DOCTYPE package><package/>', 'broken'):
            manifest.write_text(xml)
            self.assertEqual(self.detect().kind, 'unknown_frozen')
        manifest.unlink()
        self.assertEqual(self.detect().kind, 'unknown_frozen')

    def test_inno_source_and_unknown_are_distinct(self):
        (self.current/'unins000.exe').touch()
        self.assertEqual(self.detect().kind, 'inno')
        with patch.object(sys, 'frozen', False, create=True):
            self.assertEqual(installation.detect_installation().kind, 'source')
        with patch.object(sys, 'platform', 'darwin'), patch.object(sys, 'frozen', True, create=True):
            self.assertEqual(installation.detect_installation().kind, 'unknown_frozen')

    def test_lifecycle_invokes_sdk_without_auto_apply_and_workers_skip_it(self):
        app = Mock()
        app.set_auto_apply_on_startup.return_value = app
        app.set_args.return_value = app
        factory = Mock(return_value=app)
        with patch.object(installation, 'detect_installation', return_value=installation.Installation('velopack')), \
             patch.dict(sys.modules, {'velopack': SimpleNamespace(App=factory)}):
            self.assertTrue(installation.initialize_velopack(['TEMPO.exe', '--veloapp-install', '1.0.2']))
            app.set_auto_apply_on_startup.assert_called_once_with(False)
            app.set_args.assert_called_once_with(['--veloapp-install', '1.0.2'])
            app.run.assert_called_once_with()
            app.reset_mock(); factory.reset_mock()
            self.assertFalse(installation.initialize_velopack(['TEMPO.exe', '--probe-cuda', 'report']))
            factory.assert_not_called()

    def test_other_installations_do_not_import_sdk(self):
        with patch.object(installation, 'detect_installation', return_value=installation.Installation('inno')), \
             patch.dict(sys.modules, {'velopack': None}):
            self.assertFalse(installation.initialize_velopack(['TEMPO.exe', '--veloapp-uninstall', '1.0.2']))

    def test_normal_startup_defers_sdk_cleanup_until_all_leases_are_held(self):
        from tempo_update_lock import application_lock, installation_lock, update_lock
        profile = self.root / 'profile'
        app = Mock()
        app.set_auto_apply_on_startup.return_value = app
        app.set_args.return_value = app
        observed = []
        def cleanup():
            for lease in (application_lock(profile), installation_lock(self.root), update_lock(self.root / 'packages')):
                with self.assertRaises(BlockingIOError):
                    with lease:
                        pass
            observed.append('guarded')
        app.run.side_effect = cleanup
        previous = installation._application_lease
        try:
            with patch.object(installation, 'detect_installation', return_value=installation.Installation('velopack', self.root)), \
                 patch.dict(sys.modules, {'velopack': SimpleNamespace(App=Mock(return_value=app))}), \
                 patch('tempo_config.get_config_dir', return_value=profile / 'config'), \
                 patch('tempo_config.get_base_dir', return_value=self.current), \
                 patch('tempo_update_bootstrap.bootstrap_packaged_storage'), \
                 patch.object(installation.atexit, 'register'):
                self.assertFalse(installation.initialize_velopack(['TEMPO.exe']))
                app.run.assert_not_called()
                installation.prepare_packaged_startup(['TEMPO.exe'])
            self.assertEqual(observed, ['guarded'])
        finally:
            if installation._application_lease is not previous:
                installation._application_lease.__exit__(None, None, None)
            installation._application_lease = previous

    def test_packager_probe_runs_sdk_before_an_installed_layout_exists(self):
        app = Mock()
        app.set_auto_apply_on_startup.return_value = app
        app.set_args.return_value = app
        with patch.object(sys, 'platform', 'win32'), patch.object(sys, 'frozen', True, create=True), \
             patch.object(installation, 'detect_installation', return_value=installation.Installation('unknown_frozen')), \
             patch.dict(sys.modules, {'velopack': SimpleNamespace(App=Mock(return_value=app))}):
            self.assertTrue(installation.initialize_velopack(['TEMPO.exe', '--veloapp-install', '1.0.2']))
            app.run.assert_called_once()

    def test_incomplete_current_layout_does_not_bypass_startup_protection(self):
        with patch.object(sys, 'platform', 'win32'), patch.object(sys, 'frozen', True, create=True), \
             patch.object(sys, 'executable', str(self.executable)), \
             patch.object(installation, 'detect_installation', return_value=installation.Installation('unknown_frozen')):
            with self.assertRaisesRegex(RuntimeError, 'incomplete'):
                installation.prepare_packaged_startup(['TEMPO.exe'])


if __name__ == '__main__':
    unittest.main()
