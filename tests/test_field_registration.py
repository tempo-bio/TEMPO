"""Incomplete or conflicting field registrations remain unplaced."""
import copy
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from PyQt6.QtCore import QCoreApplication, QEvent
from PyQt6.QtWidgets import QApplication
import pandas as pd
import tempo_config as config
import setup_wizard as setup
with patch('platform.machine', return_value='x86_64'):
    import tempo_gui as gui
from tests.presentation_fixtures import write_dataset

APP = QApplication.instance() or QApplication([])


class FieldRegistrationTests(unittest.TestCase):

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

    def test_first_unassociated_cell_does_not_hide_valid_field_registration(self):
        path = write_dataset(self.root, registered=True)
        data = pd.read_csv(path)
        data.loc[0, 'Spatial_Metadata_File'] = None
        data.to_csv(path, index=False)
        view = gui.AnalyzerTab(); self.addCleanup(self.dispose, view)
        view.load_csv(str(path))
        self.assertIn(('recording-1', 'FOV001'), view.spatial_registrations)
        self.assertEqual(len(view.current_x_vals), 12)
        self.assertEqual(len(view.current_well_data), 12)

    def test_conflicting_field_registration_remains_unplaced(self):
        path = write_dataset(self.root, registered=True)
        data = pd.read_csv(path)
        data.loc[0, 'Spatial_Metadata_File'] = 'conflicting-registration.json'
        data.to_csv(path, index=False)
        view = gui.AnalyzerTab(); self.addCleanup(self.dispose, view)
        view.load_csv(str(path))
        self.assertNotIn(('recording-1', 'FOV001'), view.spatial_registrations)
        self.assertTrue(any('Conflicting' in warning for warning in view.spatial_warnings))
        self.assertEqual(len(view.current_well_data), 12)


if __name__ == "__main__":
    unittest.main()
