"""Onboarding stages, embedded protected writes, and responsive native forms."""
import copy
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from PyQt6.QtCore import QCoreApplication, QEvent, Qt, QUrl
from PyQt6.QtGui import QFont, QTextCursor
from PyQt6.QtTest import QTest
from PyQt6.QtWidgets import QApplication, QPushButton, QStyle, QStyleOptionButton

import setup_wizard as setup
import tempo_tasks
from tempo_config import DEFAULT_PREFERENCES
from tempo_ui import install_ui
from tests.qt_lifecycle import close_and_dispose

APP = QApplication.instance() or QApplication([])


def settle():
    for _ in range(12):
        APP.processEvents()


class WorkspaceGuideTests(unittest.TestCase):
    def setUp(self):
        install_ui(APP)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        for item in (
            patch.object(setup, 'run_background'),
            patch.object(setup.tempo_preferences, '_preferences', copy.deepcopy(DEFAULT_PREFERENCES)),
            patch.object(setup.tempo_hub, 'token', None),
            patch.object(setup.tempo_hub, '_credentials_ready', True),
            patch.object(setup.tempo_oauth, 'client_id', return_value='fixture-client'),
            patch.object(setup.sys, 'prefix', self.temp.name),
        ):
            item.start(); self.addCleanup(item.stop)
        self.view = setup.SetupWizardDialog()
        self.addCleanup(close_and_dispose, self.view)
        self.view.show(); settle()

    def wait_for_install(self):
        for _ in range(200):
            settle()
            if self.view._model_task is None:
                return
            QTest.qWait(5)
        self.fail('The bounded fixture install did not finish.')

    @staticmethod
    def manifest():
        from tempo_model_catalog import MODEL_FILES
        return {'revision': 'fixture', 'models': {name: {'sha256': 'a' * 64} for name in MODEL_FILES.values()}}

    def test_models_retry_without_repeating_account_save_and_finish_only_after_marker(self):
        marker = Path(self.temp.name) / 'tempo_setup_complete'
        self.view.stack.setCurrentWidget(self.view.models_page)
        with patch.object(setup.tempo_hub, 'model_manifest', return_value=self.manifest()), \
             patch.object(setup.tempo_hub, 'download_model_release', side_effect=[ValueError('checksum mismatch'), 3]) as download, \
             patch.object(setup, 'save_hf_token') as save:
            self.view.btn_models.click(); self.wait_for_install()
            self.assertFalse(marker.exists())
            self.assertIn('checksum mismatch', self.view.model_error.details.toPlainText())
            self.assertFalse(self.view._models_ready)
            self.view.btn_models.click(); self.wait_for_install()
            self.assertTrue(marker.exists())
            self.assertTrue(self.view._models_ready)
            self.assertTrue(self.view.model_error.isHidden())
            self.assertTrue(all(label.text() == 'Ready' for label in self.view.model_statuses.values()))
            self.view.btn_models.click()
            self.assertIs(self.view.stack.currentWidget(), self.view.ready_page)
            self.assertNotEqual(self.view.result(), setup.QDialog.DialogCode.Accepted)
            self.view.btn_open.click()
            self.assertEqual(self.view.result(), setup.QDialog.DialogCode.Accepted)
        self.assertEqual(download.call_count, 2)
        save.assert_not_called()

    def test_install_blocks_escape_close_and_quit_until_worker_is_joined_but_keeps_help_available(self):
        release, entered = threading.Event(), threading.Event()
        def download(_, **kwargs):
            entered.set()
            if not release.wait(3):
                raise TimeoutError('Fixture release was not signaled.')
        self.view.stack.setCurrentWidget(self.view.models_page)
        with patch.object(setup.tempo_hub, 'model_manifest', return_value=self.manifest()), \
             patch.object(setup.tempo_hub, 'download_model_release', side_effect=download):
            self.view._install_models()
            try:
                self.assertTrue(entered.wait(1))
                self.assertFalse(self.view.btn_models.isEnabled())
                self.assertFalse(self.view.btn_models_back.isEnabled())
                self.assertTrue(tempo_tasks.has_active_tasks())
                self.assertIn('Installing the published model release…', tempo_tasks.pending_task_titles())
                QTest.keyClick(self.view, Qt.Key.Key_Escape)
                self.assertFalse(self.view.close())
                self.view.accept()
                self.assertTrue(self.view.isVisible())
                QCoreApplication.sendEvent(APP, QEvent(QEvent.Type.Quit))
                self.assertIn('outcome has not been confirmed', self.view.model_wait_status.text())
                self.view._open_guide('models'); settle()
                self.assertTrue(self.view.guide.isVisible())
                self.assertFalse(self.view.guide.isModal())
                self.assertIn('Keep TEMPO open', self.view.guide.browser.toPlainText())
                QTest.keyClick(self.view.guide, Qt.Key.Key_Escape)
                self.assertFalse(self.view.guide.isVisible())
                self.assertIsNotNone(self.view._model_task)
            finally:
                release.set(); self.wait_for_install()
        self.assertNotIn('Installing the published model release…', tempo_tasks.pending_task_titles())
        self.assertTrue(self.view._models_ready)

    def test_incomplete_release_never_downloads_or_records_completion(self):
        manifest = self.manifest()
        manifest['models'].pop(next(iter(manifest['models'])))
        with patch.object(setup.tempo_hub, 'model_manifest', return_value=manifest), \
             patch.object(setup.tempo_hub, 'download_model_release') as download:
            self.view._install_models(); self.wait_for_install()
        download.assert_not_called()
        self.assertFalse((Path(self.temp.name) / 'tempo_setup_complete').exists())
        self.assertIn('incomplete', self.view.model_error.details.toPlainText())

    def test_failed_completion_marker_keeps_retry_and_explains_installed_models(self):
        with patch.object(setup.tempo_hub, 'model_manifest', return_value=self.manifest()), \
             patch.object(setup.tempo_hub, 'download_model_release', return_value=3), \
             patch('builtins.open', side_effect=PermissionError('Fixture folder is read-only.')):
            self.view._install_models(); self.wait_for_install()
        self.assertFalse(self.view._models_ready)
        self.assertTrue(self.view.btn_models.isEnabled())
        self.assertIn('Models were installed', self.view.model_error.details.toPlainText())

    def test_requirements_use_same_quit_and_join_protection(self):
        release, entered = threading.Event(), threading.Event()
        def check(worker):
            entered.set()
            release.wait(3)
            worker.finished_signal.emit(True, 'Fixture desktop packages found.')
        with patch.object(setup.SetupWorkerThread, 'run', check):
            self.view.btn_run_diag.click()
            try:
                self.assertTrue(entered.wait(1))
                self.assertFalse(self.view.close())
                self.assertIn('Checking this computer…', tempo_tasks.pending_task_titles())
            finally:
                release.set()
                for _ in range(200):
                    settle()
                    if self.view._diagnostics_task is None:
                        break
                    QTest.qWait(5)
        self.assertIsNone(self.view._diagnostics_task)
        self.assertTrue(self.view.btn_diag_next.isEnabled())
        self.assertFalse(self.view.btn_run_diag.isVisible())

    def test_bundled_guides_navigate_locally_and_open_only_clicked_web_links(self):
        guide = self.view.guide
        with patch('tempo_onboarding.QDesktopServices.openUrl', return_value=True) as external:
            self.view._open_guide('account'); settle()
            self.assertIn('Create your own', guide.browser.toPlainText())
            cursor = guide.browser.document().find('Account and credential security')
            self.assertTrue(cursor.charFormat().isAnchor())
            self.assertEqual(cursor.charFormat().foreground().color().name(), '#79c0ff')
            guide.follow_link(QUrl('CREDENTIAL_SECURITY.md'))
            self.assertIn('TEMPO credential storage', guide.browser.toPlainText())
            external.assert_not_called()
            before = guide.browser.toPlainText()
            guide.follow_link(QUrl.fromLocalFile('/etc/passwd'))
            self.assertEqual(guide.browser.toPlainText(), before)
            guide.follow_link(QUrl('https://huggingface.co/join'))
            external.assert_called_once_with(QUrl('https://huggingface.co/join'))

    def click_guide_link(self, href, guide=None):
        guide = guide or self.view.guide
        block = guide.browser.document().begin()
        while block.isValid():
            iterator = block.begin()
            while not iterator.atEnd():
                fragment = iterator.fragment()
                if fragment.isValid() and fragment.charFormat().anchorHref() == href:
                    cursor = QTextCursor(guide.browser.document())
                    cursor.setPosition(fragment.position() + min(1, fragment.length() - 1))
                    guide.browser.setTextCursor(cursor)
                    guide.browser.ensureCursorVisible(); settle()
                    point = guide.browser.cursorRect(cursor).center()
                    self.assertTrue(guide.browser.viewport().rect().contains(point))
                    QTest.mouseMove(guide.browser.viewport(), point)
                    QTest.mouseClick(guide.browser.viewport(), Qt.MouseButton.LeftButton, pos=point)
                    settle()
                    return
                iterator += 1
            block = block.next()
        self.fail('Guide link was not rendered: ' + href)

    def test_license_link_opens_bundled_text_by_pointer(self):
        guide = self.view.guide
        guide.follow_link(QUrl.fromLocalFile(str(guide.root/'README.md')))
        guide.show(); settle()
        with patch('tempo_onboarding.QDesktopServices.openUrl') as external:
            self.click_guide_link('LICENSE')
        self.assertEqual(guide.current_path, guide.root/'LICENSE')
        self.assertIn('GNU AFFERO GENERAL PUBLIC LICENSE', guide.browser.toPlainText())
        block = guide.browser.document().begin()
        while block.isValid():
            iterator = block.begin()
            while not iterator.atEnd():
                fragment = iterator.fragment()
                if fragment.isValid():
                    self.assertFalse(fragment.charFormat().isAnchor())
                iterator += 1
            block = block.next()
        external.assert_not_called()

    def test_section_link_preserves_hyphens_in_heading_words(self):
        original_font = APP.font(); self.addCleanup(APP.setFont, original_font)
        for points in (12, 24):
            with self.subTest(points=points):
                APP.setFont(QFont('Arial', points))
                guide = setup.SetupGuide(); self.addCleanup(close_and_dispose, guide)
                guide.resize(620, 540); guide.show(); settle()
                guide.follow_link(QUrl.fromLocalFile(str(guide.root/'README.md')))
                self.click_guide_link('docs/WORKSPACE_ADMINISTRATION.md#browser-sign-in-application', guide)
                cursor = guide.browser.textCursor()
                self.assertEqual(cursor.blockFormat().headingLevel(), 2)
                self.assertEqual(cursor.block().text(), 'Browser sign-in application')
                bounds = guide.browser.document().documentLayout().blockBoundingRect(cursor.block())
                self.assertGreater(bounds.height(), 0, 'The target heading was omitted from the rendered layout.')
                self.assertGreaterEqual(guide.browser.cursorRect(cursor).top(), 0)

    def test_fixed_footer_and_scrolling_at_ordinary_wide_small_and_large_font_sizes(self):
        view = self.view
        view.txt_collab_id.setText('Fixture laboratory with a longer display name')
        view._toggle_manual_sign_in()
        view.account_error.set_error('Your account was saved locally, but the shared display name could not be updated. Save again to retry.', ConnectionError('Fixture connection is unavailable.'))
        view.account_error.toggle.click()
        for width, height, points in ((840, 660, 10), (1100, 700, 10), (480, 540, 10), (620, 540, 24), (480, 540, 24)):
            view.setFont(QFont('Arial', points)); view.resize(width, height)
            for page in (view.welcome_page, view.account_page, view.models_page, view.ready_page):
                with self.subTest(size=(width, height, points), page=view.stack.indexOf(page)):
                    view.stack.setCurrentWidget(page); settle()
                    self.assertEqual(page.horizontalScrollBar().maximum(), 0)
                    self.assertLessEqual(page.widget().width(), page.viewport().width())
                    for button in view.footers.currentWidget().findChildren(QPushButton):
                        if not button.isVisible():
                            continue
                        option = QStyleOptionButton(); button.initStyleOption(option)
                        bounds = button.style().subElementRect(QStyle.SubElement.SE_PushButtonContents, option, button)
                        self.assertGreaterEqual(bounds.width(), button.fontMetrics().horizontalAdvance(button.text()))
                        self.assertGreaterEqual(bounds.height(), button.fontMetrics().height())
                        self.assertTrue(view.rect().contains(button.mapTo(view, button.rect().bottomRight())))
            self.assertEqual(view.txt_collab_id.font().pointSize(), points)
        self.assertGreater(view.account_page.verticalScrollBar().maximum(), 0)


if __name__ == '__main__':
    unittest.main()
