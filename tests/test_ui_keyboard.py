"""Keyboard operation of shared error disclosure, without remote services."""
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
import unittest
from PyQt6.QtCore import Qt, QPoint
from PyQt6.QtTest import QTest
from PyQt6.QtGui import QFont
from PyQt6.QtWidgets import (QApplication, QDialog, QVBoxLayout, QPushButton, QCheckBox,
                            QRadioButton, QToolButton, QSlider, QSpinBox, QComboBox,
                            QLineEdit, QListWidget, QTabWidget, QWidget)
from tempo_ui import ErrorDetails, FilenameLabel, InformationButton, OnOffSwitch, SlidingChoice, install_ui

APP = QApplication.instance() or QApplication([])
install_ui(APP)


class FocusAppearanceTests(unittest.TestCase):
    def test_controls_keep_the_same_paint_after_pointer_departure_and_focus_change(self):
        factories = [lambda: QCheckBox('Show outlines'), lambda: QRadioButton('Option'),
                     lambda: QPushButton('Apply'), QToolButton,
                     lambda: InformationButton('Preview', 'Image details'),
                     lambda: OnOffSwitch('Stimulation'),
                     lambda: SlidingChoice(('Manual','Timed'),('manual','timed')),
                     lambda: QSlider(Qt.Orientation.Horizontal), QComboBox,
                     QListWidget, QTabWidget]
        for factory in factories:
            with self.subTest(control=factory):
                view = QDialog(); self.addCleanup(view.deleteLater)
                layout = QVBoxLayout(view); control = factory(); layout.addWidget(control)
                if isinstance(control, QCheckBox): control.setChecked(True)
                if isinstance(control, QRadioButton): control.setChecked(True)
                if isinstance(control, QComboBox): control.addItems(['First', 'Second'])
                if isinstance(control, QListWidget): control.addItems(['First', 'Second']); control.setCurrentRow(0)
                if isinstance(control, QTabWidget): control.addTab(QWidget(), 'First'); control.addTab(QWidget(), 'Second')
                if isinstance(control, QToolButton): control.setText('Tools')
                other = QPushButton('Next'); layout.addWidget(other)
                view.show(); APP.processEvents()
                QTest.mouseMove(other, QPoint(2, 2)); APP.processEvents()
                control.setFocus(Qt.FocusReason.TabFocusReason); APP.processEvents()
                focused = control.grab().toImage()
                other.setFocus(); APP.processEvents()
                self.assertEqual(focused, control.grab().toImage(), type(control).__name__)
                view.close()

    def test_stimulation_controls_keep_state_and_keyboard_operation_without_click_glow(self):
        for control,key in ((OnOffSwitch('Stimulation'),Qt.Key.Key_Space),
                            (SlidingChoice(('Manual','Timed'),('manual','timed')),Qt.Key.Key_Left)):
            with self.subTest(control=type(control).__name__):
                view=QDialog(); self.addCleanup(view.deleteLater)
                layout=QVBoxLayout(view); layout.addWidget(control)
                next_button=QPushButton('Next'); layout.addWidget(next_button)
                view.show(); APP.processEvents()
                QTest.mouseClick(control,Qt.MouseButton.LeftButton,
                                 pos=QPoint(3*control.width()//4,control.height()//2))
                QTest.mouseMove(next_button,QPoint(2,2)); APP.processEvents()
                self.assertTrue(control.hasFocus())
                self.assertTrue(control.isChecked() if isinstance(control,OnOffSwitch) else control.currentData()=='timed')
                clicked=control.grab().toImage()
                next_button.setFocus(); APP.processEvents()
                self.assertEqual(clicked,control.grab().toImage())
                QTest.keyClick(next_button,Qt.Key.Key_Backtab)
                self.assertIs(APP.focusWidget(),control)
                QTest.keyClick(control,key)
                self.assertFalse(control.isChecked() if isinstance(control,OnOffSwitch) else control.currentData()=='timed')
                view.close()

    def test_checkbox_click_departure_and_keyboard_keep_state_without_glow(self):
        view = QDialog(); self.addCleanup(view.deleteLater)
        layout = QVBoxLayout(view)
        box = QCheckBox('Every cell in this image is annotated'); layout.addWidget(box)
        next_button = QPushButton('Next'); layout.addWidget(next_button)
        view.show(); APP.processEvents()
        QTest.mouseClick(box, Qt.MouseButton.LeftButton, pos=QPoint(8, box.height() // 2))
        QTest.mouseMove(next_button, QPoint(2, 2)); APP.processEvents()
        self.assertTrue(box.isChecked()); self.assertTrue(box.hasFocus())
        clicked = box.grab().toImage(); next_button.setFocus(); APP.processEvents()
        self.assertEqual(clicked, box.grab().toImage())
        QTest.keyClick(next_button, Qt.Key.Key_Backtab)
        self.assertIs(APP.focusWidget(), box)
        QTest.keyClick(box, Qt.Key.Key_Space)
        self.assertFalse(box.isChecked())
        QTest.keyClick(box, Qt.Key.Key_Tab)
        self.assertIs(APP.focusWidget(), next_button)
        view.close()

    def test_text_field_border_is_identical_with_keyboard_focus(self):
        view = QDialog(); self.addCleanup(view.deleteLater)
        layout = QVBoxLayout(view)
        field = QLineEdit('Dataset'); layout.addWidget(field)
        button = QPushButton('Next'); layout.addWidget(button)
        view.show(); APP.processEvents()
        QTest.mouseMove(button, QPoint(2, 2)); field.setFocus(); APP.processEvents()
        focused = field.grab().toImage()
        button.setFocus(); APP.processEvents(); ordinary = field.grab().toImage()
        self.assertEqual(focused.copy(0, 0, focused.width(), 3), ordinary.copy(0, 0, ordinary.width(), 3))
        view.close()

    def test_spinbox_border_is_neutral_while_text_remains_editable(self):
        view = QDialog(); self.addCleanup(view.deleteLater)
        layout = QVBoxLayout(view); field = QSpinBox(); layout.addWidget(field)
        button = QPushButton('Next'); layout.addWidget(button)
        view.show(); APP.processEvents()
        QTest.mouseMove(button, QPoint(2, 2)); field.setFocus(); APP.processEvents()
        focused = field.grab().toImage()
        button.setFocus(); APP.processEvents(); ordinary = field.grab().toImage()
        self.assertEqual(focused.copy(0, 0, focused.width(), 3), ordinary.copy(0, 0, ordinary.width(), 3))
        field.setFocus(); QTest.keyClick(field, Qt.Key.Key_Up)
        self.assertEqual(field.value(), 1)
        view.close()

    def test_default_button_keeps_enter_activation_without_default_decoration(self):
        view = QDialog(); self.addCleanup(view.deleteLater)
        layout = QVBoxLayout(view); field = QLineEdit(); layout.addWidget(field)
        button = QPushButton('Save'); button.setAutoDefault(False); layout.addWidget(button)
        clicks = []; button.clicked.connect(lambda: clicks.append(True))
        view.show(); field.setFocus(); QTest.mouseMove(field, QPoint(2, 2)); APP.processEvents()
        ordinary = button.grab().toImage(); button.setDefault(True); APP.processEvents()
        self.assertEqual(ordinary, button.grab().toImage())
        QTest.keyClick(field, Qt.Key.Key_Return)
        self.assertEqual(clicks, [True]); self.assertTrue(button.isDefault())
        view.close()


class ErrorKeyboardTests(unittest.TestCase):
    def test_filename_can_be_selected_and_left_with_tab(self):
        view = QDialog(); self.addCleanup(view.close)
        layout = QVBoxLayout(view)
        name = FilenameLabel(); name.setText('original_image_name.tif'); layout.addWidget(name)
        action = QPushButton('Next'); layout.addWidget(action)
        view.show(); APP.processEvents()
        name.setFocus(); name.selectAll()
        self.assertEqual(name.textCursor().selectedText(), 'original_image_name.tif')
        QTest.keyClick(name, Qt.Key.Key_Tab)
        self.assertIs(APP.focusWidget(), action)
        QTest.keyClick(action, Qt.Key.Key_Backtab)
        self.assertIs(APP.focusWidget(), name)

    def test_narrow_error_summary_reserves_wrapped_text_height(self):
        view = QDialog(); self.addCleanup(view.close)
        font = QFont(view.font()); font.setPointSize(18); view.setFont(font)
        layout = QVBoxLayout(view)
        error = ErrorDetails(); layout.addWidget(error)
        error.set_error('Submission did not finish. Review the details and image statuses before retrying Submit Selected.', ConnectionError('offline'))
        view.resize(310, 360); view.show()
        for _ in range(4): APP.processEvents()
        self.assertGreaterEqual(error.summary.height(), error.summary.heightForWidth(error.summary.width()))
        view.resize(260, 360)
        for _ in range(4): APP.processEvents()
        self.assertGreaterEqual(error.summary.height(), error.summary.heightForWidth(error.summary.width()))

    def test_details_can_be_opened_read_and_left_with_keyboard(self):
        view = QDialog(); self.addCleanup(view.close)
        layout = QVBoxLayout(view)
        error = ErrorDetails(); layout.addWidget(error)
        retry = QPushButton('Retry'); layout.addWidget(retry)
        clicks = []; retry.clicked.connect(lambda: clicks.append(True))
        error.set_error('Could not connect. Choose Retry.', ConnectionError('offline'))
        view.show(); APP.processEvents()
        error.toggle.setFocus()
        QTest.keyClick(error.toggle, Qt.Key.Key_Space); APP.processEvents()
        self.assertFalse(error.details.isHidden())
        QTest.keyClick(error.toggle, Qt.Key.Key_Tab)
        self.assertIs(APP.focusWidget(), error.details)
        QTest.keyClick(error.details, Qt.Key.Key_Tab)
        self.assertIs(APP.focusWidget(), retry)
        QTest.keyClick(retry, Qt.Key.Key_Space)
        self.assertEqual(clicks, [True])
        QTest.keyClick(retry, Qt.Key.Key_Backtab)
        self.assertIs(APP.focusWidget(), error.details)
