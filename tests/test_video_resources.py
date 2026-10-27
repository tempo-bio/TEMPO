"""Video captures are released after metadata, decoding and initialization failures."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import numpy as np
from masking_engine import process_cell_masks
from vk_math_engine import process_cell_kinematics


class VideoResourceTests(unittest.TestCase):

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.video = self.root/'synthetic.avi'; self.video.touch()

    def test_mask_capture_released_on_metadata_failure(self):
        capture = Mock(); capture.get.side_effect = OSError('metadata read failed')
        with patch('masking_engine.cv2.VideoCapture', return_value=capture):
            with self.assertRaises(OSError):
                process_cell_masks(str(self.video), str(self.root), [])
        capture.release.assert_called_once()

    def test_kinematics_capture_released_on_metadata_failure(self):
        capture = Mock(); capture.get.side_effect = OSError('metadata read failed')
        with patch('vk_math_engine.cv2.VideoCapture', return_value=capture):
            with self.assertRaises(OSError):
                process_cell_kinematics(str(self.video), str(self.root), 'cell_1')
        capture.release.assert_called_once()

    def test_kinematics_capture_released_on_frame_loop_failure(self):
        frame = np.zeros((8, 8, 3), np.uint8)
        capture = Mock(); capture.get.return_value = 100
        capture.read.side_effect = [(True, frame), (True, frame), OSError('decoder failed')]
        with patch('vk_math_engine.cv2.VideoCapture', return_value=capture), \
             patch('vk_math_engine.autodetect_reference_frame', return_value=0):
            with self.assertRaises(OSError):
                process_cell_kinematics(str(self.video), str(self.root), 'cell_1')
        capture.release.assert_called_once()

    def test_kinematics_missing_first_frame_returns_false_and_releases(self):
        frame = np.zeros((8, 8, 3), np.uint8)
        capture = Mock(); capture.get.return_value = 100
        capture.read.side_effect = [(True, frame), (False, None)]
        with patch('vk_math_engine.cv2.VideoCapture', return_value=capture), \
             patch('vk_math_engine.autodetect_reference_frame', return_value=0):
            self.assertFalse(process_cell_kinematics(str(self.video), str(self.root), 'cell_1'))
        capture.release.assert_called_once()

    def test_all_capture_resources_are_released_even_if_writer_cleanup_fails(self):
        video = self.root/'synthetic.avi'; video.touch()
        capture = Mock(); capture.get.side_effect = [32, 32, 100]; capture.read.return_value = (False, None)
        first, second = Mock(), Mock()
        first.release.side_effect = OSError('writer cleanup failed')
        cells = [{'id': name, 'polygon': [[1,1], [10,1], [10,10]]} for name in ('cell_1', 'cell_2')]
        with patch('masking_engine.cv2.VideoCapture', return_value=capture), \
             patch('masking_engine.cv2.VideoWriter', side_effect=[first, second]):
            with self.assertRaises(OSError):
                process_cell_masks(str(video), str(self.root), cells, export_debug_avis=True)
        first.release.assert_called_once(); second.release.assert_called_once()
        capture.release.assert_called_once()


if __name__ == "__main__":
    unittest.main()
