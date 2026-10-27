"""Browser authorization is staged, cancellable, and saved only on request."""
import copy
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from PyQt6.QtWidgets import QApplication, QLineEdit, QPushButton, QStyle, QStyleOptionButton
from PyQt6.QtGui import QFont
from tempo_config import DEFAULT_PREFERENCES, tempo_preferences
from tempo_hub import tempo_hub
from tempo_tasks import dispose_closed_views
import setup_wizard as setup

APP = QApplication.instance() or QApplication([])
OAUTH_TOKEN = 'fixture-browser-credential'


class OAuthAccountTests(unittest.TestCase):
    def setUp(self):
        self.pending = []
        self.attempt = Mock(authorization_url='https://huggingface.co/oauth/authorize?fixture=1')
        self.attempt.wait.return_value = OAUTH_TOKEN
        self.api = Mock()
        self.api.whoami.return_value = dict(type='user', id='fixture-id', name='fixture-researcher')
        self.api.repo_info.return_value = SimpleNamespace(private=True)
        for item in (
            patch.object(tempo_preferences, '_preferences', copy.deepcopy(DEFAULT_PREFERENCES)),
            patch.object(tempo_hub, 'token', None),
            patch.object(tempo_hub, '_credentials_ready', True),
            patch.object(setup.tempo_oauth, 'client_id', return_value='fixture-public-client'),
            patch.object(setup.tempo_oauth, 'BrowserSignIn', return_value=self.attempt),
            patch.object(setup.QDesktopServices, 'openUrl', return_value=True),
            patch.object(setup, 'run_background', side_effect=lambda action, done: self.pending.append((action, done))),
            patch.object(setup, 'run_task', side_effect=lambda parent, title, action: action()),
            patch.object(setup, 'run_read_task', side_effect=lambda parent, title, action: action()),
            patch('huggingface_hub.HfApi', return_value=self.api),
            patch.object(tempo_hub, '_init_credentials'),
            patch.object(tempo_hub, 'authenticated_account', return_value='hf:fixture-id'),
            patch.object(tempo_hub, 'publish_collaborator_profile'),
            patch.object(tempo_preferences, 'save', return_value=True),
        ):
            item.start(); self.addCleanup(item.stop)
        self.save_token = patch.object(setup, 'save_hf_token').start()
        self.addCleanup(patch.stopall)
        self.view = setup.SetupWizardDialog(account_only=True)
        self.view.txt_collab_id.setText('Fixture Laboratory')
        self.view.show(); APP.processEvents()
        self.addCleanup(self.dispose)

    def dispose(self):
        self.view.close(); dispose_closed_views(self.view); APP.processEvents()

    def finish_next(self):
        action, done = self.pending.pop(0)
        try:
            result = action()
        except Exception as error:
            done(None, error)
        else:
            done(result, None)

    def authorize(self):
        self.view.btn_browser_sign_in.click()
        self.finish_next()  # Prepare the local callback and open the browser.
        self.finish_next()  # Receive authorization and verify repository access.

    def test_authorization_does_not_save_or_populate_a_token_field(self):
        self.authorize()
        self.save_token.assert_not_called()
        self.assertEqual(self.view._pending_oauth_token, OAUTH_TOKEN)
        self.assertIn('fixture-researcher', self.view.oauth_status.text())
        self.assertTrue(self.view.btn_finish.isEnabled())
        self.assertTrue(self.view.manual_account_fields.isHidden())
        for field in self.view.findChildren(QLineEdit):
            self.assertNotIn(OAUTH_TOKEN, field.text())
        self.view.btn_finish.click()
        self.save_token.assert_called_once_with(OAUTH_TOKEN)
        self.assertIsNone(self.view._pending_oauth_token)
        self.assertEqual(self.view.result(), setup.QDialog.DialogCode.Accepted)

    def test_preparation_and_late_authorization_are_cancelled_on_close(self):
        self.view.btn_browser_sign_in.click()
        self.view.close()
        self.finish_next()
        self.attempt.close.assert_called_once()
        self.save_token.assert_not_called()
        self.assertIsNone(self.view._pending_oauth_token)

    def test_cancel_during_browser_wait_ignores_late_success(self):
        self.view.btn_browser_sign_in.click(); self.finish_next()
        self.assertEqual(self.view.btn_browser_sign_in.text(), 'Cancel sign-in')
        self.assertFalse(self.view.btn_finish.isEnabled())
        self.view._save_and_finish(); self.save_token.assert_not_called()
        self.view.btn_browser_sign_in.click()
        self.attempt.cancel.assert_called_once()
        self.finish_next()
        self.assertIsNone(self.view._pending_oauth_token)
        self.save_token.assert_not_called()

    def test_permission_failure_does_not_replace_the_saved_account(self):
        self.api.auth_check.side_effect = PermissionError('Rejected ' + OAUTH_TOKEN)
        self.authorize()
        self.assertIsNone(self.view._pending_oauth_token)
        self.assertNotIn(OAUTH_TOKEN, self.view.account_error.details.toPlainText())
        self.assertIn('Workspace access', self.view.account_error.details.toPlainText())
        self.save_token.assert_not_called()

    def test_switching_to_token_entry_discards_browser_credential(self):
        self.authorize()
        self.view.btn_manual_sign_in.click()
        self.assertIsNone(self.view._pending_oauth_token)
        self.assertTrue(self.view.manual_account_fields.isVisible())
        self.assertEqual(self.view.oauth_status.text(), '')
        self.save_token.assert_not_called()

    def test_changed_account_rejects_a_late_browser_result(self):
        self.view.btn_browser_sign_in.click(); self.finish_next()
        tempo_hub.token = 'another-fixture-account'
        self.finish_next()
        self.assertIsNone(self.view._pending_oauth_token)
        self.assertIn('changed', self.view.account_error.summary.text())
        self.save_token.assert_not_called()

    def test_secure_storage_failure_retains_the_pending_credential_for_retry(self):
        self.authorize()
        self.save_token.side_effect = setup.CredentialStorageError('Secure storage is locked.')
        self.view.btn_finish.click()
        self.assertEqual(self.view._pending_oauth_token, OAUTH_TOKEN)
        self.assertTrue(self.view.isVisible())
        self.assertIn('could not be saved', self.view.account_error.summary.text())
        self.assertNotIn(OAUTH_TOKEN, self.view.account_error.details.toPlainText())

    def test_browser_open_failure_closes_listener_without_saving(self):
        with patch.object(setup.QDesktopServices, 'openUrl', return_value=False):
            self.view.btn_browser_sign_in.click(); self.finish_next()
        self.attempt.close.assert_called_once()
        self.assertIsNone(self.view._oauth_attempt)
        self.assertIn('Could not open your browser', self.view.account_error.summary.text())
        self.save_token.assert_not_called()

    def test_initial_onboarding_has_three_distinct_steps_and_no_empty_account_status(self):
        view = setup.SetupWizardDialog(); self.addCleanup(view.close)
        view._on_diag_finished(True, 'Fixture desktop packages found.')
        view.btn_diag_next.click()
        self.assertIs(view.stack.currentWidget(), view.account_page)
        self.assertEqual([label.accessibleName() for label in view.shell.labels],
                         ['App complete', 'Account current step', 'Models'])
        self.assertTrue(view.lbl_account_identity.isHidden())
        self.assertEqual(view.lbl_account_identity.text(), '')
        self.assertTrue(view.manual_account_fields.isHidden())

    def test_large_font_walkthrough_navigation_preserves_button_text(self):
        view = setup.SetupWizardDialog(); self.addCleanup(view.close)
        font = QFont(view.font()); font.setPointSize(24); view.setFont(font)
        view.resize(620, 480); view.stack.setCurrentWidget(view.account_page)
        view.show()
        for _ in range(12): APP.processEvents()
        for button in view.footers.currentWidget().findChildren(QPushButton):
            if not button.isVisible():
                continue
            option = QStyleOptionButton(); button.initStyleOption(option)
            text_rect = button.style().subElementRect(QStyle.SubElement.SE_PushButtonContents, option, button)
            self.assertGreaterEqual(text_rect.width(), button.fontMetrics().horizontalAdvance(button.text()))
        self.assertTrue(view.btn_finish.isVisible())
        self.assertTrue(view.rect().contains(view.btn_finish.mapTo(view, view.btn_finish.rect().bottomRight())))
        self.assertGreater(view.account_page.verticalScrollBar().maximum(), 0)


if __name__ == '__main__':
    unittest.main()
