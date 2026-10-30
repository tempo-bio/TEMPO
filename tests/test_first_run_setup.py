"""Packaged onboarding handoff with isolated per-user state and no services."""
import copy
import json
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from PyQt6 import sip
from PyQt6.QtCore import QTimer
from PyQt6.QtWidgets import QApplication, QDialog

import setup_wizard as setup
import tempo_first_run as first_run
from tempo_config import DEFAULT_PREFERENCES
from tests.qt_lifecycle import close_and_dispose, wait_for_qt

APP = QApplication.instance() or QApplication([])
ROOT = Path(__file__).resolve().parents[1]


class IsolatedSetupState:
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        location = patch.object(first_run, 'get_config_dir', return_value=str(self.root/'profile'))
        location.start(); self.addCleanup(location.stop)


class FirstRunStateTests(IsolatedSetupState, unittest.TestCase):
    def test_only_valid_completed_user_state_skips_packaged_setup(self):
        self.assertEqual(first_run.setup_stage(), 'welcome')
        for document in ({'schema_version': 1, 'stage': 'models'},
                         {'schema_version': True, 'stage': 'complete'},
                         {'schema_version': 2, 'stage': 'complete'},
                         {'schema_version': 1, 'stage': {}}, []):
            path = first_run.setup_state_path()
            path.parent.mkdir(exist_ok=True)
            path.write_text(json.dumps(document))
            self.assertNotEqual(first_run.setup_stage(), 'complete')
        path.write_text('{broken')
        self.assertEqual(first_run.setup_stage(), 'welcome')
        first_run.save_setup_stage('complete')
        self.assertEqual(first_run.setup_stage(), 'complete')

    def test_partial_progress_does_not_change_preferences_or_installation_marker(self):
        installation = self.root/'installation'
        installation.mkdir()
        marker = installation/'tempo_setup_complete'
        marker.write_text('legacy source marker')
        path = first_run.setup_state_path()
        path.parent.mkdir()
        preferences = path.parent/'tempo_preferences.json'
        preferences.write_text('{"personal": "preserved"}')
        with patch.object(first_run.sys, 'prefix', str(installation)):
            self.assertEqual(first_run.setup_stage(), 'welcome')
            first_run.save_setup_stage('models')
        self.assertEqual(first_run.setup_stage(), 'models')
        self.assertEqual(marker.read_text(), 'legacy source marker')
        self.assertEqual(preferences.read_text(), '{"personal": "preserved"}')
        self.assertFalse(list(path.parent.glob('*.tmp')))

    def test_failed_state_replace_preserves_previous_progress_and_removes_temporary(self):
        first_run.save_setup_stage('account')
        with patch.object(first_run.os, 'replace', side_effect=PermissionError('Read-only profile')):
            with self.assertRaises(PermissionError):
                first_run.save_setup_stage('complete')
        self.assertEqual(first_run.setup_stage(), 'account')
        self.assertFalse(list(first_run.setup_state_path().parent.glob('*.tmp')))

    def test_manual_guided_setup_cannot_downgrade_completed_state(self):
        first_run.save_setup_stage('complete')
        for stage in ('welcome', 'account', 'models'):
            first_run.save_setup_stage(stage)
            self.assertEqual(first_run.setup_stage(), 'complete')

    def test_packaged_cancel_disposes_dialog_without_destroying_application(self):
        wizard = QDialog()
        QTimer.singleShot(0, wizard.reject)
        previous = APP.quitOnLastWindowClosed()
        with patch.object(first_run.sys, 'frozen', True, create=True), \
             patch.object(setup, 'SetupWizardDialog', return_value=wizard) as create:
            self.assertFalse(first_run.run_packaged_setup(APP))
        create.assert_called_once_with()
        self.assertTrue(sip.isdeleted(wizard))
        self.assertIs(QApplication.instance(), APP)
        self.assertEqual(APP.quitOnLastWindowClosed(), previous)
        self.assertEqual(first_run.setup_stage(), 'welcome')

    def test_packaged_completion_hands_off_once_and_later_launch_skips_dialog(self):
        wizard = QDialog()
        def finish():
            first_run.save_setup_stage('complete')
            wizard.accept()
        QTimer.singleShot(0, finish)
        with patch.object(first_run.sys, 'frozen', True, create=True), \
             patch.object(setup, 'SetupWizardDialog', return_value=wizard) as create:
            self.assertTrue(first_run.run_packaged_setup(APP))
            self.assertTrue(first_run.run_packaged_setup(APP))
        create.assert_called_once_with()
        self.assertTrue(sip.isdeleted(wizard))
        self.assertIs(QApplication.instance(), APP)

    def test_accepting_uncompleted_dialog_does_not_count_as_setup_completion(self):
        wizard = QDialog()
        QTimer.singleShot(0, wizard.accept)
        with patch.object(first_run.sys, 'frozen', True, create=True), \
             patch.object(setup, 'SetupWizardDialog', return_value=wizard):
            self.assertFalse(first_run.run_packaged_setup(APP))
        self.assertEqual(first_run.setup_stage(), 'welcome')

    def test_source_launch_does_not_enter_packaged_onboarding(self):
        with patch.object(first_run.sys, 'frozen', False, create=True), \
             patch.object(setup, 'SetupWizardDialog') as create:
            self.assertTrue(first_run.run_packaged_setup(APP))
        create.assert_not_called()


class PackagedWizardTests(IsolatedSetupState, unittest.TestCase):
    def setUp(self):
        super().setUp()
        for item in (
            patch.object(setup.sys, 'frozen', True, create=True),
            patch.object(setup.sys, '_MEIPASS', str(ROOT), create=True),
            patch.object(setup.sys, 'prefix', str(self.root/'read-only installation')),
            patch.object(setup, 'run_background', return_value=None),
            patch.object(setup.tempo_preferences, '_preferences', copy.deepcopy(DEFAULT_PREFERENCES)),
            patch.object(setup.tempo_hub, 'token', 'fixture-token'),
            patch.object(setup.tempo_hub, '_credentials_ready', True),
            patch.object(setup, 'fetch_shared_settings', return_value=None),
        ):
            item.start(); self.addCleanup(item.stop)

    def view(self):
        view = setup.SetupWizardDialog()
        self.addCleanup(close_and_dispose, view)
        return view

    def test_cancel_after_account_save_resumes_at_models_with_saved_personal_name(self):
        setup.tempo_preferences.set('collaboration', 'collaborator_id', 'Existing Laboratory')
        first_run.save_setup_stage('models')
        view = self.view()
        self.assertIs(view.stack.currentWidget(), view.models_page)
        self.assertEqual(view.txt_collab_id.text(), 'Existing Laboratory')
        self.assertFalse(view._models_ready)
        view.reject()
        self.assertEqual(first_run.setup_stage(), 'models')

    def test_successful_requirements_resume_at_account_without_rechecking(self):
        view = self.view()
        view._on_diag_finished(True, 'Fixture packages found.')
        self.assertEqual(first_run.setup_stage(), 'account')
        view.reject()
        resumed = self.view()
        self.assertIs(resumed.stack.currentWidget(), resumed.account_page)
        self.assertTrue(resumed._requirements_ready)
        self.assertTrue(resumed.btn_diag_next.isEnabled())

    def test_saved_account_advances_progress_only_after_shared_profile_is_confirmed(self):
        first_run.save_setup_stage('account')
        view = self.view()
        view.txt_collab_id.setText('Existing Laboratory')
        with patch('huggingface_hub.HfApi'), \
             patch.object(setup.tempo_hub, '_init_credentials'), \
             patch.object(setup.tempo_hub, 'authenticated_account', return_value='hf:fixture'), \
             patch.object(setup.tempo_preferences, 'save', return_value=True), \
             patch.object(setup, 'run_read_task', side_effect=lambda parent, title, action: action()), \
             patch.object(setup, 'run_task', side_effect=lambda parent, title, action: action()), \
             patch.object(setup.tempo_hub, 'publish_collaborator_profile', side_effect=[ConnectionError('offline'), True]):
            view._save_and_finish()
            self.assertEqual(first_run.setup_stage(), 'account')
            self.assertIn('saved locally', view.account_error.summary.text())
            view._save_and_finish()
        self.assertEqual(first_run.setup_stage(), 'models')
        self.assertIs(view.stack.currentWidget(), view.models_page)

    def test_completion_requires_models_and_records_only_final_open_action(self):
        first_run.save_setup_stage('models')
        view = self.view()
        view.btn_open.click()
        self.assertEqual(first_run.setup_stage(), 'models')
        view._models_ready = True
        view.stack.setCurrentWidget(view.ready_page)
        self.assertNotEqual(first_run.setup_stage(), 'complete')
        view.btn_open.click()
        self.assertEqual(first_run.setup_stage(), 'complete')
        self.assertEqual(view.result(), QDialog.DialogCode.Accepted)
        self.assertFalse((Path(setup.sys.prefix)/'tempo_setup_complete').exists())

    def test_completion_write_failure_keeps_ready_page_open_for_retry(self):
        view = self.view()
        view._models_ready = True
        view.stack.setCurrentWidget(view.ready_page)
        view.show(); APP.processEvents()
        with patch.object(first_run, 'save_setup_stage', side_effect=PermissionError('Read-only profile')):
            view.btn_open.click()
        self.assertTrue(view.isVisible())
        self.assertIs(view.stack.currentWidget(), view.ready_page)
        self.assertIn('choose Open TEMPO again', view.ready_error.summary.text())
        self.assertNotEqual(first_run.setup_stage(), 'complete')
        view.btn_open.click()
        self.assertEqual(first_run.setup_stage(), 'complete')

    def test_frozen_model_installation_never_writes_inside_payload(self):
        from tempo_model_catalog import MODEL_FILES
        first_run.save_setup_stage('models')
        view = self.view()
        manifest = {'revision': 'fixture', 'models': {name: {'sha256': 'a'*64} for name in MODEL_FILES.values()}}
        with patch.object(setup.tempo_hub, 'model_manifest', return_value=manifest), \
             patch.object(setup.tempo_hub, 'download_model_release', return_value=3):
            view._install_models()
            self.assertTrue(wait_for_qt(lambda: view._model_task is None))
        self.assertTrue(view._models_ready)
        self.assertEqual(first_run.setup_stage(), 'models')
        self.assertFalse(Path(setup.sys.prefix).exists())

    def test_optional_nvidia_download_runs_only_when_selected_in_models_step(self):
        from tempo_model_catalog import MODEL_FILES
        first_run.save_setup_stage('models')
        manifest = {'revision': 'fixture', 'models': {name: {'sha256': 'a'*64} for name in MODEL_FILES.values()}}
        with patch('tempo_nvidia_setup.release_descriptor', return_value={'archive_size': 3 * 1024**3}):
            view = self.view()
        view.stack.setCurrentWidget(view.models_page)
        view.show(); APP.processEvents()
        self.assertFalse(view.nvidia_option.isChecked())
        self.assertTrue(view.nvidia_option.isVisible())
        with patch.object(setup.tempo_hub, 'model_manifest', return_value=manifest), \
             patch.object(setup.tempo_hub, 'download_model_release', return_value=3), \
             patch('tempo_nvidia_setup.install_nvidia_runtime') as install:
            view._install_models()
            self.assertTrue(wait_for_qt(lambda: view._model_task is None))
            install.assert_not_called()
            view._models_ready = False
            view.nvidia_option.setChecked(True)
            view._install_models()
            self.assertTrue(wait_for_qt(lambda: view._model_task is None))
            install.assert_called_once()
        self.assertTrue(view._models_ready)

    def test_nvidia_selected_after_model_install_is_not_skipped(self):
        from tempo_model_catalog import MODEL_FILES
        manifest = {'revision':'fixture', 'models':{name:{'sha256':'a'*64} for name in MODEL_FILES.values()}}
        with patch('tempo_nvidia_setup.release_descriptor', return_value={'archive_size':1}):
            view = self.view()
        view.stack.setCurrentWidget(view.models_page); view.show(); APP.processEvents()
        view._models_ready = True
        view.nvidia_option.setChecked(True)
        with patch.object(setup.tempo_hub, 'model_manifest', return_value=manifest), \
             patch.object(setup.tempo_hub, 'download_model_release', return_value=3), \
             patch('tempo_nvidia_setup.install_nvidia_runtime') as install:
            view._install_models()
            self.assertTrue(wait_for_qt(lambda: view._model_task is None))
            install.assert_called_once()
        self.assertTrue(view._nvidia_ready)

    def test_first_signin_discovers_custom_repositories_before_checking_access(self):
        from tempo_shared_settings import values
        settings = dict(values(), hf_org_id='fixture-lab', hf_dataset_repo='fixture-lab/images', hf_models_repo='fixture-lab/models')
        record = {'revision':'fixture', 'settings':settings}
        view = self.view(); view.txt_collab_id.setText('Fixture Lab')
        default = setup.tempo_hub.get_dataset_repo()
        api = Mock()
        def info(*, repo_id, **kwargs):
            if repo_id == default:
                raise PermissionError('Default repository is not this workspace')
        api.repo_info.side_effect = info
        with patch('huggingface_hub.HfApi', return_value=api), \
             patch.object(setup, 'fetch_shared_settings', return_value=record) as discover, \
             patch.object(setup, 'run_read_task', side_effect=lambda parent,title,action:action()), \
             patch.object(setup, 'run_task', side_effect=lambda *a,**k:None), \
             patch.object(setup.tempo_hub, '_init_credentials'), \
             patch.object(setup.tempo_hub, 'authenticated_account', return_value='hf:fixture-account'), \
             patch.object(setup.tempo_preferences, 'save', return_value=True), \
             patch.object(setup, 'apply_shared_settings') as apply:
            view._save_and_finish()
        discover.assert_called_once()
        api.auth_check.assert_called_once_with(repo_id='fixture-lab/images', repo_type='dataset', write=True)
        apply.assert_called_once_with(record)
        self.assertIs(view.stack.currentWidget(), view.models_page)

    def test_failed_optional_nvidia_download_can_be_skipped_without_losing_models(self):
        from tempo_model_catalog import MODEL_FILES
        first_run.save_setup_stage('models')
        manifest = {'revision': 'fixture', 'models': {name: {'sha256': 'a'*64} for name in MODEL_FILES.values()}}
        with patch('tempo_nvidia_setup.release_descriptor', return_value={'archive_size': 3 * 1024**3}):
            view = self.view()
        view.stack.setCurrentWidget(view.models_page)
        view.show(); APP.processEvents()
        view.nvidia_option.setChecked(True)
        with patch.object(setup.tempo_hub, 'model_manifest', return_value=manifest), \
             patch.object(setup.tempo_hub, 'download_model_release', return_value=3), \
             patch('tempo_nvidia_setup.install_nvidia_runtime', side_effect=RuntimeError('GPU check failed')):
            view._install_models()
            self.assertTrue(wait_for_qt(lambda: view._model_task is None))
        self.assertFalse(view._models_ready)
        self.assertTrue(all(status.text() == 'Ready' for status in view.model_statuses.values()))
        self.assertIn('clear the checkbox', view.model_error.summary.text())
        view.nvidia_option.setChecked(False)
        with patch.object(setup.tempo_hub, 'model_manifest', return_value=manifest), \
             patch.object(setup.tempo_hub, 'download_model_release', return_value=3):
            view._install_models()
            self.assertTrue(wait_for_qt(lambda: view._model_task is None))
        self.assertTrue(view._models_ready)

    def test_requirements_are_verified_without_pip_in_frozen_application(self):
        worker = setup.SetupWorkerThread()
        results = []
        worker.finished_signal.connect(lambda ok, detail: results.append(ok))
        with patch('importlib.util.find_spec', return_value=object()), \
             patch.object(setup.subprocess, 'run') as run:
            worker.run()
        self.assertEqual(results, [True])
        run.assert_not_called()

    def test_standalone_frozen_handoff_relaunches_executable_without_python_script(self):
        wizard = Mock()
        wizard.exec.return_value = QDialog.DialogCode.Accepted
        with patch.object(setup, 'SetupWizardDialog', return_value=wizard), \
             patch('tempo_tasks.dispose_closed_views'), patch('tempo_tasks.dispose_application'), \
             patch.object(setup.subprocess, 'Popen') as launch:
            setup.run_setup_application(APP)
        self.assertEqual(launch.call_args.args[0], [setup.sys.executable])
