"""Pointer regressions for drawing, refresh preservation and button focus."""
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from PyQt6.QtCore import Qt, QPoint, QPointF
from PyQt6.QtGui import QImage, QPolygonF
from PyQt6.QtTest import QTest
from PyQt6.QtWidgets import QApplication, QWidget, QPushButton, QVBoxLayout
from tempo_tasks import dispose_closed_views
from tempo_ui import install_ui
with patch('platform.machine', return_value='x86_64'):
    from tempo_gui import AnnotationView

APP = QApplication.instance() or QApplication([])
install_ui(APP)


class AnnotationDrawingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.image = str(Path(self.temp.name)/'image.png')
        image = QImage(400, 400, QImage.Format.Format_RGB32); image.fill(Qt.GlobalColor.white); image.save(self.image)
        self.view = AnnotationView(); self.view.resize(600, 500)
        self.view.set_image(self.image); self.view.show(); APP.processEvents()
        self.outlines = []; self.notices = []
        self.view.polygon_drawn.connect(self.outlines.append)
        self.view.drawing_status.connect(self.notices.append)
        self.addCleanup(self.dispose)

    def dispose(self):
        self.view.close(); dispose_closed_views(self.view); APP.processEvents()

    def click(self, x, y, button=Qt.MouseButton.LeftButton, double=False):
        action = QTest.mouseDClick if double else QTest.mouseClick
        action(self.view.viewport(), button, pos=self.view.mapFromScene(QPointF(x, y)))

    def test_rapid_third_vertex_is_retained_and_right_click_finishes_once(self):
        self.click(80, 80); self.click(85, 80); self.click(85, 85, double=True)
        self.assertEqual(len(self.view.current_polygon_pts), 3)
        self.assertFalse(self.outlines)
        self.click(85, 85, Qt.MouseButton.RightButton)
        self.assertEqual(len(self.outlines), 1)
        self.assertEqual(len(self.outlines[0]), 3)
        self.assertFalse(self.view.current_polygon_pts)
        self.click(85, 85, Qt.MouseButton.RightButton)
        self.assertEqual(len(self.outlines), 1)

    def test_double_click_same_position_does_not_duplicate_or_finish(self):
        self.click(80, 80); self.click(80, 80, double=True)
        self.assertEqual(len(self.view.current_polygon_pts), 1)
        self.assertFalse(self.outlines)
        self.click(180, 80); self.click(180, 180, double=True)
        self.assertEqual(len(self.view.current_polygon_pts), 3)

    def test_incomplete_or_zero_area_finish_preserves_outline(self):
        self.click(80, 80); self.click(180, 80)
        self.click(180, 80, Qt.MouseButton.RightButton)
        self.assertEqual(len(self.view.current_polygon_pts), 2)
        self.assertIn('three corners', self.notices[-1])
        self.click(280, 80); self.click(280, 80, Qt.MouseButton.RightButton)
        self.assertEqual(len(self.view.current_polygon_pts), 3)
        self.assertFalse(self.outlines)
        self.click(280, 180); self.click(280, 180, Qt.MouseButton.RightButton)
        self.assertEqual(len(self.outlines), 1)

    def test_escape_cancels_only_unfinished_geometry(self):
        self.click(80, 80); self.click(180, 80); self.click(180, 180)
        self.click(180, 180, Qt.MouseButton.RightButton)
        self.click(200, 200)
        QTest.keyClick(self.view, Qt.Key.Key_Escape)
        self.assertFalse(self.view.current_polygon_pts)
        self.assertEqual(len(self.outlines), 1)
        self.assertEqual(self.notices[-1], '')

    def test_same_image_mode_and_zoom_preserve_coordinates(self):
        self.click(80, 80); self.click(180, 80)
        points = [point[:] for point in self.view.current_polygon_pts]
        self.view.set_image(self.image); self.view.set_draw_mode('polygon')
        self.view.zoom_image(1.2)
        self.assertEqual(self.view.current_polygon_pts, points)
        self.click(180, 180); expected = [point[:] for point in self.view.current_polygon_pts]
        self.click(180, 180, Qt.MouseButton.RightButton)
        self.assertEqual(self.outlines[0], expected)

    def test_readonly_and_changed_mode_discard_unfinished_geometry(self):
        self.click(80, 80); self.view.set_draw_mode('none')
        self.click(180, 180, double=True)
        self.assertFalse(self.view.current_polygon_pts)
        self.assertFalse(self.outlines)

    def test_selection_during_box_drag_preserves_box_until_ordinary_release(self):
        self.view.set_draw_mode('box')
        viewport = self.view.viewport()
        QTest.mousePress(viewport, Qt.MouseButton.LeftButton,
                         pos=self.view.mapFromScene(QPointF(80, 80)))
        QTest.mouseMove(viewport, self.view.mapFromScene(QPointF(180, 180)))
        rectangle = self.view.temp_box_item.rect()
        self.assertGreater(rectangle.width(), 5)
        QTest.mouseClick(viewport, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.ControlModifier,
                         self.view.mapFromScene(QPointF(200, 200)))
        self.assertEqual(self.view.temp_box_item.rect(), rectangle)
        self.assertFalse(self.outlines)
        QTest.keyClick(self.view, Qt.Key.Key_Escape)
        self.assertIsNone(self.view.temp_box_item)

    def test_edge_tolerance_stays_in_screen_pixels_and_refresh_resets_cycle(self):
        item = self.view.scene.addPolygon(QPolygonF([
            QPointF(100,100), QPointF(200,100), QPointF(200,200), QPointF(100,200)]))
        self.view.set_mask_items([item])
        selections = []; self.view.mask_selected.connect(lambda *args: selections.append(args))
        for factor in (1, 2, .25):
            self.view.zoom_image(factor)
            edge = self.view.mapFromScene(QPointF(100,150))
            for distance, count in ((-2, 1), (-7, 1)):
                QTest.mouseClick(self.view.viewport(), Qt.MouseButton.LeftButton,
                                 Qt.KeyboardModifier.ControlModifier, edge + QPoint(distance,0))
                self.assertEqual(len(selections), count)
            selections.clear()
        other = str(Path(self.temp.name)/'other.png')
        QImage(self.image).save(other)
        self.view.set_image(other)
        self.assertEqual(self.view.mask_items, [])

    def test_focus_does_not_change_button_pixels_or_text_geometry(self):
        window = QWidget(); layout = QVBoxLayout(window)
        first = QPushButton('Reserve Image'); second = QPushButton('Fit Image')
        layout.addWidget(first); layout.addWidget(second)
        window.show(); APP.processEvents()
        second.setFocus(); APP.processEvents()
        before = first.grab().toImage(); rectangle = first.rect()
        first.setFocus(Qt.FocusReason.TabFocusReason); APP.processEvents()
        self.assertEqual(first.grab().toImage(), before)
        self.assertEqual(first.rect(), rectangle)
        clicks = []; first.clicked.connect(lambda: clicks.append(True))
        QTest.keyClick(first, Qt.Key.Key_Space)
        self.assertEqual(clicks, [True])
        window.close(); dispose_closed_views(window); APP.processEvents()


if __name__ == '__main__':
    unittest.main()
