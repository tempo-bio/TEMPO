"""Setup/update gates with mocked network and isolated model files."""
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from types import SimpleNamespace
from PyQt6.QtWidgets import QApplication
from PyQt6.QtGui import QFont
import update_wizard as updates
import setup_wizard as setup
from tests.qt_lifecycle import close_and_dispose

APP = QApplication.instance() or QApplication([])


class SetupModelUpdateTests(unittest.TestCase):
    def test_windows_source_setup_uses_the_same_verified_lock_as_the_installer(self):
        worker = setup.SetupWorkerThread(); results = []
        worker.finished_signal.connect(lambda ok, detail: results.append((ok, detail)))
        with patch.object(setup.sys, 'frozen', False, create=True), \
             patch.object(setup.sys, 'prefix', 'synthetic-isolated-environment'), \
             patch.object(setup.sys, 'version_info', (3, 13, 16)), \
             patch.object(setup, 'os', SimpleNamespace(name='nt', path=SimpleNamespace(isdir=lambda path:False))), \
             patch.object(setup.subprocess, 'CREATE_NO_WINDOW', 0, create=True), \
             patch.object(setup.subprocess, 'run') as run:
            worker.run()
        self.assertTrue(results[0][0], results)
        commands = [call.args[0] for call in run.call_args_list]
        self.assertIn(['--no-deps', '--require-hashes', '-r', 'requirements-windows.lock'],
                      [command[4:] for command in commands if command[1:4] == ['-m', 'pip', 'install']])
        self.assertTrue(any(command[1:] == ['-m', 'scripts.windows_native_bundle'] for command in commands))
        self.assertFalse(any('nvidia-smi' in command for command in commands))

    def test_startup_model_probe_cannot_migrate_or_use_plaintext_credentials(self):
        from tempo_secure_store import CredentialMigrationRequired
        with patch.object(updates.tempo_hub, 'ensure_credentials', side_effect=CredentialMigrationRequired('Migration required.')) as load, \
             patch.object(updates.urllib.request, 'urlopen') as request:
            with self.assertRaises(CredentialMigrationRequired):
                updates.model_release_probe()
        load.assert_called_once_with()
        request.assert_not_called()

    def test_missing_secure_storage_dependency_fails_desktop_setup(self):
        worker = setup.SetupWorkerThread(); results = []
        worker.finished_signal.connect(lambda ok, detail: results.append((ok, detail)))
        with patch.object(setup.sys, 'frozen', True, create=True), \
             patch('importlib.util.find_spec', side_effect=lambda name: None if name == 'keyring' else object()), \
             patch.object(setup.subprocess, 'run') as run:
            worker.run()
        self.assertFalse(results[0][0])
        self.assertIn('keyring', results[0][1])
        run.assert_not_called()

    def test_update_commands_disable_hidden_git_credential_prompts(self):
        with patch.object(updates.subprocess, 'run', return_value=SimpleNamespace(returncode=0, stdout='done', stderr='')) as run:
            self.assertEqual(updates.checked_command(['git', 'pull', '--ff-only']), 'done')
        env = run.call_args.kwargs['env']
        self.assertEqual(env['GIT_TERMINAL_PROMPT'], '0')
        self.assertEqual(env['GCM_INTERACTIVE'], 'Never')

    def test_dataset_write_denial_does_not_save_credentials_or_profile(self):
        view = setup.SetupWizardDialog(account_only=True); self.addCleanup(close_and_dispose,view)
        view.txt_collab_id.setText('Researcher'); view.txt_token.setText('test-token')
        with patch('huggingface_hub.HfApi') as api, patch.object(setup, 'save_hf_token') as login, \
             patch.object(setup.tempo_preferences, 'save') as save:
            api.return_value.auth_check.side_effect = PermissionError('dataset write denied')
            view._save_and_finish()
        api.return_value.auth_check.assert_called_once_with(repo_id=setup.tempo_hub.get_dataset_repo(), repo_type='dataset', write=True)
        login.assert_not_called(); save.assert_not_called()
        self.assertIn('dataset write denied', view.account_error.details.toPlainText())

    def test_restart_failure_reports_that_installation_succeeded(self):
        view = updates.SoftwareUpdateWizard(); self.addCleanup(close_and_dispose,view)
        with patch.object(updates, 'perform_software_update', return_value='Installed'), \
             patch.object(updates.os, 'execl', side_effect=OSError('restart unavailable')), \
             patch.object(updates.QMessageBox, 'information') as popup:
            view.run_update()
        popup.assert_not_called()
        self.assertIn('Software was updated', view.error_details.summary.text())
        self.assertIn('restart unavailable', view.error_details.details.toPlainText())

    @unittest.skipIf(os.name == 'nt', 'Checks the non-Windows requirements path')
    def test_desktop_requirements_check_does_not_install_or_probe_nvidia(self):
        worker = setup.SetupWorkerThread(); results = []
        worker.finished_signal.connect(lambda ok, detail: results.append((ok, detail)))
        with patch('importlib.util.find_spec', return_value=object()), \
             patch.object(setup.subprocess, 'run') as run:
            worker.run()
        run.assert_not_called()
        self.assertTrue(results[0][0])
        self.assertIn('checked separately', results[0][1])

    def test_frozen_windows_setup_checks_bundle_without_relaunching_executable_as_pip(self):
        worker = setup.SetupWorkerThread(); results = []
        worker.finished_signal.connect(lambda ok, detail: results.append((ok, detail)))
        with patch.object(setup.sys, 'frozen', True, create=True), \
             patch.object(setup.sys, 'prefix', setup.sys.base_prefix), \
             patch.object(setup, 'os', SimpleNamespace(name='nt')), \
             patch('importlib.util.find_spec', return_value=object()), \
             patch.object(setup.subprocess, 'run') as run:
            worker.run()
        run.assert_not_called()
        self.assertTrue(results[0][0], results)

    def test_frozen_setup_reports_missing_runtime_without_installing_into_executable(self):
        worker = setup.SetupWorkerThread(); results = []
        worker.finished_signal.connect(lambda ok, detail: results.append((ok, detail)))
        with patch.object(setup.sys, 'frozen', True, create=True), \
             patch('importlib.util.find_spec', side_effect=lambda name: None if name == 'cv2' else object()), \
             patch.object(setup.subprocess, 'run') as run:
            worker.run()
        run.assert_not_called()
        self.assertFalse(results[0][0]); self.assertIn('cv2', results[0][1])

    @unittest.skipIf(os.name == 'nt', 'Checks the non-Windows requirements path')
    def test_missing_desktop_package_fails_requirements_check(self):
        worker = setup.SetupWorkerThread(); results = []
        worker.finished_signal.connect(lambda ok, detail: results.append((ok, detail)))
        with patch('importlib.util.find_spec', side_effect=lambda name: None if name == 'cv2' else object()):
            worker.run()
        self.assertFalse(results[0][0])
        self.assertIn('cv2', results[0][1])

    def test_software_check_uses_configured_authenticated_git_remote(self):
        responses = [SimpleNamespace(returncode=0, stdout='main\n'),
                     SimpleNamespace(returncode=0, stdout='newrevision\trefs/heads/main\n'),
                     SimpleNamespace(returncode=0, stdout='oldrevision\n'),
                     SimpleNamespace(returncode=0, stdout='0\t1\n')]
        with patch.object(updates.subprocess, 'run', side_effect=responses) as run:
            found, details = updates.software_release(timeout=5)
        self.assertTrue(found)
        self.assertIn('newrevision', details)
        self.assertEqual(run.call_args_list[0].args[0], ['git', 'branch', '--show-current'])
        self.assertEqual(run.call_args_list[1].args[0], ['git', 'ls-remote', '--exit-code', 'origin', 'refs/heads/main'])
        self.assertEqual(run.call_args_list[0].kwargs['env']['GIT_TERMINAL_PROMPT'], '0')

    def test_private_remote_failure_is_not_reported_up_to_date(self):
        with patch.object(updates.subprocess, 'run', return_value=SimpleNamespace(returncode=128, stdout='')):
            with self.assertRaisesRegex(RuntimeError, 'Git credentials'):
                updates.software_release()

    def setUp(self):
        self.addCleanup(patch.stopall)
        patch.object(setup.tempo_hub, 'publish_collaborator_profile', return_value=True).start()
        patch.object(setup, 'run_background', return_value=None).start()
        patch.object(updates.QTimer, 'singleShot').start()
        patch.object(updates, 'run_task', side_effect=lambda parent, title, action: action()).start()
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        patch.object(updates, 'BASE_DIR', self.temp.name).start()
        patch('tempo_config.get_base_dir', return_value=self.temp.name).start()
        patch('tempo_model_catalog.installed_model_dir', return_value=Path(self.temp.name)/'installed').start()
        self.manifest = {'revision': 'release', 'models': {name: {'sha256': 'a' * 64} for name in updates.MODEL_FILES.values()}}
        patch.object(updates.tempo_hub, 'model_manifest', return_value=self.manifest).start()
        patch.object(updates.tempo_hub, 'calculate_sha256', return_value='a' * 64).start()
        self.view = updates.ModelWeightsUpdateWizard()
        self.addCleanup(close_and_dispose,self.view)

    def test_large_account_text_scrolls_instead_of_clipping_recovery(self):
        view = setup.SetupWizardDialog(account_only=True); self.addCleanup(close_and_dispose,view)
        font = QFont(view.font()); font.setPointSize(18); view.setFont(font)
        view.account_error.set_error(
            'Cloud credentials were saved, but the annotation profile could not be saved. Check folder permissions and save again.',
            PermissionError('Profile is not writable.'))
        view.show(); APP.processEvents()
        view.account_error.toggle.click(); APP.processEvents()
        scroll = view.stack.currentWidget()
        self.assertGreater(scroll.verticalScrollBar().maximum(), 0)
        summary = view.account_error.summary
        self.assertGreaterEqual(summary.height(), summary.heightForWidth(summary.width()))
        buttons = [button for button in view.findChildren(setup.QPushButton) if button.text() == 'Save Account']
        self.assertFalse(scroll.isAncestorOf(buttons[0]))
        self.assertTrue(view.rect().contains(buttons[0].mapTo(view, buttons[0].rect().bottomRight())))

    def test_optional_guide_does_not_save_or_verify_or_replace_the_account_page(self):
        with patch.object(setup.tempo_hub, 'token', None), \
             patch.object(setup, 'save_hf_token') as save_token, \
             patch.object(setup.tempo_preferences, 'save') as save_preferences, \
             patch.object(setup.tempo_hub, 'authenticated_account') as verify:
            view = setup.SetupWizardDialog(account_only=True); self.addCleanup(close_and_dispose,view)
            view._open_invitation_help()
            self.assertIs(view.stack.currentWidget(), view.account_page)
            self.assertIn('Accept the invitation emails', view.guide.browser.toPlainText())
            self.assertEqual(view.result(), setup.QDialog.DialogCode.Rejected)
            view.guide.close()
        save_token.assert_not_called()
        save_preferences.assert_not_called()
        verify.assert_not_called()

    def test_guided_setup_moves_directly_to_account_and_back_preserves_input(self):
        view = setup.SetupWizardDialog(); self.addCleanup(close_and_dispose,view)
        view._on_diag_finished(True, 'Fixture requirements available.')
        view.btn_diag_next.click()
        self.assertIs(view.stack.currentWidget(), view.account_page)
        view.txt_collab_id.setText('Unsaved Laboratory')
        view.btn_account_back.click()
        self.assertIs(view.stack.currentWidget(), view.welcome_page)
        view.btn_diag_next.click()
        self.assertEqual(view.txt_collab_id.text(), 'Unsaved Laboratory')

    def test_current_release_has_no_download_action(self):
        for name in self.manifest['models']:
            (Path(self.temp.name) / name).touch()
        self.view.audit()
        self.assertFalse(self.view.sync.isEnabled())
        self.assertIn('All models are up to date', self.view.log.toPlainText())

    def test_missing_weights_enable_update_and_use_model_names(self):
        self.view.audit()
        self.assertTrue(self.view.sync.isEnabled())
        self.assertIn('Nano Scout', self.view.log.toPlainText())
        self.assertNotIn('Metrics: {', self.view.log.toPlainText())

    def test_partial_release_cannot_be_installed(self):
        self.manifest['models'].pop(next(iter(self.manifest['models'])))
        self.view.audit()
        self.assertFalse(self.view.sync.isEnabled())
        self.assertIsNone(self.view.manifest)
        self.assertIn('incomplete', self.view.error_details.details.toPlainText())

    def test_failed_recheck_clears_previous_installable_release(self):
        self.view.audit()
        with patch.object(updates.tempo_hub, 'model_manifest', side_effect=ConnectionError('offline')):
            self.view.audit()
        self.assertFalse(self.view.sync.isEnabled())
        self.assertIsNone(self.view.manifest)

    def test_failed_model_check_can_retry_in_same_dialog(self):
        with patch.object(updates.tempo_hub, 'model_manifest', side_effect=ConnectionError('offline')):
            self.view.audit()
        self.assertTrue(self.view.retry.isEnabled())
        self.assertFalse(self.view.sync.isEnabled())
        self.assertTrue(self.view.error_details.details.isHidden())
        self.view.error_details.toggle.click()
        self.assertFalse(self.view.error_details.details.isHidden())
        self.assertIn('offline', self.view.error_details.details.toPlainText())
        self.view.retry.click()
        self.assertEqual(self.view.manifest, self.manifest)
        self.assertTrue(self.view.sync.isEnabled())
        self.assertNotIn('offline', self.view.log.toPlainText())
        self.assertTrue(self.view.error_details.isHidden())

    def test_setup_requires_token_before_persisting_profile(self):
        view = setup.SetupWizardDialog(); self.addCleanup(close_and_dispose,view)
        view.txt_collab_id.setText('Researcher'); view.txt_token.clear()
        with patch.object(setup.tempo_hub, 'token', None), patch.object(setup.QMessageBox, 'warning') as warning, patch.object(setup.tempo_preferences, 'save') as save:
            view._save_and_finish()
        save.assert_not_called()
        warning.assert_not_called()
        self.assertIn('token', view.account_error.summary.text())

    def test_software_check_failure_clears_stale_update_and_retries(self):
        view = updates.SoftwareUpdateWizard(); self.addCleanup(close_and_dispose,view)
        with patch.object(updates, 'software_release', return_value=(True, 'New release')):
            view.check_release()
        self.assertTrue(view.btn_update.isEnabled())
        with patch.object(updates, 'software_release', side_effect=ConnectionError('offline')):
            view.check_release()
        self.assertFalse(view.btn_update.isEnabled())
        self.assertTrue(view.error_details.details.isHidden())
        view.error_details.toggle.click()
        self.assertIn('offline', view.error_details.details.toPlainText())
        with patch.object(updates, 'software_release', return_value=(False, '')):
            view.retry.click()
        self.assertTrue(view.error_details.isHidden())
        self.assertIn('up to date', view.log_view.toPlainText())

    def test_failed_software_install_requires_recheck_and_does_not_restart(self):
        view = updates.SoftwareUpdateWizard(); self.addCleanup(close_and_dispose,view)
        with patch.object(updates, 'perform_software_update', side_effect=RuntimeError('Local changes need review')), \
             patch.object(updates.os, 'execl') as restart:
            view.run_update()
        restart.assert_not_called()
        self.assertFalse(view.btn_update.isEnabled())
        self.assertTrue(view.retry.isEnabled())
        self.assertIn('Local changes need review', view.error_details.details.toPlainText())

    def test_setup_installs_whole_release_before_completion_marker(self):
        view = setup.SetupWizardDialog(); self.addCleanup(close_and_dispose,view)
        view.txt_collab_id.setText('Researcher'); view.txt_token.setText('fake-test-token')
        with patch.object(setup, 'run_task', side_effect=lambda parent, title, action: action()), \
             patch('huggingface_hub.HfApi'), patch.object(setup, 'save_hf_token'), \
             patch.dict(os.environ), patch.object(setup.tempo_hub, '_init_credentials'), \
             patch.object(setup.tempo_hub, 'authenticated_account', return_value='hf:fixture'), \
             patch.object(setup.tempo_preferences, 'set'), patch.object(setup.tempo_preferences, 'save', return_value=True), \
             patch.object(setup.tempo_hub, 'download_model_release', side_effect=RuntimeError('checksum mismatch')) as download, \
             patch.object(setup.QMessageBox, 'warning') as warning, patch.object(setup.sys, 'prefix', self.temp.name):
            view._save_and_finish()
            self.assertIs(view.stack.currentWidget(), view.models_page)
            download.assert_not_called()
            view._install_models()
            from PyQt6.QtTest import QTest
            for _ in range(100):
                APP.processEvents()
                if view._model_task is None:
                    break
                QTest.qWait(10)
            self.assertIsNone(view._model_task)
        download.assert_called_once_with(self.manifest)
        self.assertFalse((Path(self.temp.name) / 'tempo_setup_complete').exists())
        warning.assert_not_called()
        self.assertIn('checksum mismatch', view.model_error.details.toPlainText())
        self.assertIn('Your account was saved', view.model_error.summary.text())
        self.assertTrue(view.model_error.details.isHidden())

    def test_account_update_skips_dependency_and_model_setup(self):
        view=setup.SetupWizardDialog(account_only=True); self.addCleanup(close_and_dispose,view)
        self.assertIs(view.stack.currentWidget(), view.account_page)
        view.txt_collab_id.setText('Researcher'); view.txt_token.setText('fake-test-token')
        with patch.object(setup,'run_task',side_effect=lambda parent,title,action:action()), \
             patch('huggingface_hub.HfApi'), patch.object(setup, 'save_hf_token'), \
             patch.dict(os.environ), patch.object(setup.tempo_hub,'_init_credentials'), \
             patch.object(setup.tempo_hub, 'authenticated_account', return_value='hf:fixture'), \
             patch.object(setup.tempo_preferences,'set'), patch.object(setup.tempo_preferences,'save',return_value=True), \
             patch.object(setup.tempo_hub,'download_model_release') as download, \
             patch.object(setup.SetupWorkerThread,'start') as install, \
             patch.object(setup.sys,'prefix',self.temp.name):
            view._save_and_finish()
        download.assert_not_called(); install.assert_not_called()
        self.assertFalse((Path(self.temp.name)/'tempo_setup_complete').exists())
        self.assertEqual(view.result(),setup.QDialog.DialogCode.Accepted)

    def test_profile_save_failure_reports_partial_save_and_allows_retry(self):
        view = setup.SetupWizardDialog(account_only=True); self.addCleanup(close_and_dispose,view)
        view.txt_collab_id.setText('Researcher'); view.txt_token.setText('fake-test-token')
        saved = []; view.account_saved.connect(lambda: saved.append(True))
        with patch.object(setup, 'run_task', side_effect=lambda parent, title, action: action()), \
             patch('huggingface_hub.HfApi'), patch.object(setup, 'save_hf_token'), \
             patch.object(setup.tempo_hub, 'token', 'fake-test-token'), \
             patch.dict(os.environ), patch.object(setup.tempo_hub, '_init_credentials'), \
             patch.object(setup.tempo_hub, 'authenticated_account', return_value='hf:fixture'), \
             patch.object(setup.tempo_preferences, 'set'), \
             patch.object(setup.tempo_preferences, 'save', side_effect=[False, True]):
            view._save_and_finish()
            self.assertEqual(saved, [])
            self.assertIn('Cloud credentials were saved', view.account_error.summary.text())
            self.assertNotEqual(view.result(), setup.QDialog.DialogCode.Accepted)
            view._save_and_finish()
        self.assertEqual(saved, [True])
        self.assertTrue(view.account_error.isHidden())
        self.assertEqual(view.result(), setup.QDialog.DialogCode.Accepted)
