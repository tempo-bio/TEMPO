"""Verify an approved package is installed only into the application profile."""
import hashlib
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from scripts.build_optional_package import RELEASE_VERSION, build
from tempo_optional_packages import INVESTIGATION, PACKAGES, install, is_installed, remove, show_package_manager


class OptionalPackageTests(unittest.TestCase):
    def test_reproducible_install_rejects_tampering_and_removal_preserves_research(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = build(root / 'release')
            copy = build(root / 'rebuild')
            payload = source.read_bytes()
            self.assertEqual(payload, copy.read_bytes())
            self.assertEqual(hashlib.sha256(payload).hexdigest(),
                             PACKAGES[INVESTIGATION].archives[RELEASE_VERSION])
            profile = root / 'profile' / 'optional-packages'
            research = root / 'research.json'
            research.write_bytes(b'Existing experiment')
            self.assertFalse(is_installed(INVESTIGATION, profile))
            self.assertEqual(install(source, profile), (INVESTIGATION, RELEASE_VERSION))
            installed, = profile.iterdir()
            self.assertEqual(installed.read_bytes(), payload)
            copy.write_bytes(payload + b'Changed')
            with self.assertRaisesRegex(ValueError, 'approved TEMPO package'):
                install(copy, profile)
            self.assertEqual(installed.read_bytes(), payload)
            remove(INVESTIGATION, profile)
            remove(INVESTIGATION, profile)
            self.assertFalse(is_installed(INVESTIGATION, profile))
            self.assertEqual(research.read_bytes(), b'Existing experiment')

    def test_install_and_remove_from_optional_packages_dialog(self):
        os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
        from PyQt6.QtCore import Qt
        from PyQt6.QtTest import QTest
        from PyQt6.QtWidgets import QApplication, QLabel, QListWidget, QPushButton

        app = QApplication.instance() or QApplication([])
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = build(root)
            profile = root / 'profile'
            with patch('tempo_optional_packages.package_root', return_value=profile), \
                 patch('PyQt6.QtWidgets.QFileDialog.getOpenFileName', return_value=(str(source), '')):
                dialog = show_package_manager(None)
                dialog.show()
                app.processEvents()
                try:
                    listing = dialog.findChild(QListWidget)
                    buttons = {button.text(): button for button in dialog.findChildren(QPushButton)}
                    status = dialog.findChild(QLabel)
                    self.assertFalse(buttons['Remove'].isEnabled())
                    QTest.mouseClick(buttons['Install Package…'], Qt.MouseButton.LeftButton)
                    self.assertIn('Installed', listing.currentItem().text())
                    self.assertIn('Restart TEMPO', status.text())
                    self.assertTrue(is_installed(INVESTIGATION, profile))
                    QTest.mouseClick(buttons['Remove'], Qt.MouseButton.LeftButton)
                    self.assertIn('Available', listing.currentItem().text())
                    self.assertFalse(is_installed(INVESTIGATION, profile))
                finally:
                    dialog.close()
