"""Windows icon files, logo preservation, and static/animated framing agreement."""
import hashlib
from pathlib import Path
import struct
import tempfile
import unittest

from PyQt6.QtCore import QRect
from PyQt6.QtGui import QImage

from scripts.build_app_icons import build_icons
from tempo_app_icon import ASSETS, WINDOWS_ICON_SIZES, application_icon_path, render_app_icon


def opaque_bounds(image):
    points = [(x, y) for y in range(image.height()) for x in range(image.width())
              if image.pixelColor(x, y).alpha() >= 230]
    return QRect(min(x for x, _ in points), min(y for _, y in points),
                 max(x for x, _ in points) - min(x for x, _ in points) + 1,
                 max(y for _, y in points) - min(y for _, y in points) + 1)


class AppIconTests(unittest.TestCase):
    def test_windows_asset_keeps_complete_original_logo_and_other_platforms_keep_original_framing(self):
        original = QImage(str(ASSETS / 'tempo_icon.png'))
        windows = QImage(str(application_icon_path('win32')))
        self.assertEqual((original.width(), original.height()), (1254, 1254))
        self.assertEqual((windows.width(), windows.height()), (1068, 1068))
        self.assertEqual(windows.convertToFormat(QImage.Format.Format_ARGB32),
                         original.copy(93, 93, 1068, 1068).convertToFormat(QImage.Format.Format_ARGB32))
        self.assertEqual(application_icon_path('darwin'), ASSETS / 'tempo_icon.png')
        self.assertEqual(application_icon_path('linux'), ASSETS / 'tempo_icon.png')

    def test_ico_contains_native_small_sizes_and_matches_runtime_icon_framing(self):
        data = (ASSETS / 'tempo_icon.ico').read_bytes()
        reserved, kind, count = struct.unpack_from('<HHH', data)
        self.assertEqual((reserved, kind), (0, 1))
        original = QImage(str(ASSETS / 'tempo_icon.png'))
        sizes = set()
        for index in range(count):
            width, height, palette, reserved, planes, bits, length, offset = struct.unpack_from(
                '<BBBBHHII', data, 6 + 16 * index)
            width, height = width or 256, height or 256
            self.assertEqual((width, planes, bits), (height, 1, 32))
            self.assertGreaterEqual(offset, 6 + 16 * count)
            self.assertLessEqual(offset + length, len(data))
            frame = QImage.fromData(data[offset:offset + length], 'PNG')
            self.assertEqual((frame.width(), frame.height()), (width, height))
            self.assertEqual(frame.convertToFormat(QImage.Format.Format_ARGB32),
                             render_app_icon(original, width, 'win32'))
            self.assertEqual(frame.pixelColor(0, 0).alpha(), 0)
            self.assertEqual(frame.pixelColor(width - 1, height - 1).alpha(), 0)
            bounds = opaque_bounds(frame)
            # Tiny antialiased ICO frames can lose an opaque pixel at each edge.
            self.assertGreaterEqual(bounds.width(), .96 * width - 2)
            self.assertGreaterEqual(bounds.height(), .91 * height - 2)
            sizes.add(width)
        self.assertEqual(sizes, set(WINDOWS_ICON_SIZES))
        self.assertTrue({16, 24, 32, 48, 64, 128, 256}.issubset(sizes))

    def test_asset_rebuild_is_deterministic_and_never_replaces_original_artwork(self):
        original = ASSETS / 'tempo_icon.png'
        original_digest = hashlib.sha256(original.read_bytes()).hexdigest()
        with tempfile.TemporaryDirectory() as directory:
            for rebuilt in build_icons(Path(directory)):
                self.assertEqual(hashlib.sha256(rebuilt.read_bytes()).hexdigest(),
                                 hashlib.sha256((ASSETS / rebuilt.name).read_bytes()).hexdigest())
            self.assertFalse((Path(directory) / 'tempo_icon.png').exists())
        self.assertEqual(hashlib.sha256(original.read_bytes()).hexdigest(), original_digest)
