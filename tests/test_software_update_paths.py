"""Source and packaged updates use the correct platform and execution path."""
import copy
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import Mock, patch

from PyQt6.QtCore import QCoreApplication, QEvent
from PyQt6.QtWidgets import QApplication
import tempo_config as config
import setup_wizard as setup
import update_wizard as updates
with patch('platform.machine', return_value='x86_64'):
    import tempo_gui as gui

APP = QApplication.instance() or QApplication([])


class SoftwareUpdatePathTests(unittest.TestCase):

    def setUp(self):
        background = patch.object(setup, 'run_background', return_value=None)
        background.start(); self.addCleanup(background.stop)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.preferences = copy.deepcopy(config.DEFAULT_PREFERENCES)
        for name, value in [('CONFIG_DIR', str(self.root)), ('CONFIG_PATH', str(self.root/'preferences.json'))]:
            p = patch.object(config, name, value); p.start(); self.addCleanup(p.stop)
        p = patch.object(config.tempo_preferences, '_preferences', self.preferences)
        p.start(); self.addCleanup(p.stop)
        p = patch.object(config.tempo_preferences, 'load_error', '')
        p.start(); self.addCleanup(p.stop)

    def dispose(self, view):
        view.close(); view.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        APP.processEvents()

    def test_recovery_branch_update_probe_does_not_contact_remote_or_change_checkout(self):
        with patch.object(updates.subprocess, 'run', return_value=SimpleNamespace(
                returncode=0, stdout='recovery/quit-and-fix-list\n')) as run:
            found, details = updates.software_release()
        self.assertFalse(found); self.assertIn('checkout was left unchanged', details)
        self.assertEqual(len(run.call_args_list), 1)
        self.assertEqual(run.call_args.args[0], ['git', 'branch', '--show-current'])

    def test_packaged_updater_opens_releases_without_git_pip_or_restart(self):
        with patch.object(updates.sys, 'frozen', True, create=True), \
             patch.object(updates.QTimer, 'singleShot'), \
             patch.object(updates, 'run_read_task') as read, \
             patch.object(updates, 'run_task') as write, \
             patch.object(updates.QDesktopServices, 'openUrl', return_value=True) as browser, \
             patch.object(updates.os, 'execl') as restart, \
             patch.object(updates, 'checked_command') as command:
            view = updates.SoftwareUpdateWizard(); self.addCleanup(self.dispose, view)
            view.check_release()
            self.assertEqual(view.btn_update.text(), 'Open Releases')
            self.assertTrue(view.btn_update.isEnabled())
            view.run_update()
            browser.assert_called_once()
            self.assertEqual(browser.call_args.args[0].toString(), updates.SOFTWARE_RELEASES_URL)
            read.assert_not_called(); write.assert_not_called(); restart.assert_not_called()
            with self.assertRaisesRegex(RuntimeError, 'release installer'):
                updates.perform_software_update()
            command.assert_not_called()

    def test_windows_source_update_uses_windows_runtime_requirements(self):
        revisions = iter(('before', 'after'))
        def execute(command):
            if command == ['git', 'rev-parse', 'HEAD']:
                return next(revisions)
            return 'main' if command == ['git', 'branch', '--show-current'] else ''
        with patch.object(updates, 'checked_command', side_effect=execute) as command, \
             patch.object(updates, 'os', SimpleNamespace(name='nt')), \
             patch.object(updates.sys, 'frozen', False, create=True):
            updates.perform_software_update()
        self.assertIn([updates.sys.executable, '-m', 'pip', 'install', '-r', 'requirements-windows.txt'],
                      [call.args[0] for call in command.call_args_list])

    def test_no_change_does_not_restart_or_leave_update_enabled(self):
        with patch.object(updates.QTimer, 'singleShot'), \
             patch.object(updates, 'run_task', return_value=None), \
             patch.object(updates, 'run_read_task', return_value=(False, 'This copy is newer than the published version.')), \
             patch.object(updates.os, 'execl') as restart:
            view = updates.SoftwareUpdateWizard(); self.addCleanup(self.dispose, view)
            view.btn_update.setEnabled(True)
            view.run_update()
            restart.assert_not_called()
            self.assertFalse(view.btn_update.isEnabled())
            self.assertTrue(view.retry.isEnabled())
            self.assertIn('newer than', view.log_view.toPlainText())

    def test_startup_offers_plain_text_software_update_without_markdown_dependency(self):
        callbacks = []
        splash = Mock(update_available=True, update_details='Available software revision: new123',
                      software_update_available=True, model_update_available=False)
        window = Mock()
        def event_loop():
            while callbacks:
                callbacks.pop(0)()
            return 0
        old_quit = APP.quitOnLastWindowClosed()
        try:
            with patch.object(updates, 'SplashScreen', return_value=splash), \
                 patch.object(gui, 'TempoDashboard', return_value=window), \
                 patch.object(updates, 'SoftwareUpdateWizard') as software, \
                 patch.object(updates, 'ModelWeightsUpdateWizard') as models, \
                 patch.object(gui.QTimer, 'singleShot', side_effect=lambda delay, action: callbacks.append(action)), \
                 patch.object(APP, 'exec', side_effect=event_loop), \
                 patch('tempo_tasks.dispose_closed_views'):
                self.assertEqual(gui.run_desktop_application(APP), 0)
                software.assert_called_once_with(window, commit_details=splash.update_details)
                software.return_value.exec.assert_called_once()
                models.assert_not_called()
        finally:
            APP.setQuitOnLastWindowClosed(old_quit)

    def test_splash_retains_explicit_update_types(self):
        with patch.object(updates.StartupUpdateCheck, 'start'), patch.object(updates.QTimer, 'singleShot'):
            splash = updates.SplashScreen(); self.addCleanup(self.dispose, splash)
            splash.update_check._results = {'software': (True, 'Plain text revision'), 'models': False}
            splash._finished(True, 'Plain text revision')
            self.assertTrue(splash.software_update_available)
            self.assertFalse(splash.model_update_available)
            splash.update_check._results = {'software': (False, 'Current'), 'models': True}
            splash._finished(True, 'Models')
            self.assertFalse(splash.software_update_available)
            self.assertTrue(splash.model_update_available)

    def test_windows_source_setup_installs_windows_model_requirements_with_deadline(self):
        worker = setup.SetupWorkerThread(); outcomes = []
        worker.finished_signal.connect(lambda success, message: outcomes.append((success, message)))
        with patch.object(setup.sys, 'frozen', False, create=True), \
             patch.object(setup.sys, 'version_info', (3, 13, 16)), \
             patch.object(setup, 'os', SimpleNamespace(name='nt', path=SimpleNamespace(isdir=lambda path: False))), \
             patch.object(setup.subprocess, 'CREATE_NO_WINDOW', 0, create=True), \
             patch.object(setup.subprocess, 'run', return_value=SimpleNamespace(returncode=0)) as run:
            worker.run()
        installs = [call for call in run.call_args_list if call.args[0][1:4] == ['-m', 'pip', 'install']]
        self.assertTrue(outcomes[-1][0], outcomes)
        self.assertTrue(any('requirements-windows.lock' in call.args[0] for call in installs))
        self.assertTrue(all('--require-hashes' in call.args[0] and '--no-deps' in call.args[0] for call in installs))
        self.assertFalse(any('requirements-training.txt' in call.args[0] for call in installs))
        self.assertTrue(all(call.kwargs.get('timeout') == 900 for call in installs))


@unittest.skipUnless(shutil.which('git'), 'Git is required for source update tests')
class SoftwareHistoryTests(unittest.TestCase):
    """Use real commit graphs and a local remote; never contact GitHub."""

    def setUp(self):
        temporary = tempfile.TemporaryDirectory(); self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.remote = self.root/'origin.git'; self.publisher = self.root/'publisher'; self.checkout = self.root/'checkout'
        self.git(self.root, 'init', '--bare', '-q', str(self.remote))
        self.git(self.root, 'init', '-q', '--initial-branch=main', str(self.publisher))
        (self.publisher/'source.txt').write_text('original\n')
        self.git(self.publisher, 'add', 'source.txt'); self.commit(self.publisher, 'Original release')
        self.git(self.publisher, 'remote', 'add', 'origin', str(self.remote))
        self.git(self.publisher, 'push', '-qu', 'origin', 'main')
        self.git(self.root, 'clone', '-q', '--branch', 'main', str(self.remote), str(self.checkout))
        patched = patch.object(updates, 'BASE_DIR', str(self.checkout)); patched.start(); self.addCleanup(patched.stop)
        patched = patch.object(updates.sys, 'frozen', False, create=True); patched.start(); self.addCleanup(patched.stop)

    @staticmethod
    def git(root, *arguments):
        # Allow for Git process startup and local disk contention on Windows CI.
        # This bounds fixture setup only; updater deadlines remain unchanged.
        result = subprocess.run(['git', '-c', 'user.name=Fixture', '-c', 'user.email=fixture@example.invalid',
                                 '-c', 'commit.gpgsign=false', *arguments],
                                cwd=root, capture_output=True, text=True, check=True, timeout=30)
        return result.stdout.strip()

    def commit(self, root, message):
        self.git(root, 'commit', '--allow-empty', '-qm', message)
        return self.git(root, 'rev-parse', 'HEAD')

    def publish(self):
        revision = self.commit(self.publisher, 'New release')
        self.git(self.publisher, 'push', '-q', 'origin', 'main')
        return revision

    def test_same_revision_has_no_update(self):
        found, details = updates.software_release(timeout=10)
        self.assertFalse(found); self.assertIn('up to date', details)

    def test_local_commits_do_not_trigger_repeated_update_prompts(self):
        self.commit(self.checkout, 'Local improvement')
        self.commit(self.checkout, 'Another local improvement')
        for _ in range(2):
            found, details = updates.software_release(timeout=10)
            self.assertFalse(found); self.assertIn('newer than', details)

    def test_new_remote_history_is_detected_without_changing_local_work(self):
        previous = self.git(self.checkout, 'rev-parse', 'HEAD'); published = self.publish()
        source = self.checkout/'source.txt'; source.write_text('unsaved local edit\n')
        fetch_head = self.checkout/'.git/FETCH_HEAD'; fetch_head.write_text('preserve this fetch record\n')
        found, details = updates.software_release(timeout=10)
        self.assertTrue(found); self.assertIn(published[:12], details)
        self.assertEqual(self.git(self.checkout, 'rev-parse', 'HEAD'), previous)
        self.assertEqual(source.read_text(), 'unsaved local edit\n')
        self.assertEqual(fetch_head.read_text(), 'preserve this fetch record\n')
        self.assertEqual(self.git(self.checkout, 'branch', '--show-current'), 'main')

    def test_divergent_history_does_not_offer_an_automatic_update(self):
        previous = self.commit(self.checkout, 'Local improvement'); self.publish()
        found, details = updates.software_release(timeout=10)
        self.assertFalse(found); self.assertIn('merged manually', details)
        self.assertNotIn('up to date', details)
        self.assertEqual(self.git(self.checkout, 'rev-parse', 'HEAD'), previous)

    def test_real_update_advances_to_published_commit_and_preserves_local_files(self):
        published = self.publish()
        (self.checkout/'source.txt').write_text('unsaved edit\n')
        (self.checkout/'new.txt').write_text('new local file\n')
        commands = []
        execute = updates.checked_command
        def install(command):
            commands.append(command)
            if command[1:4] == ['-m', 'pip', 'install']:
                return ''  # Never modify the test interpreter's packages.
            return execute(command)
        with patch.object(updates, 'checked_command', side_effect=install), \
             patch.object(updates.sys, 'prefix', str(self.root/'venv')):
            message = updates.perform_software_update()
        self.assertIn('updated', message)
        self.assertEqual(self.git(self.checkout, 'rev-parse', 'HEAD'), published)
        self.assertEqual((self.checkout/'source.txt').read_text(), 'unsaved edit\n')
        self.assertEqual((self.checkout/'new.txt').read_text(), 'new local file\n')
        self.assertEqual(sum(command[1:4] == ['-m', 'pip', 'install'] for command in commands), 1)
        self.assertFalse(updates.software_release(timeout=10)[0])

    def test_no_op_update_preserves_local_commits_and_uncommitted_files(self):
        previous = self.commit(self.checkout, 'Local improvement')
        (self.checkout/'source.txt').write_text('unsaved edit\n')
        (self.checkout/'new.txt').write_text('new local file\n')
        before = self.git(self.checkout, 'status', '--porcelain')
        with patch.object(updates, 'checked_command', wraps=updates.checked_command) as command, \
             patch.object(updates.sys, 'prefix', str(self.root/'venv')):
            self.assertIsNone(updates.perform_software_update())
        self.assertEqual(self.git(self.checkout, 'rev-parse', 'HEAD'), previous)
        self.assertEqual(self.git(self.checkout, 'status', '--porcelain'), before)
        self.assertEqual((self.checkout/'source.txt').read_text(), 'unsaved edit\n')
        self.assertEqual((self.checkout/'new.txt').read_text(), 'new local file\n')
        self.assertFalse(any('-m' in call.args[0] and 'pip' in call.args[0] for call in command.call_args_list))
        self.assertFalse(updates.software_release(timeout=10)[0])


if __name__ == "__main__":
    unittest.main()
