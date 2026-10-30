"""Unit and integration tests for TEMPO themes (Dark, Light, Oatmeal)."""
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
import unittest
from unittest.mock import patch
from PyQt6.QtWidgets import QApplication
from PyQt6.QtGui import QPalette, QColor
from PyQt6.QtCore import Qt

from tempo_config import tempo_preferences, DEFAULT_PREFERENCES
from tempo_ui import (THEMES, get_current_theme, get_current_theme_name,
                      apply_theme, install_ui, ThemeSegmentedChoice,
                      description_color, guidance_color)

APP = QApplication.instance() or QApplication([])
with patch('platform.machine', return_value='x86_64'):
    import tempo_gui as gui


class ThemeSystemTests(unittest.TestCase):

    def setUp(self):
        install_ui(APP, 'dark')

    def tearDown(self):
        apply_theme('dark', APP)

    def test_default_preferences_includes_theme(self):
        self.assertIn('appearance', DEFAULT_PREFERENCES)
        self.assertIn('theme', DEFAULT_PREFERENCES['appearance'])
        spec = DEFAULT_PREFERENCES['appearance']['theme']
        self.assertEqual(spec['value'], 'dark')
        self.assertEqual(spec['options'], ['dark', 'light', 'oatmeal'])

    def test_apply_theme_switches_palette_and_active_name(self):
        for theme_name in ('dark', 'light', 'oatmeal'):
            apply_theme(theme_name, APP)
            self.assertEqual(get_current_theme_name(), theme_name)
            current = get_current_theme()
            self.assertEqual(current['name'], theme_name)
            palette = APP.palette()
            expected_window = QColor(current['window'])
            self.assertEqual(palette.color(QPalette.ColorRole.Window), expected_window)

    def test_oatmeal_theme_contrast_and_non_reused_accents(self):
        oat = THEMES['oatmeal']
        self.assertEqual(oat['window'], '#CEBEA5')
        self.assertEqual(oat['base'], '#DDD0BD')
        self.assertEqual(oat['desc_blue'], '#1A4366')
        self.assertEqual(oat['guidance_mint'], '#1B5E35')
        self.assertEqual(oat['stim_digits'], '#1A3B2B')
        self.assertEqual(oat['scope_bg'], '#E2D5C0')
        self.assertEqual(oat['scope_trace'], '#1A4366')
        self.assertNotEqual(oat['desc_blue'], THEMES['dark']['desc_blue'])
        self.assertNotEqual(oat['guidance_mint'], THEMES['dark']['guidance_mint'])
        self.assertNotEqual(oat['stim_digits'], THEMES['dark']['stim_digits'])

    def test_theme_segmented_choice_behavior(self):
        choice = ThemeSegmentedChoice(['dark', 'light', 'oatmeal'], 'dark')
        self.assertEqual(choice.value(), 'dark')
        self.assertTrue(choice.buttons['dark'].isChecked())
        self.assertFalse(choice.buttons['light'].isChecked())
        self.assertFalse(choice.buttons['oatmeal'].isChecked())
        # Labels must be stable and clean without extra state text
        self.assertEqual(choice.buttons['dark'].text(), 'Dark')
        self.assertEqual(choice.buttons['light'].text(), 'Light')
        self.assertEqual(choice.buttons['oatmeal'].text(), 'Oatmeal')

        # Test switching
        emitted = []
        choice.themeChanged.connect(emitted.append)
        choice.buttons['oatmeal'].click()
        self.assertEqual(choice.value(), 'oatmeal')
        self.assertEqual(emitted, ['oatmeal'])
        self.assertTrue(choice.buttons['oatmeal'].isChecked())
        self.assertFalse(choice.buttons['dark'].isChecked())
        # Labels must remain unchanged
        self.assertEqual(choice.buttons['oatmeal'].text(), 'Oatmeal')

    def test_settings_tab_theme_integration(self):
        settings_tab = gui.SettingsTab()
        self.assertIn(('appearance', 'theme'), settings_tab.widgets)
        theme_widget = settings_tab.widgets[('appearance', 'theme')]
        self.assertIsInstance(theme_widget, ThemeSegmentedChoice)
        
        # Verify initial value matches current preferences
        current_pref = tempo_preferences.get('appearance', 'theme', 'dark')
        self.assertEqual(theme_widget.value(), current_pref)

        # Switch theme in UI and verify save state updates
        theme_widget.buttons['light'].click()
        self.assertEqual(settings_tab.preference_values()[('appearance', 'theme')], 'light')
        self.assertEqual(settings_tab.lbl_save_status.text(), 'Unsaved changes')

    def test_dashboard_theme_synchronization(self):
        dashboard = gui.TempoDashboard()
        try:
            dashboard.apply_theme_preferences('oatmeal')
            self.assertEqual(get_current_theme_name(), 'oatmeal')
            self.assertEqual(dashboard.tab_capture.capture_controls.amplitude.styleSheet().find('#1A3B2B') != -1, True)
            self.assertEqual(dashboard.tab_analyzer.glw.backgroundBrush().color(), QColor('#E2D5C0'))
        finally:
            dashboard.close()


if __name__ == '__main__':
    unittest.main()
