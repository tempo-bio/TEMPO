"""Tests for TEMPO GUI in-app uninstallation dialog and launcher."""
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
import subprocess
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, Mock, patch

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QApplication, QDialog, QMainWindow

from tests.qt_lifecycle import close_and_dispose
from tempo_ui import install_ui

with patch('platform.machine', return_value='x86_64'):
    import tempo_gui as gui
from tempo_uninstall import UninstallTempoDialog, find_windows_uninstaller

APP = QApplication.instance() or QApplication([])
install_ui(APP)


def close_fixture(view):
    close_and_dispose(view)


class UninstallDialogTests(unittest.TestCase):
    def setUp(self):
        self.app = APP

    def test_find_windows_uninstaller(self):
        with patch.object(sys, 'platform', 'linux'), patch.object(sys, 'frozen', False, create=True):
            self.assertIsNone(find_windows_uninstaller())

        with patch.object(sys, 'platform', 'win32'), patch.object(Path, 'exists', return_value=True):
            uninstaller = find_windows_uninstaller()
            self.assertIsNotNone(uninstaller)
            self.assertTrue(str(uninstaller).endswith('unins000.exe'))

    def test_dialog_initialization_and_preflight_no_unsynced(self):
        parent = QMainWindow()
        self.addCleanup(close_fixture, parent)
        parent.tab_annotator = SimpleNamespace(universal_annotations={
            'img1.jpg': {'sync_pending': False},
            'img2.jpg': {'sync_pending': False}
        })
        with patch('tempo_uninstall.find_windows_uninstaller', return_value=Path('/test/Programs/TEMPO/unins000.exe')):
            dlg = UninstallTempoDialog(parent)
            self.addCleanup(close_fixture, dlg)

        self.assertEqual(dlg.windowTitle(), 'Uninstall TEMPO')
        self.assertFalse(dlg.cb_prefs.isChecked())
        self.assertFalse(dlg.cb_creds.isChecked())
        self.assertIn('All cloud annotations are synchronized', dlg.notice.text())
        self.assertEqual(dlg.btn_uninstall.text(), 'Uninstall and Exit')

    def test_dialog_preflight_with_unsynced_drafts(self):
        parent = QMainWindow()
        self.addCleanup(close_fixture, parent)
        parent.tab_annotator = SimpleNamespace(universal_annotations={
            'img1.jpg': {'sync_pending': True},
            'img2.jpg': {'sync_pending': True}
        })
        with patch('tempo_uninstall.find_windows_uninstaller', return_value=None):
            dlg = UninstallTempoDialog(parent)
            self.addCleanup(close_fixture, dlg)

        self.assertIn('Warning: You have 2 local annotation drafts pending sync', dlg.notice.text())
        self.assertIn('Clean', dlg.btn_uninstall.text())

    def test_execute_uninstall_launches_process_and_cleans_requested_data(self):
        parent = QMainWindow()
        self.addCleanup(close_fixture, parent)
        parent.close = Mock()

        fake_uninstaller = Path('/fake/path/unins000.exe')
        with patch('tempo_uninstall.find_windows_uninstaller', return_value=fake_uninstaller), \
             patch.object(Path, 'exists', return_value=True), \
             patch('tempo_secure_store.delete_hf_token') as mock_del_token, \
             patch('tempo_hub.tempo_hub') as mock_hub, \
             patch('os.path.exists', return_value=True), \
             patch('os.remove') as mock_remove, \
             patch('subprocess.Popen') as mock_popen, \
             patch.object(QApplication, 'quit') as mock_quit:

            dlg = UninstallTempoDialog(parent)
            self.addCleanup(close_fixture, dlg)

            dlg.cb_prefs.setChecked(True)
            dlg.cb_creds.setChecked(True)

            dlg.execute_uninstall()

            mock_del_token.assert_called_once()
            self.assertIsNone(mock_hub.token)
            mock_remove.assert_called_once()
            mock_popen.assert_called_once()
            self.assertIn(str(fake_uninstaller), mock_popen.call_args[0][0])
            parent.close.assert_called_once()
            mock_quit.assert_called_once()

    def test_settings_tab_tools_menu_has_uninstall_action(self):
        settings = gui.SettingsTab()
        self.addCleanup(close_fixture, settings)

        menu_actions = [btn.text() for btn in settings.tools_menu.buttons]
        self.assertIn('Uninstall TEMPO...', menu_actions)

        with patch.object(settings, 'run_uninstall') as mock_run, \
             patch('tempo_uninstall.UninstallTempoDialog.exec'):
            uninstall_btn = [btn for btn in settings.tools_menu.buttons if btn.text() == 'Uninstall TEMPO...'][0]
            uninstall_btn.click()
            mock_run.assert_called_once()


if __name__ == '__main__':
    unittest.main()
