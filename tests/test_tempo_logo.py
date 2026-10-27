"""Offscreen animation direction, image stability, and window lifecycle checks."""
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
from pathlib import Path
import unittest
from unittest.mock import patch

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QImage, QPainter
from PyQt6.QtTest import QTest
from PyQt6.QtWidgets import QApplication, QLabel

from tempo_logo import AnimatedTempoLogo, AnimatedTempoWordmark, AboutTempoDialog, trace_interval

APP = QApplication.instance() or QApplication([])


class TempoLogoTests(unittest.TestCase):
    def dispose(self, widget):
        widget.close()
        widget.deleteLater()
        APP.processEvents()

    def render(self, logo, seconds):
        logo._timer.stop()
        with patch('tempo_logo.trace_interval', return_value=trace_interval(seconds)):
            image = QImage(logo.size(), QImage.Format.Format_ARGB32)
            image.fill(Qt.GlobalColor.transparent)
            painter = QPainter(image)
            logo.render(painter)
            painter.end()
        return image

    def colored_pixels(self, image):
        return [(x, y) for y in range(image.height()) for x in range(image.width())
                if (c := image.pixelColor(x, y)).green() > 120 and c.green() - c.red() > 45]

    def logo(self):
        logo = AnimatedTempoLogo(width=320)
        self.addCleanup(self.dispose, logo)
        logo.resize(logo.sizeHint())
        logo.show()
        APP.processEvents()
        return logo

    def test_draw_and_erase_travel_forward_and_leave_no_endpoint_dot(self):
        logo = self.logo()
        empty = self.render(logo, 0)
        self.assertEqual(empty, self.render(logo, 2.95))
        self.assertEqual(empty, self.render(logo, 3))
        self.assertFalse(self.colored_pixels(empty))
        draws = [self.colored_pixels(self.render(logo, t)) for t in (.3, .6, .9, 1.5)]
        self.assertEqual(sorted(map(len, draws)), list(map(len, draws)))
        self.assertEqual(sorted(max(x for x, _ in pixels) for pixels in draws),
                         [max(x for x, _ in pixels) for pixels in draws])
        erases = [self.colored_pixels(self.render(logo, t)) for t in (1.65, 1.95, 2.25, 2.55)]
        self.assertEqual(sorted(map(len, erases), reverse=True), list(map(len, erases)))
        self.assertEqual(sorted(min(x for x, _ in pixels) for pixels in erases),
                         [min(x for x, _ in pixels) for pixels in erases])

    def test_underlay_contains_complete_white_letters_and_an_empty_dark_gap(self):
        logo = self.logo()
        base = logo._base
        self.assertEqual((base.width(), base.height()), (940, 410))
        for x, y in ((125, 225), (230, 140), (585, 140), (870, 205)):
            color = base.pixelColor(x, y)
            self.assertGreater(color.alpha(), 240)
            self.assertGreater(min(color.red(), color.green(), color.blue()), 200)
        for x, y in ((20, 20), (450, 210), (498, 73), (452, 338)):
            color = base.pixelColor(x, y)
            self.assertGreater(color.alpha(), 240)
            self.assertLess(max(color.red(), color.green(), color.blue()), 60)

    def test_letters_stay_still_and_stroke_stays_inside_its_patch(self):
        logo = self.logo()
        base = self.render(logo, 0)
        for seconds in (.3, .7, 1.5, 2.1, 2.65):
            frame = self.render(logo, seconds)
            for region in ((15, 40, 55, 60), (70, 40, 45, 35), (198, 40, 38, 20), (240, 35, 60, 70)):
                self.assertEqual(base.copy(*region), frame.copy(*region), (seconds, region))
            for y in range(frame.height()):
                for x in range(frame.width()):
                    if frame.pixel(x, y) != base.pixel(x, y):
                        self.assertTrue(70 <= x <= 230 and 10 <= y <= 130, (seconds, x, y))

    def test_timer_runs_only_while_the_logo_is_visible(self):
        logo = self.logo()
        self.assertTrue(logo._timer.isActive())
        before = logo._clock.nsecsElapsed()
        QTest.qWait(40)
        self.assertGreater(logo._clock.nsecsElapsed(), before)
        logo.hide()
        self.assertFalse(logo._timer.isActive())
        logo.show()
        self.assertTrue(logo._timer.isActive())
        logo.close()
        self.assertFalse(logo._timer.isActive())

    def test_setup_wordmark_remains_visible_across_steps_and_pauses_on_close(self):
        import setup_wizard as setup
        with patch.object(setup.SetupWizardDialog, '_check_saved_token'):
            view = setup.SetupWizardDialog()
            self.addCleanup(self.dispose, view)
            view.show()
            APP.processEvents()
            self.assertTrue(view.logo._timer.isActive())
            view.stack.setCurrentIndex(1)
            APP.processEvents()
            self.assertTrue(view.logo._timer.isActive())
            view.stack.setCurrentIndex(0)
            APP.processEvents()
            self.assertTrue(view.logo._timer.isActive())
            view.close()
            self.assertFalse(view.logo._timer.isActive())

    def test_transparent_wordmark_preserves_the_parent_background_and_static_letters(self):
        from PyQt6.QtWidgets import QWidget
        parent = QWidget()
        parent.resize(320, 123)
        parent.setStyleSheet('background: #653a48;')
        wordmark = AnimatedTempoWordmark(parent, width=320)
        wordmark.resize(wordmark.sizeHint())
        parent.show(); APP.processEvents()
        self.addCleanup(self.dispose, parent)
        frames = []
        wordmark._timer.stop()
        for seconds in (0, .7, 1.5, 2.1, 2.95):
            with patch('tempo_logo.trace_interval', return_value=trace_interval(seconds)):
                frames.append(parent.grab().toImage())
        self.assertEqual(frames[0], frames[-1])
        for frame in frames:
            self.assertEqual(frame.pixelColor(0, 0).name(), '#653a48')
            for region in ((5, 30, 55, 50), (245, 25, 55, 70)):
                self.assertEqual(frame.copy(*region), frames[0].copy(*region))
        self.assertGreater(len(self.colored_pixels(frames[2])), len(self.colored_pixels(frames[0])))

    def test_older_qt_fallback_preserves_the_same_trace_and_direction(self):
        import tempo_logo
        logo = self.logo()
        native = self.colored_pixels(self.render(logo, .75))
        trim = tempo_logo._trimmed_path
        with patch('tempo_logo._trimmed_path', side_effect=lambda path, start, end: trim(object(), start, end)):
            fallback = self.colored_pixels(self.render(logo, .75))
        self.assertLess(abs(len(native) - len(fallback)), max(20, len(native) * .01))
        self.assertLessEqual(abs(max(x for x, _ in native) - max(x for x, _ in fallback)), 1)

    def test_about_centers_animation_above_selectable_details_and_closes_with_escape(self):
        path = Path(__file__).resolve().parents[1] / 'tempo_gui.py'
        with patch('tempo_release.VERSION', '1.0.0'):
            dialog = AboutTempoDialog(application_path=path)
        self.addCleanup(self.dispose, dialog)
        dialog.show()
        APP.processEvents()
        self.assertIn(str(path), dialog.details.toPlainText())
        self.assertIn('Python:', dialog.details.toPlainText())
        self.assertIn('Qt:', dialog.details.toPlainText())
        self.assertTrue(dialog.details.isReadOnly())
        self.assertFalse(any(label.text() == 'TEMPO' for label in dialog.findChildren(QLabel)))
        self.assertEqual(dialog.logo.version_label.text(), 'v1.0')
        self.assertLessEqual(abs(dialog.logo.geometry().center().x() - dialog.rect().center().x()), 1)
        self.assertLess(dialog.logo.geometry().bottom(), dialog.details.mapTo(dialog, dialog.details.rect().topLeft()).y())
        QTest.keyClick(dialog, Qt.Key.Key_Escape)
        self.assertFalse(dialog.isVisible())
        self.assertFalse(dialog.logo._timer.isActive())

    def test_about_wraps_long_paths_at_small_width_and_large_font(self):
        from PyQt6.QtGui import QFont
        dialog = AboutTempoDialog(application_path=Path('/long-application-folder' * 10) / 'tempo_gui.py')
        self.addCleanup(self.dispose, dialog)
        dialog.setFont(QFont('Arial', 24))
        dialog.resize(360, 260)
        dialog.show()
        APP.processEvents()
        self.assertFalse(dialog.details.horizontalScrollBar().isVisible())
        self.assertEqual(dialog.details.horizontalScrollBar().maximum(), 0)
        self.assertGreater(dialog.details.verticalScrollBar().maximum(), 0)
        self.assertTrue(dialog.details.viewport().rect().isValid())

    def test_about_version_uses_app_background_color_and_changes_without_new_artwork(self):
        from PyQt6.QtGui import QFont, QPalette
        old = AboutTempoDialog(application_path=__file__)
        self.addCleanup(self.dispose, old)
        with patch('tempo_release.VERSION', '1.2.3'):
            current = AboutTempoDialog(application_path=__file__)
        self.addCleanup(self.dispose, current)
        self.assertEqual(current.logo.version_label.text(), 'v1.2.3')
        self.assertEqual(old.logo._base.cacheKey(), current.logo._base.cacheKey())
        for size, font in (((620, 360), 13), ((900, 450), 13), ((360, 260), 24)):
            current.setFont(QFont('Arial', font))
            current.resize(*size)
            current.show()
            APP.processEvents()
            label = current.logo.version_label
            self.assertEqual(label.palette().color(QPalette.ColorRole.WindowText),
                             APP.palette().color(QPalette.ColorRole.Window))
            self.assertTrue(current.logo._target_rect().contains(label.geometry().toRectF()))
            self.assertLess(label.width(), current.logo.width() * .4)
        self.assertIsNone(self.logo().version_label)

    def test_launcher_disposal_cancels_delayed_status_and_animation(self):
        import update_wizard as updates
        from PyQt6.QtCore import QCoreApplication, QEvent
        import sys
        failures = []
        with patch.object(updates.StartupUpdateCheck, 'start'), patch.object(sys, 'excepthook', side_effect=lambda *args: failures.append(args)):
            view = updates.SplashScreen()
            view.show()
            APP.processEvents()
            self.assertTrue(view.logo._timer.isActive())
            view.reject()
            self.assertFalse(view.logo._timer.isActive())
            self.assertFalse(view._status_timer.isActive())
            view.deleteLater()
            QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
            QTest.qWait(130)
        self.assertFalse(failures)


if __name__ == '__main__':
    unittest.main()
