"""Exercise tabbed calibration through Acquisition with simulated camera input."""

import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

import numpy as np
from PyQt6.QtCore import Qt
from PyQt6.QtTest import QTest
from PyQt6.QtWidgets import QApplication
from tifffile import imwrite

import tempo_capture_ui as capture_ui
from tempo_focus_calibration import FocusCalibration, FocusSettings, load_profiles, update_profile
from tempo_recording import NativeWriter, metadata_path
from tests.qt_lifecycle import close_and_dispose, wait_for_qt
from tests.test_focus_calibration import focal_planes

APP = QApplication.instance() or QApplication([])
with patch('platform.machine', return_value='x86_64'):
    import tempo_gui as gui


class Camera:
    def __init__(self):
        self.frame = focal_planes(1)[0]
        self.gain = '0'

    def getCameraDevice(self): return 'Camera'
    def getImageWidth(self): return self.frame.shape[1]
    def getImageHeight(self): return self.frame.shape[0]
    def getImageBitDepth(self): return 12
    def getExposure(self): return 5.
    def getAvailableConfigGroups(self): return ()
    def getProperty(self, camera, name):
        return {'Gain': self.gain, 'Binning': '1', 'PixelType': 'Mono16'}[name]
    def isSequenceRunning(self): return False
    def snapImage(self): pass
    def getImage(self): return self.frame
    def getRemainingImageCount(self): return 1
    def getLastImage(self): return self.frame


class FocusDraftTests(unittest.TestCase):
    def test_multipage_import_is_lossless_and_failed_batch_preserves_draft(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            frames = focal_planes(1)
            settings = FocusSettings('a' * 64, 12, (96, 96), 'uint16')
            store = FocusCalibration(root, settings)
            draft = store.add_field(0, 'A01')
            imwrite(root / 'focus-stack.tif', np.stack(frames), photometric='minisblack')
            imwrite(root / 'wrong-size.tif', np.zeros((64, 64), np.uint16))
            with self.assertRaisesRegex(ValueError, 'Expected uint16 camera pixels'):
                store.import_planes(draft['revision'], 1, [root / 'focus-stack.tif', root / 'wrong-size.tif'])
            self.assertEqual(store.load(), draft)
            self.assertEqual(list((store.folder / 'planes').glob('*.tif')), [])

            draft = store.import_planes(draft['revision'], 1, [root / 'focus-stack.tif'])
            planes = draft['fields'][0]['planes']
            self.assertEqual(len(planes), 2)
            for plane, expected in zip(planes, frames):
                np.testing.assert_array_equal(store.read_plane(plane['id']), expected)
            with self.assertRaisesRegex(ValueError, 'changed in another window'):
                store.label_plane(1, 1, planes[0]['id'], 'sharp')

    def test_profiles_are_selected_per_settings_and_previous_profile_can_be_restored(self):
        from tempo_processing import atomic_write
        from tempo_quality import METHOD
        with TemporaryDirectory() as directory:
            root = Path(directory)
            signature, other_signature = 'a' * 64, 'b' * 64
            legacy = dict(id='legacy', signature=signature, method=METHOD)
            updated = dict(legacy, id='updated')
            other = dict(legacy, id='other', signature=other_signature)
            atomic_write(root / 'focus_profile.json', legacy)
            update_profile(root, signature, updated)
            update_profile(root, other_signature, other)
            self.assertEqual(load_profiles(root), ({signature: updated, other_signature: other}, []))
            self.assertEqual(update_profile(root, signature), legacy)
            self.assertEqual(load_profiles(root)[0], {signature: legacy, other_signature: other})


class FocusCalibrationUiTests(unittest.TestCase):
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        for target, value in (('get_config_dir', str(self.root / 'config')), ('get_data_dir', str(self.root / 'data'))):
            patcher = patch.object(capture_ui, target, return_value=value)
            patcher.start()
            self.addCleanup(patcher.stop)
        patcher = patch.object(capture_ui, 'local_scout_path', side_effect=RuntimeError('No Scout model in this test'))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.view = gui.CaptureTab()
        self.addCleanup(close_and_dispose, self.view)
        self.view.resize(1280, 800)
        self.view.show()
        self.camera = Camera()
        self.view.core = self.camera
        self.tools = self.view.capture_controls
        self.tools.refresh_controls()
        QTest.mouseClick(self.view.focus_calibration_button, Qt.MouseButton.LeftButton)
        self.dialog = self.tools.focus_dialog
        self.assertTrue(self.dialog.isVisible())
        self.addCleanup(self.finish_dialog)

    def finish_dialog(self):
        self.view.timer.stop()
        self.assertTrue(wait_for_qt(lambda: not (self.dialog.operation_in_progress or
                         self.dialog.capture_view.loading or self.dialog.review_view.loading)))
        self.dialog.close()

    def click(self, button):
        self.assertTrue(button.isEnabled(), button.text())
        QTest.mouseClick(button, Qt.MouseButton.LeftButton)
        self.assertTrue(wait_for_qt(lambda: not self.dialog.operation_in_progress))
        self.assertFalse(self.dialog.error.isVisible(), self.dialog.error.summary.text())

    def set_label(self, row, label):
        self.dialog.planes.setCurrentRow(row)
        self.dialog.labels.setCurrentIndex(self.dialog.labels.findData(label))
        self.assertTrue(wait_for_qt(lambda: not self.dialog.operation_in_progress))
        self.assertEqual(self.dialog._plane()['label'], label)

    def test_live_capture_import_fit_and_apply_without_restarting_acquisition(self):
        self.click(self.dialog.use_camera)
        self.assertEqual(self.dialog.store.settings.sensor_bits, 12)
        self.assertFalse(self.dialog.take_plane.isEnabled())
        self.click(self.dialog.new_field)
        for z, frame in zip((0, 2), focal_planes(1)):
            self.camera.frame = frame
            self.view.z_index.setValue(z)
            self.click(self.dialog.take_plane)
        self.set_label(0, 'sharp')
        self.set_label(1, 'out_of_focus')
        self.assertFalse(self.dialog.fit_button.isEnabled())

        self.click(self.dialog.new_field)
        path = self.root / 'held-out-stack.tif'
        imwrite(path, np.stack(focal_planes(2)), photometric='minisblack')
        with patch('tempo_focus_calibration_ui.QFileDialog.getOpenFileNames', return_value=([str(path)], '')), \
             patch('tempo_focus_calibration_ui.confirm_action', return_value=True):
            self.click(self.dialog.import_tiffs)
        self.set_label(0, 'sharp')
        self.set_label(1, 'out_of_focus')

        self.dialog.close()
        self.view.focus_calibration_button.click()
        self.click(self.dialog.use_camera)
        self.assertEqual(self.dialog.fields.count(), 2)
        self.click(self.dialog.fit_button)
        self.assertIs(self.dialog.tabs.currentWidget(), self.dialog.review_page)
        self.assertEqual(self.dialog.results.rowCount(), 2)
        self.assertIn('2 of 2 match', self.dialog.summary.text())
        self.assertTrue(wait_for_qt(lambda: not self.dialog.review_view.image.pixmap().isNull()))
        self.click(self.dialog.apply_button)

        signature = self.dialog.store.settings.signature
        profile = self.tools.focus_profiles[signature]
        self.assertEqual(load_profiles(self.root / 'config')[0][signature], profile)
        self.assertFalse(self.dialog.apply_button.isEnabled())
        self.view.timer.start(60000)
        for plane, result in zip(focal_planes(2), ('Sharp', 'Out of focus')):
            self.camera.frame = plane
            self.tools._last_quality_check = 0
            self.view.update_frame()
            self.assertTrue(wait_for_qt(lambda: not self.view._preview_pending and
                            self.tools._quality_task is None and self.tools._count_task is None))
            self.assertEqual(self.tools.focus_label.text(), f'Focus: {result} · Beta')
        self.tools._record_context = {'signature': 'optical-signature', 'quality_signature': signature}
        self.assertEqual(self.view.recording_options()['focus_profile'], profile)

    def test_changed_camera_settings_do_not_add_to_the_existing_draft(self):
        self.click(self.dialog.use_camera)
        self.click(self.dialog.new_field)
        self.camera.gain = '1'
        QTest.mouseClick(self.dialog.take_plane, Qt.MouseButton.LeftButton)
        self.assertTrue(self.dialog.error.isVisible())
        self.assertIn('settings changed', self.dialog.error.summary.text())
        self.assertEqual(self.dialog.planes.count(), 0)

    def test_offline_import_and_false_sharp_review(self):
        self.view.core = None
        reference = self.root / 'reference.tif'
        writer = NativeWriter(reference, 10, sensor_bits=12)
        signature = capture_ui.optical_context(self.camera)['quality_signature']
        writer.metadata['quality_signature'] = signature
        try:
            writer.write(focal_planes(1)[0])
        finally:
            writer.close()
        with patch('tempo_focus_calibration_ui.QFileDialog.getOpenFileName',
                   return_value=(str(metadata_path(reference)), '')):
            self.click(self.dialog.use_metadata)
        self.assertFalse(self.dialog.take_plane.isEnabled())
        for index in (1, 2):
            self.click(self.dialog.new_field)
            path = self.root / f'field-{index}.tif'
            imwrite(path, np.stack(focal_planes(index)), photometric='minisblack')
            with patch('tempo_focus_calibration_ui.QFileDialog.getOpenFileNames', return_value=([str(path)], '')), \
                 patch('tempo_focus_calibration_ui.confirm_action', return_value=True):
                self.click(self.dialog.import_tiffs)
            self.set_label(0, 'sharp' if index == 1 else 'out_of_focus')
            self.set_label(1, 'out_of_focus' if index == 1 else 'sharp')
        self.click(self.dialog.fit_button)
        self.assertIn('1 blurred plane reported as Sharp', self.dialog.summary.text())
        with patch('tempo_focus_calibration_ui.confirm_action', return_value=False) as confirm:
            self.click(self.dialog.apply_button)
        confirm.assert_called_once()
        self.assertNotIn(signature, self.tools.focus_profiles)
