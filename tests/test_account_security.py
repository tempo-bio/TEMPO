"""Account secrets never populate names, saved-token forms or diagnostics."""
import copy
import json
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from PyQt6.QtWidgets import QApplication, QLineEdit, QMainWindow, QPushButton
from PyQt6.QtGui import QFont
from PyQt6.QtCore import Qt, QPoint
from PyQt6.QtTest import QTest
from tempo_credentials import contains_access_token, safe_profile_name, redact_credentials
import tempo_config as config_module
from tempo_config import DEFAULT_PREFERENCES, TempoConfig, tempo_preferences
from tempo_hub import TempoHub, tempo_hub
from tempo_secure_store import CredentialMigrationRequired, CredentialStorageError
from tempo_tasks import dispose_closed_views
import setup_wizard as setup
with patch('platform.machine', return_value='x86_64'):
    import tempo_gui as gui

APP = QApplication.instance() or QApplication([])
TOKEN = 'hf_SYNTHETICAccountSecurityFixture0123456789'


class AccountSecurityTests(unittest.TestCase):
    def test_failed_profile_save_retains_the_previous_visible_profile(self):
        tempo_preferences.set('collaboration', 'collaborator_id', 'Original Laboratory')
        view = setup.SetupWizardDialog(account_only=True); self.addCleanup(view.close)
        view.txt_collab_id.setText('Replacement Laboratory')
        view.txt_token.setText(TOKEN)
        with patch('huggingface_hub.HfApi'), patch.object(setup, 'save_hf_token'), \
             patch.object(tempo_hub, '_init_credentials'), \
             patch.object(tempo_hub, 'authenticated_account', return_value='hf:fixture'), \
             patch.object(tempo_preferences, 'save', return_value=False), \
             patch.object(tempo_hub, 'publish_collaborator_profile') as publish:
            view._save_and_finish()
        self.assertEqual(tempo_preferences.get('collaboration', 'collaborator_id'), 'Original Laboratory')
        self.assertIn('profile could not be saved', view.account_error.summary.text())
        self.assertEqual(view.txt_collab_id.text(), 'Replacement Laboratory')
        publish.assert_not_called()

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        for item in (
            patch.object(tempo_hub, 'publish_collaborator_profile', return_value=True),
            patch.object(tempo_preferences, '_preferences', copy.deepcopy(DEFAULT_PREFERENCES)),
            patch.object(tempo_hub, 'token', TOKEN),
            patch.object(tempo_hub, 'get_account_display_name', return_value='fixture-researcher'),
            patch.object(setup, 'run_read_task', side_effect=lambda parent, title, action: action()),
            patch.object(setup, 'run_task', side_effect=lambda parent, title, action: action()),
            patch.object(setup, 'run_background', return_value=None),
            patch.dict(os.environ, {}, clear=False),
        ):
            item.start(); self.addCleanup(item.stop)
        tempo_preferences.set('collaboration', 'collaborator_id', 'Fixture Lab')

    def view(self):
        view = setup.SetupWizardDialog(account_only=True)
        if view.manual_account_fields.isHidden():
            view.btn_manual_sign_in.click()
        self.addCleanup(self.dispose, view)
        return view

    @staticmethod
    def dispose(view):
        view.close()
        dispose_closed_views(view)
        APP.processEvents()

    def test_saved_token_is_absent_from_form_and_username_is_separate(self):
        with patch.object(tempo_hub, '_identity_token', TOKEN), patch.object(tempo_hub, '_account_id', 'hf:fixture-id'):
            view = self.view()
        self.assertEqual(view.txt_token.text(), setup.SavedTokenField.SAVED_MASK)
        self.assertEqual(view.txt_token.replacement_token(), '')
        self.assertTrue(view.txt_token.isReadOnly())
        self.assertEqual(view.txt_token.echoMode(), QLineEdit.EchoMode.Password)
        self.assertEqual(view.txt_collab_id.text(), 'Fixture Lab')
        self.assertIn('fixture-researcher', view.lbl_account_identity.text())
        self.assertNotIn('fixture-researcher', view.txt_collab_id.text())
        self.assertEqual(view.txt_token.placeholderText(), '')
        for font_size in (12, 18, 24):
            font = QFont(view.font()); font.setPointSize(font_size); view.setFont(font)
            view.show(); APP.processEvents()
            for field in view.findChildren(QLineEdit):
                self.assertNotIn(TOKEN, field.text())
                self.assertNotIn(TOKEN, field.toolTip())
                self.assertNotIn(TOKEN, field.accessibleName())

    def test_pasting_token_into_profile_clears_it_and_blocks_saving(self):
        view = self.view()
        APP.clipboard().setText(TOKEN)
        self.addCleanup(lambda: APP.clipboard().clear())
        view.txt_collab_id.selectAll(); view.txt_collab_id.paste()
        self.assertEqual(view.txt_collab_id.text(), '')
        view.txt_collab_id.undo()
        self.assertEqual(view.txt_collab_id.text(), '')
        self.assertIn('token field', view.account_error.summary.text())
        with patch.object(tempo_preferences, 'save') as save:
            view._save_and_finish()
        save.assert_not_called()
        self.assertEqual(tempo_preferences.get('collaboration', 'collaborator_id'), 'Fixture Lab')

    def test_corrupt_saved_profile_is_never_displayed_in_either_page(self):
        tempo_preferences._preferences['collaboration']['collaborator_id']['value'] = TOKEN
        self.assertEqual(self.view().txt_collab_id.text(), '')
        settings = gui.SettingsTab(); self.addCleanup(self.dispose, settings)
        field = settings.widgets[('collaboration', 'collaborator_id')]
        self.assertEqual(field.text(), '')
        settings.refresh_account_fields()
        self.assertEqual(field.text(), '')

    def test_account_ids_cannot_replace_names(self):
        tempo_preferences._preferences['collaboration']['collaborator_id']['value'] = 'hf:fixture-public-id'
        self.assertEqual(self.view().txt_collab_id.text(), '')
        with self.assertRaisesRegex(ValueError, 'account IDs'):
            tempo_preferences.set('collaboration', 'collaborator_id', 'hf:fixture-public-id')
        view = self.view(); view.txt_collab_id.setText('hf:fixture-public-id')
        self.assertEqual(view.txt_collab_id.text(), '')

    def test_settings_reject_token_and_allow_correcting_name(self):
        settings = gui.SettingsTab(); self.addCleanup(self.dispose, settings)
        field = settings.widgets[('collaboration', 'collaborator_id')]
        field.setText(TOKEN)
        self.assertEqual(field.text(), '')
        with patch.object(tempo_preferences, 'save_to_disk') as save:
            settings.save_preferences()
        save.assert_not_called()
        self.assertIn('not an access token', settings.lbl_save_status.text())
        field.setText('Corrected Lab')
        self.assertEqual(settings.lbl_save_status.text(), 'Unsaved changes')

    def test_unchanged_mask_reuses_credentials_without_rewriting_them(self):
        view = self.view()
        with patch('huggingface_hub.HfApi') as api, patch.object(setup, 'save_hf_token') as login, \
             patch.object(tempo_hub, '_init_credentials'), patch.object(tempo_hub, 'authenticated_account'), \
             patch.object(tempo_preferences, 'save', return_value=True):
            view._save_and_finish()
        api.assert_called_once_with(token=TOKEN)
        login.assert_not_called()
        self.assertEqual(view.txt_token.text(), '')

    def test_account_error_details_redact_active_and_replacement_tokens(self):
        view = self.view(); replacement = 'hf_SYNTHETICReplacement0123456789'
        view.txt_token.setText(replacement)
        with patch('huggingface_hub.HfApi') as api, patch.object(setup, 'save_hf_token') as login:
            api.return_value.whoami.side_effect = RuntimeError('Authorization: Bearer ' + replacement + ' old=' + TOKEN)
            view._save_and_finish()
        login.assert_not_called()
        detail = view.account_error.details.toPlainText()
        self.assertNotIn(TOKEN, detail); self.assertNotIn(replacement, detail)
        self.assertIn('[redacted]', detail)
        self.assertEqual(view.txt_token.echoMode(), QLineEdit.EchoMode.Password)
        view.reject(); self.assertEqual(view.txt_token.text(), '')

    def test_token_shaped_names_cannot_be_persisted(self):
        with self.assertRaisesRegex(ValueError, 'profile names'):
            tempo_preferences.set('collaboration', 'collaborator_id', TOKEN)
        candidate = copy.deepcopy(tempo_preferences.get_all())
        candidate['collaboration']['collaborator_id']['value'] = TOKEN
        path = Path(self.temp.name) / 'preferences.json'
        with patch.object(config_module, 'CONFIG_DIR', self.temp.name), patch.object(config_module, 'CONFIG_PATH', str(path)):
            self.assertFalse(tempo_preferences.save(candidate))
        self.assertFalse(path.exists())

    def test_loading_corrupt_profile_preserves_other_preferences(self):
        for invalid_name in (TOKEN, 'hf:fixture-public-id'):
            with self.subTest(invalid_name_type='token' if invalid_name == TOKEN else 'account ID'):
                saved = copy.deepcopy(DEFAULT_PREFERENCES)
                saved['collaboration']['collaborator_id']['value'] = invalid_name
                saved['capture']['target_fps']['value'] = 177
                path = Path(self.temp.name) / 'preferences.json'; path.write_text(json.dumps(saved))
                instance = object.__new__(TempoConfig)
                with patch.object(config_module, 'CONFIG_PATH', str(path)):
                    instance.load()
                self.assertEqual(instance.get('collaboration', 'collaborator_id'), '')
                self.assertEqual(instance.get('capture', 'target_fps'), 177)
                self.assertEqual(json.loads(path.read_text()), saved)

    def test_verified_identity_cannot_display_a_token_or_stale_username(self):
        hub = object.__new__(TempoHub); hub.token = TOKEN
        hub.api = Mock(); hub.api.whoami.return_value = {'id': 'fixture-id', 'name': TOKEN}
        self.assertEqual(hub.authenticated_account(), 'hf:fixture-id')
        self.assertEqual(hub.get_account_display_name(), 'Verified account')
        hub.api.whoami.return_value['name'] = 'fixture-researcher'
        hub.authenticated_account(force_refresh=True)
        self.assertEqual(hub.get_account_display_name(), 'fixture-researcher')
        hub.token = 'hf_SYNTHETICOtherAccount0123456789'
        self.assertEqual(hub.get_account_display_name(), 'Account not verified')

    def test_secret_guard_handles_nonstandard_known_credentials(self):
        secret = 'synthetic-nonstandard-credential'
        self.assertTrue(contains_access_token(secret, secret))
        self.assertEqual(safe_profile_name('Lab ' + secret, secret), '')
        self.assertEqual(redact_credentials('Bearer ' + secret, secret), 'Bearer [redacted]')
        self.assertEqual(safe_profile_name('Fixture Lab'), 'Fixture Lab')

    def test_saved_token_status_and_replacement_box_are_separate(self):
        view = self.view()
        self.assertEqual(view.lbl_token_status.text(), 'Token saved, not verified')
        with patch.object(tempo_hub, '_identity_token', TOKEN), patch.object(tempo_hub, '_account_id', 'hf:fixture-id'):
            view._refresh_token_status()
            self.assertEqual(view.lbl_token_status.text(), 'Token active')
            self.assertEqual(view.txt_token.text(), setup.SavedTokenField.SAVED_MASK)
            self.assertEqual(view.txt_token.placeholderText(), '')
            view.txt_token.setText('hf_SYNTHETICReplacement0123456789')
            self.assertEqual(view.lbl_token_status.text(), 'Token active')
        with patch.object(tempo_hub, 'token', None):
            view._refresh_token_status()
            self.assertEqual(view.lbl_token_status.text(), 'No token saved')
            self.assertEqual(view.txt_token.placeholderText(), '')

    def test_form_checks_actual_token_and_shows_verification_failure(self):
        view = self.view()
        with patch.object(setup, 'run_background', side_effect=self.background_immediately), \
             patch.object(tempo_hub, 'authenticated_account', side_effect=PermissionError('Rejected ' + TOKEN)) as verify:
            view.show(); APP.processEvents()
        verify.assert_called_once_with(force_refresh=True)
        self.assertEqual(view.lbl_token_status.text(), 'Token saved, verification failed')
        self.assertEqual(view.lbl_account_identity.text(), '')
        self.assertNotIn(TOKEN, view.account_error.details.toPlainText())
        self.assertEqual(view.txt_token.placeholderText(), '')

    def test_pointer_click_drag_and_arrows_cannot_select_part_of_saved_mask(self):
        view = self.view(); view.show(); APP.processEvents()
        field = view.txt_token
        for position in (QPoint(6, field.height()//2), field.rect().center(), QPoint(field.width()-6, field.height()//2)):
            QTest.mouseClick(field, Qt.MouseButton.LeftButton, pos=position)
            self.assertEqual(field.selectedText(), field.SAVED_MASK)
            self.assertEqual(field.selectionStart(), 0)
            QTest.keyClick(field, Qt.Key.Key_Left)
            self.assertEqual(field.selectedText(), field.SAVED_MASK)
            QTest.keyClick(field, Qt.Key.Key_Home)
            self.assertEqual(field.selectedText(), field.SAVED_MASK)
        QTest.mousePress(field, Qt.MouseButton.LeftButton, pos=field.rect().center())
        QTest.mouseMove(field, QPoint(6, field.height()//2))
        QTest.mouseRelease(field, Qt.MouseButton.LeftButton, pos=QPoint(6, field.height()//2))
        self.assertEqual(field.selectedText(), field.SAVED_MASK)

    def test_saved_mask_blocks_typing_paste_and_copy(self):
        view = self.view(); view.show(); APP.processEvents()
        field = view.txt_token
        QTest.mouseClick(field, Qt.MouseButton.LeftButton, pos=field.rect().center())
        QTest.keyClicks(field, 'accidental-edit')
        self.assertEqual(field.text(), field.SAVED_MASK)
        APP.clipboard().setText('replacement-fixture')
        self.addCleanup(lambda: APP.clipboard().clear())
        QTest.keyClick(field, Qt.Key.Key_V, Qt.KeyboardModifier.ControlModifier)
        field.paste()
        self.assertEqual(field.text(), field.SAVED_MASK)
        APP.clipboard().setText('clipboard-fixture')
        QTest.keyClick(field, Qt.Key.Key_C, Qt.KeyboardModifier.ControlModifier)
        field.copy()
        self.assertEqual(APP.clipboard().text(), 'clipboard-fixture')
        self.assertEqual(field.replacement_token(), '')

    def test_delete_and_backspace_clear_saved_mask_as_one_unit(self):
        for key in (Qt.Key.Key_Delete, Qt.Key.Key_Backspace):
            with self.subTest(key=key):
                view = self.view(); view.show(); APP.processEvents()
                field = view.txt_token
                QTest.mouseClick(field, Qt.MouseButton.LeftButton, pos=field.rect().center())
                QTest.keyClick(field, key)
                self.assertEqual(field.text(), '')
                self.assertFalse(field.isReadOnly())
                self.assertTrue(field.replacement_pending)
                self.assertEqual(view.lbl_token_status.text(), 'Token change not saved')
                field.undo()
                self.assertEqual(field.text(), '')
                QTest.keyClicks(field, 'hf_ReplacementFixture0123456789')
                self.assertEqual(field.replacement_token(), 'hf_ReplacementFixture0123456789')
                self.assertEqual(field.echoMode(), QLineEdit.EchoMode.Password)

    def test_saved_mask_preserves_tab_navigation_and_escape_cancellation(self):
        view = self.view(); view.show(); APP.processEvents()
        field = view.txt_token
        QTest.mouseClick(field, Qt.MouseButton.LeftButton, pos=field.rect().center())
        QTest.keyClick(field, Qt.Key.Key_Tab)
        self.assertIsNot(APP.focusWidget(), field)
        QTest.keyClick(APP.focusWidget(), Qt.Key.Key_Backtab)
        self.assertIs(APP.focusWidget(), field)
        self.assertEqual(field.selectedText(), field.SAVED_MASK)
        QTest.keyClick(field, Qt.Key.Key_Delete)
        QTest.keyClicks(field, 'replacement-fixture')
        QTest.keyClick(field, Qt.Key.Key_Escape)
        self.assertFalse(view.isVisible())
        self.assertEqual(field.text(), '')
        self.assertEqual(tempo_hub.token, TOKEN)

    def test_cleared_saved_mask_cannot_silently_reuse_key_on_save(self):
        view = self.view(); view.show(); APP.processEvents()
        QTest.mouseClick(view.txt_token, Qt.MouseButton.LeftButton, pos=view.txt_token.rect().center())
        QTest.keyClick(view.txt_token, Qt.Key.Key_Backspace)
        with patch('huggingface_hub.HfApi') as api, patch.object(setup, 'save_hf_token') as save, patch.object(tempo_preferences, 'save') as profile:
            view._save_and_finish()
        api.assert_not_called(); save.assert_not_called(); profile.assert_not_called()
        self.assertIn('Enter a replacement', view.account_error.summary.text())
        self.assertEqual(tempo_hub.token, TOKEN)
        view.reject(); view.show(); APP.processEvents()
        self.assertEqual(view.txt_token.text(), view.txt_token.SAVED_MASK)

    def test_background_check_cannot_restore_mask_over_replacement_input(self):
        view = self.view()
        view.txt_token.begin_replacement()
        view.txt_token.setText('hf_ReplacementFixture0123456789')
        view._refresh_token_status()
        self.assertEqual(view.txt_token.replacement_token(), 'hf_ReplacementFixture0123456789')
        self.assertFalse(view.txt_token.isReadOnly())

    def test_form_loads_saved_credentials_without_migrating_in_background(self):
        view = self.view()
        def ensure():
            tempo_hub.credential_error = 'Unlock secure storage.'
            raise CredentialMigrationRequired('Saved token needs migration.')
        with patch.object(tempo_hub, 'token', None), patch.object(tempo_hub, '_credentials_ready', False), \
             patch.object(tempo_hub, 'ensure_credentials', side_effect=ensure) as load, \
             patch.object(setup, 'run_background', side_effect=self.background_immediately), \
             patch.object(setup, 'run_task') as write:
            view.show(); APP.processEvents()
            self.assertEqual(view.lbl_token_status.text(), 'Saved token unavailable')
        load.assert_called_once_with(); write.assert_not_called()

    def test_late_token_check_cannot_overwrite_a_reopened_form(self):
        view = self.view(); callbacks = []
        with patch.object(setup, 'run_background', side_effect=lambda action, done: callbacks.append(done)):
            view.show(); APP.processEvents()
            self.assertEqual(view.lbl_token_status.text(), 'Checking token…')
            view.reject(); view.show(); APP.processEvents()
            self.assertEqual(len(callbacks), 2)
            callbacks[0](None, PermissionError('old verification failed'))
            self.assertEqual(view.lbl_token_status.text(), 'Checking token…')
            with patch.object(tempo_hub, '_identity_token', TOKEN), patch.object(tempo_hub, '_account_id', 'hf:fixture-id'):
                callbacks[1]('hf:fixture-id', None)
            self.assertEqual(view.lbl_token_status.text(), 'Token active')

    def test_failed_secure_save_retains_current_account_and_unsaved_form(self):
        view = self.view()
        replacement = 'hf_SYNTHETICReplacement0123456789'
        view.txt_token.setText(replacement)
        with patch('huggingface_hub.HfApi'), \
             patch.object(setup, 'save_hf_token', side_effect=CredentialStorageError('Unlock secure storage and retry.')) as save_token, \
             patch.object(tempo_hub, '_init_credentials') as refresh, \
             patch.object(tempo_preferences, 'save') as save_profile:
            view._save_and_finish()
        save_token.assert_called_once_with(replacement)
        refresh.assert_not_called(); save_profile.assert_not_called()
        self.assertEqual(tempo_hub.token, TOKEN)
        self.assertEqual(view.txt_token.text(), replacement)
        self.assertIn('Unlock secure storage', view.account_error.details.toPlainText())

    def test_new_token_uses_native_store_without_sdk_login_or_environment_write(self):
        view = self.view()
        replacement = 'hf_SYNTHETICReplacement0123456789'
        view.txt_token.setText(replacement)
        old_environment = dict(os.environ)
        with patch('huggingface_hub.HfApi'), patch('huggingface_hub.login') as sdk_login, \
             patch.object(setup, 'save_hf_token') as save_token, \
             patch.object(tempo_hub, '_init_credentials'), \
             patch.object(tempo_hub, 'authenticated_account'), \
             patch.object(tempo_preferences, 'save', return_value=True):
            view._save_and_finish()
        save_token.assert_called_once_with(replacement)
        sdk_login.assert_not_called()
        self.assertEqual(dict(os.environ), old_environment)
        self.assertEqual(view.txt_token.text(), '')

    def dashboard_host(self):
        class Host(gui.TempoDashboard):
            def __init__(self):
                QMainWindow.__init__(self)
                self._account_generation = 0
                self.account_indicator = QPushButton(self)
                self.tab_maintainer = Mock()
            def closeEvent(self, event):
                QMainWindow.closeEvent(self, event)
        view = Host(); self.addCleanup(self.dispose, view)
        return view

    @staticmethod
    def background_immediately(action, done):
        try:
            result = action()
        except Exception as error:
            done(None, error)
        else:
            done(result, None)

    def test_startup_migration_uses_protected_write_before_account_verification(self):
        view = self.dashboard_host()
        def ensure(*, migrate=False):
            if tempo_hub._credentials_ready:
                return
            if not migrate:
                raise CredentialMigrationRequired('Migration required.')
            tempo_hub.token = TOKEN
        with patch.object(tempo_hub, 'token', None), \
             patch.object(tempo_hub, '_credentials_ready', False), \
             patch.object(tempo_hub, 'ensure_credentials', side_effect=ensure) as load, \
             patch.object(tempo_hub, 'authenticated_account', return_value='hf:fixture-id'), \
             patch.object(gui, 'run_background', side_effect=self.background_immediately), \
             patch.object(gui, 'run_task', side_effect=lambda parent, title, action: action()) as write:
            view.refresh_account_indicator()
            self.assertIn('fixture-researcher', view.account_indicator.text())
        self.assertEqual([call.kwargs for call in load.call_args_list], [{}, {'migrate': True}, {}])
        self.assertEqual(write.call_args.args[:2], (view, 'Securing saved account…'))
        view.tab_maintainer.verify_access.assert_called_once()

    def test_startup_migration_failure_is_visible_and_does_not_authenticate(self):
        view = self.dashboard_host()
        def ensure(*, migrate=False):
            if not migrate:
                raise CredentialMigrationRequired('Migration required.')
            tempo_hub.credential_error = 'Unlock secure storage and retry.'
            raise CredentialStorageError(tempo_hub.credential_error)
        with patch.object(tempo_hub, 'token', None), \
             patch.object(tempo_hub, '_credentials_ready', False), \
             patch.object(tempo_hub, 'ensure_credentials', side_effect=ensure), \
             patch.object(tempo_hub, 'authenticated_account') as verify, \
             patch.object(gui, 'run_background', side_effect=self.background_immediately), \
             patch.object(gui, 'run_task', side_effect=lambda parent, title, action: action()):
            view.refresh_account_indicator()
            self.assertEqual(view.account_indicator.text(), 'Account storage unavailable')
            self.assertIn('Unlock secure storage', view.account_indicator.toolTip())
        verify.assert_not_called()

    def test_first_start_without_saved_credentials_becomes_signed_out(self):
        view = self.dashboard_host()
        with patch.object(tempo_hub, 'token', None), \
             patch.object(tempo_hub, '_credentials_ready', False), \
             patch.object(tempo_hub, 'ensure_credentials', side_effect=lambda: setattr(tempo_hub, 'token', None)), \
             patch.object(tempo_hub, 'authenticated_account') as verify, \
             patch.object(gui, 'run_background', side_effect=self.background_immediately), \
             patch.object(gui, 'run_task') as write:
            view.refresh_account_indicator()
            self.assertEqual(view.account_indicator.text(), 'Signed out')
        verify.assert_not_called(); write.assert_not_called()


if __name__ == '__main__':
    unittest.main()
