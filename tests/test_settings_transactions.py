"""Settings persistence preserves active values and previous files on failure."""
import copy
import json
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from PyQt6.QtCore import QCoreApplication, QEvent
from PyQt6.QtWidgets import QApplication
import tempo_config as config
import setup_wizard as setup
with patch('platform.machine', return_value='x86_64'):
    import tempo_gui as gui

APP = QApplication.instance() or QApplication([])


class SettingsTransactionTests(unittest.TestCase):

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

    def test_activity_icon_opt_in_applies_only_after_successful_personal_save_and_survives_reload(self):
        view = gui.SettingsTab(); self.addCleanup(self.dispose, view)
        checkbox = view.widgets['appearance', 'animate_activity_icon']
        self.assertFalse(checkbox.isChecked())
        applied = []
        view.settings_saved.connect(lambda: applied.append(config.tempo_preferences.get('appearance', 'animate_activity_icon')))
        checkbox.setChecked(True)
        self.assertFalse(config.tempo_preferences.get('appearance', 'animate_activity_icon'))
        with patch.object(config.tempo_preferences, 'save_to_disk', return_value=False):
            view.save_preferences()
        self.assertFalse(config.tempo_preferences.get('appearance', 'animate_activity_icon'))
        self.assertFalse(applied)
        view.save_preferences()
        self.assertEqual(applied, [True])
        config.tempo_preferences.load()
        self.assertTrue(config.tempo_preferences.get('appearance', 'animate_activity_icon'))

    def test_settings_failed_save_preserves_active_values_and_disk(self):
        self.assertTrue(config.tempo_preferences.save())
        before = Path(config.CONFIG_PATH).read_bytes()
        view = gui.SettingsTab(); self.addCleanup(self.dispose, view)
        saved = []; view.settings_saved.connect(lambda: saved.append(True))
        view.widgets['capture', 'target_fps'].setValue(123)
        with patch.object(config.tempo_preferences, 'save_to_disk', return_value=False):
            view.save_preferences()
        self.assertEqual(config.tempo_preferences.get('capture', 'target_fps'), 100)
        self.assertEqual(Path(config.CONFIG_PATH).read_bytes(), before)
        self.assertEqual(saved, [])
        self.assertEqual(view.widgets['capture', 'target_fps'].value(), 123)
        self.assertIn('could not be saved', view.lbl_save_status.text())

    def test_settings_partial_write_failure_keeps_previous_file(self):
        self.assertTrue(config.tempo_preferences.save())
        before = Path(config.CONFIG_PATH).read_bytes()
        def fail(value, stream, **kwargs):
            stream.write('{partial')
            raise OSError('simulated full disk')
        with patch('tempo_config.json.dump', side_effect=fail):
            self.assertFalse(config.tempo_preferences.save())
        self.assertEqual(Path(config.CONFIG_PATH).read_bytes(), before)
        self.assertEqual(list(self.root.glob('*.tmp')), [])

    def test_settings_replace_failure_keeps_previous_file(self):
        self.assertTrue(config.tempo_preferences.save())
        before = Path(config.CONFIG_PATH).read_bytes()
        with patch('tempo_config.os.replace', side_effect=OSError('simulated locked destination')):
            self.assertFalse(config.tempo_preferences.save())
        self.assertEqual(Path(config.CONFIG_PATH).read_bytes(), before)
        self.assertEqual(list(self.root.glob('*.tmp')), [])

    def test_invalid_saved_settings_reject_the_whole_file_without_rewriting(self):
        for invalid in ('bad', None, True, float('nan'), 123.5):
            with self.subTest(value=invalid):
                integer_key = 'savgol_window_pct'
                saved = {'capture': {'target_fps': {'value': 150}},
                         'kinetics': {integer_key: {'value': invalid}}}
                path = Path(config.CONFIG_PATH); path.write_text(json.dumps(saved))
                before = path.read_bytes()
                config.tempo_preferences.load()
                self.assertEqual(config.tempo_preferences.get('capture', 'target_fps'), 100)
                self.assertIn(integer_key, config.tempo_preferences.load_error)
                self.assertEqual(path.read_bytes(), before)
        view = gui.SettingsTab(); self.addCleanup(self.dispose, view)
        self.assertIn('Defaults are shown', view.lbl_save_status.text())

    def test_valid_settings_normalize_integer_type_and_preserve_decimal_precision(self):
        path = Path(config.CONFIG_PATH)
        path.write_text(json.dumps({'capture': {'target_fps': {'value': 123.0}},
                                    'kinetics': {'asls_p': {'value': 0.00137}}}))
        config.tempo_preferences.load()
        self.assertIs(type(config.tempo_preferences.get('capture', 'target_fps')), int)
        self.assertEqual(config.tempo_preferences.get('kinetics', 'asls_p'), 0.00137)
        self.assertEqual(config.tempo_preferences.load_error, '')
        self.assertTrue(config.tempo_preferences.save())
        self.assertEqual(json.loads(path.read_text())['kinetics']['asls_p']['value'], 0.00137)

    def test_saved_acquisition_panel_widths_do_not_discard_other_preferences(self):
        config.tempo_preferences.set('capture','panel_widths',[330,700,350])
        config.tempo_preferences.set('capture','target_fps',150)
        self.assertTrue(config.tempo_preferences.save())
        config.tempo_preferences.load()
        self.assertEqual(config.tempo_preferences.load_error,'')
        self.assertEqual(config.tempo_preferences.get('capture','panel_widths'),[330,700,350])
        self.assertEqual(config.tempo_preferences.get('capture','target_fps'),150)

    def test_saved_panel_widths_reject_malformed_sizes_without_rewriting(self):
        path=Path(config.CONFIG_PATH)
        for value in ('300,650,320',[300,650],[300,0,320],[300,-1,320],[300,True,320],[300,float('nan'),320],[300,1.5,320]):
            with self.subTest(value=value):
                path.write_text(json.dumps({'capture':{'panel_widths':{'value':value}}}))
                before=path.read_bytes(); config.tempo_preferences.load()
                self.assertIn('panel_widths',config.tempo_preferences.load_error)
                self.assertEqual(path.read_bytes(),before)


if __name__ == "__main__":
    unittest.main()
